"""借りている部品の探し方と言い換え（core/parts.py）のユニットテスト。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import parts


def make_parts(base: Path) -> Path:
    (base / "ptcore").mkdir(parents=True)
    (base / "ptcore" / "splicename.py").write_text("", encoding="utf-8")
    return base


class TestPartsPath(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bundled = self.root / "siblings" / parts.PART_NAME
        patches = [
            mock.patch.object(parts, "PROJECT_ROOT", self.root / "project"),
            mock.patch.object(parts, "BUNDLED", self.bundled),
            mock.patch.object(parts, "DEV_DIRS_FILE", self.root / "dirs.json"),
            mock.patch.dict(os.environ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop(parts.PARTS_ENV, None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _dev_dirs(self, text: str) -> None:
        (self.root / "dirs.json").write_text(text, encoding="utf-8")

    def test_uses_the_copy_in_the_project(self) -> None:
        make_parts(self.bundled)
        self.assertEqual(parts.parts_path(), str(self.bundled))

    def test_uses_the_next_folder_named_in_the_dev_file(self) -> None:
        make_parts(self.root / "real_name")
        self._dev_dirs('{"placement_core": "real_name"}')
        self.assertEqual(parts.parts_path(), str(self.root / "real_name"))

    def test_environment_comes_first(self) -> None:
        make_parts(self.bundled)
        other = make_parts(self.root / "other")
        os.environ[parts.PARTS_ENV] = str(other)
        self.assertEqual(parts.parts_path(), str(other))

    def test_stale_environment_does_not_hide_the_parts(self) -> None:
        """古い環境変数のせいで、BPM の読み取りや配置まで止まらないこと。"""
        make_parts(self.bundled)
        os.environ[parts.PARTS_ENV] = str(self.root / "gone")
        self.assertEqual(parts.parts_path(), str(self.bundled))

    def test_nothing_found_returns_a_path_for_the_message(self) -> None:
        self.assertEqual(parts.parts_path(), str(self.bundled))
        os.environ[parts.PARTS_ENV] = str(self.root / "gone")
        self.assertEqual(parts.parts_path(), str(self.root / "gone"))

    def test_empty_folder_is_not_the_parts(self) -> None:
        self.bundled.mkdir(parents=True)
        real = make_parts(self.root / "real_name")
        self._dev_dirs('{"placement_core": "real_name"}')
        self.assertEqual(parts.parts_path(), str(real))

    def test_broken_dev_file_is_ignored(self) -> None:
        for text in ("{", "[]", '{"other": "x"}', '["placement_core"]'):
            with self.subTest(text=text):
                self._dev_dirs(text)
                self.assertEqual(parts.parts_path(), str(self.bundled))


class TestAddToPath(unittest.TestCase):
    def test_adds_once_and_reports_a_missing_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(parts.add_ptcore_to_path(tmp))
            make_parts(Path(tmp))
            with mock.patch("sys.path", []) as path:
                self.assertTrue(parts.add_ptcore_to_path(tmp))
                self.assertTrue(parts.add_ptcore_to_path(tmp))
                self.assertEqual(path, [os.path.join(tmp, "ptcore")])


class TestPartMessage(unittest.TestCase):
    def test_japanese_stays_as_it_is(self) -> None:
        self.assertEqual(parts.part_message("元のファイルがありません: a.wav"),
                         "元のファイルがありません: a.wav")

    def test_english_rewords_and_keeps_the_rest(self) -> None:
        with mock.patch.object(parts, "is_english", return_value=True):
            self.assertEqual(
                parts.part_message("元のファイルがありません: キック.wav"),
                "The source file does not exist: キック.wav")
            self.assertEqual(parts.part_message("60 秒で終わりませんでした"),
                             "60 seconds passed without finishing")
            self.assertEqual(
                parts.part_message("ffmpeg が失敗しました: 終了コード 1"),
                "ffmpeg failed: exit code 1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
