"""integration.applescript のエスケープロジックのユニットテスト。

osascript を実行しない純粋関数部分のみを検証する（OS非依存）。
"""

from __future__ import annotations

import unittest

from integration.applescript import escape_applescript, quote


class TestEscapeApplescript(unittest.TestCase):
    def test_plain(self) -> None:
        self.assertEqual(escape_applescript("kick.wav"), "kick.wav")

    def test_double_quote_escaped(self) -> None:
        self.assertEqual(escape_applescript('a"b'), 'a\\"b')

    def test_backslash_escaped(self) -> None:
        self.assertEqual(escape_applescript("a\\b"), "a\\\\b")

    def test_order_backslash_then_quote(self) -> None:
        # \" は バックスラッシュ→引用符 の順で \\\" になる
        self.assertEqual(escape_applescript('\\"'), '\\\\\\"')

    def test_path_with_spaces(self) -> None:
        p = "/Volumes/Audio/My Samples/kick 01.wav"
        self.assertEqual(escape_applescript(p), p)

    def test_quote_wraps(self) -> None:
        self.assertEqual(quote("kick.wav"), '"kick.wav"')
        self.assertEqual(quote('a"b'), '"a\\"b"')


if __name__ == "__main__":
    unittest.main(verbosity=2)
