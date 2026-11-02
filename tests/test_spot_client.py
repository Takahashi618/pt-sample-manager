"""spot 経路の PTSL クライアントのユニットテスト（接続を偽物に差し替える）。

`SpotPTSLClient` は Node のブリッジ経由で Pro Tools と話すが、ここでは
`_conn` に応答を作り込んだ偽物を入れ、**問い合わせの回数と、編集カーソルを
元に戻す手順**を確かめる。実機では確かめにくい部分だけを見る。
"""

from __future__ import annotations

import unicodedata
import unittest
from dataclasses import dataclass, field

from integration.ptsl_spot_client import SpotPTSLClient


@dataclass
class _Response:
    ok: bool = True
    body: dict = field(default_factory=dict)
    failure_text: str = ""


class FakeConn:
    """160 BPM・44.1kHz のセッションを模す。1 拍 = 16537.5 サンプル。

    PTSL はサンプル位置を整数で返すので、1 拍目は 16537 に切り捨てられる
    （1 小節 66150 を拍数で割って端数を拾う、が grid_info の要点）。
    """

    BARS = {"1|1|000": 0, "1|2|000": 16537, "2|1|000": 66150}

    def __init__(self, in_time: str = "12345",
                 out_time: str = "23456") -> None:
        self.calls: list[tuple[str, dict]] = []
        self._in = in_time
        self._out = out_time

    def send(self, command: str, body: dict) -> _Response:
        self.calls.append((command, dict(body)))
        if command == "GetSessionSampleRate":
            return _Response(body={"sample_rate": "SR_44100"})
        if command == "GetEditSelection":
            return _Response(body={"in_time": self._in, "out_time": self._out})
        if command == "SetTimelineSelection":
            if body.get("location_type") == "TLType_BarsBeats":
                self._in = self._out = str(self.BARS[body["in_time"]])
            else:
                self._in, self._out = body["in_time"], body["out_time"]
            return _Response()
        return _Response(ok=False, failure_text=f"未対応: {command}")

    def count(self, command: str) -> int:
        return sum(1 for name, _ in self.calls if name == command)


def make_client(conn: FakeConn | None = None) -> SpotPTSLClient:
    client = SpotPTSLClient()
    client._conn = conn if conn is not None else FakeConn()
    return client


class TestGridInfo(unittest.TestCase):
    def test_measures_one_beat_with_sub_sample_precision(self):
        self.assertEqual(make_client().grid_info(), (0, 16537.5))

    def test_detect_tempo_comes_from_the_grid(self):
        self.assertAlmostEqual(make_client().detect_tempo(), 160.0)

    def test_restores_the_selection_range(self):
        """**範囲選択を点に潰さない。** 始点だけ戻すと終点が失われる。"""
        conn = FakeConn(in_time="1000", out_time="5000")
        make_client(conn).grid_info()
        moves = [b for name, b in conn.calls if name == "SetTimelineSelection"]
        last = moves[-1]
        self.assertEqual((last["in_time"], last["out_time"]), ("1000", "5000"))
        self.assertEqual(last["location_type"], "TLType_Samples")

    def test_second_call_does_not_measure_again(self):
        """配置 1 回でテンポの確認とスナップの 2 度呼ばれる。往復を増やさない。"""
        conn = FakeConn()
        client = make_client(conn)
        client.grid_info()
        moves = conn.count("SetTimelineSelection")
        client.grid_info()
        client.detect_tempo()
        client.session_sample_rate()
        self.assertEqual(conn.count("SetTimelineSelection"), moves)
        self.assertEqual(conn.count("GetSessionSampleRate"), 1)

    def test_cache_is_dropped_when_the_connection_closes(self):
        client = make_client()
        client.grid_info()
        client.close()
        self.assertIsNone(client._grid_cache)
        self.assertIsNone(client._rate_cache)

    def test_failure_is_not_cached(self):
        class Broken(FakeConn):
            def send(self, command, body):
                if command == "GetEditSelection":
                    return _Response(ok=False, failure_text="だめ")
                return super().send(command, body)

        client = make_client(Broken())
        self.assertIsNone(client.grid_info())
        self.assertIsNone(client._grid_cache)

    def test_failure_does_not_move_the_cursor(self):
        """測れないときは何も触らない（元の位置が読めなければ戻せない）。"""
        class Broken(FakeConn):
            def send(self, command, body):
                if command == "GetEditSelection":
                    return _Response(ok=False, failure_text="だめ")
                return super().send(command, body)

        conn = Broken()
        make_client(conn).grid_info()
        self.assertEqual(conn.count("SetTimelineSelection"), 0)


def _session_text(track: str, clip: str) -> str:
    """ExportSessionInfoAsText の出力（必要なところだけ）。"""
    return (
        "SESSION NAME:\tテスト\n"
        "SAMPLE RATE:\t44100.000000\n"
        "\n"
        "T R A C K  L I S T I N G\n"
        f"TRACK NAME:\t{track} (Stereo)\n"
        "COMMENTS:\t\n"
        "USER DELAY:\t0 Samples\n"
        "STATE:\t\n"
        "CHANNEL \tEVENT   \tCLIP NAME\tSTART TIME\tEND TIME\t"
        "DURATION\tSTATE\n"
        f"1       \t1       \t{clip}\t           3|1\t           4|2\t"
        "      1|1| 820\tUnmuted\n"
    )


class TestTracksHolding(unittest.TestCase):
    """配置の確認は、名前の書き方（濁点を分けた NFD か NFC か）に左右されない。"""

    def _client(self, text: str) -> SpotPTSLClient:
        class Conn:
            def send(self, command, body):
                return _Response(body={"session_info": text})

        client = SpotPTSLClient()
        client._conn = Conn()
        try:
            client._load()          # sessioninfo を読む（借りている部品が要る）
        except Exception as exc:
            self.skipTest(f"借りている部品を読めません: {exc}")
        return client

    def test_decomposed_file_name_matches_composed_clip(self):
        """Mac で作った NFD のファイル名と、NFC で返るクリップ名が合う。

        合わないと、置けているのに「確認できない」として取り消してしまう。
        """
        client = self._client(_session_text("オーディオ 1", "ドラム_ループ"))
        stem = unicodedata.normalize("NFD", "ドラム_ループ")
        self.assertEqual(client.tracks_holding(stem), ["オーディオ 1"])

    def test_composed_file_name_matches_decomposed_clip(self):
        text = unicodedata.normalize(
            "NFD", _session_text("オーディオ 1", "ドラム_ループ"))
        client = self._client(text)
        self.assertEqual(client.tracks_holding("ドラム_ループ"),
                         ["オーディオ 1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
