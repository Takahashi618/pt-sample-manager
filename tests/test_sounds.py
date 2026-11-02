"""通知音のユニットテスト。

**読み上げだけだと、時間のかかる処理が終わったことに気づきにくい。**
スキャンや配置の完了を短い音で先に伝える。

音そのものは鳴らさず、判断（成功か失敗か・鳴らす条件か）を検証する。
"""

from __future__ import annotations

import os
import sys
import unittest

from ui import sounds


class TestSilentSwitch(unittest.TestCase):
    """`SM_SILENT` で無音にできること。

    **システムのミュートは使わない**（VoiceOver まで黙るため）。
    音を止めたいときはこの環境変数を使う。
    """

    def setUp(self):
        self.saved = os.environ.get("SM_SILENT")

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("SM_SILENT", None)
        else:
            os.environ["SM_SILENT"] = self.saved

    def test_unset_is_not_silent(self):
        os.environ.pop("SM_SILENT", None)
        self.assertFalse(sounds.is_silent())

    def test_zero_is_not_silent(self):
        os.environ["SM_SILENT"] = "0"
        self.assertFalse(sounds.is_silent())

    def test_one_is_silent(self):
        os.environ["SM_SILENT"] = "1"
        self.assertTrue(sounds.is_silent())

    def test_silent_blocks_playback(self):
        os.environ["SM_SILENT"] = "1"
        self.assertFalse(sounds.play("success"))


class TestSoundSelection(unittest.TestCase):
    def test_known_kinds_have_a_file(self):
        if sys.platform != "darwin":
            self.skipTest("macOS のシステム音を使う")
        for kind in ("success", "failure", "start"):
            self.assertIsNotNone(sounds.sound_path(kind), kind)

    def test_unknown_kind_has_none(self):
        self.assertIsNone(sounds.sound_path("知らない種類"))

    def test_success_and_failure_differ(self):
        """聞き分けられることが目的なので、別の音であること。"""
        self.assertNotEqual(
            sounds.sound_path("success"), sounds.sound_path("failure"))


class TestMessageToSound(unittest.TestCase):
    """文言の先頭から成否を判断する。

    規約は `core/placement_planner.py` の `[情報]` / `[警告]` / `[エラー]`。
    """

    def setUp(self):
        self.saved = os.environ.get("SM_SILENT")
        os.environ["SM_SILENT"] = "1"   # 実際には鳴らさない
        self.calls = []
        self._orig = sounds.play
        sounds.play = lambda kind: self.calls.append(kind) or True

    def tearDown(self):
        sounds.play = self._orig
        if self.saved is None:
            os.environ.pop("SM_SILENT", None)
        else:
            os.environ["SM_SILENT"] = self.saved

    def test_info_is_success(self):
        sounds.play_for_message("[情報] 配置しました。")
        self.assertEqual(self.calls, ["success"])

    def test_error_is_failure(self):
        sounds.play_for_message("[エラー] 接続できません。")
        self.assertEqual(self.calls, ["failure"])

    def test_warning_is_failure(self):
        """警告も失敗の音。細かく鳴らし分けても聞き分けられない。"""
        sounds.play_for_message("[警告] 別のトラックに置かれました。")
        self.assertEqual(self.calls, ["failure"])

    def test_english_marks(self):
        """英語の読み上げでも同じ音にする。"""
        sounds.play_for_message("[Error] Cannot connect.")
        sounds.play_for_message("[Warning] It went to another track.")
        sounds.play_for_message("[Info] Placed.")
        self.assertEqual(self.calls, ["failure", "failure", "success"])

    def test_plain_message_is_success(self):
        sounds.play_for_message("176件を登録しました。")
        self.assertEqual(self.calls, ["success"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
