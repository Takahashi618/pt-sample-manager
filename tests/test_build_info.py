"""ビルド情報の読み取りのユニットテスト。

**「動作が古い」と「検証が古い」を混同しないこと**が要。
ランチャは常に最新のソースを起動するので、ビルドが古くても
動くコードは最新。文言でそこを取り違えると、利用者が
「古い版が動いている」と誤解する。
"""

from __future__ import annotations

import os
import plistlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from core import build_info


def _make_bundle(root: Path, **entries) -> Path:
    """テスト用の .app を作る。"""
    bundle = root / "PTサンプルマネージャ.app"
    contents = bundle / "Contents"
    contents.mkdir(parents=True, exist_ok=True)
    with open(contents / "Info.plist", "wb") as f:
        plistlib.dump(entries, f)
    return bundle


class TestReadBuildInfo(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reads_recorded_values(self) -> None:
        bundle = _make_bundle(
            self.root,
            CFBundleShortVersionString="1.0",
            CFBundleVersion="2026.0831.0510",
            SMBuildDate="2026-08-31 05:10",
            SMBuildTimestamp=1756600000.0,
            SMGitCommit="abc1234",
        )
        info = build_info.read_build_info(bundle)
        self.assertEqual(info.version, "1.0")
        self.assertEqual(info.built_at, "2026-08-31 05:10")
        self.assertEqual(info.commit, "abc1234")
        self.assertAlmostEqual(info.built_stamp, 1756600000.0)
        self.assertTrue(info.known)

    def test_missing_bundle_is_not_an_error(self) -> None:
        """まだビルドしていなくても起動を止めない。"""
        info = build_info.read_build_info(self.root / "ない.app")
        self.assertFalse(info.known)

    def test_broken_plist_is_not_an_error(self) -> None:
        bundle = self.root / "PTサンプルマネージャ.app"
        (bundle / "Contents").mkdir(parents=True)
        (bundle / "Contents" / "Info.plist").write_text("こわれている")
        self.assertFalse(build_info.read_build_info(bundle).known)

    def test_non_numeric_timestamp_falls_back(self) -> None:
        bundle = _make_bundle(self.root, SMBuildTimestamp="いつか",
                              SMBuildDate="2026-08-31 05:10")
        self.assertEqual(build_info.read_build_info(bundle).built_stamp, 0.0)


class TestStaleness(unittest.TestCase):
    """ビルド後にソースを直したかどうか。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "core").mkdir()
        self.source = self.root / "core" / "library.py"
        self.source.write_text("# ソース\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _touch(self, when: float) -> None:
        os.utime(self.source, (when, when))

    def test_source_older_than_build_is_fresh(self) -> None:
        self._touch(1000.0)
        info = build_info.BuildInfo(built_at="いつか", built_stamp=2000.0)
        self.assertFalse(build_info.is_stale(info, self.root))

    def test_source_newer_than_build_is_stale(self) -> None:
        self._touch(3000.0)
        info = build_info.BuildInfo(built_at="いつか", built_stamp=2000.0)
        self.assertTrue(build_info.is_stale(info, self.root))

    def test_no_build_info_counts_as_stale(self) -> None:
        """一度もビルドしていないなら、確かめていないのと同じ。"""
        self.assertTrue(build_info.is_stale(build_info.BuildInfo(), self.root))

    def test_latest_mtime_takes_the_newest(self) -> None:
        self._touch(1000.0)
        other = self.root / "app.py"
        other.write_text("# 入口\n")
        os.utime(other, (5000.0, 5000.0))
        self.assertAlmostEqual(
            build_info.latest_source_mtime(self.root), 5000.0)


class TestDescribeJa(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "core").mkdir()
        self.source = self.root / "core" / "library.py"
        self.source.write_text("# ソース\n")
        os.utime(self.source, (1000.0, 1000.0))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fresh_build_states_it_matches(self) -> None:
        info = build_info.BuildInfo(
            version="1.0", built_at="2026-08-31 05:10",
            built_stamp=2000.0, commit="abc1234")
        text = build_info.describe_ja(info, self.root)
        self.assertIn("[情報]", text)
        self.assertIn("2026-08-31 05:10", text)
        self.assertIn("abc1234", text)
        self.assertIn("ビルド時点のまま", text)

    def test_stale_build_does_not_say_the_app_is_outdated(self) -> None:
        """**ここが要。** 動くコードは最新。古いのは検証のほう。"""
        os.utime(self.source, (9000.0, 9000.0))
        info = build_info.BuildInfo(
            version="1.0", built_at="2026-08-31 05:10", built_stamp=2000.0)
        text = build_info.describe_ja(info, self.root)
        self.assertIn("動作は最新", text)
        self.assertIn("検証は済んでいません", text)

    def test_no_build_info_says_it_runs_the_latest(self) -> None:
        text = build_info.describe_ja(build_info.BuildInfo(), self.root)
        self.assertIn("最新のソースで動作します", text)


class TestDistribution(unittest.TestCase):
    """配布版（PyInstaller で固めた .app）のバージョン情報。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reads_the_flavor(self) -> None:
        bundle = _make_bundle(self.root, CFBundleShortVersionString="1.0.0",
                              SMBuildDate="2026-10-24 19:30",
                              SMGitCommit="abc1234",
                              SMFlavor=build_info.FLAVOR_DISTRIBUTION)
        info = build_info.read_build_info(bundle)
        self.assertTrue(info.distribution)
        self.assertEqual(info.version, "1.0.0")

    def test_describes_version_date_and_ai(self) -> None:
        """配布版は新しさの判定をせず、版・作成日時・AI で作った旨を言う。"""
        info = build_info.BuildInfo(version="1.0.0",
                                    built_at="2026-10-24 19:30",
                                    commit="abc1234",
                                    flavor=build_info.FLAVOR_DISTRIBUTION)
        text = build_info.describe_ja(info, self.root)
        self.assertTrue(text.startswith("[情報] PTサンプルマネージャ 配布版"))
        self.assertIn("バージョン 1.0.0", text)
        self.assertIn("2026-10-24 19:30", text)
        self.assertIn("Anthropic", text)
        self.assertIn("MIT", text)
        # 手元の版の言い方（ソースやビルドの道具）は出さない
        self.assertNotIn("ソース", text)
        self.assertNotIn("tools/", text)

    def test_local_build_has_no_flavor(self) -> None:
        info = build_info.BuildInfo(built_at="2026-10-24 19:30",
                                    built_stamp=1.0)
        self.assertFalse(info.distribution)

    def test_controller_reads_the_bundle_it_is_told(self) -> None:
        """配布版は起動時に SM_APP_BUNDLE で自分の .app を教える。"""
        from unittest import mock

        from core.library import Library
        from ui.controller import AppController

        bundle = _make_bundle(self.root, CFBundleShortVersionString="9.9.9",
                              SMBuildDate="2026-10-24 19:30",
                              SMFlavor=build_info.FLAVOR_DISTRIBUTION)
        lib = Library(":memory:")
        try:
            with mock.patch.dict(os.environ, {"SM_APP_BUNDLE": str(bundle)}):
                text = AppController(lib).describe_build_ja()
        finally:
            lib.close()
        self.assertIn("バージョン 9.9.9", text)
        self.assertIn("配布版", text)


class TestBuildNumber(unittest.TestCase):
    def test_is_sortable_by_time(self) -> None:
        """macOS はこの値で新旧を判断するので、必ず増える形にする。"""
        earlier = build_info.format_build_number(
            datetime(2026, 8, 31, 5, 10))
        later = build_info.format_build_number(
            datetime(2026, 9, 1, 5, 10))
        self.assertLess(earlier, later)
        self.assertEqual(earlier, "2026.0831.0510")


if __name__ == "__main__":
    unittest.main(verbosity=2)
