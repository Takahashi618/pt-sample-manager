r"""ui.controller（非GUI）のユニットテスト。

wx を起動せず、AppController のロジックを検証する。
試聴は NullBackend 注入の PreviewPlayer を使う。

実行:
    PYTHONPATH=. python -m pytest tests/test_controller.py -q
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from audio.player import NullBackend, PreviewPlayer
from core.library import Library
from core.models import Sample, SampleKind, Source, SourceKind
from core.session_settings import SessionSettings
from ui.controller import AppController, SearchForm


class TestSearchForm(unittest.TestCase):
    def test_empty_form_is_all_none(self) -> None:
        kw = SearchForm().to_kwargs()
        self.assertIsNone(kw["text"])
        self.assertIsNone(kw["kind"])
        self.assertIsNone(kw["category"])
        self.assertIsNone(kw["tags"])

    def test_tags_csv_parsing(self) -> None:
        kw = SearchForm(tags_csv=" drum , 808 ,, ").to_kwargs()
        self.assertEqual(kw["tags"], ["drum", "808"])

    def test_text_trim(self) -> None:
        kw = SearchForm(text="  kick ").to_kwargs()
        self.assertEqual(kw["text"], "kick")


class TestAppController(unittest.TestCase):
    def setUp(self) -> None:
        self.lib = Library(":memory:")
        self._seed()
        self.player = PreviewPlayer(NullBackend())
        self.ctrl = AppController(self.lib, self.player)

    def tearDown(self) -> None:
        self.lib.close()

    def _seed(self) -> None:
        def add(path: str, name: str, kind: SampleKind) -> Sample:
            return self.lib.add_sample(
                Sample(
                    path=path,
                    filename=name,
                    file_format=Sample.derive_format(name),
                    kind=kind,
                )
            )

        s1 = add("/a/kick.wav", "kick.wav", SampleKind.ONESHOT)
        self.lib.set_tags(s1.id, ["drum"])
        self.lib.set_category(s1.id, "Drums")
        add("/a/drum_loop.wav", "drum_loop.wav", SampleKind.LOOP)

    def test_search_returns_rows_and_summary(self) -> None:
        rows = self.ctrl.search(SearchForm())
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.ctrl.result_count, 2)
        self.assertIn("2件", self.ctrl.search_result_summary_ja())

    def test_search_no_match_summary(self) -> None:
        self.ctrl.search(SearchForm(text="zzz"))
        self.assertIn("ありませんでした", self.ctrl.search_result_summary_ja())

    def test_search_by_kind(self) -> None:
        rows = self.ctrl.search(SearchForm(kind=SampleKind.LOOP))
        self.assertEqual(len(rows), 1)
        self.assertIn("drum_loop.wav", rows[0])

    def test_search_combined(self) -> None:
        rows = self.ctrl.search(
            SearchForm(text="kick", category="Drums", tags_csv="drum")
        )
        self.assertEqual(len(rows), 1)

    def test_get_sample_out_of_range(self) -> None:
        self.ctrl.search(SearchForm())
        self.assertIsNone(self.ctrl.get_sample(99))

    def test_preview_toggle_loads_and_plays(self) -> None:
        self.ctrl.search(SearchForm(text="kick"))
        desc = self.ctrl.preview_toggle(0)
        self.assertIn("再生中", desc)
        # 同じファイルを再度トグル → 停止
        desc2 = self.ctrl.preview_toggle(0)
        self.assertIn("停止中", desc2)

    def test_preview_without_selection(self) -> None:
        self.ctrl.search(SearchForm())
        self.assertIn("選択されていません", self.ctrl.preview_toggle(99))

    def test_path_for_copy(self) -> None:
        self.ctrl.search(SearchForm(text="kick"))
        self.assertEqual(self.ctrl.get_path_for_copy(0), "/a/kick.wav")

    def test_set_tags_and_research(self) -> None:
        self.ctrl.search(SearchForm(text="drum_loop"))
        ok = self.ctrl.set_tags(0, "groove, percussion")
        self.assertTrue(ok)
        # 付け直したタグで検索できる
        rows = self.ctrl.search(SearchForm(tags_csv="groove"))
        self.assertEqual(len(rows), 1)

    def test_set_kind(self) -> None:
        self.ctrl.search(SearchForm(text="kick"))
        ok = self.ctrl.set_kind(0, SampleKind.LOOP)
        self.assertTrue(ok)
        rows = self.ctrl.search(SearchForm(kind=SampleKind.LOOP))
        # kick.wav が LOOP になり drum_loop.wav と合わせて2件
        self.assertEqual(len(rows), 2)

    def test_preview_disabled_when_no_player(self) -> None:
        ctrl = AppController(self.lib, None)
        ctrl.search(SearchForm(text="kick"))
        self.assertIn("試聴できません", ctrl.preview_toggle(0))

    def test_place_in_pro_tools_no_selection(self) -> None:
        # サンプル未選択（範囲外 index）なら PTSL 接続前に文言を返す。
        # この分岐は OS 非依存で、実機接続を伴わない。
        self.ctrl.search(SearchForm())
        msg = self.ctrl.place_in_pro_tools(99)
        self.assertIn("選択されていません", msg)

    def test_place_missing_file_returns_message(self) -> None:
        # 実在しないパスのサンプルは PTSL 接続前に中断し文言を返す。
        # この分岐も OS 非依存で、実機接続を伴わない。
        self.ctrl.search(SearchForm(text="kick"))
        msg = self.ctrl.place_in_pro_tools(0)
        self.assertIn("ファイルが見つかりません", msg)

    def test_preview_backend_error_returns_message(self) -> None:
        # バックエンドの読み込み失敗（ファイル欠損等）は例外でなく文言で返す
        class BoomBackend(NullBackend):
            def load_file(self, path: str) -> None:
                raise RuntimeError("decode error")

        ctrl = AppController(self.lib, PreviewPlayer(BoomBackend()))
        ctrl.search(SearchForm(text="kick"))
        msg = ctrl.preview_toggle(0)
        self.assertIn("再生できません", msg)

    def test_apply_edits_updates_row_and_results(self) -> None:
        self.ctrl.search(SearchForm(text="kick"))
        row = self.ctrl.apply_edits(0, "808, punchy", "Kicks",
                                    SampleKind.LOOP)
        self.assertIsNotNone(row)
        self.assertIn("808", row)
        self.assertIn("ループ", row)
        # 内部の結果リストも更新後の Sample に置き換わっている
        sample = self.ctrl.get_sample(0)
        self.assertEqual({t.name for t in sample.tags}, {"808", "punchy"})
        self.assertEqual(sample.category.name, "Kicks")
        self.assertIs(sample.kind, SampleKind.LOOP)
        # 空文字カテゴリはカテゴリなしに戻せる
        row2 = self.ctrl.apply_edits(0, "", "", SampleKind.ONESHOT)
        self.assertIsNotNone(row2)
        sample2 = self.ctrl.get_sample(0)
        self.assertEqual(sample2.tags, [])
        self.assertIsNone(sample2.category)

    def test_apply_edits_out_of_range(self) -> None:
        self.ctrl.search(SearchForm())
        result = self.ctrl.apply_edits(99, "x", "y", SampleKind.ONESHOT)
        self.assertIsNone(result)

    def test_scan_isolated_memory_uses_same_connection(self) -> None:
        # インメモリ DB では既存接続をそのまま使い、検索で見えること
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "one_shot_x.wav").write_bytes(b"dummy")
            result = self.ctrl.scan_isolated(td)
            self.assertEqual(result.added, 1)
            rows = self.ctrl.search(SearchForm(text="one_shot_x"))
            self.assertEqual(len(rows), 1)

    def test_scan_isolated_file_db_visible_to_main_connection(self) -> None:
        # ファイル DB では別接続でスキャンし、主接続の検索から見えること
        # （ワーカースレッドから安全に呼べることの根拠）
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "lib.db"
            folder = Path(td) / "samples"
            folder.mkdir()
            (folder / "kick2.wav").write_bytes(b"dummy")
            lib = Library(db_path)
            try:
                ctrl = AppController(lib, None)
                result = ctrl.scan_isolated(str(folder))
                self.assertEqual(result.added, 1)
                rows = ctrl.search(SearchForm(text="kick2"))
                self.assertEqual(len(rows), 1)
            finally:
                lib.close()


class TestSessionSettingsOnController(unittest.TestCase):
    """セッション条件の保持と、配置前の加工の分岐。"""

    def setUp(self) -> None:
        self.lib = Library(":memory:")
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "settings.json"
        self.ctrl = AppController(self.lib, None, settings_path=self.path)

    def tearDown(self) -> None:
        self.lib.close()
        self.dir.cleanup()

    def sample(self, filename: str = "loop.wav") -> Sample:
        return Sample(
            path=str(Path(self.dir.name) / filename),
            filename=filename,
            file_format="wav",
            kind=SampleKind.LOOP,
        )

    def test_default_settings_do_not_render(self) -> None:
        self.assertFalse(self.ctrl.session_settings.can_render)

    def test_update_persists(self) -> None:
        self.ctrl.update_session_settings(SessionSettings(bpm=128, key="Am"))
        self.assertTrue(self.path.exists())
        self.assertEqual(SessionSettings.load(self.path).bpm, 128)

    def test_update_returns_readable_text(self) -> None:
        text = self.ctrl.update_session_settings(SessionSettings(bpm=128))
        self.assertIn("128", text)

    def test_render_is_skipped_without_settings(self) -> None:
        """条件が無ければ元のパスをそのまま使う。"""
        path, note, error = self.ctrl._render_for_session(self.sample())
        self.assertEqual(path, self.sample().path)
        self.assertEqual(note, "")
        self.assertEqual(error, "")

    def test_unknown_tempo_needs_no_render(self) -> None:
        """素材の BPM が分からなければ合わせようがない。素通しでよい。"""
        self.ctrl.update_session_settings(SessionSettings(bpm=128))
        path, _, error = self.ctrl._render_for_session(self.sample())
        self.assertEqual(path, self.sample().path)
        self.assertEqual(error, "")

    def test_render_failure_blocks_placement(self) -> None:
        """加工に失敗したら配置しない。

        意図しない音程・尺で置いてしまうより、何もしないほうがよい。
        84 BPM と読める名前にして、存在しないファイルで失敗させる。
        """
        self.ctrl.update_session_settings(SessionSettings(bpm=128))
        target = self.sample("ABC_84_loop_test.wav")
        path, _, error = self.ctrl._render_for_session(target)
        self.assertEqual(path, "")
        self.assertTrue(error)
        self.assertIn("配置しませんでした", error)


class TestSourceRegistration(unittest.TestCase):
    """**読み取り先の追加で嘘を言わない。**

    Library は名前が重なると既存の読み取り先を返す（名前は一意）。
    そのまま「追加しました」と読むと、増えていないのに増えたことになる。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lib = Library(":memory:")
        self.ctrl = AppController(self.lib, PreviewPlayer(NullBackend()))

    def tearDown(self) -> None:
        self.lib.close()
        self.tmp.cleanup()

    def test_new_folder_has_no_conflict(self) -> None:
        self.assertEqual(
            self.ctrl.source_conflict("", str(self.root / "a")), ("", ""))

    def test_same_folder_is_reported_as_registered(self) -> None:
        self.ctrl.register_source("A", str(self.root / "a"))
        reason, message = self.ctrl.source_conflict("", str(self.root / "a"))
        self.assertEqual(reason, "root")
        self.assertIn("登録済み", message)

    def test_trailing_slash_is_still_the_same_folder(self) -> None:
        """書き方の違いで二重登録しない。"""
        self.ctrl.register_source("A", str(self.root / "a"))
        reason, _ = self.ctrl.source_conflict("", str(self.root / "a") + "/")
        self.assertEqual(reason, "root")

    def test_same_name_for_another_folder_asks_for_a_new_name(self) -> None:
        """フォルダ名を名前にするので、別の場所の同名フォルダで起きる。"""
        self.ctrl.register_source("", str(self.root / "x" / "Samples"))
        reason, message = self.ctrl.source_conflict(
            "", str(self.root / "y" / "Samples"))
        self.assertEqual(reason, "name")
        self.assertIn("Samples", message)
        self.assertIn("別の名前", message)

    def test_register_normalizes_the_root(self) -> None:
        src = self.ctrl.register_source("", str(self.root / "a") + "/")
        self.assertEqual(src.root, str(self.root / "a"))
        self.assertEqual(src.name, "a")

    def test_register_expands_the_home_folder(self) -> None:
        src = self.ctrl.register_source("home", "~/なにか")
        self.assertFalse(src.root.startswith("~"))

    def test_scan_warns_when_a_folder_is_missing(self) -> None:
        """**通知音は文言の先頭で決まる。** 失敗があれば [警告] を付ける。"""
        gone = self.ctrl.register_source("gone", str(self.root / "ない"))
        message = self.ctrl.scan_sources([gone])
        self.assertTrue(message.startswith("[警告]"), message)
        self.assertIn("フォルダが存在しません", message)

    def test_scan_without_trouble_has_no_warning(self) -> None:
        folder = self.root / "ok"
        folder.mkdir()
        src = self.ctrl.register_source("ok", str(folder))
        message = self.ctrl.scan_sources([src])
        self.assertFalse(message.startswith("[警告]"), message)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTargetTrack(unittest.TestCase):
    """**配置先をアプリ側で指定する。**

    画面を見ずに Pro Tools でトラックを選ぶのは難しく、選び損ねると
    「オーディオトラックが選ばれていません」で配置が失敗する。
    """

    def setUp(self) -> None:
        self.lib = Library(":memory:")
        self.ctrl = AppController(self.lib, PreviewPlayer(NullBackend()))

    def tearDown(self) -> None:
        self.lib.close()

    def test_defaults_to_following_pro_tools(self) -> None:
        self.assertEqual(self.ctrl.target_track, "")
        self.assertIn("Pro Tools", self.ctrl.describe_target_track())

    def test_setting_a_track_is_reported(self) -> None:
        message = self.ctrl.set_target_track("Audio 3")
        self.assertEqual(self.ctrl.target_track, "Audio 3")
        self.assertIn("Audio 3", message)
        self.assertIn("Audio 3", self.ctrl.describe_target_track())

    def test_clearing_goes_back_to_the_selection(self) -> None:
        self.ctrl.set_target_track("Audio 3")
        message = self.ctrl.set_target_track("")
        self.assertEqual(self.ctrl.target_track, "")
        self.assertIn("Pro Tools", message)

    def test_whitespace_is_trimmed(self) -> None:
        self.ctrl.set_target_track("  Audio 2  ")
        self.assertEqual(self.ctrl.target_track, "Audio 2")


class TestScanSources(unittest.TestCase):
    """**登録済みをまとめて走査する。**

    再スキャンのたびにフォルダを選び直させないため。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lib = Library(str(self.root / "lib.db"))
        self.ctrl = AppController(self.lib, PreviewPlayer(NullBackend()))

    def tearDown(self) -> None:
        self.lib.close()
        self.tmp.cleanup()

    def _folder(self, name: str, files: int) -> object:
        folder = self.root / name
        folder.mkdir()
        for i in range(files):
            (folder / f"{name}_{i}.wav").write_bytes(b"RIFF____WAVEfmt ")
        return self.ctrl.register_source(name, str(folder))

    def test_scans_every_source(self) -> None:
        a = self._folder("alpha", 2)
        b = self._folder("bravo", 3)
        message = self.ctrl.scan_sources([a, b])
        self.assertIn("5件を登録", message)
        self.assertIn("2 件の読み取り先", message)
        self.assertEqual(self.lib.count(), 5)

    def test_second_run_skips_what_is_registered(self) -> None:
        a = self._folder("alpha", 2)
        self.ctrl.scan_sources([a])
        message = self.ctrl.scan_sources([a])
        self.assertIn("0件を登録", message)
        self.assertIn("2件は登録済み", message)

    def test_a_missing_folder_does_not_stop_the_rest(self) -> None:
        """**外付けが外れていても、残りは走査する。**"""
        a = self._folder("alpha", 2)
        gone = self.ctrl.register_source("gone", str(self.root / "ない"))
        message = self.ctrl.scan_sources([gone, a])
        self.assertIn("2件を登録", message)
        self.assertEqual(self.lib.count(), 2)

    def test_empty_list_is_harmless(self) -> None:
        self.assertIn("0件を登録", self.ctrl.scan_sources([]))


class TestTempoMismatch(unittest.TestCase):
    """**設定のテンポが実際と違うと、配置が実用にならない。**

    実機で 160 BPM のセッションに 130 BPM と設定されていた。素材が
    160/130 = 1.23 倍に引き伸ばされ、メトロノームと合わなくなっていた。
    """

    def setUp(self) -> None:
        self.lib = Library(":memory:")

    def tearDown(self) -> None:
        self.lib.close()

    def _ctrl(self, bpm):
        return AppController(
            self.lib, PreviewPlayer(NullBackend()),
            SessionSettings(bpm=bpm, auto_render=True))

    def test_warns_when_they_differ(self) -> None:
        warning = self._ctrl(130).tempo_mismatch_warning(160.0)
        self.assertTrue(warning.startswith("[警告]"))
        self.assertIn("130", warning)
        self.assertIn("160", warning)

    def test_names_the_real_shortcut(self) -> None:
        """案内するキーは実際の割り当て（コマンドとS）。

        以前は「Sキー」と読んでいた。いまの S は一覧の頭文字への移動になる。
        """
        warning = self._ctrl(130).tempo_mismatch_warning(160.0)
        self.assertNotIn("Sキー", warning)
        self.assertIn("コマンドとS", warning)

    def test_silent_when_they_match(self) -> None:
        self.assertEqual(self._ctrl(160).tempo_mismatch_warning(160.0), "")

    def test_tolerates_rounding(self) -> None:
        """逆算なので端数が出る。0.05 未満は同じとみなす。"""
        self.assertEqual(self._ctrl(160).tempo_mismatch_warning(159.99), "")

    def test_silent_when_tempo_is_unknown(self) -> None:
        self.assertEqual(self._ctrl(130).tempo_mismatch_warning(None), "")

    def test_silent_when_not_configured(self) -> None:
        ctrl = AppController(self.lib, PreviewPlayer(NullBackend()),
                             SessionSettings())
        self.assertEqual(ctrl.tempo_mismatch_warning(160.0), "")


class TestClearSessionTempo(unittest.TestCase):
    """**終了時にテンポとキーを空へ戻す。**

    別のセッションを開いたまま前回のテンポで配置すると、素材が違う倍率で
    伸縮される（実機で 160 BPM のセッションに 130 BPM が残っていた）。
    気づかないまま置いてしまうのが一番困る。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "settings.json"
        self.lib = Library(":memory:")

    def tearDown(self) -> None:
        self.lib.close()
        self.tmp.cleanup()

    def _ctrl(self, settings):
        return AppController(self.lib, PreviewPlayer(NullBackend()),
                             settings, self.path)

    def test_clears_tempo_and_key(self) -> None:
        ctrl = self._ctrl(SessionSettings(bpm=130, key="Am"))
        ctrl.clear_session_tempo()
        self.assertIsNone(ctrl.session_settings.bpm)
        self.assertEqual(ctrl.session_settings.key, "")

    def test_keeps_the_placement_preferences(self) -> None:
        """配置のしかたは好みなので残す。"""
        ctrl = self._ctrl(SessionSettings(
            bpm=130, at_cursor=False, position="17|1|000",
            copies=4, every_bars=2, snap_to_beat=False))
        ctrl.clear_session_tempo()
        got = ctrl.session_settings
        self.assertFalse(got.at_cursor)
        self.assertEqual(got.position, "17|1|000")
        self.assertEqual(got.copies, 4)
        self.assertFalse(got.snap_to_beat)

    def test_the_cleared_state_survives_a_restart(self) -> None:
        """**次に起動したときも空であること。** ここが要。"""
        self._ctrl(SessionSettings(bpm=130, key="Am")).clear_session_tempo()
        reloaded = SessionSettings.load(self.path)
        self.assertIsNone(reloaded.bpm)
        self.assertEqual(reloaded.key, "")

    def test_no_longer_renders_after_clearing(self) -> None:
        """テンポもキーも無ければ加工しない（そのまま置く）。"""
        ctrl = self._ctrl(SessionSettings(bpm=130, key="Am"))
        ctrl.clear_session_tempo()
        self.assertFalse(ctrl.session_settings.can_render)

    def test_works_without_a_settings_file(self) -> None:
        ctrl = AppController(self.lib, PreviewPlayer(NullBackend()),
                             SessionSettings(bpm=130), None)
        ctrl.clear_session_tempo()
        self.assertIsNone(ctrl.session_settings.bpm)


class TestToggleFavorite(unittest.TestCase):
    """**よく使う素材に印を付けて、それだけを出す。**

    数千件あると、前に使ったものを名前で探し直すのは手間になる。
    """

    def setUp(self) -> None:
        self.lib = Library(":memory:")
        for name in ("kick.wav", "snare.wav"):
            self.lib.add_sample(Sample(
                path=f"/a/{name}", filename=name,
                file_format="wav", kind=SampleKind.ONESHOT))
        self.ctrl = AppController(self.lib, PreviewPlayer(NullBackend()))
        self.ctrl.search(SearchForm())

    def tearDown(self) -> None:
        self.lib.close()

    def test_marks_and_reports(self) -> None:
        message, row = self.ctrl.toggle_favorite(0)
        self.assertIn("お気に入りに追加", message)
        self.assertIn("1 件", message)
        self.assertIn("お気に入り", row)

    def test_pressing_again_removes_it(self) -> None:
        self.ctrl.toggle_favorite(0)
        message, row = self.ctrl.toggle_favorite(0)
        self.assertIn("お気に入りから外し", message)
        self.assertNotIn("お気に入り", row.split("、")[1])

    def test_survives_a_new_search(self) -> None:
        """**印は保存される。** 検索し直しても残ること。"""
        self.ctrl.toggle_favorite(0)
        rows = self.ctrl.search(SearchForm())
        self.assertIn("お気に入り", rows[0])

    def test_filters_to_favorites_only(self) -> None:
        self.ctrl.toggle_favorite(0)
        rows = self.ctrl.search(SearchForm(favorite_only=True))
        self.assertEqual(len(rows), 1)
        self.assertIn("kick.wav", rows[0])

    def test_counts_them(self) -> None:
        self.assertEqual(self.ctrl.favorite_count(), 0)
        self.ctrl.toggle_favorite(0)
        self.ctrl.toggle_favorite(1)
        self.assertEqual(self.ctrl.favorite_count(), 2)

    def test_nothing_selected_is_handled(self) -> None:
        message, row = self.ctrl.toggle_favorite(-1)
        self.assertIn("選択されていません", message)
        self.assertIsNone(row)


class TestDropResult(unittest.TestCase):
    """**一覧から行を消すときは内部リストも一緒に消す。**

    UI（ListBox）だけ消すと index がずれ、以後の選択が別のサンプルを掴む。
    """

    def setUp(self) -> None:
        self.lib = Library(":memory:")
        for name in ("a.wav", "b.wav", "c.wav"):
            self.lib.add_sample(Sample(
                path=f"/x/{name}", filename=name,
                file_format="wav", kind=SampleKind.ONESHOT))
        self.ctrl = AppController(self.lib, PreviewPlayer(NullBackend()))
        self.ctrl.search(SearchForm())

    def tearDown(self) -> None:
        self.lib.close()

    def test_removes_the_row_and_shifts_the_rest(self) -> None:
        self.ctrl.drop_result(1)   # b.wav を消す
        self.assertEqual(self.ctrl.result_count, 2)
        self.assertEqual(self.ctrl.get_sample(1).filename, "c.wav")

    def test_out_of_range_is_harmless(self) -> None:
        self.ctrl.drop_result(99)
        self.ctrl.drop_result(-1)
        self.assertEqual(self.ctrl.result_count, 3)


class TestPreviewVolumeControl(unittest.TestCase):
    """試聴の音量を覚えて、キーで上げ下げする。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "s.json"
        self.lib = Library(":memory:")
        self.backend = NullBackend()
        self.ctrl = AppController(
            self.lib, PreviewPlayer(self.backend),
            SessionSettings(preview_volume=0.5), self.path)

    def tearDown(self) -> None:
        self.lib.close()
        self.tmp.cleanup()

    def test_reports_as_a_percentage(self) -> None:
        self.assertIn("30", self.ctrl.set_preview_volume(0.3))

    def test_nudges_up_and_down(self) -> None:
        self.ctrl.nudge_preview_volume(True)
        self.assertAlmostEqual(self.ctrl.preview_volume, 0.6)
        self.ctrl.nudge_preview_volume(False)
        self.assertAlmostEqual(self.ctrl.preview_volume, 0.5)

    def test_says_when_it_cannot_go_further(self) -> None:
        """**変わっていないことに気づけるようにする。**"""
        self.ctrl.set_preview_volume(1.0)
        self.assertIn("これ以上上げられません",
                      self.ctrl.nudge_preview_volume(True))
        self.ctrl.set_preview_volume(0.0)
        self.assertIn("これ以上下げられません",
                      self.ctrl.nudge_preview_volume(False))

    def test_is_saved_for_next_time(self) -> None:
        self.ctrl.set_preview_volume(0.2)
        self.assertAlmostEqual(
            SessionSettings.load(self.path).preview_volume, 0.2)

    def test_is_not_cleared_with_the_tempo(self) -> None:
        """音量は好みなので、終了時のクリアで消さない。"""
        self.ctrl.set_preview_volume(0.2)
        self.ctrl.clear_session_tempo()
        self.assertAlmostEqual(self.ctrl.preview_volume, 0.2)

    def test_reaches_the_backend(self) -> None:
        self.ctrl.set_preview_volume(0.4)
        self.assertAlmostEqual(self.backend._volume, 0.4)

    def test_edge_is_reported_after_real_nudges(self) -> None:
        """**実際のキー連打の経路でも端の通知が出ること。**

        0.1 刻みの足し引きは浮動小数の誤差が積もる。丸めずに等値比較
        すると、端に達した直後の数回が「変わっていないのに無反応」に
        なっていた（0.1 - 0.1 = 2.8e-17 のため）。
        """
        self.ctrl.set_preview_volume(0.5)
        for _ in range(5):
            self.ctrl.nudge_preview_volume(False)
        self.assertEqual(self.ctrl.preview_volume, 0.0)
        self.assertIn("これ以上下げられません",
                      self.ctrl.nudge_preview_volume(False))

        self.ctrl.set_preview_volume(0.5)
        for _ in range(5):
            self.ctrl.nudge_preview_volume(True)
        self.assertEqual(self.ctrl.preview_volume, 1.0)
        self.assertIn("これ以上上げられません",
                      self.ctrl.nudge_preview_volume(True))


class TestPreviewWithSettings(unittest.TestCase):
    """**置く前に耳で確かめる。** 加工した結果を鳴らす。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.wav = self.root / "kick.wav"
        self.wav.write_bytes(b"RIFF____WAVEfmt ")
        self.lib = Library(":memory:")
        self.sample = self.lib.add_sample(Sample(
            path=str(self.wav), filename="kick.wav",
            file_format="wav", kind=SampleKind.ONESHOT))
        self.ctrl = AppController(self.lib, PreviewPlayer(NullBackend()))

    def tearDown(self) -> None:
        self.lib.close()
        self.tmp.cleanup()

    def test_plays_without_rendering_when_unset(self) -> None:
        message = self.ctrl.preview_with_settings(
            self.sample, SessionSettings())
        self.assertTrue(message.startswith("[情報]"))
        self.assertIn("加工せず", message)

    def test_auto_render_off_is_stated_correctly(self) -> None:
        """**加工しない理由を取り違えない。**

        テンポが入っていて「自動で加工する」だけをオフにした場合、
        「テンポもキーも未設定」と読むのは嘘になる（設定が消えたと
        誤解される）。
        """
        message = self.ctrl.preview_with_settings(
            self.sample, SessionSettings(bpm=120.0, auto_render=False))
        self.assertIn("自動加工がオフ", message)
        self.assertNotIn("未設定", message)

    def test_nothing_selected_is_reported(self) -> None:
        message = self.ctrl.preview_with_settings(None, SessionSettings())
        self.assertTrue(message.startswith("[警告]"))

    def test_missing_file_is_reported(self) -> None:
        self.wav.unlink()
        message = self.ctrl.preview_with_settings(
            self.sample, SessionSettings())
        self.assertTrue(message.startswith("[エラー]"))
        self.assertIn("見つかりません", message)

    def test_without_a_player_it_says_so(self) -> None:
        ctrl = AppController(self.lib, None)
        self.assertIn("試聴できません",
                      ctrl.preview_with_settings(
                          self.sample, SessionSettings()))


class TestPreviewToggleWithSettings(unittest.TestCase):
    """**止める手段がないと鳴りっぱなしになる。**

    設定を変えながら何度も聞くので、同じキー・同じボタンで
    止められるようにする。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.wav = Path(self.tmp.name) / "kick.wav"
        self.wav.write_bytes(b"RIFF____WAVEfmt ")
        self.lib = Library(":memory:")
        self.sample = self.lib.add_sample(Sample(
            path=str(self.wav), filename="kick.wav",
            file_format="wav", kind=SampleKind.ONESHOT))
        self.ctrl = AppController(self.lib, PreviewPlayer(NullBackend()))

    def tearDown(self) -> None:
        self.lib.close()
        self.tmp.cleanup()

    def test_first_press_plays(self) -> None:
        message = self.ctrl.preview_toggle_with_settings(
            self.sample, SessionSettings())
        self.assertIn("試聴しています", message)
        self.assertTrue(self.ctrl.preview_is_playing)

    def test_second_press_stops(self) -> None:
        self.ctrl.preview_toggle_with_settings(
            self.sample, SessionSettings())
        message = self.ctrl.preview_toggle_with_settings(
            self.sample, SessionSettings())
        self.assertIn("止めました", message)
        self.assertFalse(self.ctrl.preview_is_playing)

    def test_third_press_plays_again(self) -> None:
        """止めたあと、もう一度押せば鳴る。"""
        for _ in range(2):
            self.ctrl.preview_toggle_with_settings(
                self.sample, SessionSettings())
        message = self.ctrl.preview_toggle_with_settings(
            self.sample, SessionSettings())
        self.assertIn("試聴しています", message)

    def test_not_playing_without_a_player(self) -> None:
        ctrl = AppController(self.lib, None)
        self.assertFalse(ctrl.preview_is_playing)


class TestPreparePlaySplit(unittest.TestCase):
    """**準備（重い）と再生（UI スレッド限定）を分けて呼べること。**

    ダイアログは準備をワーカースレッドで行い、結果を UI スレッドで
    鳴らす（加工の数秒で画面と VoiceOver を固まらせないため）。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.wav = Path(self.tmp.name) / "kick.wav"
        self.wav.write_bytes(b"RIFF____WAVEfmt ")
        self.lib = Library(":memory:")
        self.sample = self.lib.add_sample(Sample(
            path=str(self.wav), filename="kick.wav",
            file_format="wav", kind=SampleKind.ONESHOT))
        self.ctrl = AppController(self.lib, PreviewPlayer(NullBackend()))

    def tearDown(self) -> None:
        self.lib.close()
        self.tmp.cleanup()

    def test_prepare_then_play_plays(self) -> None:
        prepared = self.ctrl.prepare_preview_with_settings(
            self.sample, SessionSettings())
        self.assertTrue(prepared.ok)
        message = self.ctrl.play_prepared_preview(prepared)
        self.assertIn("試聴しています", message)
        self.assertTrue(self.ctrl.preview_is_playing)

    def test_prepare_reports_missing_file(self) -> None:
        self.wav.unlink()
        prepared = self.ctrl.prepare_preview_with_settings(
            self.sample, SessionSettings())
        self.assertFalse(prepared.ok)
        self.assertIn("見つかりません", prepared.error)

    def test_play_passes_the_error_through(self) -> None:
        """準備の失敗はそのまま読み上げ文言になる。"""
        self.wav.unlink()
        prepared = self.ctrl.prepare_preview_with_settings(
            self.sample, SessionSettings())
        self.assertEqual(self.ctrl.play_prepared_preview(prepared),
                         prepared.error)

    def test_stop_if_active_reports_what_it_did(self) -> None:
        """止めたかどうかを返す（止めただけなら「加工しています」と
        言わずに済ませるため）。"""
        self.assertFalse(self.ctrl.stop_preview_if_active())
        self.ctrl.preview_with_settings(self.sample, SessionSettings())
        self.assertTrue(self.ctrl.stop_preview_if_active())
        self.assertFalse(self.ctrl.preview_is_playing)

    def test_stop_if_active_without_a_player(self) -> None:
        ctrl = AppController(self.lib, None)
        self.assertFalse(ctrl.stop_preview_if_active())


class TestPackChoicesFollowTheForm(unittest.TestCase):
    """検索条件に合うパックだけを選択肢に出す。"""

    def setUp(self) -> None:
        self.lib = Library(":memory:")
        self.source = self.lib.add_source(
            Source(name="s", root="/r", kind=SourceKind.SPLICE))
        for pack, kind in (("loops", SampleKind.LOOP),
                           ("shots", SampleKind.ONESHOT)):
            self.lib.add_sample(Sample(
                path=f"/r/{pack}/x.wav", filename="x.wav",
                file_format="wav", kind=kind,
                source_id=self.source.id, pack=pack))
        self.ctrl = AppController(self.lib, PreviewPlayer(NullBackend()))

    def tearDown(self) -> None:
        self.lib.close()

    def test_no_form_lists_everything(self) -> None:
        got = dict(self.ctrl.list_packs_with_counts())
        self.assertEqual(set(got), {"loops", "shots"})

    def test_form_narrows_the_choices(self) -> None:
        form = SearchForm(kind=SampleKind.LOOP)
        got = dict(self.ctrl.list_packs_with_counts(None, form))
        self.assertEqual(set(got), {"loops"})

    def test_selected_pack_does_not_narrow_itself(self) -> None:
        """パックを選んでいても、選択肢は全部出す（切り替えるため）。"""
        form = SearchForm(pack="loops")
        got = dict(self.ctrl.list_packs_with_counts(None, form))
        self.assertEqual(set(got), {"loops", "shots"})

    def test_list_packs_follows_the_form(self) -> None:
        form = SearchForm(kind=SampleKind.ONESHOT)
        self.assertEqual(self.ctrl.list_packs(None, form), ["shots"])
