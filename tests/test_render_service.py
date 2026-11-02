"""配置前の加工（移調・タイムストレッチ）のユニットテスト。

借りている部品の splicename / prep / dsp を繋ぐ層。**部品が
無い環境でも sample_manager は動かなければならない**ので、その分岐も見る。

実際に音を加工するテストは、加工の道具（ffmpeg か rubberband）が
入っている環境でだけ走らせる。
"""

from __future__ import annotations

import math
import os
import struct
import tempfile
import unittest
import wave

from core.session_settings import SessionSettings
from integration import render_service as rs


def write_wav(path: str, seconds: float = 2.0, rate: int = 44100,
              channels: int = 2) -> None:
    """テスト用のサイン波を書く。"""
    frames = int(seconds * rate)
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        data = bytearray()
        for i in range(frames):
            value = int(12000 * math.sin(2 * math.pi * 440 * i / rate))
            for _ in range(channels):
                data += struct.pack("<h", value)
        w.writeframes(bytes(data))


class TestNoSettings(unittest.TestCase):
    """条件が無ければ何もしない。元のパスをそのまま返す。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.dir.name, "loop.wav")
        write_wav(self.src)

    def tearDown(self):
        self.dir.cleanup()

    def test_empty_settings_pass_through(self):
        outcome = rs.render_for_session(self.src, "loop.wav",
                                        SessionSettings())
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.path, self.src)
        self.assertFalse(outcome.changed)

    def test_auto_render_off_passes_through(self):
        settings = SessionSettings(bpm=120, key="Am", auto_render=False)
        outcome = rs.render_for_session(self.src, "loop.wav", settings)
        self.assertEqual(outcome.path, self.src)

    def test_pass_through_says_nothing(self):
        """加工していないのに読み上げると邪魔になる。"""
        outcome = rs.render_for_session(self.src, "loop.wav",
                                        SessionSettings())
        self.assertEqual(outcome.describe_ja(), "")


class TestOutcomeMessages(unittest.TestCase):
    def test_pitch_only(self):
        o = rs.RenderOutcome(path="x", changed=True, semitones=-3,
                             source_bpm=84, source_key="Cマイナー")
        text = o.describe_ja()
        self.assertIn("3 半音下げ", text)
        self.assertIn("84 BPM", text)
        self.assertIn("Cマイナー", text)

    def test_stretch_only(self):
        o = rs.RenderOutcome(path="x", changed=True, ratio=0.7)
        self.assertIn("0.700 倍", o.describe_ja())

    def test_both(self):
        o = rs.RenderOutcome(path="x", changed=True, semitones=2, ratio=1.25)
        text = o.describe_ja()
        self.assertIn("2 半音上げ", text)
        self.assertIn("1.250 倍", text)

    def test_error_is_stated(self):
        o = rs.RenderOutcome(path="x", error="道具がありません")
        self.assertIn("道具がありません", o.describe_ja())
        self.assertFalse(o.ok)


class TestChannelAdaptation(unittest.TestCase):
    """**Pro Tools はチャンネル構成が合わないクリップを置かせてくれない。**

    素材の側を合わせれば置ける。以前は「モノラルなので配置を中止」で
    終わっていた（利用者からの指摘、2026-08-06）。
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        rs.set_work_dir(os.path.join(self.dir.name, "rendered"))
        self.mono = os.path.join(self.dir.name, "mono.wav")
        self.stereo = os.path.join(self.dir.name, "stereo.wav")
        write_wav(self.mono, seconds=0.3, channels=1)
        write_wav(self.stereo, seconds=0.3, channels=2)

    def tearDown(self):
        self.dir.cleanup()

    def channels_of(self, path):
        with wave.open(path) as w:
            return w.getnchannels()

    def test_matching_channels_do_nothing(self):
        out = rs.adapt_channels(self.stereo, "stereo.wav", 2)
        self.assertTrue(out.ok)
        self.assertFalse(out.changed)
        self.assertEqual(out.path, self.stereo)

    def test_pass_through_says_nothing(self):
        out = rs.adapt_channels(self.stereo, "stereo.wav", 2)
        self.assertEqual(out.describe_ja(), "")

    @unittest.skipUnless(rs.available(), "部品か ffmpeg が無い")
    def test_mono_becomes_stereo(self):
        out = rs.adapt_channels(self.mono, "mono.wav", 2)
        self.assertTrue(out.ok, out.error)
        self.assertTrue(out.changed)
        self.assertEqual(self.channels_of(out.path), 2)

    @unittest.skipUnless(rs.available(), "部品か ffmpeg が無い")
    def test_mono_to_stereo_is_not_lossy(self):
        """複製するだけなので音は変わらない。そう伝えること。"""
        out = rs.adapt_channels(self.mono, "mono.wav", 2)
        self.assertFalse(out.lossy)
        self.assertIn("音は変わりません", out.describe_ja())

    @unittest.skipUnless(rs.available(), "部品か ffmpeg が無い")
    def test_stereo_becomes_mono(self):
        out = rs.adapt_channels(self.stereo, "stereo.wav", 1)
        self.assertTrue(out.ok, out.error)
        self.assertEqual(self.channels_of(out.path), 1)

    @unittest.skipUnless(rs.available(), "部品か ffmpeg が無い")
    def test_stereo_to_mono_is_lossy(self):
        """左右が混ざるので音が変わる。黙って変えない。"""
        out = rs.adapt_channels(self.stereo, "stereo.wav", 1)
        self.assertTrue(out.lossy)
        self.assertIn("音が変わります", out.describe_ja())

    def test_missing_file_reports_an_error(self):
        out = rs.adapt_channels(
            os.path.join(self.dir.name, "no.wav"), "no.wav", 2)
        self.assertFalse(out.ok)


class TestSampleMusic(unittest.TestCase):
    """ファイル名から BPM とキーを読む。設定の手がかりに使う。"""

    @unittest.skipUnless(rs.available(), "部品が無い")
    def test_reads_bpm_and_key(self):
        music = rs.read_sample_music("RKU_AST_84_songstarter_loop_Cmin.wav")
        self.assertEqual(music.bpm, 84)
        self.assertEqual(music.key, "Cm")
        self.assertIn("マイナー", music.key_label)
        self.assertTrue(music.known)

    @unittest.skipUnless(rs.available(), "部品が無い")
    def test_key_is_ready_for_the_settings_field(self):
        """設定のキー欄にそのまま入れられる形であること。"""
        music = rs.read_sample_music("XX_120_loop_Am.wav")
        self.assertIn(music.key, ("Am", "A"))

    @unittest.skipUnless(rs.available(), "部品が無い")
    def test_unknown_name_is_not_an_error(self):
        music = rs.read_sample_music("なにもわからない.wav")
        self.assertFalse(music.known)
        self.assertEqual(music.describe_ja(), "")

    @unittest.skipUnless(rs.available(), "部品が無い")
    def test_describe_is_readable(self):
        music = rs.read_sample_music("AU_PTT_130_hat_loop_invite.wav")
        self.assertIn("130 BPM", music.describe_ja())


class TestMissingParts(unittest.TestCase):
    """部品が無い環境でも落ちないこと。"""

    def setUp(self):
        self.saved = rs.PARTS_PATH
        rs.PARTS_PATH = "/この経路は存在しない"
        self.dir = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.dir.name, "loop.wav")
        write_wav(self.src, seconds=0.2)

    def tearDown(self):
        rs.PARTS_PATH = self.saved
        self.dir.cleanup()

    def test_available_is_false(self):
        self.assertFalse(rs.available())

    def test_describe_says_where_it_looked(self):
        self.assertIn("/この経路は存在しない", rs.describe_backend())

    def test_render_reports_the_reason(self):
        outcome = rs.render_for_session(
            self.src, "loop.wav", SessionSettings(bpm=120))
        self.assertFalse(outcome.ok)
        self.assertIn("ptcore", outcome.error)

    def test_key_validation_is_skipped(self):
        """検証できない環境では素通しにする（入力を妨げない）。"""
        self.assertEqual(rs.validate_key("なんでも"), "")


@unittest.skipUnless(rs.available(), "部品か加工の道具が無い")
class TestRealRender(unittest.TestCase):
    """実際に加工する。部品と ffmpeg がある環境でだけ走る。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        rs.set_work_dir(os.path.join(self.dir.name, "rendered"))
        # ファイル名に BPM とキーを埋める（splicename が読む形）
        self.src = os.path.join(self.dir.name, "ABC_84_loop_test_Cmin.wav")
        write_wav(self.src, seconds=2.0, rate=44100, channels=2)

    def tearDown(self):
        self.dir.cleanup()

    def render(self, settings):
        return rs.render_for_session(
            self.src, os.path.basename(self.src), settings,
            channels=2, sample_rate=44100, duration=2.0)

    def test_matching_tempo_needs_no_work(self):
        outcome = self.render(SessionSettings(bpm=84, sample_rate=44100))
        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.path, self.src)

    def test_tempo_change_produces_a_new_file(self):
        outcome = self.render(SessionSettings(bpm=120, sample_rate=44100))
        self.assertTrue(outcome.ok, outcome.error)
        self.assertTrue(outcome.changed)
        self.assertNotEqual(outcome.path, self.src)
        self.assertTrue(os.path.exists(outcome.path))

    def test_tempo_change_shortens_the_file(self):
        """84 -> 120 BPM なら 0.7 倍になる。"""
        outcome = self.render(SessionSettings(bpm=120, sample_rate=44100))
        with wave.open(outcome.path) as w:
            seconds = w.getnframes() / w.getframerate()
        self.assertAlmostEqual(seconds, 2.0 * 0.7, delta=0.1)

    def test_key_change_transposes(self):
        outcome = self.render(SessionSettings(key="Am", sample_rate=44100))
        self.assertTrue(outcome.ok, outcome.error)
        self.assertTrue(outcome.changed)
        # Cマイナー -> Aマイナー は -3 半音
        self.assertEqual(outcome.semitones, -3)

    def test_key_change_keeps_the_length(self):
        outcome = self.render(SessionSettings(key="Am", sample_rate=44100))
        with wave.open(outcome.path) as w:
            seconds = w.getnframes() / w.getframerate()
        self.assertAlmostEqual(seconds, 2.0, delta=0.1)

    def test_message_mentions_the_source(self):
        outcome = self.render(SessionSettings(bpm=120, key="Am",
                                              sample_rate=44100))
        text = outcome.describe_ja()
        self.assertIn("84 BPM", text)
        self.assertIn("加工しました", text)

    def test_unreadable_key_is_reported_not_raised(self):
        """**失敗しても例外は投げない**（設定ファイルを手で書いた場合など）。

        以前は ValueError が抜けて、配置では「予期しないエラー」、試聴では
        「準備に失敗」としか読まれなかった。
        """
        outcome = self.render(SessionSettings(key="H", sample_rate=44100))
        self.assertFalse(outcome.ok)
        self.assertIn("H", outcome.error)
        self.assertEqual(outcome.path, self.src)

    def test_same_input_gives_the_same_output_path(self):
        """同じ素材・同じ加工なら作業フォルダが散らからない。"""
        a = self.render(SessionSettings(bpm=120, sample_rate=44100))
        b = self.render(SessionSettings(bpm=120, sample_rate=44100))
        self.assertEqual(a.path, b.path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
