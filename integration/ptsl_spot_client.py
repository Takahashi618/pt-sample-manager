"""PTSL クライアント（spot 経路・macOS / Pro Tools 専用）。

`ptsl_client.py`（py-ptsl 版）の置き換え。**py-ptsl を使わない。**

py-ptsl 601.0.1 は Pro Tools の新しいコマンドに追随していない。
そのため旧版では次の制約を抱えていた。

- `import_audio` にバグがあり低レベル op で回避していた
- 「トラック削除 API は無い」ものとして一時トラックを clear + rename で再利用
- 一時トラックへ取り込んで Copy / Paste する経路しか採れなかった

**PTSL 自体にはどちらの機能もある。** 借りている部品が proto を直接読む
経路を用意し、実機で検証済み。

    ImportAudioToClipList  ->  SpotClipsByID

これで**一時トラックも Copy / Paste も選択状態への依存も要らなくなる。**

### 接続の実体は 2 つ（2026-10-24 から）

  grpc  `integration/ptsl_grpc.py`。Python の grpcio で直接つなぐ。**既定**
  node  `ptcore/ptsl.py`。Node のブリッジ経由。grpcio が
        無いときの控え

以前は node だけだった（python3.14 用の grpcio が無かったため）。node は
Node 本体・Node の部品・Avid の SDK の proto という
**受け取った人の Mac にはふつう無いもの**に頼るので、配布版では動かない。grpc なら
grpcio だけで済む。環境変数 `SM_PTSL_TRANSPORT=node` で node に戻せる
（動きを比べるとき用）。

どちらでも、送るコマンドと受け取る応答の形は同じ（窓口の形を合わせてある）。
セッション情報の読み取り（`sessioninfo`）は、どちらでも 借りている部品の
部品を使う。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unicodedata
from typing import List, Optional, Protocol

from core.i18n import tr
from core.parts import add_ptcore_to_path, part_message, parts_path
from core.placement_planner import TrackInfo

# 借りている部品を読めるようにする（探し方は core/parts.py）
PARTS_PATH = parts_path()


try:
    # 旧クライアントと例外型を共有する。別物にすると placement_service が
    # 両方を握らねばならなくなるため
    from integration.ptsl_client import PTSLError
except ImportError:  # pragma: no cover - 旧クライアントが無い環境
    class PTSLError(Exception):
        """PTSL 操作の失敗を表す正規化済み例外。"""


# 接続の実体を選ぶ環境変数（"node" で Node のブリッジに戻す）
TRANSPORT_ENV = "SM_PTSL_TRANSPORT"


def transport() -> str:
    """使う接続の実体。"grpc"（既定）か "node"。

    grpcio が無ければ node。環境変数で node を指定することもできる。
    """
    if os.environ.get(TRANSPORT_ENV, "").strip().lower() == "node":
        return "node"
    from integration.ptsl_grpc import available

    return "grpc" if available() else "node"


# Node のブリッジを読み込んだときの名前（下の説明を参照）
_BRIDGE_ALIAS = "_sm_ptcore_ptsl"


def _bridge_module():
    """Node のブリッジ（`ptcore/ptsl.py`）を読み込む。

    **名前を変えて、ファイルの場所から読む。** py-ptsl のパッケージ名も
    `ptsl` なので、`import ptsl` だと `sys.path` の順によってどちらが
    来るか決まらない（取り違えると「PtslConnection が無い」で落ちる）。
    """
    module = sys.modules.get(_BRIDGE_ALIAS)
    if module is not None:
        return module
    path = os.path.join(PARTS_PATH, "ptcore", "ptsl.py")
    if not os.path.isfile(path):
        raise PTSLError(tr(f"grpcio が読めず、Node のブリッジも見つかりません（{path}）。"
                           "grpcio は pip install grpcio で入ります",
                           "grpcio cannot be loaded and the Node bridge was "
                           f"not found ({path}). Install grpcio with: "
                           "pip install grpcio"))
    spec = importlib.util.spec_from_file_location(_BRIDGE_ALIAS, path)
    if spec is None or spec.loader is None:
        raise PTSLError(tr(f"Node のブリッジを読めません: {path}",
                           f"Cannot load the Node bridge: {path}"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[_BRIDGE_ALIAS] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(_BRIDGE_ALIAS, None)
        raise PTSLError(tr(f"Node のブリッジを読めません: {exc}",
                           f"Cannot load the Node bridge: {exc}")) from exc
    return module


# PTSL の文字列 enum を placement_planner の数値へ。
# proto の値と一致している（TT_Audio=2、TF_Stereo=2、SetExplicitly=2）。
_TRACK_TYPES = {
    "TType_Unknown": 0, "TT_Unknown": 0,
    "TType_Midi": 1, "TT_Midi": 1,
    "TType_Audio": 2, "TT_Audio": 2, "AudioTrack": 2,
    "TType_Aux": 3, "TT_Aux": 3, "Aux": 3,
}
_TRACK_FORMATS = {
    "TF_Unknown": 0, "TFormat_Unknown": 0,
    "TF_Mono": 1, "TFormat_Mono": 1,
    "TF_Stereo": 2, "TFormat_Stereo": 2,
}
_ATTR_STATES = {
    "TAState_Unknown": 0,
    "TAState_None": 1, "None": 1,
    "SetExplicitly": 2, "TAState_SetExplicitly": 2,
    "SetImplicitly": 3, "TAState_SetImplicitly": 3,
    "SetExplicitlyAndImplicitly": 4, "TAState_SetExplicitlyAndImplicitly": 4,
}

# 位置のやり取りに使う形式。
# **配置にはサンプルを使う。** タイムコードはフレーム単位なので、
# 拍や 16 分音符の位置がフレーム境界に乗らず、置いた位置がずれる
# （実機で確認: 30fps・44.1kHz で最大 3 サンプル）。
_TIME_TYPE = "TLType_TimeCode"   # 読み上げ用（利用者にはこちらが分かりやすい）
_SAMPLES = "TLType_Samples"      # 配置・計算用
_BARS_BEATS = "TLType_BarsBeats"


class SpotClientProtocol(Protocol):
    """spot 経路の配置に必要な最小インターフェース。

    `placement_service.place_sample_spot` はこの Protocol にのみ依存する。
    実体は `SpotPTSLClient`（実機）またはテストのスタブ。
    """

    def session_is_open(self) -> bool: ...
    def list_tracks(self) -> List[TrackInfo]: ...
    def get_cursor_timecode(self) -> str: ...
    def import_to_clip_list(self, posix_path: str) -> List[str]: ...

    def spot_clips(self, clip_ids: List[str], track_name: str,
                   location: str, time_type: str = ...) -> None: ...

    def tracks_holding(self, clip_name: str) -> List[str]: ...
    def undo(self, steps: int) -> None: ...


class SpotPTSLClient:
    """Pro Tools への接続。with 文で使う。

        with SpotPTSLClient() as client:
            client.list_tracks()

    **Pro Tools が起動していないと接続できない。** PTSL サーバーは
    Pro Tools のプロセス内にあるため。
    """

    def __init__(self, application_name: str = "sample_manager") -> None:
        self.application_name = application_name
        self._conn = None
        self._sessioninfo = None
        # 接続している間は変わらない値。**配置 1 回で何度も問い合わせない**
        # （テンポの確認とスナップでグリッドを 2 度測っていた。1 度の測定で
        # カーソルを動かす往復が 7 回ある）。接続を閉じたら忘れる
        self._rate_cache: Optional[float] = None
        self._grid_cache: Optional[tuple] = None

    # -- 接続 ---------------------------------------------------------------

    def _load(self) -> None:
        """借りている部品（セッション情報の読み取り）を読み込む。

        import は接続時まで遅らせる。配置の確認（`tracks_holding`）に要る。
        """
        if not add_ptcore_to_path(PARTS_PATH):
            raise PTSLError(
                tr(f"借りている部品（ptcore）が見つかりません: {PARTS_PATH}。環境変数 "
                   "PLACEMENT_CORE_PATH で場所を指定できます",
                   f"The bundled part (ptcore) was not found: {PARTS_PATH}. "
                   "You can set its location with the environment variable "
                   "PLACEMENT_CORE_PATH")
            )
        try:
            import sessioninfo as sessioninfo_module
        except ImportError as exc:
            raise PTSLError(tr(f"借りている部品を読めません: {exc}",
                               "Cannot load the bundled part: "
                               f"{exc}")) from exc
        self._sessioninfo = sessioninfo_module

    def _connect(self):
        """PTSL への接続を作って開く（grpc が既定、無ければ node）。"""
        if transport() == "grpc":
            from integration.ptsl_grpc import GrpcPtslConnection

            return GrpcPtslConnection(
                application_name=self.application_name).open()
        return _bridge_module().PtslConnection().open()

    def open(self) -> "SpotPTSLClient":
        if self._conn is not None:
            return self
        self._load()
        try:
            self._conn = self._connect()
        except Exception as exc:
            raise PTSLError(part_message(str(exc))) from exc
        self._rate_cache = None
        self._grid_cache = None
        return self

    def close(self) -> None:
        conn, self._conn = self._conn, None
        self._rate_cache = None
        self._grid_cache = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def __enter__(self) -> "SpotPTSLClient":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- 低レベル -----------------------------------------------------------

    def _send(self, command: str, body: Optional[dict] = None):
        if self._conn is None:
            self.open()
        try:
            return self._conn.send(command, body or {})
        except Exception as exc:
            raise PTSLError(part_message(str(exc))) from exc

    def _require(self, command: str, body: Optional[dict] = None):
        res = self._send(command, body)
        if not res.ok:
            reason = part_message(res.failure_text)
            raise PTSLError(tr(f"{command} が失敗しました: {reason}",
                               f"{command} failed: {reason}"))
        return res

    # -- 問い合わせ ---------------------------------------------------------

    def session_is_open(self) -> bool:
        res = self._send("GetSessionName")
        if not res.ok or not isinstance(res.body, dict):
            return False
        return bool(res.body.get("session_name"))

    def list_tracks(self) -> List[TrackInfo]:
        """トラック一覧を placement_planner の TrackInfo へ詰め替える。"""
        res = self._require("GetTrackList")
        body = res.body if isinstance(res.body, dict) else {}
        tracks = []
        for raw in body.get("track_list") or []:
            attrs = raw.get("track_attributes") or {}
            tracks.append(
                TrackInfo(
                    name=raw.get("name", ""),
                    track_type=_TRACK_TYPES.get(raw.get("type", ""), 0),
                    track_format=_TRACK_FORMATS.get(raw.get("format", ""), 0),
                    is_selected_state=_ATTR_STATES.get(
                        attrs.get("is_selected", ""), 0),
                    has_edit_selection_state=_ATTR_STATES.get(
                        attrs.get("has_edit_selection", ""), 0),
                    contains_clips=bool(attrs.get("contains_clips", False)),
                )
            )
        return tracks

    def get_cursor_timecode(self) -> str:
        """編集カーソルの位置をタイムコードで返す。

        `GetEditSelection` は `location_type` が必須。省略すると
        `PT_InvalidParameter` になる（実機で確認済み）。
        """
        res = self._require("GetEditSelection", {"location_type": _TIME_TYPE})
        body = res.body if isinstance(res.body, dict) else {}
        value = body.get("in_time") or ""
        return str(value).strip()

    def get_cursor_samples(self) -> str:
        """編集カーソルの位置をサンプル数で返す。

        **配置にはこちらを使う。** タイムコードはフレーム単位なので、
        音楽的な位置（拍や 16 分音符）がフレーム境界に乗らず、
        置いた位置がわずかにずれる（実機で確認: 最大 3 サンプル）。
        サンプルなら丸めが起きない。
        """
        res = self._require("GetEditSelection", {"location_type": _SAMPLES})
        body = res.body if isinstance(res.body, dict) else {}
        return str(body.get("in_time") or "").strip()

    def set_cursor(self, location: str, time_type: str = _SAMPLES) -> None:
        """編集カーソルを動かす。"""
        self._require("SetTimelineSelection", {
            "in_time": location, "out_time": location,
            "location_type": time_type})

    def _edit_selection(self, time_type: str) -> tuple:
        """編集選択の始点と終点。**戻すときは両方要る**（範囲選択を保つ）。"""
        res = self._require("GetEditSelection", {"location_type": time_type})
        body = res.body if isinstance(res.body, dict) else {}
        start = str(body.get("in_time") or "").strip()
        end = str(body.get("out_time") or "").strip() or start
        return start, end

    def get_cursor_bars_beats(self) -> str:
        """編集カーソルの位置を小節・拍で返す（例 `10| 2| 373`）。

        **拍の上にいるかどうかは Pro Tools 自身の解釈で確かめる。**
        等間隔グリッドの計算はテンポが途中で変わるセッションでは
        近似になるため（`placement_service._snap_cursor` を参照）。
        """
        res = self._require("GetEditSelection",
                            {"location_type": _BARS_BEATS})
        body = res.body if isinstance(res.body, dict) else {}
        return str(body.get("in_time") or "").strip()

    def session_sample_rate(self) -> Optional[float]:
        """セッションのサンプルレート。取れなければ None。

        `GetSessionSampleRate` は `SR_44100` のような文字列を返す。
        接続している間は変わらないので、一度取れたら覚えておく。
        """
        if self._rate_cache:
            return self._rate_cache
        res = self._send("GetSessionSampleRate")
        if not res.ok or not isinstance(res.body, dict):
            return None
        raw = str(res.body.get("sample_rate") or "")
        digits = "".join(ch for ch in raw if ch.isdigit())
        try:
            value = float(digits)
        except ValueError:
            return None
        if value <= 0:
            return None
        self._rate_cache = value
        return value

    def grid_info(self) -> Optional[tuple]:
        """配置のグリッドを求める。戻り値は (小節1の位置, 1拍のサンプル数)。

        **拍にスナップするために要る。** 画面を見ずにカーソルを拍の上へ
        置くのは難しく、外れた位置に置くとメトロノームと合わない。
        `detect_tempo` もこの測定結果から BPM を出す（測るのはここだけ）。

        編集カーソルを一時的に動かすので、**必ず元の位置へ戻す。**
        取れなければ None。

        接続している間は変わらないので、一度測れたら覚えておく
        （配置 1 回でテンポの確認とスナップの 2 度呼ばれる）。
        """
        if self._grid_cache is not None:
            return self._grid_cache
        rate = self.session_sample_rate()
        if not rate:
            return None

        saved = ("", "")
        try:
            saved = self._edit_selection(_SAMPLES)
            self.set_cursor("1|1|000", _BARS_BEATS)
            first = int(self.get_cursor_samples())
            self.set_cursor("1|2|000", _BARS_BEATS)
            one_beat = int(self.get_cursor_samples())
            self.set_cursor("2|1|000", _BARS_BEATS)
            one_bar = int(self.get_cursor_samples())
        except (PTSLError, ValueError):
            return None
        finally:
            # **範囲選択も元どおりにする。** 始点だけ戻すと、利用者が
            # Pro Tools で取っていた選択範囲が点に潰れる
            if saved[0]:
                try:
                    self._require("SetTimelineSelection", {
                        "in_time": saved[0], "out_time": saved[1],
                        "location_type": _SAMPLES})
                except PTSLError:
                    pass

        rough = one_beat - first
        span = one_bar - first
        if rough <= 0 or span <= 0:
            return None

        # **1 拍だけで測ると精度が足りない。** PTSL はサンプル位置を整数で
        # 返すので、1 拍の長さに 1 サンプル未満の端数がある場合
        # （160 BPM・44.1kHz なら 16537.5）に切り捨てられ、遠くの拍ほど
        # ずれが積もる。1 小節を拍数で割ると端数まで拾える。
        # 拍数は概算から求めるので**拍子を決め打ちしない**（3/4 でも動く）。
        beats_per_bar = max(1, round(span / rough))
        self._grid_cache = (first, span / beats_per_bar)
        return self._grid_cache

    def detect_tempo(self) -> Optional[float]:
        """セッションのテンポを逆算する。

        **PTSL はテンポを返さない。** セッション情報のヘッダーにも
        入っていない（実機で確認）。測定は `grid_info` に集約してある
        （1 小節を拍数で割ることで 1 サンプル未満の端数まで拾う。
        1 拍だけの測定だと切り捨てで精度が落ちる）。ここでは
        その 1 拍の長さを BPM に直すだけ。

            BPM = 60 × サンプルレート ÷ 1 拍のサンプル数

        テンポマップがある場合は先頭付近の値になる。
        """
        rate = self.session_sample_rate()
        if not rate:
            return None
        grid = self.grid_info()
        if grid is None:
            return None
        # 逆算なので端数が出る。0.01 まで丸めれば実用上は十分
        return round(60.0 * rate / grid[1], 2)

    def tracks_holding(self, clip_name: str) -> List[str]:
        """そのクリップが載っているトラック名を全部返す。

        **配置の成否はこれで判断する。** `SpotClipsByID` は
        `status: Completed` を返しても置いていないことがあり、
        チャンネル構成が合わないと指定を無視して別のトラックへ置く。
        """
        res = self._send("ExportSessionInfoAsText", {
            "include_file_list": False,
            "include_clip_list": False,
            "include_markers": False,
            "include_plugin_list": False,
            "include_track_edls": True,
            "output_type": "ESI_String",
            "location_type": "TLType_BarsBeats",
        })
        if not res.ok or not isinstance(res.body, dict):
            return []
        text = res.body.get("session_info")
        if not isinstance(text, str) or self._sessioninfo is None:
            return []
        # **濁点の書き方の違いで取り違えない。** Mac のアプリが作った日本語の
        # ファイル名は濁点を分けた形（NFD）で、Pro Tools がクリップ名を
        # どちらの形で返すかは決まっていない。形が違うと部分一致が外れ、
        # 置けているのに「確認できない」として取り消してしまう。
        # 両方を NFC にそろえてから比べる（トラック名も同じ形になる）
        info = self._sessioninfo.parse_session_info(
            unicodedata.normalize("NFC", text))
        return info.tracks_with_clip(unicodedata.normalize("NFC", clip_name))

    # -- 配置 ---------------------------------------------------------------

    def import_to_clip_list(self, posix_path: str) -> List[str]:
        """クリップリストへ取り込み、クリップ ID を全部返す。

        **`audio_operations` は指定しない。** `CopyAudio` はサンプルレートが
        違うと失敗し、`ConvertAudio` は同じだと失敗する。Pro Tools に
        任せるのが唯一の正解（実機で全組み合わせを確認済み）。

        **ID は `destination_file_list` の全エントリから集める。**
        変換が起きると L / R が別ファイルに分かれて 2 エントリになるため、
        1 エントリ目だけ見ると片チャンネルしか置けない。
        """
        res = self._require("ImportAudioToClipList",
                            {"file_list": [posix_path]})
        body = res.body if isinstance(res.body, dict) else {}

        failures = body.get("failure_list") or []
        if failures:
            reason = failures[0].get("failure_message", tr("理由不明",
                                                           "unknown reason"))
            raise PTSLError(tr(f"取り込みに失敗しました: {reason}",
                               f"Import failed: {reason}"))

        clip_ids: List[str] = []
        for entry in body.get("file_list") or []:
            for dest in entry.get("destination_file_list") or []:
                clip_ids.extend(dest.get("clip_id_list") or [])
        if not clip_ids:
            raise PTSLError(tr("取り込んだクリップの ID を取得できませんでした",
                               "Could not get the ID of the imported clip"))
        return clip_ids

    def spot_clips(self, clip_ids: List[str], track_name: str,
                   location: str, time_type: str = _TIME_TYPE) -> None:
        """クリップを対象トラックの指定位置へ直接置く。

        ステレオ素材は複数の ID でひとつのクリップを構成するので、
        `src_clips` にまとめて渡す。

        `time_type` は位置文字列の読み方。既定はタイムコード
        （編集カーソル位置をそのまま渡すため）。小節・拍で指定するなら
        `TLType_BarsBeats` を渡す。
        """
        self._require("SpotClipsByID", {
            "src_clips": list(clip_ids),
            "dst_track_name": track_name,
            "dst_location_data": {
                "location_type": "SLType_Start",
                "location": {"location": location, "time_type": time_type},
            },
        })

    def undo(self, steps: int) -> None:
        """巻き戻す。**1 コマンド 1 段**（実機で確認済み）。"""
        for _ in range(max(0, steps)):
            res = self._send("Undo")
            if not res.ok:
                break
