"""ダイアログ・ボタンのキー操作ヘルパー（wx 依存の小物）。

**Enter の扱いを 1 箇所に集める。** macOS の wx には

  - 既定ボタン以外のボタンは Enter で押せない（スペースのみ）
  - 選択肢（Choice）にフォーカスがあると Enter が既定ボタンへ届かない
    ことがある（実機で「決定できない」と指摘を受けた）

という 2 つの穴があり、メインウィンドウと各ダイアログで同じ対処が要る。
片方だけ直すと、キー一覧の約束（「ボタンでエンター、そのボタンを押す」
「ダイアログでエンター、決定して閉じる」`ui/keys_dialog.py`）と実際が
ずれるので、ここに共通化してある。
"""

from __future__ import annotations

import wx


def press_button(button: wx.Button) -> None:
    """ボタンをキー操作から押したことにする（クリックに変換する）。"""
    click = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
    click.SetEventObject(button)
    button.GetEventHandler().ProcessEvent(click)


def handle_dialog_enter_escape(dialog: wx.Dialog, evt: wx.KeyEvent) -> bool:
    """ダイアログ共通の Enter / Escape 処理。処理したら True。

    - ボタンにフォーカスがあるときの Enter は**そのボタン**を押す。
      ここを飛ばして「決定」にすると、キャンセルボタンの上で Enter を
      押したのに決定になってしまう
    - それ以外の Enter は決定（AffirmativeId）のボタンを押す
    - Escape は取り消し（EscapeId）のボタンを押す

    ダイアログの EVT_CHAR_HOOK から使う。False のときは呼び出し側で
    evt.Skip() すること（文字入力などの通常処理を妨げない）。
    """
    if evt.GetModifiers() != wx.MOD_NONE:
        return False
    key = evt.GetKeyCode()
    if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
        focused = wx.Window.FindFocus()
        if isinstance(focused, wx.Button):
            press_button(focused)
            return True
        ok = dialog.FindWindow(dialog.GetAffirmativeId())
        if isinstance(ok, wx.Button):
            press_button(ok)
            return True
        return False
    if key == wx.WXK_ESCAPE:
        cancel = dialog.FindWindow(dialog.GetEscapeId())
        if isinstance(cancel, wx.Button):
            press_button(cancel)
            return True
        return False
    return False
