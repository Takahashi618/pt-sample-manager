"""読み取り先フォルダ（ソース）とパックのユニットテスト。

Splice のライブラリと自前の素材フォルダを分けて登録し、
検索で切り替えられるようにするための仕組み。

**既存 DB を壊さないマイグレーション**の検証も含む。
346 件が入っている実 DB に列を足す必要があったため。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.library import Library
from core.models import (
    AudioMetadata,
    Sample,
    SampleKind,
    Source,
    SourceKind,
)
from core.scanner import extract_pack, looks_like_splice, scan_folder


def make_sample(name: str, source_id: int | None = None, pack: str = "",
                channels: int = 2,
                kind: SampleKind = SampleKind.LOOP) -> Sample:
    return Sample(
        path=f"/x/{name}",
        filename=name,
        file_format="wav",
        kind=kind,
        source_id=source_id,
        pack=pack,
        metadata=AudioMetadata(channels=channels),
    )


class TestExtractPack(unittest.TestCase):
    """Splice のパス構造からパック名を拾う。"""

    def test_typical_splice_path(self):
        got = extract_pack(
            "/S/sounds/packs/Noir Sounds/One_Shots/a.wav", "/S")
        self.assertEqual(got, "Noir Sounds")

    def test_packs_directly_under_root(self):
        self.assertEqual(
            extract_pack("/S/packs/Pack A/loops/b.wav", "/S"), "Pack A")

    def test_file_directly_in_packs_has_no_pack(self):
        """Splice には packs 直下に置かれたファイルが混ざっている。"""
        self.assertEqual(extract_pack("/S/sounds/packs/SAUCE.WAV", "/S"), "")

    def test_no_packs_directory(self):
        self.assertEqual(extract_pack("/other/deep/a.wav", "/other"), "")

    def test_pack_name_with_spaces_and_symbols(self):
        got = extract_pack(
            "/S/packs/AL HUG - Noir Sounds/x/a.wav", "/S")
        self.assertEqual(got, "AL HUG - Noir Sounds")


class TestLooksLikeSplice(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_packs_under_root(self):
        (self.root / "packs").mkdir()
        self.assertTrue(looks_like_splice(self.root))

    def test_sounds_packs(self):
        (self.root / "sounds" / "packs").mkdir(parents=True)
        self.assertTrue(looks_like_splice(self.root))

    def test_plain_folder(self):
        (self.root / "drums").mkdir()
        self.assertFalse(looks_like_splice(self.root))


class TestSourceCrud(unittest.TestCase):
    def setUp(self):
        self.lib = Library(":memory:")

    def tearDown(self):
        self.lib.close()

    def test_add_and_list(self):
        self.lib.add_source(Source(name="Splice", root="/S",
                                   kind=SourceKind.SPLICE))
        self.lib.add_source(Source(name="自作", root="/G"))
        names = [s.name for s in self.lib.list_sources()]
        self.assertEqual(names, ["Splice", "自作"])

    def test_listed_in_registration_order(self):
        """コマンドと数字の番号は登録した順。名前の順にすると、あとから
        「A…」を足しただけで覚えた番号がずれる（以前は名前の順だった）。
        """
        self.lib.add_source(Source(name="Splice", root="/S",
                                   kind=SourceKind.SPLICE))
        self.lib.add_source(Source(name="Ambient", root="/A"))
        self.lib.add_source(Source(name="自作", root="/G"))
        names = [s.name for s in self.lib.list_sources()]
        self.assertEqual(names, ["Splice", "Ambient", "自作"])

    def test_same_root_is_not_duplicated(self):
        a = self.lib.add_source(Source(name="A", root="/S"))
        b = self.lib.add_source(Source(name="別名", root="/S"))
        self.assertEqual(a.id, b.id)
        self.assertEqual(len(self.lib.list_sources()), 1)

    def test_same_name_is_not_duplicated(self):
        a = self.lib.add_source(Source(name="A", root="/S"))
        b = self.lib.add_source(Source(name="A", root="/T"))
        self.assertEqual(a.id, b.id)

    def test_kind_survives_a_round_trip(self):
        self.lib.add_source(Source(name="S", root="/S",
                                   kind=SourceKind.SPLICE))
        self.assertIs(self.lib.list_sources()[0].kind, SourceKind.SPLICE)

    def test_remove_deletes_its_samples(self):
        src = self.lib.add_source(Source(name="S", root="/S"))
        self.lib.add_sample(make_sample("a.wav", src.id))
        self.lib.add_sample(make_sample("b.wav", src.id))
        removed = self.lib.remove_source(src.id)
        self.assertEqual(removed, 2)
        self.assertEqual(self.lib.search(), [])

    def test_remove_can_keep_samples(self):
        """フォルダを移しただけのときは中身を残せる。"""
        src = self.lib.add_source(Source(name="S", root="/S"))
        self.lib.add_sample(make_sample("a.wav", src.id))
        self.lib.remove_source(src.id, delete_samples=False)
        remaining = self.lib.search()
        self.assertEqual(len(remaining), 1)
        self.assertIsNone(remaining[0].source_id)

    def test_removing_one_source_leaves_the_other(self):
        a = self.lib.add_source(Source(name="A", root="/A"))
        b = self.lib.add_source(Source(name="B", root="/B"))
        self.lib.add_sample(make_sample("a.wav", a.id))
        self.lib.add_sample(make_sample("b.wav", b.id))
        self.lib.remove_source(a.id)
        self.assertEqual([s.filename for s in self.lib.search()], ["b.wav"])


class TestPackListing(unittest.TestCase):
    def setUp(self):
        self.lib = Library(":memory:")
        self.splice = self.lib.add_source(
            Source(name="S", root="/S", kind=SourceKind.SPLICE))
        self.plain = self.lib.add_source(Source(name="G", root="/G"))
        self.lib.add_sample(make_sample("a.wav", self.splice.id, "Noir"))
        self.lib.add_sample(make_sample("b.wav", self.splice.id, "Noir"))
        self.lib.add_sample(make_sample("c.wav", self.splice.id, "Tape"))
        self.lib.add_sample(make_sample("d.wav", self.plain.id))

    def tearDown(self):
        self.lib.close()

    def test_lists_unique_packs(self):
        self.assertEqual(self.lib.list_packs(), ["Noir", "Tape"])

    def test_scoped_to_a_source(self):
        self.assertEqual(self.lib.list_packs(self.plain.id), [])

    def test_samples_without_a_pack_are_excluded(self):
        self.assertNotIn("", self.lib.list_packs())


class TestSearchFilters(unittest.TestCase):
    def setUp(self):
        self.lib = Library(":memory:")
        self.splice = self.lib.add_source(
            Source(name="S", root="/S", kind=SourceKind.SPLICE))
        self.plain = self.lib.add_source(Source(name="G", root="/G"))
        self.lib.add_sample(make_sample(
            "loop_st.wav", self.splice.id, "Noir", 2, SampleKind.LOOP))
        self.lib.add_sample(make_sample(
            "shot_mono.wav", self.splice.id, "Noir", 1, SampleKind.ONESHOT))
        self.lib.add_sample(make_sample(
            "loop_tape.wav", self.splice.id, "Tape", 2, SampleKind.LOOP))
        self.lib.add_sample(make_sample(
            "own.wav", self.plain.id, "", 2, SampleKind.ONESHOT))

    def tearDown(self):
        self.lib.close()

    def names(self, **kwargs):
        return sorted(s.filename for s in self.lib.search(**kwargs))

    def test_by_channels_stereo(self):
        self.assertEqual(self.names(channels=2),
                         ["loop_st.wav", "loop_tape.wav", "own.wav"])

    def test_by_channels_mono(self):
        self.assertEqual(self.names(channels=1), ["shot_mono.wav"])

    def test_by_kind_loop(self):
        self.assertEqual(self.names(kind=SampleKind.LOOP),
                         ["loop_st.wav", "loop_tape.wav"])

    def test_by_kind_oneshot(self):
        self.assertEqual(self.names(kind=SampleKind.ONESHOT),
                         ["own.wav", "shot_mono.wav"])

    def test_by_source(self):
        self.assertEqual(self.names(source_id=self.plain.id), ["own.wav"])

    def test_by_pack(self):
        self.assertEqual(self.names(pack="Noir"),
                         ["loop_st.wav", "shot_mono.wav"])

    def test_filters_combine_with_and(self):
        self.assertEqual(
            self.names(pack="Noir", channels=2, kind=SampleKind.LOOP),
            ["loop_st.wav"])

    def test_no_filter_returns_everything(self):
        self.assertEqual(len(self.names()), 4)


class TestScanWithSource(unittest.TestCase):
    """スキャン時に読み取り先とパックを紐づける。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        pack_dir = self.root / "packs" / "My Pack" / "loops"
        pack_dir.mkdir(parents=True)
        (pack_dir / "a.wav").write_bytes(b"dummy")
        (self.root / "packs" / "loose.wav").write_bytes(b"dummy")
        self.lib = Library(":memory:")

    def tearDown(self):
        self.lib.close()
        self.dir.cleanup()

    def test_splice_source_records_packs(self):
        src = self.lib.add_source(
            Source(name="S", root=str(self.root), kind=SourceKind.SPLICE))
        scan_folder(self.lib, self.root, src)
        packs = {s.filename: s.pack for s in self.lib.search()}
        self.assertEqual(packs["a.wav"], "My Pack")
        self.assertEqual(packs["loose.wav"], "")

    def test_generic_source_records_no_pack(self):
        """通常のフォルダではパックを付けない（構造が違うため）。"""
        src = self.lib.add_source(
            Source(name="G", root=str(self.root), kind=SourceKind.GENERIC))
        scan_folder(self.lib, self.root, src)
        self.assertEqual({s.pack for s in self.lib.search()}, {""})

    def test_samples_are_linked_to_the_source(self):
        src = self.lib.add_source(
            Source(name="S", root=str(self.root), kind=SourceKind.SPLICE))
        scan_folder(self.lib, self.root, src)
        self.assertTrue(all(s.source_id == src.id
                            for s in self.lib.search()))

    def test_scanning_without_a_source_still_works(self):
        """読み取り先を指定しないスキャン（従来の動き）。"""
        scan_folder(self.lib, self.root)
        samples = self.lib.search()
        self.assertEqual(len(samples), 2)
        self.assertTrue(all(s.source_id is None for s in samples))


class TestMigration(unittest.TestCase):
    """**すでに使われている DB を壊さない。**

    実際に 346 件入った DB に列を足す必要があったので、
    旧スキーマから開いても中身が残ることを確かめる。
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "old.db"
        # source_id も pack も無い、旧バージョンの DB を作る
        conn = sqlite3.connect(self.path)
        conn.executescript("""
            CREATE TABLE categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE);
            CREATE TABLE tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE);
            CREATE TABLE samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL,
                file_format TEXT NOT NULL,
                kind TEXT NOT NULL,
                duration_sec REAL, sample_rate INTEGER,
                bit_depth INTEGER, channels INTEGER,
                category_id INTEGER REFERENCES categories(id),
                added_at TEXT NOT NULL);
            CREATE TABLE sample_tags (
                sample_id INTEGER NOT NULL REFERENCES samples(id),
                tag_id INTEGER NOT NULL REFERENCES tags(id),
                PRIMARY KEY (sample_id, tag_id));
        """)
        conn.execute(
            "INSERT INTO samples(path, filename, file_format, kind, "
            "channels, added_at) VALUES "
            "('/old/a.wav', 'a.wav', 'wav', 'loop', 2, "
            "'2026-01-01T00:00:00+00:00')")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.dir.cleanup()

    def test_existing_rows_survive(self):
        lib = Library(self.path)
        try:
            samples = lib.search()
            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[0].filename, "a.wav")
        finally:
            lib.close()

    def test_new_columns_default_to_empty(self):
        lib = Library(self.path)
        try:
            sample = lib.search()[0]
            self.assertIsNone(sample.source_id)
            self.assertEqual(sample.pack, "")
        finally:
            lib.close()

    def test_new_features_work_on_migrated_db(self):
        lib = Library(self.path)
        try:
            src = lib.add_source(Source(name="S", root="/S"))
            lib.add_sample(make_sample("new.wav", src.id, "Pack"))
            self.assertEqual(lib.list_packs(), ["Pack"])
            self.assertEqual(len(lib.search(channels=2)), 2)
        finally:
            lib.close()

    def test_opening_twice_is_safe(self):
        """マイグレーションを二度走らせても壊れないこと。"""
        for _ in range(2):
            lib = Library(self.path)
            lib.close()
        lib = Library(self.path)
        try:
            self.assertEqual(len(lib.search()), 1)
        finally:
            lib.close()


class TestScanResultWording(unittest.TestCase):
    """**失敗があれば先頭で分かる。** 通知音は文言の先頭で決まる。"""

    def test_errors_put_a_warning_first(self):
        from core.scanner import ScanResult

        result = ScanResult(errors=[("/x", "フォルダが存在しません")])
        text = result.describe_ja()
        self.assertTrue(text.startswith("[警告]"), text)
        self.assertIn("フォルダが存在しません", text)

    def test_success_has_no_prefix(self):
        from core.scanner import ScanResult

        self.assertTrue(ScanResult(added=3).describe_ja().startswith("3件"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
