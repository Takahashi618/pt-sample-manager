"""PTSL への直接の接続（integration/ptsl_grpc.py）のユニットテスト。

Pro Tools には接続しない。次の 3 つを見る。

1. **封筒の符号化が protobuf と同じバイト列になること。** py-ptsl の
   生成済み PTSL_pb2（`.venv` に入っている）と突き合わせる。無い環境では飛ばす
2. **コマンドの番号が proto と一致すること。** py-ptsl の列挙と、手元に
   Avid の SDK があればその PTSL.proto とも突き合わせる
3. **応答の均し方**（Completed だけが成功、情報と失敗の分け方）と、
   接続できないときに分かる文で失敗すること
"""

from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path

from integration import ptsl_grpc as pg


def _py_ptsl():
    """py-ptsl の生成済みモジュール。無ければ None。

    **`ptsl` という名前は 2 つある。** py-ptsl のパッケージと、借りている部品の
    `ptcore/ptsl.py`（Node のブリッジ）。ほかのテストが ptcore を `sys.path`
    の先頭へ足したあとだと、`from ptsl import PTSL_pb2` はブリッジのほうを
    掴んで失敗する（全部のテストを通しで回すと実際に起きた。本体の
    ptsl_grpc が py-ptsl に頼らない理由でもある）。ここでは ptcore を
    一時的に外して読む。
    """
    import sys

    saved_path = list(sys.path)
    current = sys.modules.get("ptsl")
    if current is not None and not hasattr(current, "__path__"):
        del sys.modules["ptsl"]          # ブリッジのほう（パッケージではない）
    sys.path[:] = [p for p in sys.path
                   if Path(p).name != "ptcore"]
    try:
        from ptsl import PTSL_pb2
    except ImportError:
        return None
    finally:
        sys.path[:] = saved_path
    return PTSL_pb2


class TestEncoding(unittest.TestCase):
    def setUp(self) -> None:
        self.pb = _py_ptsl()
        if self.pb is None:
            self.skipTest("py-ptsl が無いので突き合わせられない")

    def _reference(self, command: str, body: str, session: str = "",
                   version=pg.DEFAULT_VERSION) -> bytes:
        pb = self.pb
        major, minor, revision = version
        return pb.Request(
            header=pb.RequestHeader(
                command=pb.CommandId.Value("CId_" + command),
                session_id=session, version=major, version_minor=minor,
                version_revision=revision),
            request_body_json=body).SerializeToString()

    def test_register_connection_matches_protobuf(self) -> None:
        body = json.dumps({"company_name": "sample_manager",
                           "application_name": "sample_manager"})
        self.assertEqual(
            pg.encode_request(70, body),
            self._reference("RegisterConnection", body))

    def test_command_with_session_matches_protobuf(self) -> None:
        body = json.dumps({"location_type": "TLType_Samples"})
        session = "0123abcd-4567-89ef-0123-456789abcdef"
        self.assertEqual(
            pg.encode_request(131, body, session_id=session),
            self._reference("GetEditSelection", body, session))

    def test_japanese_track_name_matches_protobuf(self) -> None:
        """トラック名は日本語のことがある（UTF-8 のまま送る）。"""
        body = json.dumps({"src_clips": ["a", "b"],
                           "dst_track_name": "オーディオ 1"},
                          ensure_ascii=False)
        self.assertEqual(
            pg.encode_request(124, body, session_id="s"),
            self._reference("SpotClipsByID", body, "s"))

    def test_empty_body_matches_protobuf(self) -> None:
        self.assertEqual(pg.encode_request(42, "", session_id="s"),
                         self._reference("GetSessionName", "", "s"))

    def test_decodes_a_protobuf_response(self) -> None:
        pb = self.pb
        raw = pb.Response(
            header=pb.ResponseHeader(
                command=pb.CommandId.Value("CId_GetSessionName"),
                status=pb.TaskStatus.Value("Completed"), progress=100,
                version=2026, version_minor=4),
            response_body_json=json.dumps({"session_name": "曲 1"},
                                          ensure_ascii=False),
            response_error_json="").SerializeToString()
        status, body, error = pg.decode_response(raw)
        self.assertEqual(status, pg.STATUS_COMPLETED)
        self.assertEqual(json.loads(body), {"session_name": "曲 1"})
        self.assertEqual(error, "")

    def test_decodes_a_failed_response(self) -> None:
        pb = self.pb
        errors = {"errors": [{"command_error_type": "PT_InvalidParameter",
                              "command_error_message": "だめ",
                              "is_warning": False}]}
        raw = pb.Response(
            header=pb.ResponseHeader(status=pb.TaskStatus.Value("Failed")),
            response_error_json=json.dumps(errors)).SerializeToString()
        status, body, error = pg.decode_response(raw)
        self.assertEqual(status, 4)
        self.assertEqual(body, "")
        self.assertEqual(json.loads(error), errors)


class TestCommandIds(unittest.TestCase):
    def test_match_py_ptsl(self) -> None:
        pb = _py_ptsl()
        if pb is None:
            self.skipTest("py-ptsl が無い")
        for name, number in pg.COMMAND_IDS.items():
            self.assertEqual(pb.CommandId.Value("CId_" + name), number, name)

    def test_match_the_sdk_proto_when_present(self) -> None:
        """手元に Avid の SDK があれば、その proto の番号とも突き合わせる。"""
        from tools import build_release as br

        # 場所は環境変数か、作者の手元の設定（公開しない）
        place = (os.environ.get("PTSL_PROTO_PATH")
                 or br.read_private(br.PRIVATE_FILE).get("ptsl_proto", ""))
        proto = Path(place).expanduser()
        if not place or not proto.is_file():
            self.skipTest("Avid の SDK が無い")
        text = proto.read_text(encoding="utf-8")
        for name, number in pg.COMMAND_IDS.items():
            m = re.search(rf"CId_{name}\s*=\s*(\d+)\s*;", text)
            self.assertIsNotNone(m, name)
            self.assertEqual(int(m.group(1)), number, name)

    def test_covers_every_command_the_client_sends(self) -> None:
        """spot クライアントが送るコマンドは全部、番号を知っていること。

        知らないコマンドは送る前に失敗として返すので、配置が途中で止まる。
        """
        source = Path(__file__).resolve().parent.parent / \
            "integration" / "ptsl_spot_client.py"
        sent = set(re.findall(r'_(?:send|require)\(\s*"(\w+)"',
                              source.read_text(encoding="utf-8")))
        self.assertTrue(sent)
        self.assertEqual(sent - set(pg.COMMAND_IDS), set())


class TestWireFormat(unittest.TestCase):
    """protobuf の部品が無くても確かめられる、符号化そのものの性質。"""

    def test_varint_round_trip(self) -> None:
        for value in (0, 1, 127, 128, 300, 2 ** 31 - 1, 2 ** 63 - 1):
            data = pg._varint(value)
            self.assertEqual(pg._read_varint(data, 0), (value, len(data)))

    def test_negative_int32_takes_ten_bytes(self) -> None:
        """protobuf の int32 の負の数は 64 ビットの 2 の補数（10 バイト）。"""
        self.assertEqual(len(pg._varint(-1)), 10)

    def test_zero_fields_are_not_written(self) -> None:
        """proto3 の既定値（0 と空文字）は書かない。"""
        self.assertEqual(pg.encode_request(0, "", version=(0, 0, 0)), b"")

    def test_unknown_fields_are_skipped(self) -> None:
        """知らない欄（progress や版の欄）があっても読める。"""
        header = (pg._int_field(2, 42) + pg._int_field(3, 3)
                  + pg._int_field(4, 100)
                  + pg._bytes_field(8, b'{"x": 1}'))
        raw = (pg._bytes_field(1, header)
               + pg._bytes_field(2, b'{"session_name": "s"}')
               + pg._bytes_field(9, b"future"))
        status, body, _ = pg.decode_response(raw)
        self.assertEqual(status, 3)
        self.assertEqual(json.loads(body), {"session_name": "s"})

    def test_truncated_response_is_an_error(self) -> None:
        raw = pg._bytes_field(2, b'{"a": 1}')[:-3]
        with self.assertRaises(ValueError):
            pg.decode_response(raw)


class TestResultShape(unittest.TestCase):
    """応答の均し方。Node のブリッジと同じ。"""

    def test_only_completed_is_ok(self) -> None:
        for status, ok in ((3, True), (4, False), (2, False), (6, False)):
            self.assertEqual(pg._result("X", status, "", "").ok, ok, status)

    def test_status_is_named(self) -> None:
        self.assertEqual(pg._result("X", 3, "", "").status, "Completed")
        self.assertEqual(pg._result("X", 99, "", "").status, "99")

    def test_warnings_are_infos_not_failures(self) -> None:
        """Completed でもエラーの欄に情報が入る（サンプルレートの変換など）。"""
        errors = json.dumps({"errors": [
            {"command_error_message": "must be converted", "is_warning": True},
            {"command_error_message": "本当の失敗", "is_warning": False},
        ]})
        result = pg._result("ImportAudioToClipList", 3, "{}", errors)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.infos), 1)
        self.assertEqual(result.failure_text, "本当の失敗")

    def test_failure_text_without_reason(self) -> None:
        self.assertEqual(pg._result("X", 4, "", "").failure_text,
                         "理由は返っていません")

    def test_broken_json_does_not_raise(self) -> None:
        result = pg._result("X", 3, "{こわれた", "{こわれた")
        self.assertEqual(result.body, "{こわれた")
        self.assertEqual(result.failures, [])


class TestConnection(unittest.TestCase):
    def test_unknown_command_is_a_failure_not_an_exception(self) -> None:
        conn = pg.GrpcPtslConnection()
        conn._call = lambda *a, **k: self.fail("送ってはいけない")
        result = conn.send("NoSuchCommand")
        self.assertFalse(result.ok)
        self.assertIn("知らないコマンド", result.failure_text)

    def test_sends_with_the_session_id(self) -> None:
        sent: list = []

        def call(request, timeout=None):
            sent.append(request)
            return (pg._bytes_field(1, pg._int_field(3, 3))
                    + pg._bytes_field(2, b'{"session_name": "s"}'))

        conn = pg.GrpcPtslConnection()
        conn._call = call
        conn.session_id = "abc"
        result = conn.send("CId_GetSessionName")
        self.assertTrue(result.ok)
        self.assertEqual(result.body, {"session_name": "s"})
        header = pg.parse_fields(pg.parse_fields(sent[0])[1][0])
        self.assertEqual(header[4], [b"abc"])
        self.assertEqual(header[2], [42])

    def test_unreachable_server_says_what_to_check(self) -> None:
        """Pro Tools が起動していないときは、そう分かる文で失敗する。"""
        if not pg.available():
            self.skipTest("grpcio が無い")
        # 使われていないはずの番号（閉じたポート）へつなぐ
        conn = pg.GrpcPtslConnection(server="127.0.0.1:9")
        with self.assertRaises(pg.GrpcPtslError) as caught:
            conn.open()
        self.assertIn("Pro Tools", str(caught.exception))
        self.assertIsNone(conn._call)

    def test_rpc_error_messages(self) -> None:
        class Code:
            def __init__(self, name):
                self.name = name

        class Err(Exception):
            def __init__(self, name):
                super().__init__(name)
                self._name = name

            def code(self):
                return Code(self._name)

            def details(self):
                return "詳細"

        self.assertIn("起動しているか",
                      pg._describe_rpc_error(Err("UNAVAILABLE"), 10))
        self.assertIn("180 秒",
                      pg._describe_rpc_error(Err("DEADLINE_EXCEEDED"), 180))
        self.assertIn("詳細", pg._describe_rpc_error(Err("INTERNAL"), 10))


class TestTransportChoice(unittest.TestCase):
    def setUp(self) -> None:
        from integration import ptsl_spot_client

        self.client_module = ptsl_spot_client
        self.saved = os.environ.get(ptsl_spot_client.TRANSPORT_ENV)

    def tearDown(self) -> None:
        name = self.client_module.TRANSPORT_ENV
        if self.saved is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = self.saved

    def test_grpc_is_the_default_when_available(self) -> None:
        os.environ.pop(self.client_module.TRANSPORT_ENV, None)
        expected = "grpc" if pg.available() else "node"
        self.assertEqual(self.client_module.transport(), expected)

    def test_node_can_be_chosen(self) -> None:
        os.environ[self.client_module.TRANSPORT_ENV] = "node"
        self.assertEqual(self.client_module.transport(), "node")

    def test_bridge_is_loaded_under_its_own_name(self) -> None:
        """py-ptsl の `ptsl` と取り違えないよう、別の名前で読む。"""
        try:
            module = self.client_module._bridge_module()
        except Exception as exc:
            self.skipTest(f"借りている部品を読めません: {exc}")
        self.assertTrue(hasattr(module, "PtslConnection"))
        self.assertEqual(module.__name__, "_sm_ptcore_ptsl")


if __name__ == "__main__":
    unittest.main(verbosity=2)
