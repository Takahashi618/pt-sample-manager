r"""core 層のユニットテスト。

外部依存（soundfile 等）に頼らず、標準 wave で
生成した実 WAV を使って metadata / scanner / library を検証する。

実行:
    PYTHONPATH=. python -m pytest tests/test_core.py -q
"""

from __future__ import annotations

import tempfile
import unicodedata
import unittest
import wave
from pathlib import Path

from core.library import Library
from core.metadata import extract_metadata
from core.models import (AudioMetadata, Sample, SampleKind,
                         Source, SourceKind)
from core.scanner import guess_kind, scan_folder


def _make_wav(path: Path, *, seconds: float = 0.5, rate: int = 44100,
              channels: int = 1, width: int = 2) -> None:
    """テスト用の実 WAV を生成する（無音）。

    フレームは sampwidth(width) に一致させる必要があるため、
    1フレーム = width * channels バイトのゼロ列として生成する。
    （16bit 固定で pack すると width!=2 のとき総バイト数が食い違い、
     getnframes が誤った値を返す。）
    """
    n_frames = int(seconds * rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        frame = b"\x00" * (width * channels)  # 無音1フレーム
        wf.writeframes(frame * n_frames)


class TestModels(unittest.TestCase):
    def test_derive_filename_posix_and_windows(self) -> None:
        self.assertEqual(
            Sample.derive_filename("/Volumes/Audio/kick.wav"), "kick.wav"
        )
        self.assertEqual(
            Sample.derive_filename(r"C:\samples\snare.wav"), "snare.wav"
        )

    def test_derive_format(self) -> None:
        self.assertEqual(Sample.derive_format("kick.WAV"), "wav")
        self.assertEqual(Sample.derive_format("loop.aiff"), "aiff")

    def test_metadata_describe_ja(self) -> None:
        meta = AudioMetadata(
            duration_sec=1.5, sample_rate=44100, bit_depth=16, channels=2
        )
        desc = meta.describe_ja()
        self.assertIn("1.5秒", desc)
        self.assertIn("ステレオ", desc)

    def test_metadata_describe_ja_unknown_duration(self) -> None:
        self.assertIn("長さ不明", AudioMetadata().describe_ja())


class TestMetadata(unittest.TestCase):
    def test_extract_wav(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "test.wav"
            _make_wav(p, seconds=1.0, rate=48000, channels=2, width=3)
            meta = extract_metadata(p)
            self.assertEqual(meta.sample_rate, 48000)
            self.assertEqual(meta.channels, 2)
            self.assertEqual(meta.bit_depth, 24)
            self.assertAlmostEqual(meta.duration_sec, 1.0, places=2)

    def test_extract_unreadable_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "broken.wav"
            p.write_bytes(b"not a real wav")
            meta = extract_metadata(p)
            self.assertIsNone(meta.sample_rate)


class TestScannerGuessKind(unittest.TestCase):
    def test_loop_hint(self) -> None:
        self.assertEqual(guess_kind("drum_loop_120.wav"), SampleKind.LOOP)
        self.assertEqual(guess_kind("amen_groove.wav"), SampleKind.LOOP)

    def test_oneshot_default(self) -> None:
        self.assertEqual(guess_kind("kick_01.wav"), SampleKind.ONESHOT)

    def test_japanese_loop_hint_in_decomposed_form(self) -> None:
        """Mac のアプリが作ったファイル名は濁点を分けた形（NFD）。

        「ループ」の「プ」が「フ」と半濁点の 2 文字になっていても、
        ループと推定すること（以前はワンショットになっていた）。
        """
        name = unicodedata.normalize("NFD", "ドラム_ループ.wav")
        self.assertEqual(guess_kind(name), SampleKind.LOOP)

    def test_halfwidth_katakana_loop_hint(self) -> None:
        self.assertEqual(guess_kind("ﾄﾞﾗﾑ_ﾙｰﾌﾟ.wav"), SampleKind.LOOP)


class TestSearchNormalization(unittest.TestCase):
    """検索はファイル名の書き方の違い（NFD・全角・大文字）に左右されない。"""

    def setUp(self) -> None:
        self.lib = Library(":memory:")

    def tearDown(self) -> None:
        self.lib.close()

    def _add(self, name: str) -> None:
        self.lib.add_sample(Sample(path=f"/a/{name}", filename=name,
                                   file_format="wav"))

    def test_decomposed_name_is_found_by_composed_query(self) -> None:
        """日本語入力で打った「ドラム」（NFC）で、NFD のファイル名が見つかる。"""
        self._add(unicodedata.normalize("NFD", "ドラム_ループ.wav"))
        results = self.lib.search(text="ドラム")
        self.assertEqual(len(results), 1)

    def test_composed_name_is_found_by_decomposed_query(self) -> None:
        self._add("ドラム_ループ.wav")
        query = unicodedata.normalize("NFD", "ループ")
        self.assertEqual(len(self.lib.search(text=query)), 1)

    def test_fullwidth_query_matches_ascii_name(self) -> None:
        """全角英数のまま打った「ｋｉｃｋ」でも kick が見つかる。"""
        self._add("Kick_01.wav")
        self.assertEqual(len(self.lib.search(text="ｋｉｃｋ")), 1)

    def test_non_ascii_letters_ignore_case(self) -> None:
        """SQLite の LOWER は英字しか小文字にしない。Ä も ä で見つかる。"""
        self._add("ÄRGER_Loop.wav")
        self.assertEqual(len(self.lib.search(text="ärger")), 1)

    def test_fullwidth_percent_is_still_literal(self) -> None:
        """全角の ％ は半角の % にそろう。そのあとで LIKE の記号として打ち消す。"""
        self._add("mix 100%.wav")
        self._add("mix 100x.wav")
        self.assertEqual(len(self.lib.search(text="100％")), 1)

    def test_pack_counts_use_the_same_folding(self) -> None:
        """パックの集計も検索と同じ比べ方をする（選んで 0 件にならない）。"""
        self.lib.add_sample(Sample(
            path="/s/packs/P/x.wav", pack="P", file_format="wav",
            filename=unicodedata.normalize("NFD", "ドラム.wav")))
        self.assertEqual(self.lib.list_packs_with_counts(text="ドラム"),
                         [("P", 1)])


class TestLibrary(unittest.TestCase):
    def setUp(self) -> None:
        self.lib = Library(":memory:")

    def tearDown(self) -> None:
        self.lib.close()

    def _sample(self, path: str, name: str, kind: SampleKind) -> Sample:
        return Sample(
            path=path,
            filename=name,
            file_format=Sample.derive_format(name),
            kind=kind,
        )

    def test_add_and_get(self) -> None:
        s = self.lib.add_sample(
            self._sample("/a/kick.wav", "kick.wav", SampleKind.ONESHOT)
        )
        self.assertIsNotNone(s.id)
        self.assertEqual(self.lib.count(), 1)
        fetched = self.lib.get_sample_by_path("/a/kick.wav")
        self.assertEqual(fetched.filename, "kick.wav")

    def test_add_duplicate_is_idempotent(self) -> None:
        self.lib.add_sample(
            self._sample("/a/kick.wav", "kick.wav", SampleKind.ONESHOT)
        )
        self.lib.add_sample(
            self._sample("/a/kick.wav", "kick.wav", SampleKind.ONESHOT)
        )
        self.assertEqual(self.lib.count(), 1)

    def test_search_by_text(self) -> None:
        self.lib.add_sample(
            self._sample("/a/kick.wav", "kick.wav", SampleKind.ONESHOT)
        )
        self.lib.add_sample(
            self._sample("/a/snare.wav", "snare.wav", SampleKind.ONESHOT)
        )
        results = self.lib.search(text="kick")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].filename, "kick.wav")

    def test_search_text_underscore_is_literal(self) -> None:
        """LIKE の _ は「任意の 1 文字」だが、文字どおりに探すこと。

        サンプル名は AB_EN_… のように _ だらけなので、エスケープしないと
        kick_1 の検索に kickx1 までヒットしてしまう。
        """
        self.lib.add_sample(
            self._sample("/a/kick_1.wav", "kick_1.wav", SampleKind.ONESHOT)
        )
        self.lib.add_sample(
            self._sample("/a/kickx1.wav", "kickx1.wav", SampleKind.ONESHOT)
        )
        results = self.lib.search(text="kick_1")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].filename, "kick_1.wav")

    def test_search_text_percent_is_literal(self) -> None:
        """LIKE の % も文字どおりに探すこと。"""
        self.lib.add_sample(
            self._sample("/a/mix 100%.wav", "mix 100%.wav",
                         SampleKind.ONESHOT)
        )
        self.lib.add_sample(
            self._sample("/a/mix 100x.wav", "mix 100x.wav",
                         SampleKind.ONESHOT)
        )
        self.assertEqual(len(self.lib.search(text="100%")), 1)

    def test_search_by_kind(self) -> None:
        self.lib.add_sample(
            self._sample("/a/kick.wav", "kick.wav", SampleKind.ONESHOT)
        )
        self.lib.add_sample(
            self._sample("/a/lp.wav", "lp.wav", SampleKind.LOOP)
        )
        results = self.lib.search(kind=SampleKind.LOOP)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].filename, "lp.wav")

    def test_tags_and_search_all_match(self) -> None:
        s = self.lib.add_sample(
            self._sample("/a/kick.wav", "kick.wav", SampleKind.ONESHOT)
        )
        self.lib.set_tags(s.id, ["drum", "808"])
        # 両方のタグを持つものだけがヒット（AND）
        self.assertEqual(len(self.lib.search(tags=["drum", "808"])), 1)
        # 片方しか一致しないタグ集合では…drum は一致するので1件
        self.assertEqual(len(self.lib.search(tags=["drum"])), 1)
        # 存在しないタグとの AND では 0 件
        self.assertEqual(len(self.lib.search(tags=["drum", "vocal"])), 0)

    def test_category_search(self) -> None:
        s = self.lib.add_sample(
            self._sample("/a/kick.wav", "kick.wav", SampleKind.ONESHOT)
        )
        self.lib.set_category(s.id, "Drums")
        results = self.lib.search(category="Drums")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].category.name, "Drums")

    def test_combined_and_conditions(self) -> None:
        s1 = self.lib.add_sample(
            self._sample("/a/kick_lp.wav", "kick_lp.wav", SampleKind.LOOP)
        )
        self.lib.set_tags(s1.id, ["drum"])
        self.lib.set_category(s1.id, "Drums")
        s2 = self.lib.add_sample(
            self._sample("/a/kick.wav", "kick.wav", SampleKind.ONESHOT)
        )
        self.lib.set_tags(s2.id, ["drum"])
        # text=kick かつ kind=LOOP かつ tag=drum → s1 のみ
        results = self.lib.search(
            text="kick", kind=SampleKind.LOOP, tags=["drum"]
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].filename, "kick_lp.wav")


class TestScanFolder(unittest.TestCase):
    def test_scan_registers_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _make_wav(root / "kick.wav")
            _make_wav(root / "drum_loop.wav")
            sub = root / "sub"
            sub.mkdir()
            _make_wav(sub / "snare.wav")
            # 非対応・隠しファイルは無視されること
            (root / "readme.txt").write_text("ignore me")
            (root / ".hidden.wav").write_bytes(b"x")

            lib = Library(":memory:")
            try:
                r1 = scan_folder(lib, root)
                self.assertEqual(r1.added, 3)
                self.assertEqual(lib.count(), 3)
                # 再スキャンは増殖しない
                r2 = scan_folder(lib, root)
                self.assertEqual(r2.added, 0)
                self.assertEqual(r2.skipped, 3)
                self.assertEqual(lib.count(), 3)
                # ループ推定が効いていること
                loops = lib.search(kind=SampleKind.LOOP)
                self.assertEqual(len(loops), 1)
                self.assertEqual(loops[0].filename, "drum_loop.wav")
            finally:
                lib.close()

    def test_scan_missing_folder(self) -> None:
        lib = Library(":memory:")
        try:
            r = scan_folder(lib, "Z:\\no\\such\\folder")
            self.assertEqual(r.added, 0)
            self.assertEqual(len(r.errors), 1)
        finally:
            lib.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestFavorites(unittest.TestCase):
    """**よく使う素材に印を付けて、それだけを出す。**

    数千件あると、前に使ったものを名前で探し直すのは手間になる。
    """

    def setUp(self) -> None:
        self.lib = Library(":memory:")
        self.a = self.lib.add_sample(Sample(
            path="/a/kick.wav", filename="kick.wav",
            file_format="wav", kind=SampleKind.ONESHOT))
        self.b = self.lib.add_sample(Sample(
            path="/a/snare.wav", filename="snare.wav",
            file_format="wav", kind=SampleKind.ONESHOT))

    def tearDown(self) -> None:
        self.lib.close()

    def test_defaults_to_not_a_favorite(self) -> None:
        self.assertFalse(self.lib.get_sample(self.a.id).favorite)

    def test_marks_and_unmarks(self) -> None:
        self.lib.set_favorite(self.a.id, True)
        self.assertTrue(self.lib.get_sample(self.a.id).favorite)
        self.lib.set_favorite(self.a.id, False)
        self.assertFalse(self.lib.get_sample(self.a.id).favorite)

    def test_filters_to_favorites_only(self) -> None:
        self.lib.set_favorite(self.a.id, True)
        found = self.lib.search(favorite_only=True)
        self.assertEqual([s.filename for s in found], ["kick.wav"])

    def test_without_the_filter_everything_shows(self) -> None:
        self.lib.set_favorite(self.a.id, True)
        self.assertEqual(len(self.lib.search()), 2)

    def test_combines_with_other_conditions(self) -> None:
        """ほかの絞り込みと重ねられること（AND）。"""
        self.lib.set_favorite(self.a.id, True)
        self.lib.set_favorite(self.b.id, True)
        found = self.lib.search(favorite_only=True, text="snare")
        self.assertEqual([s.filename for s in found], ["snare.wav"])

    def test_counts_them(self) -> None:
        self.assertEqual(self.lib.count_favorites(), 0)
        self.lib.set_favorite(self.a.id, True)
        self.lib.set_favorite(self.b.id, True)
        self.assertEqual(self.lib.count_favorites(), 2)

    def test_reads_out_the_mark(self) -> None:
        """**名前のすぐ後に置く。** 矢印で辿るとき真っ先に知りたい。"""
        self.lib.set_favorite(self.a.id, True)
        text = self.lib.get_sample(self.a.id).describe_ja()
        self.assertIn("お気に入り", text)
        self.assertLess(text.index("お気に入り"), text.index("ワンショット"))

    def test_silent_when_not_a_favorite(self) -> None:
        self.assertNotIn("お気に入り",
                         self.lib.get_sample(self.a.id).describe_ja())


class TestFavoriteMigration(unittest.TestCase):
    """**既に使っている DB を壊さない。** 列を後から足す。"""

    def test_adds_the_column_to_an_existing_db(self) -> None:
        import sqlite3
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            # favorite 列が無い状態の DB を作る
            con = sqlite3.connect(path)
            con.executescript("""
                CREATE TABLE samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL UNIQUE,
                    filename TEXT NOT NULL,
                    file_format TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    duration_sec REAL, sample_rate INTEGER,
                    bit_depth INTEGER, channels INTEGER,
                    category_id INTEGER, added_at TEXT NOT NULL);
                INSERT INTO samples(path, filename, file_format, kind,
                                    added_at)
                VALUES ('/a/old.wav', 'old.wav', 'wav', 'oneshot',
                        '2026-01-01T00:00:00+00:00');
            """)
            con.commit()
            con.close()

            lib = Library(path)
            try:
                # 既存の行は残り、お気に入りは付いていない
                got = lib.get_sample_by_path("/a/old.wav")
                self.assertIsNotNone(got)
                self.assertFalse(got.favorite)
                # 付け外しもできる
                lib.set_favorite(got.id, True)
                self.assertTrue(lib.get_sample(got.id).favorite)
            finally:
                lib.close()


class TestPackOrder(unittest.TestCase):
    """**中身の多い順に並べる。**

    Splice は 1 ファイル単位で落とすので 1 件だけのパックが大量にできる。
    名前順だとその中に本命が埋もれ、矢印で延々たどることになる。
    """

    def setUp(self) -> None:
        self.lib = Library(":memory:")
        source = self.lib.add_source(
            Source(name="s", root="/r", kind=SourceKind.SPLICE))
        plan = {"zebra": 3, "alpha": 1, "beta": 5, "gamma": 1}
        for pack, count in plan.items():
            for i in range(count):
                self.lib.add_sample(Sample(
                    path=f"/r/{pack}/{i}.wav", filename=f"{pack}_{i}.wav",
                    file_format="wav", kind=SampleKind.ONESHOT,
                    source_id=source.id, pack=pack))

    def tearDown(self) -> None:
        self.lib.close()

    def test_orders_by_count_desc(self) -> None:
        got = self.lib.list_packs_with_counts()
        self.assertEqual([n for n, _ in got],
                         ["beta", "zebra", "alpha", "gamma"])

    def test_ties_fall_back_to_name(self) -> None:
        """同数なら名前順で安定させる（並びが揺れると探しにくい）。"""
        got = dict(self.lib.list_packs_with_counts())
        self.assertEqual(got["alpha"], got["gamma"])
        names = [n for n, _ in self.lib.list_packs_with_counts()]
        self.assertLess(names.index("alpha"), names.index("gamma"))

    def test_list_packs_follows_the_same_order(self) -> None:
        self.assertEqual(self.lib.list_packs()[0], "beta")


class TestPackFilters(unittest.TestCase):
    """**パックの選択肢は、いまの検索条件に合うものだけを出す。**

    種別を「ループのみ」にしているのにワンショットしか無いパックが
    並ぶと、選んでも 0 件になって探せない（実機で 527 パック中
    341 件がそうなっていた）。
    """

    def setUp(self) -> None:
        self.lib = Library(":memory:")
        self.source = self.lib.add_source(
            Source(name="s", root="/r", kind=SourceKind.SPLICE))
        # loops: ループ 2・ワンショット 1 / shots: ワンショットだけ 3
        self._add("loops", "a.wav", SampleKind.LOOP, channels=2)
        self._add("loops", "b.wav", SampleKind.LOOP, channels=1)
        self._add("loops", "c.wav", SampleKind.ONESHOT, channels=2)
        for i in range(3):
            self._add("shots", f"s{i}.wav", SampleKind.ONESHOT, channels=2)

    def tearDown(self) -> None:
        self.lib.close()

    def _add(self, pack, name, kind, channels):
        return self.lib.add_sample(Sample(
            path=f"/r/{pack}/{name}", filename=name, file_format="wav",
            kind=kind, metadata=AudioMetadata(channels=channels),
            source_id=self.source.id, pack=pack))

    def test_without_filters_every_pack_shows(self) -> None:
        got = dict(self.lib.list_packs_with_counts())
        self.assertEqual(got, {"loops": 3, "shots": 3})

    def test_kind_filter_drops_packs_without_a_match(self) -> None:
        """**これが本題。** ループが無いパックは選択肢から消える。"""
        got = dict(self.lib.list_packs_with_counts(kind=SampleKind.LOOP))
        self.assertEqual(got, {"loops": 2})
        self.assertNotIn("shots", got)

    def test_counts_reflect_the_filter(self) -> None:
        """件数も条件に合うものだけ数える（3 ではなく 2）。"""
        got = dict(self.lib.list_packs_with_counts(kind=SampleKind.LOOP))
        self.assertEqual(got["loops"], 2)

    def test_channel_filter(self) -> None:
        got = dict(self.lib.list_packs_with_counts(channels=1))
        self.assertEqual(got, {"loops": 1})

    def test_text_filter(self) -> None:
        got = dict(self.lib.list_packs_with_counts(text="s0"))
        self.assertEqual(got, {"shots": 1})

    def test_favorite_filter(self) -> None:
        target = self.lib.get_sample_by_path("/r/loops/a.wav")
        self.lib.set_favorite(target.id, True)
        got = dict(self.lib.list_packs_with_counts(favorite_only=True))
        self.assertEqual(got, {"loops": 1})

    def test_tag_filter(self) -> None:
        """タグの JOIN があってもパックの集計が壊れないこと。"""
        target = self.lib.get_sample_by_path("/r/shots/s1.wav")
        self.lib.set_tags(target.id, ["drum", "808"])
        got = dict(self.lib.list_packs_with_counts(tags=["drum", "808"]))
        self.assertEqual(got, {"shots": 1})

    def test_pack_itself_is_ignored(self) -> None:
        """**自分自身で絞らない。** 絞ると 1 つしか出てこなくなる。"""
        got = dict(self.lib.list_packs_with_counts(pack="loops"))
        self.assertEqual(got, {"loops": 3, "shots": 3})

    def test_every_listed_pack_has_results(self) -> None:
        """**選択肢に出たものは、選べば必ず 1 件以上ある。**

        これが崩れると「選んでも 0 件」に戻る。
        """
        for kind in (SampleKind.LOOP, SampleKind.ONESHOT):
            for name, count in self.lib.list_packs_with_counts(kind=kind):
                found = self.lib.search(pack=name, kind=kind)
                self.assertEqual(len(found), count, f"{name} / {kind}")
                self.assertGreater(len(found), 0, name)

    def test_list_packs_takes_filters_too(self) -> None:
        self.assertEqual(self.lib.list_packs(kind=SampleKind.LOOP), ["loops"])
