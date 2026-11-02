"""起動の準備（app.py）のユニットテスト。

起動経路そのもの（DB 準備・メニュー構築）はビルドの起動スモークが通す。ここでは、その手前で環境を整える小物を見る。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import app


class TestExtendToolPath(unittest.TestCase):
    """Finder から開いたアプリの PATH には Homebrew の場所が無い。"""

    def setUp(self) -> None:
        self.saved = os.environ.get("PATH")
        self.dir = tempfile.TemporaryDirectory()
        self.brew = os.path.join(self.dir.name, "brew", "bin")
        os.makedirs(self.brew)

    def tearDown(self) -> None:
        if self.saved is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = self.saved
        self.dir.cleanup()

    def test_adds_an_existing_directory_at_the_end(self) -> None:
        """Finder と同じ PATH に、Homebrew の場所を後ろへ足す。"""
        os.environ["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        added = app._extend_tool_path((self.brew,))
        self.assertEqual(added, [self.brew])
        self.assertEqual(os.environ["PATH"],
                         f"/usr/bin:/bin:/usr/sbin:/sbin:{self.brew}")

    def test_finds_a_tool_that_was_hidden(self) -> None:
        """足したあとは shutil.which で見つかる（加工の道具の探し方と同じ）。"""
        import shutil

        tool = os.path.join(self.brew, "sm-test-tool")
        with open(tool, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(tool, 0o755)
        os.environ["PATH"] = "/usr/bin:/bin"
        self.assertIsNone(shutil.which("sm-test-tool"))
        app._extend_tool_path((self.brew,))
        self.assertEqual(shutil.which("sm-test-tool"), tool)

    def test_keeps_the_order_the_user_chose(self) -> None:
        """すでに入っていれば何もしない（利用者の決めた順を崩さない）。"""
        os.environ["PATH"] = f"{self.brew}:/usr/bin"
        self.assertEqual(app._extend_tool_path((self.brew,)), [])
        self.assertEqual(os.environ["PATH"], f"{self.brew}:/usr/bin")

    def test_skips_directories_that_do_not_exist(self) -> None:
        os.environ["PATH"] = "/usr/bin"
        missing = os.path.join(self.dir.name, "no-such-dir")
        self.assertEqual(app._extend_tool_path((missing,)), [])
        self.assertEqual(os.environ["PATH"], "/usr/bin")


class TestMakePlayer(unittest.TestCase):
    """試聴プレイヤーが作れなくても起動を止めない。"""

    def test_any_exception_gives_no_player(self) -> None:
        """just_playback は音の出る装置が無いと MiniaudioError を投げる。

        これは RuntimeError ではない（Exception の直下）。以前は
        RuntimeError だけを受けていたので、アプリが起動しなかった。
        """

        class MiniaudioError(Exception):
            pass

        def boom(*_args, **_kwargs):
            raise MiniaudioError("No playback device is available!!")

        with mock.patch("audio.player.PreviewPlayer", side_effect=boom):
            self.assertIsNone(app._make_player())

    def test_missing_library_gives_no_player(self) -> None:
        with mock.patch("audio.player.PreviewPlayer",
                        side_effect=RuntimeError("just_playback が無い")):
            self.assertIsNone(app._make_player())


@unittest.skipUnless(__import__("sys").platform == "darwin", "macOS のみ")
class TestWindowMenu(unittest.TestCase):
    """wx が足す「Window」メニューも日本語にする（読み上げに英語を混ぜない）。"""

    def _menu_bar(self):
        try:
            from AppKit import NSMenu, NSMenuItem
        except ImportError:
            self.skipTest("pyobjc が無い")
        bar = NSMenu.alloc().initWithTitle_("")
        for title, children in (("操作", ["終了"]),
                                ("Window", ["Minimize", "Zoom", "",
                                            "Bring All to Front"])):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, None, "")
            sub = NSMenu.alloc().initWithTitle_(title)
            for child in children:
                if child:
                    sub.addItemWithTitle_action_keyEquivalent_(
                        child, None, "")
                else:
                    sub.addItem_(NSMenuItem.separatorItem())
            item.setSubmenu_(sub)
            bar.addItem_(item)
        return bar

    def test_titles_become_japanese(self) -> None:
        bar = self._menu_bar()
        self.assertTrue(app._localize_window_menu(bar))
        window = bar.itemAtIndex_(1)
        self.assertEqual(window.title(), "ウインドウ")
        titles = [str(window.submenu().itemAtIndex_(i).title())
                  for i in range(window.submenu().numberOfItems())]
        self.assertEqual(titles, ["しまう", "拡大／縮小", "",
                                  "すべてを手前に移動"])

    def test_other_menus_are_left_alone(self) -> None:
        bar = self._menu_bar()
        app._localize_window_menu(bar)
        self.assertEqual(bar.itemAtIndex_(0).title(), "操作")


if __name__ == "__main__":
    unittest.main(verbosity=2)
