"""読み取り先フォルダの管理ダイアログ。VoiceOver/PC-Talker 前提。

Splice のライブラリと自前の素材フォルダを別々に登録しておき、
検索時に切り替えて表示するための入口。

アクセシビリティ設計は edit_dialog / session_dialog と同方針:
- 各コントロールに StaticText ラベルを併記し、SetName にも説明を入れる。
- 一覧は ListBox。上下矢印だけで内容が読み上げられる 1 行にする。
- 追加・削除の結果は必ず文言で伝える（見た目の変化に頼らない）。
"""

from __future__ import annotations

import os

import wx

from core.i18n import tr, plural
from ui.ax_label import label_control
from ui.wx_keys import handle_dialog_enter_escape

from core.models import SourceKind

# 種類の選択肢（表示ラベル, 値）
_KIND_CHOICES: list[tuple[str, SourceKind]] = [
    (tr("Splice（パックごとに分ける）", "Splice (split by pack)"), SourceKind.SPLICE),
    (SourceKind.GENERIC.label_ja, SourceKind.GENERIC),
]


class SourceDialog(wx.Dialog):
    """読み取り先の一覧・追加・削除。

    このダイアログの中で controller を直接操作する（追加も削除も
    即座に反映したいため）。閉じたあと呼び出し側は一覧を作り直す。
    """

    def __init__(self, parent: wx.Window, controller) -> None:
        super().__init__(parent, title=tr("読み取り先フォルダの管理",
                                          "Manage Source Folders"))
        self._controller = controller
        self._sources: list = []
        self._last_message = ""
        # 追加か削除をしたか。**閉じたあと呼び出し側が一覧を作り直す**
        # かどうかの判断に使う（外した読み取り先のサンプルを残さない）
        self._changed = False

        outer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(self, label=tr("フォルダを登録しておくと、"
                                             "検索のときに切り替えて表示できます。Splice "
                                             "として登録すると、パックごとの絞り込みが使えます。",
                                             "Add folders here to switch "
                                             "between them when you search. "
                                             "Adding a folder as Splice "
                                             "lets you filter by pack."))
        intro.SetName(tr("説明", "Description"))
        outer.Add(intro, flag=wx.ALL, border=8)

        # 一覧
        self._list = wx.ListBox(self, style=wx.LB_SINGLE)
        label_control(
            self._list,
            tr("登録済みの読み取り先一覧。上下矢印で移動し、「選んだ読み取り先を外す」で削除できる",
               "Added sources. Move with the Up and Down arrows; “Remove "
               "Selected Source” removes one"))
        outer.Add(self._labeled(tr("登録済みの読み取り先", "Added sources"), self._list),
                  proportion=1,
                  flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)

        # 追加のための入力
        add_box = wx.StaticBoxSizer(
            wx.VERTICAL, self, label=tr("読み取り先を追加", "Add a Source"))

        self._folder = wx.TextCtrl(self)
        label_control(self._folder,
                      tr("追加するフォルダのパス。参照ボタンでも選べる。エンターで追加する",
                         "Path of the folder to add. The Browse button "
                         "also chooses one. Return adds it"))
        add_box.Add(self._labeled(tr("フォルダのパス", "Folder path"), self._folder),
                    flag=wx.EXPAND | wx.ALL, border=4)

        self._browse_btn = wx.Button(self, label=tr("フォルダを参照",
                                                    "Browse for Folder"))
        label_control(self._browse_btn, tr("フォルダ選択ダイアログを開く",
                                           "Open the folder chooser"))
        add_box.Add(self._browse_btn, flag=wx.ALL, border=4)

        self._name = wx.TextCtrl(self)
        label_control(self._name,
                      tr("読み取り先の名前。空欄ならフォルダ名を使う。エンターで追加する",
                         "Name of the source. If empty, the folder name is "
                         "used. Return adds it"))
        add_box.Add(self._labeled(tr("名前（空欄でフォルダ名）",
                                     "Name (empty uses the "
                                     "folder name)"), self._name),
                    flag=wx.EXPAND | wx.ALL, border=4)

        self._kind = wx.Choice(self, choices=[c[0] for c in _KIND_CHOICES])
        label_control(self._kind, tr("読み取り先の種類の選択", "Source kind selection"))
        self._kind.SetSelection(1)
        add_box.Add(self._labeled(tr("種類", "Kind"), self._kind),
                    flag=wx.EXPAND | wx.ALL, border=4)

        self._add_btn = wx.Button(self, label=tr("この読み取り先を追加",
                                                 "Add This Source"))
        label_control(
            self._add_btn,
            tr("入力した内容で読み取り先を登録する。登録後にスキャンが要る",
               "Add a source with what you entered. It needs a scan "
               "afterward"))
        add_box.Add(self._add_btn, flag=wx.ALL, border=4)

        outer.Add(add_box, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
                  border=8)

        # 削除
        self._remove_btn = wx.Button(self, label=tr("選んだ読み取り先を外す",
                                                    "Remove Selected Source"))
        label_control(
            self._remove_btn,
            tr("一覧で選んだ読み取り先を外す。その中のサンプルも一覧から消える",
               "Remove the source chosen in the list. Its samples also "
               "leave the list"))
        outer.Add(self._remove_btn, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM,
                  border=8)

        # 状態表示
        self._status = wx.StaticText(self, label="")
        label_control(self._status, tr("状態表示", "Status"))
        outer.Add(self._status, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=8)

        buttons = self.CreateStdDialogButtonSizer(wx.CLOSE)
        outer.Add(buttons, flag=wx.ALL | wx.ALIGN_RIGHT, border=8)

        self.SetSizerAndFit(outer)
        self.SetMinSize((560, 480))
        # **Esc と Enter で閉じる。** 閉じるボタンだけのダイアログでは
        # 既定の Esc が効かないことがある（Esc は wx.ID_CANCEL のボタンに
        # しか結びつかない）。Enter は既定ボタンが無いので ID を設定しただけ
        # では効かず、macOS では追加・参照・外すの各ボタンの上でも押せない。
        # EVT_CHAR_HOOK で拾い、ボタンの上ならそのボタン、それ以外は
        # 閉じるボタンを押す（ui/wx_keys.py）
        self.SetEscapeId(wx.ID_CLOSE)
        self.SetAffirmativeId(wx.ID_CLOSE)
        # パスと名前の欄の Enter だけは「追加」にする（`_enter_action`）
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

        self._browse_btn.Bind(wx.EVT_BUTTON, self._on_browse)
        self._add_btn.Bind(wx.EVT_BUTTON, self._on_add)
        self._remove_btn.Bind(wx.EVT_BUTTON, self._on_remove)
        self.Bind(wx.EVT_BUTTON, self._on_close, id=wx.ID_CLOSE)

        self._reload()
        self._folder.SetFocus()

    def _labeled(self, label: str, ctrl: wx.Window) -> wx.BoxSizer:
        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(wx.StaticText(self, label=label), flag=wx.BOTTOM, border=2)
        box.Add(ctrl, flag=wx.EXPAND, proportion=1)
        return box

    # ------------------------------------------------------------------
    # 一覧
    # ------------------------------------------------------------------
    def _reload(self) -> None:
        self._sources = self._controller.list_sources()
        rows = []
        for s in self._sources:
            packs = self._controller.list_packs(s.id)
            suffix = tr(f"、パック {len(packs)} 件",
                        f", {plural(len(packs), 'pack')}") if packs else ""
            rows.append(tr(f"{s.name}（{s.kind.label_ja}）{suffix}: {s.root}",
                           f"{s.name} ({s.kind.label_ja}){suffix}: {s.root}"))
        self._list.Set(rows)
        if rows:
            self._list.SetSelection(0)
        # **外すボタンは無効化しない。** 最後の 1 件を外したときはこの
        # ボタンにフォーカスがあり、無効化するとフォーカスがよそへ移り
        # かねない。空のときに押されたら、外せるものが無いと読む

    def _say(self, message: str) -> None:
        """結果を状態表示に書き、**その場で読み上げる。**

        ラベルを書き換えただけでは VoiceOver は何も言わない。追加や削除の
        結果を聞くために状態表示まで移動させるのは手間なので、能動的に
        アナウンスする（閉じたあとの読み上げは呼び出し側が別に行う）。
        """
        self._last_message = message
        self._status.SetLabel(message)
        from ui.announce import announce as sr_announce

        sr_announce(message)

    @property
    def last_message(self) -> str:
        """直近の操作結果。閉じたあと呼び出し側が読み上げる。"""
        return self._last_message

    @property
    def changed(self) -> bool:
        """このダイアログで読み取り先を追加または削除したか。"""
        return self._changed

    def _enter_action(self, focused) -> str:
        """Enter で何をするか。"add"（追加）か ""（共通の処理に任せる）。

        **フォルダのパスと名前の欄では、閉じずに追加する。** このダイアログの
        決定（AffirmativeId）は「閉じる」なので、共通の処理に任せると、
        パスを打って Enter を押したとき登録されないまま閉じてしまい、
        打ち込んだパスも消える。入力欄の Enter は「その入力を確定する」
        と受け取るのが自然なので、追加のボタンを押したことにする。
        """
        if focused is self._folder or focused is self._name:
            return "add"
        return ""

    def _on_char_hook(self, evt: wx.KeyEvent) -> None:
        """Enter はボタンの上ならそのボタン、入力欄なら追加、それ以外は閉じる。

        Esc は閉じる。キー一覧の「ボタンでエンター、そのボタンを押す」
        「読み取り先の管理のパスや名前の欄でエンター、追加する」
        「ダイアログでエンター、決定して閉じる」の約束を守る。
        """
        if (evt.GetModifiers() == wx.MOD_NONE
                and evt.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER)
                and self._enter_action(wx.Window.FindFocus()) == "add"):
            self._on_add(None)
            return
        if handle_dialog_enter_escape(self, evt):
            return
        evt.Skip()

    # ------------------------------------------------------------------
    # ハンドラ
    # ------------------------------------------------------------------
    def _on_browse(self, _evt: wx.Event) -> None:
        dlg = wx.DirDialog(self, tr("読み取り先のフォルダを選んでください",
                                    "Choose a source folder"),
                           style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                self._say(tr("フォルダの選択をやめました", "Folder not chosen"))
                return
            folder = dlg.GetPath()
        finally:
            dlg.Destroy()

        self._folder.SetValue(folder)
        # 構造から Splice かどうかを推定して初期値にする（変更可）
        from core.scanner import looks_like_splice

        self._kind.SetSelection(0 if looks_like_splice(folder) else 1)
        self._say(tr(f"{folder} を選びました。種類を確認して追加してください。",
                     f"Chose {folder}. Check the kind, then add it."))

    def _on_add(self, _evt: wx.Event) -> None:
        folder = self._folder.GetValue().strip()
        if not folder:
            self._say(tr("フォルダのパスを入力してください", "Enter the folder path"))
            self._folder.SetFocus()
            return
        # **無いフォルダを登録しない。** 打ち間違えたまま登録すると、
        # あとのスキャンで「フォルダが存在しません」と出るだけで、
        # どこを直せばよいか分からない（画面を見ずにパスを打つと起きやすい）
        folder = os.path.expanduser(folder)
        if not os.path.isdir(folder):
            self._say(tr(f"フォルダが見つかりません: {folder}。パスを確かめてください",
                         f"The folder was not found: {folder}. Check the "
                         "path"))
            self._folder.SetFocus()
            return
        # **同じフォルダ・同じ名前を黙って読み替えない。** 名前が重なると
        # 既存の読み取り先が返ってきて、「追加しました」と言いながら
        # 何も増えていない状態になる
        reason, conflict = self._controller.source_conflict(
            self._name.GetValue(), folder)
        if conflict:
            self._say(conflict)
            (self._name if reason == "name" else self._folder).SetFocus()
            return

        _, kind = _KIND_CHOICES[self._kind.GetSelection()]
        source = self._controller.register_source(
            self._name.GetValue(), folder, kind)
        self._changed = True
        self._reload()
        self._say(
            tr(f"「{source.name}」を追加しました。続けて「フォルダをスキャン」で中身を登録してください。",
               f"Added “{source.name}”. Next, use Scan Folders to add its "
               "contents."))
        self._folder.SetValue("")
        self._name.SetValue("")

    def _on_remove(self, _evt: wx.Event) -> None:
        if not self._sources:
            self._say(tr("外せる読み取り先がありません", "There are no sources to remove"))
            return
        index = self._list.GetSelection()
        if index == wx.NOT_FOUND or index >= len(self._sources):
            self._say(tr("外す読み取り先を選んでください", "Choose a source to remove"))
            return
        source = self._sources[index]

        confirm = wx.MessageBox(
            tr(f"「{source.name}」を外します。この読み取り先から登録したサンプルも一覧から消えます。"
               "ファイル自体は消えません。よろしいですか。",
               f"Remove “{source.name}”? The samples added from this source "
               "also leave the list. The files themselves are not deleted."),
            tr("確認", "Confirm"), wx.YES_NO | wx.ICON_QUESTION, self)
        if confirm != wx.YES:
            self._say(tr("外すのをやめました", "The source was not removed"))
            return

        self._say(self._controller.remove_source(source.id))
        self._changed = True
        self._reload()

    def _on_close(self, evt: wx.Event) -> None:
        evt.Skip()
