"""セッション条件（テンポ・キー・サンプルレート）のユニットテスト。

**Pro Tools からテンポとキーは取れない**ので、利用者の入力をここに保持する。
打ち間違いをそのまま受け入れると、気づかないうちに全部の素材が違う尺で
置かれてしまうため、検証を厚めにしてある。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core import session_settings as sm_settings
from core.session_settings import (
    DEFAULT_RATE,
    SessionSettings,
    parse_bpm,
    parse_key,
)


class TestDefaults(unittest.TestCase):
    def test_default_does_not_render(self):
        """既定では加工しない。条件を入れて初めて加工が始まる。"""
        self.assertFalse(SessionSettings().can_render)

    def test_default_rate(self):
        self.assertEqual(SessionSettings().sample_rate, DEFAULT_RATE)

    def test_tempo_alone_enables_render(self):
        self.assertTrue(SessionSettings(bpm=120).can_render)

    def test_key_alone_enables_render(self):
        self.assertTrue(SessionSettings(key="Am").can_render)

    def test_auto_render_off_disables_it(self):
        s = SessionSettings(bpm=120, key="Am", auto_render=False)
        self.assertFalse(s.can_render)


class TestDescribe(unittest.TestCase):
    def test_unset_is_stated(self):
        text = SessionSettings().describe_ja()
        self.assertIn("テンポ未設定", text)
        self.assertIn("キー未設定", text)

    def test_values_are_included(self):
        settings = SessionSettings(bpm=128, key="Am", sample_rate=44100)
        text = settings.describe_ja()
        self.assertIn("128", text)
        # キーは「Aマイナー」の形で読む（"Am" のままだと読み上げが不定）
        self.assertIn("Aマイナー", text)
        self.assertIn("44100", text)

    def test_unreadable_key_is_read_as_is(self):
        """選択肢に無い書式のキーは、そのまま読む。

        「指定しない」と読むと、設定が消えていると誤解されるため。
        """
        text = SessionSettings(key="H moll").describe_ja()
        self.assertIn("H moll", text)
        self.assertNotIn("指定しない", text)

    def test_integer_tempo_has_no_decimal_point(self):
        """「120」を「120.0」と読み上げない。"""
        self.assertIn("テンポ 120", SessionSettings(bpm=120.0).describe_ja())

    def test_auto_render_off_is_mentioned(self):
        text = SessionSettings(bpm=120, auto_render=False).describe_ja()
        self.assertIn("自動加工オフ", text)


class TestParseBpm(unittest.TestCase):
    def test_blank_is_unset_not_an_error(self):
        for value in ("", "   ", None):
            bpm, error = parse_bpm(value)
            self.assertIsNone(bpm)
            self.assertEqual(error, "")

    def test_number(self):
        self.assertEqual(parse_bpm("128")[0], 128.0)

    def test_decimal(self):
        self.assertAlmostEqual(parse_bpm("128.5")[0], 128.5)

    def test_text_is_rejected(self):
        bpm, error = parse_bpm("はやめ")
        self.assertIsNone(bpm)
        self.assertIn("数値", error)

    def test_out_of_range_is_rejected(self):
        """打ち間違いを拾う。1200 と打っても通してはいけない。"""
        for value in ("0", "10", "1200"):
            bpm, error = parse_bpm(value)
            self.assertIsNone(bpm, value)
            self.assertTrue(error, value)

    def test_boundaries_are_accepted(self):
        self.assertIsNotNone(parse_bpm("20")[0])
        self.assertIsNotNone(parse_bpm("400")[0])


class TestParseKey(unittest.TestCase):
    def test_blank_is_unset(self):
        key, error = parse_key("")
        self.assertEqual(key, "")
        self.assertEqual(error, "")

    def test_valid_keys(self):
        for value in ("Am", "C", "F#maj", "Bbmin"):
            key, error = parse_key(value)
            self.assertEqual(error, "", f"{value}: {error}")
            self.assertEqual(key, value)

    def test_whitespace_is_trimmed(self):
        self.assertEqual(parse_key("  Am  ")[0], "Am")

    def test_nonsense_is_rejected_when_validator_is_available(self):
        key, error = parse_key("ぜんぜん違う")
        # 借りている部品が無い環境では素通しになるので、そのときは検証しない
        if error:
            self.assertEqual(key, "")


class TestPlacementOptions(unittest.TestCase):
    """配置のしかた（位置・個数・間隔）。"""

    def test_default_is_cursor_single(self):
        s = SessionSettings()
        self.assertTrue(s.at_cursor)
        self.assertEqual(s.copies, 1)
        self.assertEqual(s.positions(), [])

    def test_cursor_mode_returns_no_positions(self):
        """カーソル位置は実行時に Pro Tools から取るので計算しない。"""
        s = SessionSettings(at_cursor=True, copies=4, bpm=120)
        self.assertEqual(s.positions(), [])

    def test_single_position(self):
        s = SessionSettings(at_cursor=False, position="17|1|000")
        self.assertEqual(s.positions(), ["17|1|000"])

    def test_repeats_need_tempo(self):
        """間隔を小節で数えるのでテンポが要る。"""
        s = SessionSettings(at_cursor=False, copies=4, every_bars=4)
        self.assertFalse(s.can_repeat)
        self.assertEqual(len(s.positions()), 1)

    def test_repeats_with_tempo(self):
        s = SessionSettings(at_cursor=False, position="1|1|000",
                            copies=4, every_bars=4, bpm=120)
        self.assertTrue(s.can_repeat)
        positions = s.positions()
        self.assertEqual(len(positions), 4)
        # 4 小節おきなので 1, 5, 9, 13 小節目
        self.assertTrue(positions[0].startswith("1|"))
        self.assertTrue(positions[1].startswith("5|"))
        self.assertTrue(positions[3].startswith("13|"))

    def test_describe_mentions_the_position(self):
        s = SessionSettings(at_cursor=False, position="17|1|000")
        self.assertIn("17|1|000", s.describe_ja())

    def test_describe_mentions_cursor(self):
        self.assertIn("カーソル", SessionSettings().describe_ja())

    def test_describe_warns_when_repeats_cannot_run(self):
        s = SessionSettings(copies=4, every_bars=4)   # テンポなし
        self.assertIn("1 個だけ", s.describe_ja())

    def test_describe_states_the_repeat(self):
        s = SessionSettings(copies=4, every_bars=2, bpm=120)
        text = s.describe_ja()
        self.assertIn("4 個", text)
        self.assertIn("2 小節", text)


class TestParsePlacementInputs(unittest.TestCase):
    def test_position_accepts_bars_beats_ticks(self):
        value, error = sm_settings.parse_position("17|1|000")
        self.assertEqual(error, "")
        self.assertEqual(value, "17|1|000")

    def test_position_accepts_bars_beats(self):
        self.assertEqual(sm_settings.parse_position("5|3")[1], "")

    def test_position_rejects_blank(self):
        self.assertTrue(sm_settings.parse_position("")[1])

    def test_position_rejects_text(self):
        self.assertTrue(sm_settings.parse_position("さいしょ")[1])

    def test_position_rejects_bar_zero(self):
        """Pro Tools の小節は 1 から数える。"""
        self.assertTrue(sm_settings.parse_position("0|1|000")[1])

    def test_position_superscript_digits_do_not_crash(self):
        """isdigit は ² を通すが int() は受けない。落ちないこと。

        NFKC で半角の 2 にそろうので、2 小節目として読む。
        """
        self.assertEqual(sm_settings.parse_position("²|1"), ("2|1", ""))

    def test_position_rejects_other_scripts_digits(self):
        """NFKC でも半角にならない数字（アラビア数字など）は弾く。

        int() は受けるので、以前は検証を通って Pro Tools へそのまま渡っていた。
        """
        value, error = sm_settings.parse_position("١٧|١|٠٠٠")
        self.assertEqual(value, "")
        self.assertTrue(error)

    def test_position_fullwidth_digits_become_halfwidth(self):
        """日本語入力のまま打った全角の数字は、半角にして Pro Tools へ渡す。"""
        self.assertEqual(sm_settings.parse_position("１７|１|０００"),
                         ("17|1|000", ""))

    def test_position_fullwidth_bar_is_a_separator(self):
        """全角の縦棒（｜）も区切りとして読む。読み上げでは区別できない。"""
        self.assertEqual(sm_settings.parse_position("１７｜１｜０００"),
                         ("17|1|000", ""))

    def test_position_spaces_around_bars_are_dropped(self):
        self.assertEqual(sm_settings.parse_position(" 17 | 1 | 000 "),
                         ("17|1|000", ""))

    def test_bpm_accepts_fullwidth_decimal(self):
        """全角の小数点は float が受けない。半角にそろえてから読む。"""
        self.assertEqual(sm_settings.parse_bpm("１２０．５"), (120.5, ""))

    def test_every_bars_accepts_fullwidth_decimal(self):
        self.assertEqual(sm_settings.parse_every_bars("４．５"), (4.5, ""))

    def test_copies_accepts_fullwidth(self):
        self.assertEqual(sm_settings.parse_copies("４"), (4, ""))

    def test_every_bars_rejects_infinity_and_nan(self):
        """float は "inf" や "1e400" を受ける。配置の計算で落ちるので弾く。"""
        for text in ("inf", "-inf", "nan", "1e400", "Infinity"):
            value, error = sm_settings.parse_every_bars(text)
            self.assertTrue(error, text)
            self.assertEqual(value, 4.0, text)

    def test_infinite_bars_in_file_fall_back(self):
        """設定ファイルに無限大が入っていても、既定の 4 小節に戻す。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(
                '{"every_bars": Infinity, "copies": 4, "bpm": 120}',
                encoding="utf-8")
            loaded = SessionSettings.load(path)
        self.assertEqual(loaded.every_bars, 4.0)
        # 位置の計算が落ちない（以前は OverflowError になっていた）
        loaded.at_cursor = False
        self.assertEqual(len(loaded.positions()), 4)

    def test_copies_blank_is_one(self):
        self.assertEqual(sm_settings.parse_copies("")[0], 1)

    def test_copies_rejects_zero_and_text(self):
        self.assertTrue(sm_settings.parse_copies("0")[1])
        self.assertTrue(sm_settings.parse_copies("たくさん")[1])

    def test_copies_rejects_absurd_count(self):
        self.assertTrue(sm_settings.parse_copies("1000")[1])

    def test_every_bars_blank_is_four(self):
        self.assertEqual(sm_settings.parse_every_bars("")[0], 4.0)

    def test_every_bars_accepts_fraction(self):
        self.assertAlmostEqual(sm_settings.parse_every_bars("0.5")[0], 0.5)

    def test_every_bars_rejects_zero(self):
        self.assertTrue(sm_settings.parse_every_bars("0")[1])


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "settings.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_roundtrip(self):
        original = SessionSettings(bpm=128.5, key="Am", sample_rate=44100,
                                   auto_render=False, at_cursor=False,
                                   position="17|1|000", copies=4,
                                   every_bars=2.0)
        original.save(self.path)
        loaded = SessionSettings.load(self.path)
        self.assertEqual(loaded, original)

    def test_broken_position_in_file_falls_back(self):
        self.path.write_text(json.dumps({"position": "でたらめ"}),
                             encoding="utf-8")
        self.assertEqual(SessionSettings.load(self.path).position, "1|1|000")

    def test_missing_file_gives_defaults(self):
        loaded = SessionSettings.load(Path(self.dir.name) / "no.json")
        self.assertEqual(loaded, SessionSettings())

    def test_broken_file_gives_defaults(self):
        self.path.write_text("{ こわれている", encoding="utf-8")
        self.assertEqual(SessionSettings.load(self.path), SessionSettings())

    def test_out_of_range_value_in_file_is_dropped(self):
        """手で書き換えられた設定ファイルも検証する。"""
        self.path.write_text(json.dumps({"bpm": 99999}), encoding="utf-8")
        self.assertIsNone(SessionSettings.load(self.path).bpm)

    def test_bad_rate_falls_back(self):
        self.path.write_text(json.dumps({"sample_rate": -1}), encoding="utf-8")
        self.assertEqual(SessionSettings.load(self.path).sample_rate,
                         DEFAULT_RATE)

    def test_saving_to_unwritable_place_does_not_raise(self):
        """設定を保存できないことでアプリが止まるほうが困る。"""
        SessionSettings(bpm=120).save(Path("/この経路は存在しない/x.json"))

    def test_creates_missing_directory(self):
        nested = Path(self.dir.name) / "深い" / "階層" / "settings.json"
        SessionSettings(bpm=120).save(nested)
        self.assertTrue(nested.exists())


class TestLowercaseKeys(unittest.TestCase):
    """**小文字で保存されたキーも消さない。**

    借りている部品の検証は大文字小文字を区別しないので、以前の自由入力欄では
    "am" のまま保存できた。フラット表記と同じく、選択肢に写せないと
    ダイアログを開いて OK しただけでキーが空になる。
    """

    def test_lowercase_root_maps_to_the_choice(self):
        from core.session_settings import split_key

        self.assertEqual(split_key("am"), ("A", True))
        self.assertEqual(split_key("c#"), ("C#", False))

    def test_lowercase_flat_maps_to_the_sharp_choice(self):
        from core.session_settings import split_key

        self.assertEqual(split_key("bbm"), ("A#", True))

    def test_reads_as_the_normal_form(self):
        from core.session_settings import describe_key_ja

        self.assertEqual(describe_key_ja("am"), "Aマイナー")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestPreviewVolume(unittest.TestCase):
    """**素材によって音量差が大きい。** 好みの音量を覚えておく。"""

    def test_default_is_a_little_quiet(self):
        """いきなり最大で鳴ると耳に痛い。"""
        self.assertLess(SessionSettings().preview_volume, 1.0)
        self.assertGreater(SessionSettings().preview_volume, 0.0)

    def test_clamps_out_of_range(self):
        self.assertEqual(sm_settings.parse_volume(5.0), 1.0)
        self.assertEqual(sm_settings.parse_volume(-1.0), 0.0)

    def test_bad_values_fall_back(self):
        self.assertEqual(sm_settings.parse_volume("おおきく"),
                         sm_settings.DEFAULT_VOLUME)
        self.assertEqual(sm_settings.parse_volume(None),
                         sm_settings.DEFAULT_VOLUME)
        self.assertEqual(sm_settings.parse_volume(float("nan")),
                         sm_settings.DEFAULT_VOLUME)

    def test_describes_as_a_percentage(self):
        """0.7 では伝わらない。割合で言う。"""
        self.assertIn("70", sm_settings.describe_volume_ja(0.7))
        self.assertIn("パーセント", sm_settings.describe_volume_ja(0.7))

    def test_zero_says_it_is_silent(self):
        self.assertIn("無音", sm_settings.describe_volume_ja(0.0))

    def test_survives_a_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            SessionSettings(preview_volume=0.3).save(path)
            self.assertAlmostEqual(
                SessionSettings.load(path).preview_volume, 0.3)

    def test_default_matches_the_constant(self):
        """既定値は DEFAULT_VOLUME と一体で動く（二重定義しない）。"""
        self.assertEqual(SessionSettings().preview_volume,
                         sm_settings.DEFAULT_VOLUME)

    def test_rounds_float_noise_away(self):
        """**0.1 刻みの足し引きの誤差を吸収する。**

        0.2 + 0.1 = 0.30000000000000004 のような値が入ってくる。
        丸めないと「端に着いた」の等値判定が崩れ、保存値も汚れる。
        """
        self.assertEqual(sm_settings.parse_volume(0.2 + 0.1), 0.3)
        self.assertEqual(sm_settings.parse_volume(0.1 - 0.1 + 0.1 - 0.1), 0.0)


class TestKeyChoice(unittest.TestCase):
    """**キーは文字で打たせない。**

    「Am」「C#maj」の書式を覚えるのも、打ち間違いに気づくのも、
    画面を見ずには難しい。上下矢印で選べる形に分解する。
    """

    def test_composes_major_and_minor(self):
        self.assertEqual(sm_settings.compose_key("A", False), "A")
        self.assertEqual(sm_settings.compose_key("A", True), "Am")

    def test_no_root_means_unset(self):
        self.assertEqual(sm_settings.compose_key("", True), "")
        self.assertEqual(
            sm_settings.compose_key(sm_settings.KEY_NONE, False), "")

    def test_splits_back(self):
        self.assertEqual(sm_settings.split_key("Am"), ("A", True))
        self.assertEqual(sm_settings.split_key("A"), ("A", False))
        self.assertEqual(sm_settings.split_key("C#m"), ("C#", True))

    def test_accepts_other_spellings(self):
        """`Amin` `Cmaj` のような書き方も読む（設定の引き継ぎ用）。"""
        self.assertEqual(sm_settings.split_key("Amin"), ("A", True))
        self.assertEqual(sm_settings.split_key("Cmaj"), ("C", False))

    def test_maj_is_not_mistaken_for_minor(self):
        """**`m` を先に見ると `maj` を短調と誤る。** 長い接尾辞から見る。"""
        self.assertEqual(sm_settings.split_key("Cmaj")[1], False)

    def test_unknown_root_falls_back_to_unset(self):
        """選択肢に無い値は選べないので、指定しないに倒す。"""
        self.assertEqual(sm_settings.split_key("Hm"), ("", True))
        self.assertEqual(sm_settings.split_key("なにか"), ("", False))

    def test_round_trips_every_choice(self):
        for root in sm_settings.KEY_ROOTS:
            for minor in (False, True):
                key = sm_settings.compose_key(root, minor)
                self.assertEqual(sm_settings.split_key(key), (root, minor))

    def test_reads_out_in_japanese(self):
        self.assertEqual(sm_settings.describe_key_ja("Am"), "Aマイナー")
        self.assertEqual(sm_settings.describe_key_ja("C#"), "C#メジャー")
        self.assertEqual(
            sm_settings.describe_key_ja(""), sm_settings.KEY_NONE)

    def test_flat_spellings_map_to_the_sharp_choice(self):
        """**フラット表記は同じ音の選択肢に写す。**

        旧バージョンの自由入力では "Db" のような書き方も保存できた。
        「指定しない」に倒すと、ダイアログを開いて OK しただけで
        キーの設定が黙って消える。
        """
        self.assertEqual(sm_settings.split_key("Db"), ("C#", False))
        self.assertEqual(sm_settings.split_key("Dbm"), ("C#", True))
        self.assertEqual(sm_settings.split_key("Bb"), ("A#", False))
        self.assertEqual(sm_settings.split_key("Ebmin"), ("D#", True))

    def test_unreadable_key_reads_as_itself(self):
        """読めない書式は「指定しない」ではなく、そのまま読む。"""
        self.assertEqual(sm_settings.describe_key_ja("H moll"), "H moll")
