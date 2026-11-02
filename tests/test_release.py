"""配布物の部品の見張り（tools/release_parts.py・LICENSE.txt・docs/・resources/）。

配布版そのものを組み立てるのは tools/build_release.py（数十秒かかり、PyInstaller が要る）。
ここでは、組み立てる前に分かる食い違いを見る。

- 同梱する借り物の部品に、integration/ が読むものが全部入っているか
  （足りないと、配布版でだけその機能が黙って止まる）
- 版の番号がマニュアル・はじめにお読みください・spec で食い違っていないか
- ライセンスとマニュアルに、著作権者 Takahashi と AI で作った旨があるか
- 配る文書に、作者の名前や手元のパスが入っていないか
- 検索語のファイルが壊れていたら止まるか（黙って検査を飛ばさない）
"""

from __future__ import annotations

import ast
import re
import tempfile
import unittest
from pathlib import Path

from core import i18n
from core.version import BUNDLE_NAME, ENGLISH_NAME, VERSION
from tools import build_release as br
from tools.release_parts import PART_FILES, find_part

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

# integration/ が借り物の ptcore からフラットに読む名前（import する文から拾う）
_PTCORE_NAMES = {"audiofile", "dsp", "executor", "keydetect", "library",
                 "placement", "prep", "sessioninfo", "splicename",
                 "srformat", "timeline"}


def _flat_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif (isinstance(node, ast.ImportFrom) and node.level == 0
              and node.module):
            names.add(node.module.split(".")[0])
    return names


class TestBundledParts(unittest.TestCase):
    def test_every_ptcore_module_the_app_reads_is_bundled(self) -> None:
        bundled = {Path(rel).stem for rel in PART_FILES}
        used = set()
        for path in (ROOT / "integration").glob("*.py"):
            used |= _flat_imports(path) & _PTCORE_NAMES
        self.assertTrue(used)
        self.assertEqual(used - bundled, set())

    def test_bundled_parts_can_read_each_other(self) -> None:
        """部品が読む部品も入っていること（無いと配布版でだけ止まる）。"""
        base = find_part()
        if base is None:
            self.skipTest("借り物の部品が無い")
        bundled = {Path(rel).stem for rel in PART_FILES}
        for rel in PART_FILES:
            tree = ast.parse((base / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 1:
                    self.assertIn(node.module, bundled, rel)

    def test_spec_does_not_exclude_what_the_app_needs(self) -> None:
        """spec で外したものを、本体が import していないこと（py-ptsl は旧経路だけ）。"""
        spec = (ROOT / "tools" / "release.spec").read_text(encoding="utf-8")
        block = spec.split("excludes=[", 1)[1].split("]", 1)[0]
        excludes = set(re.findall(r'"([\w.]+)"', block))
        self.assertIn("ptsl", excludes)
        for folder in ("core", "audio", "ui", "integration"):
            for path in (ROOT / folder).glob("*.py"):
                if path.name == "ptsl_client.py":
                    continue            # 控えの旧経路。配布版では使わない
                used = _flat_imports(path)
                self.assertEqual(used & excludes, set(), path.name)


class TestVersionsAgree(unittest.TestCase):
    def test_manual_shows_the_version(self) -> None:
        text = (DOCS / "manual.html").read_text(encoding="utf-8")
        self.assertIn(f"<title>PTサンプルマネージャ マニュアル（{VERSION}）</title>", text)
        english = (DOCS / "manual_en.html").read_text(encoding="utf-8")
        self.assertIn(f"<title>PT Sample Manager Manual ({VERSION})</title>",
                      english)
        self.assertEqual(set(re.findall(
            r'<span class="version">([^<]*)</span>', english)), {VERSION})
        self.assertIn(f"{VERSION} (", english.split('id="history"', 1)[1])
        spans = re.findall(r'<span class="version">([^<]*)</span>', text)
        self.assertTrue(spans)
        self.assertEqual(set(spans), {VERSION})
        self.assertIn(f"{VERSION}（", text.split('id="history"', 1)[1])

    def test_readme_takes_the_version(self) -> None:
        for name in ("readme.txt", "readme_en.txt"):
            text = (DOCS / name).read_text(encoding="utf-8")
            self.assertIn("{VERSION}", text)

    def test_manual_txt_is_generated_from_the_html(self) -> None:
        """txt は HTML から作る。手で直すと次の作り直しで消える。"""
        import sys

        sys.path.insert(0, str(ROOT / "tools"))
        from html_to_txt import DocumentToText

        for stem in ("manual", "manual_en"):
            parser = DocumentToText()
            parser.feed((DOCS / f"{stem}.html").read_text(encoding="utf-8"))
            expected = parser.render()
            actual = (DOCS / f"{stem}.txt").read_text(encoding="utf-8-sig")
            self.assertEqual(actual.replace("\r\n", "\n"), expected)

    def test_both_manuals_have_the_same_chapters(self) -> None:
        """同じ章・同じ id（片方にだけ足した章に気づく）。"""
        ids = [re.findall(r'<h2 id="([^"]+)"',
                          (DOCS / name).read_text(encoding="utf-8"))
               for name in ("manual.html", "manual_en.html")]
        self.assertEqual(ids[0], ids[1])
        self.assertTrue(ids[0])


class TestNotices(unittest.TestCase):
    """ライセンス名は Takahashi、AI で作った旨を書く（全プロジェクトの決まり）。"""

    def test_license_names_takahashi(self) -> None:
        text = (ROOT / "LICENSE.txt").read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2026 Takahashi", text)
        self.assertIn("MIT License", text)

    def test_ai_notice_everywhere(self) -> None:
        for path in br.DOC_FILES.values():
            text = path.read_text(encoding="utf-8-sig")
            self.assertIn("Anthropic", text, path.name)
            self.assertIn("Claude", text, path.name)

    def test_version_info_mentions_ai(self) -> None:
        from core.build_info import (FLAVOR_DISTRIBUTION, BuildInfo,
                                     describe_ja)

        info = BuildInfo(version=VERSION, built_at="2026-10-24 12:00",
                         commit="abc1234", flavor=FLAVOR_DISTRIBUTION)
        text = describe_ja(info, ROOT)
        self.assertIn("配布版", text)
        self.assertIn("Claude", text)
        self.assertIn("Takahashi", text)

    def test_no_personal_information_in_documents(self) -> None:
        """検索語は手元の release_private.json にある（公開しない）。"""
        words = br.PERSONAL + br.PERSONAL_TEXT + br.PRIVATE_WORDS
        if not words:
            self.skipTest("検索語が無い（公開しているソース）")
        for path in br.DOC_FILES.values():
            found = br.find_words(path.read_text(encoding="utf-8-sig"), words)
            self.assertEqual(found, [], path.name)

    def test_clean_source_drops_comments_and_docstrings_only(self) -> None:
        source = (
            '"""モジュールの説明。"""\n# コメント\n'
            'def f():\n    """関数の説明。"""\n    return "残る文字列"\n'
            'def g():\n    """説明だけ。"""\n')
        out = br.clean_source(source)
        self.assertNotIn("説明", out)
        self.assertNotIn("コメント", out)
        self.assertIn("残る文字列", out)
        namespace: dict = {}
        exec(out, namespace)
        self.assertEqual(namespace["f"](), "残る文字列")
        self.assertIsNone(namespace["g"]())


class TestPrivateWords(unittest.TestCase):
    """検索語のファイル。**読めないまま検査を素通りしない。**"""

    GOOD = ('{"personal": ["Name"], "personal_text": ["mail"], '
            '"words": ["Project"]}')

    def _read(self, text: str | None) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "words.json"
            if text is not None:
                path.write_text(text, encoding="utf-8")
            return br.read_private(path)

    def test_missing_file_means_no_words(self) -> None:
        self.assertEqual(self._read(None), {})

    def test_words_are_lowercased(self) -> None:
        data = self._read(self.GOOD)
        self.assertEqual(data["personal"], ("name",))
        self.assertEqual(data["words"], ("project",))

    def test_broken_files_stop_the_build(self) -> None:
        for text in ('{"personal": ["x"],}', "[]",
                     '{"personal": ["x"], "personal_text": ["y"]}',
                     '{"personal": [], "personal_text": ["y"], '
                     '"words": ["z"]}',
                     '{"personal": "x", "personal_text": ["y"], '
                     '"words": ["z"]}'):
            with self.subTest(text=text):
                with self.assertRaises(SystemExit):
                    self._read(text)

    def test_find_words_ignores_case(self) -> None:
        self.assertEqual(br.find_words("A Project and NOTE",
                                       ("project", "note", "other")),
                         ["project", "note"])


class TestOwnTextChecks(unittest.TestCase):
    """検査の対象。.app の中で自分たちが書いたものは、名前の訳も Info.plist も見る。"""

    def _own(self, rel: str) -> bool:
        folder = Path("/x")
        return br._own_text(folder, folder / rel)

    def test_targets(self) -> None:
        app = f"{BUNDLE_NAME}.app"
        self.assertTrue(self._own("README (English).txt"))
        self.assertFalse(self._own(br.THIRD_PARTY))
        self.assertTrue(self._own(f"{app}/Contents/Info.plist"))
        self.assertTrue(
            self._own(f"{app}/Contents/Resources/en.lproj/InfoPlist.strings"))
        self.assertTrue(self._own(
            f"{app}/Contents/Resources/siblings/x/ptcore/dsp.py"))
        self.assertFalse(self._own(f"{app}/Contents/Resources/base.py"))


class TestLanguagesAgree(unittest.TestCase):
    """.app の言語の決まり方を、画面の言語（core/i18n.py）とそろえる。"""

    def _strings(self, lang: str) -> str:
        return (ROOT / "resources" / f"{lang}.lproj"
                / "InfoPlist.strings").read_text(encoding="utf-8")

    def test_development_region_is_the_fallback_language(self) -> None:
        """日本語も英語も無い Mac で、名前と許可の説明まで英語になるように。"""
        spec = (ROOT / "tools" / "release.spec").read_text(encoding="utf-8")
        self.assertIn('"CFBundleDevelopmentRegion": "en"', spec)
        self.assertEqual(i18n.pick_language(["fr-FR"]), "en")

    def test_japanese_has_every_permission_text(self) -> None:
        spec = (ROOT / "tools" / "release.spec").read_text(encoding="utf-8")
        keys = set(re.findall(r'"(NS\w+UsageDescription)"', spec))
        self.assertTrue(keys)
        strings = self._strings("ja")
        for key in keys:
            self.assertIn(f'"{key}" =', strings)

    def test_names_in_each_language(self) -> None:
        self.assertIn(f'"CFBundleDisplayName" = "{BUNDLE_NAME}"',
                      self._strings("ja"))
        self.assertIn(f'"CFBundleDisplayName" = "{ENGLISH_NAME}"',
                      self._strings("en"))

    def test_manuals_say_their_language(self) -> None:
        """読み上げの声と txt の記号は lang で決まる。取り違えない。"""
        for name, lang in (("manual.html", "ja"), ("manual_en.html", "en")):
            text = (DOCS / name).read_text(encoding="utf-8")
            self.assertIn(f'<html lang="{lang}">', text, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
