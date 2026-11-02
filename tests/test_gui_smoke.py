r"""GUI スモークテスト。

実際の読み上げ・表示確認は macOS/Windows 実機で行うが、ここでは
MainFrame / EditDialog が import エラー・構築エラーなく生成でき、
主要な非モーダル経路（初期表示・検索・未選択ガード・スキャン完了）が
動くことだけを検証する。ウィンドウは表示せず、生成後すぐ破棄する。

ヘッドレス環境では wx.App 生成に失敗し得るため、その場合は skip する。

実行:
    PYTHONPATH=. .venv/bin/python -m pytest tests/test_gui_smoke.py
"""

from __future__ import annotations

import sys
import unittest

from audio.player import NullBackend, PreviewPlayer
from core.library import Library
from core.models import Sample, SampleKind
from ui.controller import AppController


class TestGuiSmoke(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import wx  # noqa: F401
        except ImportError:
            self.skipTest("wxPython 未導入")
        self.lib = Library(":memory:")
        self.lib.add_sample(
            Sample(
                path="/a/kick.wav",
                filename="kick.wav",
                file_format="wav",
                kind=SampleKind.ONESHOT,
            )
        )
        self.ctrl = AppController(self.lib, PreviewPlayer(NullBackend()))

    def tearDown(self) -> None:
        if hasattr(self, "lib"):
            self.lib.close()

    def test_frame_builds_and_basic_flows(self) -> None:
        import wx

        import ui.announce as announce_mod
        from ui.edit_dialog import EditDialog
        from ui.main_frame import MainFrame

        try:
            app = wx.App(False)
        except Exception as exc:  # pragma: no cover - ヘッドレス環境
            self.skipTest(f"wx.App を生成できません: {exc}")

        # 能動アナウンスは記録のみに差し替え（テスト中の osascript 起動回避）
        announced: list[str] = []
        orig_announce = announce_mod.announce
        announce_mod.announce = announced.append

        frame = MainFrame(self.ctrl)
        try:
            # 起動時に登録済みサンプルが全件表示されている（初期表示）
            self.assertEqual(frame._result_list.GetCount(), 1)
            self.assertIn("起動しました", frame._status.GetLabel())

            # 検索を実行し、ListBox に行が入ることを確認
            frame._on_search(None)
            self.assertEqual(frame._result_list.GetCount(), 1)
            self.assertIn("kick.wav", frame._result_list.GetString(0))
            self.assertIn("1件", frame._status.GetLabel())

            # 主要コントロールに SetName が付いていること
            self.assertTrue(frame._result_list.GetName())
            self.assertTrue(frame._search_btn.GetName())
            self.assertTrue(frame._scan_btn.GetName())
            self.assertTrue(frame._edit_btn.GetName())
            self.assertTrue(frame._session_btn.GetName())

            # 未選択時の配置はワーカーを起こさず文言のみ・能動アナウンスあり
            frame._result_list.SetSelection(wx.NOT_FOUND)
            frame._on_import(None)
            self.assertIn("選択されていません", frame._status.GetLabel())
            self.assertFalse(frame._placing)
            self.assertTrue(announced)

            # スキャン完了ハンドラは一覧を現条件で更新して件数まで読み上げる
            frame._on_scan_done("1件を登録しました。")
            self.assertIn("1件を登録しました", frame._status.GetLabel())
            self.assertIn("1件見つかりました", frame._status.GetLabel())
            self.assertTrue(frame._scan_btn.IsEnabled())

            # 編集ダイアログが構築でき、初期値が反映されていること
            sample = self.ctrl.get_sample(0)
            dlg = EditDialog(frame, sample)
            try:
                self.assertIs(dlg.kind, SampleKind.ONESHOT)
                self.assertEqual(dlg.tags_csv, "")
            finally:
                dlg.Destroy()

            # セッション条件ダイアログも構築でき、往復できること
            from core.session_settings import SessionSettings
            from ui.session_dialog import SessionDialog

            dlg = SessionDialog(
                frame, SessionSettings(bpm=128, key="Am", sample_rate=44100))
            try:
                got = dlg.settings
                self.assertEqual(got.bpm, 128)
                self.assertEqual(got.key, "Am")
                self.assertEqual(got.sample_rate, 44100)
                self.assertTrue(got.auto_render)
            finally:
                dlg.Destroy()

            # 未設定の条件は空欄として出て、空欄のまま戻ること
            dlg = SessionDialog(frame, SessionSettings())
            try:
                self.assertIsNone(dlg.settings.bpm)
                self.assertEqual(dlg.settings.key, "")
            finally:
                dlg.Destroy()

            # 配置のしかたも往復できること
            dlg = SessionDialog(frame, SessionSettings(
                bpm=120, at_cursor=False, position="17|1|000",
                copies=4, every_bars=2))
            try:
                got = dlg.settings
                self.assertFalse(got.at_cursor)
                self.assertEqual(got.position, "17|1|000")
                self.assertEqual(got.copies, 4)
                self.assertAlmostEqual(got.every_bars, 2.0)
            finally:
                dlg.Destroy()

            # **開いて OK しただけで試聴の音量が既定へ戻らないこと。**
            # ダイアログが編集しない項目は引き継ぐ（settings が新規構築で
            # 返っていて、実機で 70% に戻っていた）
            dlg = SessionDialog(frame, SessionSettings(preview_volume=0.3))
            try:
                self.assertAlmostEqual(dlg.settings.preview_volume, 0.3)
            finally:
                dlg.Destroy()

            # **Enter をボタンの押下に変換できること**（ui/wx_keys.py）。
            # キャンセルボタンの上で Enter を押したのに決定になって
            # しまう、を防ぐ仕組みの土台
            from ui.wx_keys import press_button

            pressed: list[bool] = []
            probe = wx.Button(frame, label="試し")
            probe.Bind(wx.EVT_BUTTON, lambda _e: pressed.append(True))
            press_button(probe)
            self.assertTrue(pressed)
            probe.Destroy()

            # 読み取り先の管理ダイアログが構築でき、一覧が出ること
            from ui.source_dialog import SourceDialog

            dlg = SourceDialog(frame, self.ctrl)
            try:
                self.assertTrue(dlg._list.GetName())
                self.assertTrue(dlg._add_btn.GetName())
                self.assertTrue(dlg._remove_btn.GetName())
            finally:
                dlg.Destroy()

            # **お気に入りの登録と絞り込みができること。**
            # 数千件から前に使った素材を探し直すのは手間なので
            frame._result_list.SetSelection(0)
            frame._on_toggle_favorite(None)
            self.assertIn("お気に入りに追加", frame._status.GetLabel())
            self.assertIn("お気に入り", frame._result_list.GetString(0))
            frame._favorite_only.SetValue(True)
            frame._on_favorite_only(None)
            self.assertEqual(frame._result_list.GetCount(), 1)

            # **絞り込み中に解除した行は、その場で一覧から消えること。**
            # 残るとお気に入りだけの一覧に外れたものが混ざる
            frame._result_list.SetSelection(0)
            frame._on_toggle_favorite(None)
            self.assertIn("取り除きました", frame._status.GetLabel())
            self.assertEqual(frame._result_list.GetCount(), 0)
            self.assertEqual(frame._controller.result_count, 0)

            frame._favorite_only.SetValue(False)
            frame._on_favorite_only(None)
            self.assertEqual(frame._result_list.GetCount(), 1)

            # 読み取り先を番号で切り替えられること（登録が無くても落ちない）
            frame._jump_to_source(0)
            self.assertTrue(frame._status.GetLabel())
            frame._jump_to_source(8)  # 無い番号
            self.assertIn("ありません", frame._status.GetLabel())

            # 絞り込みの選択肢が並んでいること
            self.assertTrue(frame._channel_choice.GetCount() >= 3)
            self.assertTrue(frame._source_choice.GetCount() >= 1)
            self.assertTrue(frame._pack_choice.GetCount() >= 1)

            # **入力欄に読み上げ名が付いていること。**
            # macOS では SetName が効かず、タブ移動で何の欄か
            # 分からなくなっていた（ui/ax_label.py 参照）
            from ui.ax_label import set_ax_label

            for ctrl in (frame._search_text, frame._category_text,
                         frame._tags_text):
                self.assertTrue(ctrl.GetName())
                if sys.platform == "darwin":
                    # 下地のビューへ設定できること（pyobjc がある環境）
                    self.assertTrue(set_ax_label(ctrl, ctrl.GetName()))

            # **終了できること。** メニューバーが無いと macOS の
            # アプリメニューに「終了」が入らず Cmd+Q が効かない
            menubar = frame.GetMenuBar()
            self.assertIsNotNone(menubar)
            self.assertIsNotNone(menubar.FindItemById(wx.ID_EXIT))
            self.assertIsNotNone(menubar.FindItemById(wx.ID_HELP))

            # **いつビルドしたものかを確かめられること。**
            # 凍結ビルドではないので、見ただけでは中身の新しさが
            # 分からない（core/build_info.py）
            #
            # **ヘルプメニューに残っていること。** `wx.ID_ABOUT` を使うと
            # macOS が勝手にアプリメニューへ移してしまい、ヘルプから
            # 辿れなくなる（実機で指摘を受けた）
            help_labels = []
            for i in range(menubar.GetMenuCount()):
                if menubar.GetMenuLabelText(i) != "ヘルプ":
                    continue
                help_labels = [item.GetItemLabelText()
                               for item in menubar.GetMenu(i).GetMenuItems()]
            self.assertIn("バージョン情報", help_labels)
            self.assertIsNone(menubar.FindItemById(wx.ID_ABOUT))

            # **キー操作は 1 項目ずつたどれること。**
            # 全文を一度に読み上げるだけだと聞き逃す（実機で指摘を受けた）
            from ui.keys_dialog import KeysDialog, key_rows

            dlg = KeysDialog(frame)
            try:
                self.assertEqual(dlg._list.GetCount(), len(key_rows()))
                self.assertTrue(dlg._list.GetName())
                # 開いた直後から矢印でたどれるよう、先頭が選ばれている
                self.assertEqual(dlg._list.GetSelection(), 0)
            finally:
                dlg.Destroy()

            # **閉じるボタンだけのダイアログでも Enter と Esc が効くこと。**
            # 既定ボタンが無いので ID を設定しただけでは Enter で閉じない。
            # ui/wx_keys.py が押すボタンを見つけられる形になっていること
            from ui.wx_keys import handle_dialog_enter_escape

            dlg = KeysDialog(frame)
            try:
                self.assertEqual(dlg.GetAffirmativeId(), wx.ID_CLOSE)
                self.assertEqual(dlg.GetEscapeId(), wx.ID_CLOSE)
                self.assertIsInstance(dlg.FindWindow(wx.ID_CLOSE), wx.Button)
            finally:
                dlg.Destroy()

            probe = wx.Dialog(frame)
            closed: list[int] = []
            close_btn = wx.Button(probe, id=wx.ID_CLOSE)
            close_btn.Bind(wx.EVT_BUTTON, lambda e: closed.append(e.GetId()))
            probe.SetAffirmativeId(wx.ID_CLOSE)
            probe.SetEscapeId(wx.ID_CLOSE)
            try:
                for code in (wx.WXK_RETURN, wx.WXK_ESCAPE):
                    evt = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
                    evt.SetKeyCode(code)
                    self.assertTrue(handle_dialog_enter_escape(probe, evt))
                self.assertEqual(closed, [wx.ID_CLOSE, wx.ID_CLOSE])
            finally:
                probe.Destroy()

            # **読み取り先の追加は、無いフォルダと重複を弾いて読み上げること。**
            # 無いフォルダはあとのスキャンで初めて分かる。名前が重なると
            # 既存が返ってきて「追加しました」が嘘になる
            import tempfile

            dlg = SourceDialog(frame, self.ctrl)
            try:
                self.assertEqual(dlg.GetAffirmativeId(), wx.ID_CLOSE)
                self.assertIsInstance(dlg.FindWindow(wx.ID_CLOSE), wx.Button)
                announced.clear()
                dlg._folder.SetValue("/この経路は存在しない")
                dlg._on_add(None)
                self.assertIn("見つかりません", dlg.last_message)
                self.assertFalse(dlg.changed)
                self.assertTrue(announced)  # 結果はその場で読み上げる
                with tempfile.TemporaryDirectory() as tmp:
                    dlg._folder.SetValue(tmp)
                    dlg._on_add(None)
                    self.assertIn("追加しました", dlg.last_message)
                    self.assertTrue(dlg.changed)
                    dlg._folder.SetValue(tmp)
                    dlg._on_add(None)
                    self.assertIn("登録済み", dlg.last_message)
            finally:
                dlg.Destroy()

            # 操作メニューから主要な機能に辿り着けること
            labels = []
            for i in range(menubar.GetMenuCount()):
                menu = menubar.GetMenu(i)
                labels.extend(item.GetItemLabelText()
                              for item in menu.GetMenuItems())
            for expected in ("フォルダをスキャン", "読み取り先を管理",
                             "セッションの条件", "配置先のトラックを選ぶ"):
                self.assertIn(expected, labels)

            self._check_review5(wx, frame, announced)
        finally:
            announce_mod.announce = orig_announce
            frame.Destroy()
            app.Destroy()

    def _check_review5(self, wx, frame, announced: list) -> None:
        """画面まわりの見張り。"""
        from core.session_settings import SessionSettings
        from ui.session_dialog import SessionDialog
        from ui.source_dialog import SourceDialog

        # **読み上げ名に古いキーを残さない。** 修飾なしの F は、いまは
        # 一覧の頭文字への移動になる
        source_name = frame._source_choice.GetName()
        self.assertNotIn("Fキー", source_name)
        self.assertIn("コマンドとF", source_name)
        # フォルダを選ばせるのは読み取り先が無いときだけ
        self.assertNotIn("フォルダを選択し", frame._scan_btn.GetName())

        # **番号での切り替えは、登録した数を正しく言う。** 選択肢の数には
        # 「すべての読み取り先」が入っているので、そのままだと 1 つ多い
        frame._reload_sources()
        frame._jump_to_source(8)
        status = frame._status.GetLabel()
        self.assertIn("コマンドと9", status)
        self.assertNotIn("いま", status)
        registered = frame._source_choice.GetCount() - 1
        if registered:
            self.assertIn(f"{registered} 件", status)
        else:
            self.assertIn("まだ登録されていません", status)

        # **配置中もボタンを無効化しない**（フォーカスが移らないように）。
        # 多重実行は「実行中です」と読んで防ぐ
        frame._controller.place_sample_in_pro_tools = \
            lambda sample: "[情報] テスト"
        frame._result_list.SetSelection(0)
        frame._on_import(None)
        self.assertTrue(frame._import_btn.IsEnabled())
        frame._on_import(None)
        self.assertIn("実行中", frame._status.GetLabel())
        frame._on_import_done("[情報] テスト")
        self.assertFalse(frame._placing)

        # **読み取り先の管理: パスと名前の欄の Enter は「追加」。**
        # 閉じるにすると、打ち込んだパスが登録されないまま消える
        empty_lib = Library(":memory:")
        empty = AppController(empty_lib, PreviewPlayer(NullBackend()))
        dlg = SourceDialog(frame, empty)
        try:
            self.assertEqual(dlg._enter_action(dlg._folder), "add")
            self.assertEqual(dlg._enter_action(dlg._name), "add")
            self.assertEqual(dlg._enter_action(dlg._list), "")
            self.assertEqual(dlg._enter_action(dlg._add_btn), "")
            # 外すボタンは無効化しない。空なら外せるものが無いと読む
            self.assertTrue(dlg._remove_btn.IsEnabled())
            dlg._on_remove(None)
            self.assertIn("外せる読み取り先がありません", dlg.last_message)
        finally:
            dlg.Destroy()
            empty_lib.close()

        # **鳴らすものが無いのに「加工しています」と言わない。**
        prepared: list = []
        dlg = SessionDialog(
            frame, SessionSettings(bpm=120.0),
            preview_prepare=lambda s: prepared.append(s),
            preview_play=lambda p: "",
            preview_stop_if_active=lambda: False,
            preview_sample="")
        try:
            announced.clear()
            dlg._on_preview(None)
            self.assertIn("選ばれていません", dlg._fetch_note.GetLabel())
            self.assertFalse(dlg._previewing)
            self.assertFalse(prepared)
            self.assertFalse(any("加工しています" in a for a in announced))
        finally:
            dlg.Destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
