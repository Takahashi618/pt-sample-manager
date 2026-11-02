"""キー操作の一覧のユニットテスト（wx を起動しない部分）。

**説明と実際の割り当てがずれると実害が大きい。** 利用者は説明を信じて
操作するので、書いてあるキーが効かないと原因を探しようがない。
一覧の中身がメニューの割り当てと対になっているかをここで確かめる。
"""

from __future__ import annotations

import unittest

from ui.keys_dialog import KEY_HELP, describe_key, key_rows


class TestDescribeKey(unittest.TestCase):
    def test_puts_the_place_first(self):
        """どこで押すのかが先。いまいる場所と照らして聞けるようにする。"""
        text = describe_key("結果リスト", "スペース", "試聴を再生または停止する")
        self.assertTrue(text.startswith("結果リスト"))
        self.assertIn("スペース", text)
        self.assertIn("試聴", text)


class TestKeyRows(unittest.TestCase):
    def setUp(self):
        self.rows = key_rows()

    def test_has_a_row_for_every_entry(self):
        self.assertEqual(len(self.rows), len(KEY_HELP))

    def test_every_row_is_readable(self):
        """空の行や記号だけの行を作らない（読み上げても分からないため）。"""
        for row in self.rows:
            self.assertTrue(row.strip())
            self.assertGreater(len(row), 8)

    def test_covers_the_list_keys(self):
        joined = "".join(self.rows)
        for expected in ("上下矢印", "文字キー", "スペース", "エンター"):
            self.assertIn(expected, joined)

    def test_covers_every_command_shortcut(self):
        """**メニューの割り当てと対で保つ。** 片方だけ直すとずれる。"""
        joined = "".join(self.rows)
        for letter in ("C", "E", "S", "F", "T", "R"):
            self.assertIn(f"コマンドと{letter}", joined)

    def test_mentions_typing_to_jump(self):
        """修飾なしの文字キーを説明に残す。

        ここが抜けると「文字キーは何かのショートカット」と思われ、
        頭文字での移動が使われなくなる。
        """
        joined = "".join(self.rows)
        self.assertIn("頭文字", joined)

    def test_mentions_tempo_where_it_is_set(self):
        """**テンポをどこで変えるか分かること。**

        「セッションの条件」だけでは、テンポの設定がそこにあると
        気づけない（実機で一覧に載せてほしいと指摘を受けた）。
        """
        self.assertIn("テンポ", "".join(key_rows()))

    def test_does_not_use_the_slash_shortcut(self):
        """Cmd+/ は Pro Tools と重なって効かなかった。"""
        self.assertNotIn("スラッシュ", "".join(key_rows()))

    def test_mentions_favorites(self):
        """お気に入りの登録キーが分かること。"""
        joined = "".join(key_rows())
        self.assertIn("お気に入り", joined)
        self.assertIn("コマンドとD", joined)

    def test_mentions_switching_sources(self):
        """読み取り先をすぐ切り替えられることが分かること。"""
        joined = "".join(key_rows())
        self.assertIn("コマンドと数字", joined)
        self.assertIn("読み取り先", joined)
        self.assertIn("コマンドとP", joined)

    def test_mentions_dialog_enter_and_escape(self):
        """**決定と取り消しの手段が分かること。**

        選択肢にフォーカスがあると Enter が届かず「決定できない」と
        指摘を受けた経緯がある。
        """
        joined = "".join(key_rows())
        self.assertIn("エンター", joined)
        self.assertIn("エスケープ", joined)

    def test_mentions_preview_toggle_in_the_dialog(self):
        """加工した音を止める手段が分かること。"""
        joined = "".join(key_rows())
        self.assertIn("コントロールとシフトとL", joined)
        self.assertIn("止まる", joined)

    def test_avoids_shortcuts_the_system_takes(self):
        """**macOS が横取りするキーを割り当てない。**

        コマンドとスペースは Spotlight が先に取るのでアプリに届かない
        （実機で指摘を受けた）。同じ理由でコマンドとタブも使えない。
        """
        joined = "".join(key_rows())
        for reserved in ("コマンドとスペース", "コマンドとタブ"):
            self.assertNotIn(reserved, joined)

    def test_uses_words_not_symbols(self):
        """読み上げ向けに記号を使わない（「+」は読まれないことがある）。"""
        joined = "".join(self.rows)
        self.assertNotIn("+", joined)
        self.assertNotIn("⌘", joined)


class TestKeyHelpShape(unittest.TestCase):
    def test_every_entry_has_three_parts(self):
        for entry in KEY_HELP:
            self.assertEqual(len(entry), 3)
            for part in entry:
                self.assertTrue(part.strip())

    def test_no_duplicate_rows(self):
        rows = key_rows()
        self.assertEqual(len(rows), len(set(rows)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
