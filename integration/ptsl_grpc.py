"""PTSL への接続（Python の grpcio で直接つなぐ）。

借りている部品の `ptcore/ptsl.py` は Node のブリッジを子プロセスとして起動し、
標準入出力でやり取りしていた。**python3.14 用の grpcio が無かった**ためだが、
grpcio 1.76 から python3.14 のホイール（cp314・universal2）が出ている
（`.venv` に入っているもので確かめた）。

Node を経由しないことで、次の 3 つが要らなくなる。

- Node 本体（Homebrew の node）
- Node の部品（@grpc/grpc-js と proto-loader）
- Avid の PTSL SDK の `PTSL.proto`（コマンドの番号をそこから読んでいた）

**どれも受け取った人の Mac にはふつう無いもの**で、これらに頼ると配布版で配置が動かない。
問い合わせ 1 回も、Node の起動を待たないぶん速い（実機で 1 回 10 ミリ秒以下）。

### 封筒（Request / Response）は自前で組み立てる

PTSL の gRPC は「封筒」だけが protobuf で、中身は JSON の文字列
（`request_body_json`）。封筒は次の 4 つしかなく、形は 2022 年から変わっていない。

    Request        header = 1, request_body_json = 2
    RequestHeader  command = 2, version = 3, session_id = 4,
                   version_minor = 5, version_revision = 6
    Response       header = 1, response_body_json = 2, response_error_json = 3
    ResponseHeader status = 3

protobuf の符号化（可変長整数と、長さ付きの文字列）を数十行で書けるので、
**protobuf の部品と、生成済みの PTSL_pb2 を持ち込まない**。py-ptsl の
パッケージ名 `ptsl` は部品の `ptcore/ptsl.py` と同じ名前で、
`sys.path` の順によって取り違える。自前なら名前がぶつからない。
符号化が py-ptsl の PTSL_pb2 と同じバイト列になることはテストで見張っている
（`tests/test_ptsl_grpc.py`）。

### 申告する版は 2025.10 のまま

ホストは申告された版で分岐し、2026.04 以上を申告すると一部の欄を
返さなくなる。Node のブリッジと同じ値にしてある。

窓口の形（`open` / `close` / `send` と結果の `ok` / `body` /
`failure_text`）は `ptcore/ptsl.py` の PtslConnection に合わせてある。
`integration/ptsl_spot_client.py` はどちらでも同じように使える。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.i18n import COMMA, tr

# PTSL のサーバー。Pro Tools のプロセスの中で、この番号で待っている
DEFAULT_SERVER = os.environ.get("PTSL_SERVER", "localhost:31416")

# 申告する版（年・月・改訂）。上げないこと（上の説明を参照）
DEFAULT_VERSION: Tuple[int, int, int] = (2025, 10, 0)

# 1 コマンドを待つ長さ。取り込みや大きなセッションの書き出しは時間がかかる
# （Node のブリッジと同じ値）
DEFAULT_TIMEOUT = 180.0

# 最初のつなぎ込みを待つ長さ。Pro Tools が起動していなければ即座に断られる
CONNECT_TIMEOUT = 10.0

# 使うコマンドの番号（PTSL.proto の CommandId）。**番号は Avid が振り直さない**
# （2025.10 と 2026.04 の proto で一致を確かめてある）。py-ptsl 601.0.1 の
# 列挙とも一致することをテストで見張っている
COMMAND_IDS: Dict[str, int] = {
    "GetTrackList": 3,
    "ExportSessionInfoAsText": 30,
    "GetSessionSampleRate": 35,
    "GetSessionName": 42,
    "GetPTSLVersion": 55,
    "RegisterConnection": 70,
    "SetTimelineSelection": 81,
    "Undo": 104,
    "ImportAudioToClipList": 123,
    "SpotClipsByID": 124,
    "GetEditSelection": 131,
}

# TaskStatus（PTSL.proto）。**成功は Completed だけ**
STATUS_NAMES: Dict[int, str] = {
    0: "Queued",
    1: "Pending",
    2: "InProgress",
    3: "Completed",
    4: "Failed",
    5: "WaitingForUserInput",
    6: "CompletedWithBadResponse",
    7: "FailedWithBadErrorResponse",
}
STATUS_COMPLETED = 3

# gRPC のメソッド名（service PTSL の SendGrpcRequest）
_METHOD = "/ptsl.PTSL/SendGrpcRequest"


class GrpcPtslError(RuntimeError):
    """接続そのものが成立しない場合に投げる。

    コマンドの失敗は投げない。**失敗も結果として返す**ので `ok` を見ること
    （`ptcore/ptsl.py` の PtslError と同じ考え方）。
    """


@dataclass(frozen=True)
class GrpcResult:
    """PTSL の応答 1 回分。`ptcore/ptsl.py` の PtslResult と同じ形。"""

    ok: bool
    status: str
    body: Any = None
    infos: List[dict] = field(default_factory=list)
    failures: List[dict] = field(default_factory=list)
    command: str = ""

    @property
    def messages(self) -> List[str]:
        return [e.get("command_error_message", "")
                for e in list(self.infos) + list(self.failures)]

    @property
    def failure_text(self) -> str:
        """失敗の理由を 1 行にまとめる。"""
        joined = COMMA.join(e.get("command_error_message", "")
                            for e in self.failures)
        return joined or tr("理由は返っていません", "No reason was returned")


def available() -> bool:
    """grpcio が使えるか。無ければ Node のブリッジに任せる。"""
    try:
        import grpc  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# protobuf の符号化（封筒の 4 つだけ）
# ---------------------------------------------------------------------------

_VARINT = 0
_LENGTH_DELIMITED = 2


def _varint(value: int) -> bytes:
    """可変長整数。負の数は 64 ビットの 2 の補数にする（protobuf の int32 と同じ）。"""
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _key(number: int, wire_type: int) -> bytes:
    return _varint((number << 3) | wire_type)


def _int_field(number: int, value: int) -> bytes:
    """数の欄。**0 は書かない**（proto3 の既定値。protobuf と同じバイト列にする）。"""
    return _key(number, _VARINT) + _varint(value) if value else b""


def _bytes_field(number: int, data: bytes) -> bytes:
    """長さ付きの欄（文字列・入れ子のメッセージ）。空なら書かない。"""
    if not data:
        return b""
    return _key(number, _LENGTH_DELIMITED) + _varint(len(data)) + data


def encode_request(command_id: int, body_json: str = "",
                   session_id: str = "",
                   version: Tuple[int, int, int] = DEFAULT_VERSION) -> bytes:
    """Request を組み立てる。欄の番号は PTSL.proto のとおり。"""
    major, minor, revision = version
    header = (_int_field(2, command_id)
              + _int_field(3, major)
              + _bytes_field(4, session_id.encode("utf-8"))
              + _int_field(5, minor)
              + _int_field(6, revision))
    return (_bytes_field(1, header)
            + _bytes_field(2, body_json.encode("utf-8")))


def _read_varint(data: bytes, pos: int) -> Tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(data):
            raise ValueError(tr("可変長整数が途中で切れています",
                                "A variable-length integer is cut off"))
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError(tr("可変長整数が長すぎます",
                                "A variable-length integer is too long"))


def parse_fields(data: bytes) -> Dict[int, list]:
    """メッセージを欄の番号ごとの値の並びにする（知らない欄も読み飛ばせる）。"""
    fields: Dict[int, list] = {}
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        number, wire_type = key >> 3, key & 0x07
        if wire_type == _VARINT:
            value, pos = _read_varint(data, pos)
        elif wire_type == _LENGTH_DELIMITED:
            length, pos = _read_varint(data, pos)
            if pos + length > len(data):
                raise ValueError(tr("長さ付きの欄が途中で切れています",
                                    "A length-prefixed field is cut off"))
            value, pos = data[pos:pos + length], pos + length
        elif wire_type == 1:          # 64 ビット固定長
            value, pos = data[pos:pos + 8], pos + 8
        elif wire_type == 5:          # 32 ビット固定長
            value, pos = data[pos:pos + 4], pos + 4
        else:
            raise ValueError(tr(f"知らない符号化の種類です: {wire_type}",
                                f"Unknown wire type: {wire_type}"))
        fields.setdefault(number, []).append(value)
    return fields


def decode_response(data: bytes) -> Tuple[int, str, str]:
    """Response を (状態の番号, 本文の JSON, エラーの JSON) にする。"""
    fields = parse_fields(data)
    header = parse_fields(fields.get(1, [b""])[-1])
    status = header.get(3, [0])[-1]

    def text(number: int) -> str:
        raw = fields.get(number, [b""])[-1]
        return raw.decode("utf-8", errors="replace")

    return int(status), text(2), text(3)


def _result(command: str, status_id: int, body_json: str,
            error_json: str) -> GrpcResult:
    """応答を扱いやすい形へ均す（Node のブリッジの normalize と同じ）。

    **Completed でもエラーの欄に中身が入ることがある。** `is_warning` の
    ものは情報（サンプルレートを変換した、など）で、失敗ではない。
    """
    body: Any = None
    if body_json:
        try:
            body = json.loads(body_json)
        except json.JSONDecodeError:
            body = body_json
    errors: List[dict] = []
    if error_json:
        try:
            parsed = json.loads(error_json)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            errors = [e for e in parsed.get("errors") or []
                      if isinstance(e, dict)]
    return GrpcResult(
        ok=status_id == STATUS_COMPLETED,
        status=STATUS_NAMES.get(status_id, str(status_id)),
        body=body,
        infos=[e for e in errors if e.get("is_warning")],
        failures=[e for e in errors if not e.get("is_warning")],
        command=command,
    )


# ---------------------------------------------------------------------------
# 接続
# ---------------------------------------------------------------------------

class GrpcPtslConnection:
    """Pro Tools への接続。with 文で使う（`ptcore/ptsl.py` と同じ窓口）。

        with GrpcPtslConnection() as pt:
            res = pt.send("GetSessionName")
            print(res.body["session_name"])

    **Pro Tools が起動していないと接続できない。** PTSL のサーバーは
    Pro Tools のプロセスの中にあるため。
    """

    def __init__(self, server: str = DEFAULT_SERVER,
                 timeout: float = DEFAULT_TIMEOUT,
                 company_name: str = "sample_manager",
                 application_name: str = "sample_manager") -> None:
        self.server = server
        self.timeout = timeout
        self.company_name = company_name
        self.application_name = application_name
        self.session_id = ""
        self._channel = None
        self._call = None
        self._lock = threading.Lock()

    # -- 接続 ---------------------------------------------------------------

    def open(self) -> "GrpcPtslConnection":
        if self._call is not None:
            return self
        try:
            import grpc
        except ImportError as exc:
            raise GrpcPtslError(tr(f"grpcio を読み込めません: {exc}",
                                   f"Cannot load grpcio: {exc}")) from exc

        self._channel = grpc.insecure_channel(self.server)
        # 封筒は自前で符号化するので、変換の関数は渡さない（バイト列のまま）
        self._call = self._channel.unary_unary(_METHOD)

        # RegisterConnection だけは session_id を付けずに送る
        try:
            reg = self._send_raw(
                "RegisterConnection",
                {"company_name": self.company_name,
                 "application_name": self.application_name},
                timeout=CONNECT_TIMEOUT)
        except GrpcPtslError:
            self.close()
            raise
        session_id = reg.body.get("session_id") \
            if isinstance(reg.body, dict) else ""
        if not reg.ok or not session_id:
            self.close()
            raise GrpcPtslError(
                tr("Pro Tools に登録できませんでした: ",
                   "Could not register with Pro Tools: ") + reg.failure_text)
        self.session_id = str(session_id)
        return self

    def close(self) -> None:
        channel, self._channel, self._call = self._channel, None, None
        self.session_id = ""
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass

    def __enter__(self) -> "GrpcPtslConnection":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- 送受信 -------------------------------------------------------------

    def send(self, command: str,
             body: Optional[Dict[str, object]] = None) -> GrpcResult:
        """コマンドを 1 つ送って結果を返す。

        **コマンドが失敗しても例外は投げない。** 結果の `ok` を見ること。
        接続そのものが壊れた場合だけ GrpcPtslError を投げる。
        """
        if self._call is None:
            self.open()
        return self._send_raw(command, body or {})

    def _send_raw(self, command: str, body: Dict[str, object],
                  timeout: Optional[float] = None) -> GrpcResult:
        name = str(command)
        if name.startswith("CId_"):
            name = name[len("CId_"):]
        command_id = COMMAND_IDS.get(name)
        if command_id is None:
            return GrpcResult(
                ok=False, status="Unknown", command=name,
                failures=[{"command_error_message":
                           tr(f"このアプリが知らないコマンドです: {command}",
                              "This app does not know the command: "
                              f"{command}")}])

        request = encode_request(
            command_id, json.dumps(body, ensure_ascii=False),
            session_id="" if name == "RegisterConnection"
            else self.session_id)
        with self._lock:
            call = self._call
            if call is None:
                raise GrpcPtslError(tr("Pro Tools との接続が閉じています",
                                       "The connection to Pro Tools is "
                                       "closed"))
            try:
                raw = call(request, timeout=timeout or self.timeout)
            except Exception as exc:     # grpc.RpcError ほか
                raise GrpcPtslError(_describe_rpc_error(
                    exc, timeout or self.timeout)) from exc
        try:
            status_id, body_json, error_json = decode_response(raw)
        except ValueError as exc:
            raise GrpcPtslError(tr(f"Pro Tools の応答を読めません: {exc}",
                                   "Cannot read the response from Pro "
                                   f"Tools: {exc}")) \
                from exc
        return _result(name, status_id, body_json, error_json)


def _describe_rpc_error(exc: Exception, timeout: float) -> str:
    """gRPC の失敗を、何をすればよいかが分かる文にする。"""
    code = getattr(exc, "code", None)
    name = ""
    if callable(code):
        try:
            name = getattr(code(), "name", "") or ""
        except Exception:
            name = ""
    if name == "UNAVAILABLE":
        return tr("Pro Tools につながりませんでした。Pro Tools が起動しているか確認してください（PTSL は "
                  "Pro Tools のプロセスの中で動いています）",
                  "Could not connect to Pro Tools. Check that Pro Tools is "
                  "running (PTSL runs inside the Pro Tools process)")
    if name == "DEADLINE_EXCEEDED":
        return tr(f"{timeout:.0f} 秒待っても応答がありません。Pro Tools "
                  "がダイアログを出して止まっていないか確認してください",
                  f"No response after {timeout:.0f} seconds. Check that Pro "
                  "Tools is not waiting on a dialog")
    details = getattr(exc, "details", None)
    text = ""
    if callable(details):
        try:
            text = details() or ""
        except Exception:
            text = ""
    return tr(f"Pro Tools との通信に失敗しました（{name or type(exc).__name__}: "
              f"{text or exc}）",
              "Communication with Pro Tools failed "
              f"({name or type(exc).__name__}: {text or exc})")
