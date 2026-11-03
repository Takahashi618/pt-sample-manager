"""integration 層と controller 連携のテスト（macOS）。

- 方式C（pbcopy）は偽の pbcopy で往復検証（tests/__init__.py。本物のクリップボードに触らない）。
- 方式B（Import Audio）は dry_run のメッセージとスクリプト生成を検証。
- AppleScript の組み立て（quote 適用）も検証。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest

from audio.player import NullBackend, PreviewPlayer
from core.library import Library
from core.models import Sample, SampleKind
from integration.protools import (
    build_import_script,
    copy_path_to_clipboard,
    import_via_dialog,
)
from ui.controller import AppController, SearchForm

IS_MACOS = sys.platform == "darwin"


def _stop_unless_fake_pbcopy(case: unittest.TestCase) -> None:
    """本物の pbcopy が動くなら止める（利用者のコピーした内容が消える）。"""
    found = shutil.which("pbcopy") or ""
    if "sm_fake_clipboard_" not in os.path.dirname(found):
        case.fail(f"偽の pbcopy が使われていません: {found}")


class TestImportScript(unittest.TestCase):
    def test_script_contains_quoted_path(self) -> None:
        script = build_import_script("/a/My Samples/kick.wav")
        # パスが二重引用符で囲まれて埋め込まれていること
        self.assertIn('"/a/My Samples/kick.wav"', script)
        # Import Audio ショートカットが含まれること
        self.assertIn('keystroke "i" using {command down, shift down}', script)

    def test_script_escapes_quote_in_path(self) -> None:
        script = build_import_script('/a/od"d.wav')
        self.assertIn('od\\"d.wav', script)


class TestImportViaDialogDryRun(unittest.TestCase):
    def test_dry_run_does_not_execute(self) -> None:
        outcome = import_via_dialog("/a/kick.wav", dry_run=True)
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.method, "dialog")
        self.assertIn("実機検証", outcome.message)
        self.assertIn("keystroke", outcome.script)


@unittest.skipUnless(IS_MACOS, "macOS でのみ pbcopy 検証")
class TestClipboardOnMac(unittest.TestCase):
    def setUp(self) -> None:
        _stop_unless_fake_pbcopy(self)

    def test_copy_path_roundtrip(self) -> None:
        path = "/Volumes/Audio/sample path/kick 01.wav"
        outcome = copy_path_to_clipboard(path)
        self.assertTrue(outcome.success)
        pasted = subprocess.run(
            ["pbpaste"], capture_output=True, text=True, timeout=10
        ).stdout
        self.assertEqual(pasted, path)


class TestControllerIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.lib = Library(":memory:")
        self.lib.add_sample(
            Sample(
                path="/a/kick.wav",
                filename="kick.wav",
                file_format="wav",
                kind=SampleKind.ONESHOT,
            )
        )
        self.ctrl = AppController(self.lib, PreviewPlayer(NullBackend()))
        self.ctrl.search(SearchForm())

    def tearDown(self) -> None:
        self.lib.close()

    def test_import_dry_run_message(self) -> None:
        msg = self.ctrl.import_to_pro_tools(0, dry_run=True)
        self.assertIn("実機検証", msg)

    def test_import_no_selection(self) -> None:
        self.assertIn("選択されていません", self.ctrl.import_to_pro_tools(99))

    @unittest.skipUnless(IS_MACOS, "macOS でのみ pbcopy 検証")
    def test_copy_path_message(self) -> None:
        _stop_unless_fake_pbcopy(self)
        msg = self.ctrl.copy_path(0)
        self.assertIn("コピーしました", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
