r"""audio 層（PreviewPlayer）のユニットテスト。

NullBackend を注入し、音を出さずに状態遷移・トグル・シーク・
読み上げ文言を検証する。

実行:
    PYTHONPATH=. python -m pytest tests/test_audio.py -q
"""

from __future__ import annotations

import unittest

from audio.player import NullBackend, PlayerBackend, PreviewPlayer


class TestNullBackendContract(unittest.TestCase):
    def test_satisfies_protocol(self) -> None:
        # NullBackend が PlayerBackend プロトコルを満たすこと
        self.assertIsInstance(NullBackend(), PlayerBackend)


class TestPreviewPlayer(unittest.TestCase):
    def setUp(self) -> None:
        self.player = PreviewPlayer(NullBackend())

    def test_initial_state_no_file(self) -> None:
        self.assertIn("読み込まれていません", self.player.describe_state_ja())

    def test_load_then_stopped(self) -> None:
        self.player.load("/a/kick.wav")
        self.assertEqual(self.player.current_path, "/a/kick.wav")
        desc = self.player.describe_state_ja()
        self.assertIn("kick.wav", desc)
        self.assertIn("停止中", desc)

    def test_toggle_play_stop(self) -> None:
        self.player.load("/a/kick.wav")
        # 停止中 → トグルで再生
        self.player.toggle_play_stop()
        self.assertIn("再生中", self.player.describe_state_ja())
        # 再生中 → トグルで停止
        self.player.toggle_play_stop()
        self.assertIn("停止中", self.player.describe_state_ja())

    def test_toggle_pause(self) -> None:
        self.player.load("/a/kick.wav")
        self.player.play()
        self.player.toggle_pause()
        self.assertIn("一時停止中", self.player.describe_state_ja())
        self.player.toggle_pause()
        self.assertIn("再生中", self.player.describe_state_ja())

    def test_toggle_pause_when_stopped_noop(self) -> None:
        self.player.load("/a/kick.wav")
        # 停止中に一時停止トグルしても何も起きない
        self.player.toggle_pause()
        self.assertIn("停止中", self.player.describe_state_ja())

    def test_seek_relative_from_stopped_autostarts(self) -> None:
        self.player.load("/a/kick.wav")
        # 停止中からの相対シークは play してから頭出しする
        self.player.seek_relative(2.0)
        desc = self.player.describe_state_ja()
        self.assertIn("再生中", desc)
        self.assertIn("2.0秒目", desc)

    def test_seek_relative_clamps_to_zero(self) -> None:
        self.player.load("/a/kick.wav")
        self.player.play()
        self.player.seek_relative(-10.0)  # 0 未満にならない
        self.assertIn("0.0秒目", self.player.describe_state_ja())

    def test_seek_to_clamps_to_duration(self) -> None:
        self.player.load("/a/kick.wav")  # NullBackend は全長5.0秒
        self.player.seek_to(99.0)
        # duration を超えないこと
        self.assertIn("5.0秒目", self.player.describe_state_ja())

    def test_describe_includes_duration(self) -> None:
        self.player.load("/a/kick.wav")
        self.player.play()
        desc = self.player.describe_state_ja()
        self.assertIn("全長5.0秒", desc)


class TestPreviewPlayerEnglish(unittest.TestCase):
    """英語の読み上げ。日本語と同じ分岐を、同じ順で読む。"""

    def setUp(self) -> None:
        self.player = PreviewPlayer(NullBackend())

    def test_no_file(self) -> None:
        self.assertEqual(self.player.describe_state_en(), "No file is loaded")

    def test_stopped(self) -> None:
        self.player.load("/a/kick.wav")
        self.assertEqual(self.player.describe_state_en(), "kick.wav, stopped")

    def test_playing_and_paused(self) -> None:
        self.player.load("/a/kick.wav")
        self.player.play()
        self.assertEqual(self.player.describe_state_en(),
                         "kick.wav, playing, at 0.0 seconds, "
                         "length 5.0 seconds")
        self.player.toggle_pause()
        self.assertIn(", paused, ", self.player.describe_state_en())

    def test_unknown_position_and_length(self) -> None:
        player = PreviewPlayer(NoneMetricsBackend())
        player.load("/a/kick.wav")
        player.play()
        self.assertEqual(player.describe_state_en(),
                         "kick.wav, playing, position unknown")


class NoneMetricsBackend(NullBackend):
    """実機の just_playback を模した、curr_pos/duration が None を返す版。

    実機確認で判明したバグ（describe_state_ja が None 比較で TypeError）の
    再発防止用。active 中でも位置・長さが取得できないケースを再現する。
    """

    @property
    def curr_pos(self):
        return None

    @property
    def duration(self):
        return None


class TestPreviewPlayerNoneMetrics(unittest.TestCase):
    """位置・長さが None を返す実機バックエンドでも落ちないことを検証。"""

    def setUp(self) -> None:
        self.player = PreviewPlayer(NoneMetricsBackend())

    def test_describe_does_not_crash_when_playing(self) -> None:
        self.player.load("/a/kick.wav")
        self.player.play()
        # None でも例外にならず、位置不明として整形されること
        desc = self.player.describe_state_ja()
        self.assertIn("再生中", desc)
        self.assertIn("位置不明", desc)
        self.assertNotIn("全長", desc)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestIsActive(unittest.TestCase):
    """**いま鳴っているかを外から見られること。**

    再生と停止を 1 つのキーで切り替えるために要る。
    """

    def setUp(self) -> None:
        self.player = PreviewPlayer(NullBackend())

    def test_starts_inactive(self) -> None:
        self.assertFalse(self.player.is_active)

    def test_active_while_playing(self) -> None:
        self.player.load("/a/kick.wav")
        self.player.play()
        self.assertTrue(self.player.is_active)

    def test_inactive_after_stop(self) -> None:
        self.player.load("/a/kick.wav")
        self.player.play()
        self.player.stop()
        self.assertFalse(self.player.is_active)

    def test_still_active_while_paused(self) -> None:
        """一時停止中も「鳴っている」扱い（停止とは別）。"""
        self.player.load("/a/kick.wav")
        self.player.play()
        self.player.toggle_pause()
        self.assertTrue(self.player.is_active)

    def test_broken_backend_does_not_raise(self) -> None:
        """バックエンドが壊れていても False を返すだけにする。"""
        class Broken:
            @property
            def active(self):
                raise RuntimeError("こわれた")

        player = PreviewPlayer(Broken())
        self.assertFalse(player.is_active)
