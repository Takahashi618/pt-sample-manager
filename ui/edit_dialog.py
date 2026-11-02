"""サンプル編集ダイアログ（タグ・カテゴリ・種別）。VoiceOver/PC-Talker 前提。

スキャン直後のサンプルは種別が推定値・タグ無しのため、このダイアログで
手動補正する（ユーザー方針: 確定は手動）。

アクセシビリティ設計は main_frame と同方針:
- 各入力に StaticText ラベルを併記し、SetName にも説明を入れる。
- ボタンは標準の OK / キャンセル（Enter=OK、Esc=キャンセル）。
"""

from __future__ import annotations

import wx

from core.i18n import tr
from ui.ax_label import label_control
from ui.wx_keys import handle_dialog_enter_escape

from core.models import Sample, SampleKind

# 種別選択肢（表示ラベル, 値）。UNKNOWN も選べるようにする。
_KIND_CHOICES: list[tuple[str, SampleKind]] = [
    (kind.label_ja, kind) for kind in SampleKind
]


class EditDialog(wx.Dialog):
    """タグ（カンマ区切り）・カテゴリ・種別を編集するモーダルダイアログ。

    OK で閉じた後、tags_csv / category / kind プロパティから入力値を
    取り出し、AppController.apply_edits に渡す。
    """

    def __init__(self, parent: wx.Window, sample: Sample) -> None:
        super().__init__(
            parent, title=tr(f"サンプル情報の編集: {sample.filename}",
                             f"Edit Sample: {sample.filename}")
        )
        outer = wx.BoxSizer(wx.VERTICAL)

        name_label = wx.StaticText(self, label=tr(f"編集対象: {sample.filename}",
                                                  "Editing: "
                                                  f"{sample.filename}"))
        name_label.SetName(tr("編集対象のファイル名", "File name being edited"))
        outer.Add(name_label, flag=wx.ALL, border=8)

        # タグ
        self._tags = wx.TextCtrl(
            self, value=", ".join(t.name for t in sample.tags)
        )
        label_control(self._tags, tr("タグ。カンマ区切りで複数入力",
                                     "Tags. Separate several with commas"))
        outer.Add(
            self._labeled(tr("タグ（カンマ区切り）",
                             "Tags (separated by commas)"), self._tags),
            flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
            border=8,
        )

        # カテゴリ
        self._category = wx.TextCtrl(
            self,
            value=sample.category.name if sample.category else "",
        )
        label_control(self._category, tr("カテゴリ名。空欄でカテゴリなし",
                                         "Category name. Leave empty for "
                                         "no category"))
        outer.Add(
            self._labeled(tr("カテゴリ（空欄でカテゴリなし）",
                             "Category (empty for none)"), self._category),
            flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
            border=8,
        )

        # 種別
        self._kind = wx.Choice(self, choices=[c[0] for c in _KIND_CHOICES])
        label_control(self._kind, tr("種別の選択", "Type selection"))
        current = next(
            (i for i, (_, k) in enumerate(_KIND_CHOICES) if k is sample.kind),
            len(_KIND_CHOICES) - 1,
        )
        self._kind.SetSelection(current)
        outer.Add(
            self._labeled(tr("種別", "Type"), self._kind),
            flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
            border=8,
        )

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        outer.Add(buttons, flag=wx.ALL | wx.ALIGN_RIGHT, border=8)

        self.SetSizerAndFit(outer)
        self.SetMinSize((420, -1))
        self._tags.SetFocus()
        # **Enter で決定、Esc で戻る。** 選択肢（Choice）にフォーカスが
        # あると Enter が既定ボタンへ届かないことがあるため、
        # EVT_CHAR_HOOK で確実に拾う（ID の設定だけでは直らない。
        # どちらも wx の既定値なので）。ボタンの上ではそのボタンを押す。
        # 共通処理は ui/wx_keys.py
        self.SetAffirmativeId(wx.ID_OK)
        self.SetEscapeId(wx.ID_CANCEL)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

    def _on_char_hook(self, evt: wx.KeyEvent) -> None:
        """Enter は決定（ボタンの上ならそのボタン）、Esc は取り消し。"""
        if handle_dialog_enter_escape(self, evt):
            return
        evt.Skip()

    def _labeled(self, label: str, ctrl: wx.Window) -> wx.BoxSizer:
        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(wx.StaticText(self, label=label), flag=wx.BOTTOM, border=2)
        box.Add(ctrl, flag=wx.EXPAND)
        return box

    # ------------------------------------------------------------------
    # 入力値の取り出し（OK 後に呼ぶ）
    # ------------------------------------------------------------------
    @property
    def tags_csv(self) -> str:
        return self._tags.GetValue()

    @property
    def category(self) -> str:
        return self._category.GetValue()

    @property
    def kind(self) -> SampleKind:
        return _KIND_CHOICES[self._kind.GetSelection()][1]
