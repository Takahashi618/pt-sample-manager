"""表示の言語（core/i18n.py）の確認。

- 言語の決め方（Mac の「優先する言語」の上から、日本語か英語の先に出るほう。
  どちらも無ければ英語）
- **ソースの文言がすべて日本語と英語の組になっていること**（ast で見る）
- 読み上げの頭の印が両方の言語でそろっていること（通知音はこの印で選ぶ）
- 借りている部品の文言が、言い換えの表で全部英語になること（部品の文言が
  変わると、英語の読み上げに日本語が混ざる）
- **英語で起動したとき、画面・メニュー・読み上げに日本語が混ざらないこと**
  （別のプロセスで英語にして集める。tests/en_probe.py。言語は起動したときに
  決まるので、このプロセスの中では切り替えられない）
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from core import i18n, parts
from ui import sounds

ROOT = Path(__file__).resolve().parent.parent
JAPANESE = re.compile(r"[　-ヿ一-鿿＀-￯]")
FOLDERS = ("core", "ui", "integration")
# 文言ではない日本語を持つ変数（部品の文言の表、ファイル名の中を探す語、
# .app の本当の名前、文言の先頭を見て音を選ぶ印、日本語のときだけ使うメニューの訳）
NOT_TEXT_NAMES = {"_PART_WORDS", "_LOOP_HINTS", "BUNDLE_NAME",
                  "FAILURE_PREFIXES", "WINDOW_MENU_TITLE", "WINDOW_MENU_RULES"}
JAPANESE_ONLY_FUNCTIONS = {"_localize_app_menu"}
# 読み上げの頭の印（日本語 → 英語）
PREFIXES = {"[エラー]": "[Error]", "[警告]": "[Warning]", "[情報]": "[Info]"}


def source_files() -> list[Path]:
    paths = [ROOT / "app.py"]
    for folder in FOLDERS:
        paths += sorted((ROOT / folder).glob("*.py"))
    return paths


def tr_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "tr":
            yield node


def _skipped_nodes(tree: ast.AST) -> set[int]:
    """文言として調べないノード。説明文と、上の変数・関数の中。"""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if (isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                              ast.AsyncFunctionDef))
                and node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)):
            skip.add(id(node.body[0].value))
        named = isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") in NOT_TEXT_NAMES for t in node.targets)
        function = (isinstance(node, ast.FunctionDef)
                    and node.name in JAPANESE_ONLY_FUNCTIONS)
        if named or function:
            skip.update(id(c) for c in ast.walk(node))
    return skip


def untranslated(path: Path, rel: str) -> list[str]:
    """tr(日本語, 英語) に入っていない日本語の文字列。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _skipped_nodes(tree)
    problems: list[str] = []
    for call in tr_calls(tree):
        ja, en = call.args
        skip.update(id(c) for c in ast.walk(ja))
        for child in ast.walk(en):
            if (isinstance(child, ast.Constant)
                    and isinstance(child.value, str)
                    and JAPANESE.search(child.value)):
                problems.append(f"{rel}:{call.lineno} 英語の側に日本語: "
                                f"{child.value[:40]}")
            skip.add(id(child))
    for node in ast.walk(tree):
        if (id(node) not in skip and isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and JAPANESE.search(node.value)):
            problems.append(f"{rel}:{node.lineno} {node.value[:40]}")
    return problems


def _first_text(node: ast.AST) -> str:
    """文字列の先頭の決まった部分（f 文字列なら最初の差し込みの前まで）。"""
    if isinstance(node, ast.JoinedStr):
        node = node.values[0]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


class PickLanguageTest(unittest.TestCase):
    CASES = [
        (["ja-JP", "en-JP"], "ja"),
        (["en-US", "ja-JP"], "en"),
        (["en-GB"], "en"),
        (["fr-FR", "ja-JP"], "ja"),        # 日本語と英語のうち、先に出るほう
        (["fr-FR", "de-DE"], "en"),        # どちらも無ければ英語
        ([], "en"),
        (["ja_JP.UTF-8"], "ja"),           # 環境変数 LANG の書き方
        (["zh-Hans-CN", "en-CN"], "en"),
        (["Japanese_Japan"], "ja"),        # Windows のロケールの書き方
        (["English_United States"], "en"),
    ]

    def test_cases(self) -> None:
        for langs, want in self.CASES:
            with self.subTest(langs=langs):
                self.assertEqual(i18n.pick_language(langs), want)

    def test_tests_run_in_japanese(self) -> None:
        """テストは日本語の文言で書いてある（tests/__init__.py が決める）。"""
        self.assertEqual(i18n.LANG, "ja")
        self.assertEqual(i18n.tr("日本語", "English"), "日本語")
        self.assertEqual(i18n.COMMA, "、")

    def test_plural(self) -> None:
        self.assertEqual(i18n.plural(1, "sample"), "1 sample")
        self.assertEqual(i18n.plural(0, "sample"), "0 samples")
        self.assertEqual(i18n.plural(3, "sample"), "3 samples")
        self.assertEqual(i18n.plural(1.0, "bar"), "1 bar")
        self.assertEqual(i18n.plural(4.0, "bar"), "4 bars")
        self.assertEqual(i18n.plural(1.5, "bar"), "1.5 bars")
        self.assertEqual(i18n.plural(1000000, "sample"), "1000000 samples")

    def test_sentences_in_japanese_are_joined_by_a_space(self) -> None:
        self.assertEqual(i18n.sentences(["外しました", "", "2 件です。"]),
                         "外しました 2 件です。")
        self.assertEqual(i18n.sentences(["", "  "]), "")


class EveryStringIsPairedTest(unittest.TestCase):
    def test_no_japanese_string_outside_tr(self) -> None:
        problems: list[str] = []
        for path in source_files():
            problems += untranslated(path, path.relative_to(ROOT).as_posix())
        self.assertEqual(problems, [])

    def test_status_prefixes_match_in_both_languages(self) -> None:
        """通知音は文言の先頭で選ぶ。英語の印を書き違えると、失敗なのに成功の音が鳴る。"""
        checked = 0
        for path in source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for call in tr_calls(tree):
                ja, en = (_first_text(arg) for arg in call.args)
                for ja_mark, en_mark in PREFIXES.items():
                    if ja.startswith(ja_mark):
                        checked += 1
                        self.assertTrue(en.startswith(en_mark),
                                        f"{path.name}:{call.lineno} {en[:30]}")
        self.assertGreater(checked, 10)

    def test_sounds_know_the_failure_prefixes(self) -> None:
        for mark in ("[エラー]", "[警告]", "[Error]", "[Warning]"):
            self.assertIn(mark, sounds.FAILURE_PREFIXES)


class PartWordsTest(unittest.TestCase):
    """借りている部品の文言が、言い換えの表で全部英語になること。"""

    # 利用者まで届く文を返す部品（ptsl は作者の手元だけの控えの接続。公開しない）
    MODULES = ("dsp", "splicename", "library", "prep", "ptsl")

    def _messages(self, path: Path) -> set[str]:
        """raise の中の文字列と、message= に渡す文字列。"""
        tree = ast.parse(path.read_text(encoding="utf-8"))
        roots: list[ast.AST] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and node.exc is not None:
                roots.append(node.exc)
            elif isinstance(node, ast.keyword) and node.arg == "message":
                roots.append(node.value)
        return {child.value for root in roots for child in ast.walk(root)
                if isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and JAPANESE.search(child.value)}

    def test_every_message_has_english(self) -> None:
        base = Path(parts.parts_path()) / "ptcore"
        if not base.is_dir():
            self.skipTest("借りている部品が無い")
        checked = 0
        for module in self.MODULES:
            path = base / f"{module}.py"
            if not path.is_file():
                continue
            for message in self._messages(path):
                checked += 1
                text = message
                for ja, en in parts._PART_WORDS:
                    text = text.replace(ja, en)
                self.assertIsNone(JAPANESE.search(text),
                                  f"{module}: {message} → {text}")
        self.assertGreater(checked, 10)


def _probe() -> dict:
    env = dict(os.environ, SM_SILENT="1", SM_NO_ANNOUNCE="1")
    env[i18n.ENV_NAME] = "en"
    done = subprocess.run(
        [sys.executable, "-X", "utf8", str(ROOT / "tests" / "en_probe.py")],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise AssertionError(done.stderr[-2000:])
    return json.loads(done.stdout.strip().splitlines()[-1])


class EnglishScreenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.out = _probe()

    def test_language(self) -> None:
        self.assertEqual(self.out["lang"], ["en"])

    def test_nothing_is_in_japanese(self) -> None:
        for group, texts in self.out.items():
            for text in texts:
                with self.subTest(group=group, text=text):
                    self.assertIsNone(JAPANESE.search(text))

    def test_names(self) -> None:
        self.assertEqual(self.out["window"][0], "PT Sample Manager")
        self.assertIn("Actions", self.out["menus"])
        self.assertIn("Help", self.out["menus"])

    def test_every_group_was_collected(self) -> None:
        for group in ("menu_items", "widgets", "settings", "samples", "scan",
                      "placement", "build", "render", "controller", "keys",
                      "player"):
            self.assertTrue(self.out[group], group)

    def test_singular_forms(self) -> None:
        """「1 packs」「1 milliseconds」のような形で読まない。"""
        wrong = re.compile(r"\b1 (?:sample|pack|source|file|favorite|bar|"
                           r"millisecond|semitone)s\b")
        for group, texts in self.out.items():
            for text in texts:
                with self.subTest(group=group, text=text):
                    self.assertIsNone(wrong.search(text))

    def test_placing_copies_reads_as_a_sentence(self) -> None:
        joined = " ".join(self.out["placement"])
        self.assertIn("at 1|1|000 (4 copies).", joined)
        self.assertIn("The selected tracks, “Inst 1, Inst 2”, are not audio "
                      "tracks.", joined)

    def test_sentences_end_with_a_period(self) -> None:
        self.assertIn("The source was not removed. 2 sources.",
                      self.out["sentences"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
