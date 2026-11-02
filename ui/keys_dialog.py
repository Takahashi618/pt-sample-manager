"""キー操作の一覧ダイアログ。VoiceOver/PC-Talker 前提。

**一度に読み上げるだけでは聞き逃す。** 以前はステータス欄へ全文を出して
能動アナウンスしていたが、項目が多く、途中から聞き直せなかった。
メニューから開いたときはフォーカス移動とぶつかって読まれないこともある
（バージョン情報で同じ問題が起きた。`main_frame._on_show_version`）。

そこで**リストにして 1 項目ずつたどれるようにする。** 上下矢印で
「結果リストでスペース、試聴を再生または停止する」のように 1 行ずつ読める。

一覧は `main_frame` の割り当てと対で保つこと。**片方だけ直すと
説明と実際がずれる**（利用者は説明を信じて操作するので実害が大きい）。
"""

from __future__ import annotations

import wx

from core.i18n import tr
from ui.ax_label import label_control
from ui.wx_keys import handle_dialog_enter_escape

_ANYWHERE = tr("全体", "Anywhere")
_RESULTS = tr("結果リスト", "Result list")
_SESSION = tr("セッションの条件", "Session Settings")
_DIALOGS = tr("ダイアログ", "Dialogs")
_RETURN = tr("エンター", "Return")
_UP_DOWN = tr("上下矢印", "Up and Down arrows")

# (場所, キー, 動作)。読み上げるので記号ではなく言葉で書く
KEY_HELP: list[tuple[str, str, str]] = [
    (_RESULTS,
     _UP_DOWN,
     tr("項目を移動する", "Move between items")),
    (_RESULTS,
     tr("文字キー", "Letter keys"),
     tr("その頭文字のサンプルへ移動する", "Jump to the sample that starts with that letter")),
    (_RESULTS,
     tr("スペース", "Space"),
     tr("試聴を再生または停止する", "Play or stop the preview")),
    (_RESULTS,
     _RETURN,
     tr("Pro Tools へ配置する", "Place in Pro Tools")),
    (tr("検索欄", "Search fields"),
     _RETURN,
     tr("検索を実行する", "Run the search")),
    (tr("ボタン", "Buttons"),
     _RETURN,
     tr("そのボタンを押す。スペースでも押せる", "Press the button. Space also works")),
    (_ANYWHERE,
     tr("コマンドとD", "Command-D"),
     tr("選択中のサンプルをお気に入りに登録する。もう一度押すと外れる",
        "Add the selected sample to favorites. Press again to remove it")),
    (_ANYWHERE,
     tr("コマンドと数字", "Command-number"),
     tr("読み取り先を切り替える。1 はすべて、2 から先は登録した順",
        "Switch the source. 1 is all sources; 2 and up follow the order you "
        "added them")),
    (_ANYWHERE,
     tr("コマンドとP", "Command-P"),
     tr("パックの絞り込みへ移動する", "Move to the pack filter")),
    (_ANYWHERE,
     tr("コマンドと上下矢印", "Command-Up and Command-Down arrows"),
     tr("試聴の音量を上げ下げする。文字入力欄では文頭と文末への移動になる",
        "Raise or lower the preview volume. In a text field they move to "
        "the start and end of the text")),
    (_SESSION,
     _UP_DOWN,
     tr("キーの音を選ぶ", "Choose the key note")),
    (_SESSION,
     tr("左右矢印", "Left and Right arrows"),
     tr("メジャーとマイナーを切り替える", "Switch between major and minor")),
    (_SESSION,
     tr("コントロールとシフトとL", "Control-Shift-L"),
     tr("その条件で加工した音を鳴らす。もう一度押すと止まる",
        "Play the sample processed with those settings. Press again to stop")),
    (tr("読み取り先の管理のパスと名前の欄", "Path and name fields in Manage Sources"),
     _RETURN,
     tr("その読み取り先を追加する。閉じるときはエスケープ", "Add that source. Press Escape to close")),
    (_DIALOGS,
     _RETURN,
     tr("決定して閉じる", "Confirm and close")),
    (_DIALOGS,
     tr("エスケープ", "Escape"),
     tr("何も変えずに元の画面へ戻る", "Go back without changing anything")),
    (_ANYWHERE,
     tr("コマンドとC", "Command-C"),
     tr("選択中のサンプルのパスをコピーする。文字入力欄では選択した文字のコピーになる",
        "Copy the path of the selected sample. In a text field it copies "
        "the selected text")),
    (_ANYWHERE,
     tr("コマンドとE", "Command-E"),
     tr("タグ・カテゴリ・種別を編集する", "Edit the tags, category and type")),
    (_ANYWHERE,
     tr("コマンドとS", "Command-S"),
     tr("セッションのテンポとキーを設定する。配置のしかたもここで決める",
        "Set the session tempo and key. How samples are placed is also set "
        "here")),
    (_ANYWHERE,
     tr("コマンドとF", "Command-F"),
     tr("読み取り先フォルダを管理する", "Manage the source folders")),
    (_ANYWHERE,
     tr("コマンドとT", "Command-T"),
     tr("配置先のトラックを選ぶ", "Choose the destination track")),
    (_ANYWHERE,
     tr("コマンドとR", "Command-R"),
     tr("フォルダをスキャンする", "Scan the folders")),
    (_ANYWHERE,
     tr("タブ", "Tab"),
     tr("次のコントロールへ移動する。シフトと合わせると逆",
        "Move to the next control. With Shift, the previous one")),
    (_ANYWHERE,
     tr("コマンドとシフトとK", "Command-Shift-K"),
     tr("このキー操作の一覧を開く", "Open this list of keyboard commands")),
    (_ANYWHERE,
     tr("コマンドとQ", "Command-Q"),
     tr("PTサンプルマネージャを終了する", "Quit PT Sample Manager")),
]


def describe_key(place: str, key: str, action: str) -> str:
    """1 行分の読み上げ文言。

    「どこで」「何を押すと」「どうなる」の順にする。場所が先にあると、
    いま自分がいる場所と照らし合わせながら聞ける。
    """
    return tr(f"{place}で{key}、{action}", f"{place}, {key}: {action}")


def key_rows() -> list[str]:
    """一覧に並べる行。"""
    return [describe_key(*row) for row in KEY_HELP]


class KeysDialog(wx.Dialog):
    """キー操作を 1 項目ずつたどれる一覧。"""

    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent, title=tr("キー操作の一覧", "Keyboard Commands"))
        outer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(self, label=tr("上下矢印で 1 つずつ確認できます。"
                                             "エスケープキーで閉じます。",
                                             "Use the Up and Down arrows to "
                                             "go through them one at a "
                                             "time. Press Escape to close."))
        intro.SetName(tr("説明", "Description"))
        outer.Add(intro, flag=wx.ALL, border=8)

        self._list = wx.ListBox(self, style=wx.LB_SINGLE, choices=key_rows())
        label_control(
            self._list,
            tr("キー操作の一覧。上下矢印で 1 つずつ読み上げます",
               "Keyboard commands. Use the Up and Down arrows to read them "
               "one at a time"))
        outer.Add(self._list, proportion=1,
                  flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)
        if self._list.GetCount():
            self._list.SetSelection(0)

        buttons = self.CreateStdDialogButtonSizer(wx.CLOSE)
        outer.Add(buttons, flag=wx.ALL | wx.ALIGN_RIGHT, border=8)

        self.SetSizerAndFit(outer)
        self.SetMinSize((560, 420))
        # **Esc と Enter で閉じる。** 既定の Esc は wx.ID_CANCEL のボタンに
        # しか結びつかず、閉じるボタンだけのダイアログでは効かないことが
        # ある。Enter は ID を設定しただけでは効かない（既定ボタンが無く、
        # macOS では閉じるボタンの上でも Enter で押せない）ので、
        # EVT_CHAR_HOOK で拾って閉じるボタンを押す（ui/wx_keys.py）。
        # 説明文と一覧で「エスケープキーで閉じます」「ダイアログでエンター、
        # 決定して閉じる」と約束しているので、どちらも確実に効かせる
        self.SetEscapeId(wx.ID_CLOSE)
        self.SetAffirmativeId(wx.ID_CLOSE)
        self.Bind(wx.EVT_BUTTON, self._on_close, id=wx.ID_CLOSE)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        # **リストにフォーカスを置いて開く。** 開いた直後から矢印で
        # たどれるようにするため
        self._list.SetFocus()

    def _on_close(self, _evt: wx.Event) -> None:
        self.EndModal(wx.ID_CLOSE)

    def _on_char_hook(self, evt: wx.KeyEvent) -> None:
        """Enter と Esc で閉じる（共通処理は ui/wx_keys.py）。"""
        if handle_dialog_enter_escape(self, evt):
            return
        evt.Skip()
