"""能動アナウンス（ui/announce.py）のユニットテスト。

**本物の通知は送らない**（送ると VoiceOver が読み出す）。AppKit の関数と
osascript の起動を差し替えて、どの方法で送ろうとしたかだけを見る。
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest import mock

from ui import announce as an


def _appkit():
    try:
        import AppKit
    except ImportError:
        return None
    return AppKit


@unittest.skipUnless(sys.platform == "darwin", "macOS のみ")
class TestNativeAnnouncement(unittest.TestCase):
    """System Events の方法は macOS 15 で -10006 になり、何も読まれていなかった。"""

    def setUp(self) -> None:
        self.appkit = _appkit()
        if self.appkit is None:
            self.skipTest("pyobjc が無い")
        # conftest.py が止めているので、ここでは開ける（送る関数は差し替える）
        self.env = mock.patch.dict(os.environ, {"SM_NO_ANNOUNCE": ""})
        self.env.start()
        self.posted: list = []
        self.post = mock.patch.object(
            self.appkit, "NSAccessibilityPostNotificationWithUserInfo",
            side_effect=lambda *args: self.posted.append(args))
        self.post.start()
        self.popen = mock.patch.object(an.subprocess, "Popen")
        self.spawned = self.popen.start()

    def tearDown(self) -> None:
        self.popen.stop()
        self.post.stop()
        self.env.stop()

    def test_posts_announcement_requested_from_the_app(self) -> None:
        """アプリ自身から AnnouncementRequested を送る（権限が要らない正規の方法）。"""
        an.announce("配置しました")
        self.assertEqual(len(self.posted), 1)
        _target, name, info = self.posted[0]
        self.assertEqual(
            name, self.appkit.NSAccessibilityAnnouncementRequestedNotification)
        self.assertEqual(info[self.appkit.NSAccessibilityAnnouncementKey],
                         "配置しました")
        self.assertEqual(info[self.appkit.NSAccessibilityPriorityKey],
                         self.appkit.NSAccessibilityPriorityHigh)

    def test_does_not_fall_back_to_system_events(self) -> None:
        """送れたら osascript は起動しない（-10006 になるうえ、権限の確認が出る）。"""
        an.announce("配置しました")
        self.spawned.assert_not_called()

    def test_other_threads_do_not_touch_appkit(self) -> None:
        """メインスレッド以外からは送らない（AppKit はメインスレッド専用）。"""
        worker = threading.Thread(target=an.announce, args=("スキャンしました",))
        worker.start()
        worker.join()
        self.assertEqual(self.posted, [])
        self.spawned.assert_called_once()

    def test_empty_message_is_ignored(self) -> None:
        an.announce("")
        self.assertEqual(self.posted, [])
        self.spawned.assert_not_called()


class TestSilenced(unittest.TestCase):
    def test_env_var_stops_everything(self) -> None:
        """SM_NO_ANNOUNCE があれば何も送らない（テスト中に読み出さないため）。"""
        with mock.patch.dict(os.environ, {"SM_NO_ANNOUNCE": "1"}), \
                mock.patch.object(an, "_announce_native") as native, \
                mock.patch.object(an.subprocess, "Popen") as spawned:
            an.announce("配置しました")
        native.assert_not_called()
        spawned.assert_not_called()

    def test_sound_switch_does_not_silence_speech(self) -> None:
        """音を止める SM_SILENT では、読み上げは止めない。

        音だけ止めたい利用者から、読み上げまで奪わないため。
        """
        with mock.patch.dict(os.environ, {"SM_NO_ANNOUNCE": "",
                                          "SM_SILENT": "1"}), \
                mock.patch.object(an, "_announce_native",
                                  return_value=True) as native, \
                mock.patch.object(an.sys, "platform", "darwin"):
            an.announce("配置しました")
        native.assert_called_once_with("配置しました")


if __name__ == "__main__":
    unittest.main(verbosity=2)
