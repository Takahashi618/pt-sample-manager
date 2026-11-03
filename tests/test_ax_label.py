"""読み上げ名の部品（ui/ax_label.py）のテスト。

本命の名前が付いたら、同じ文をヒント（AXHelp）へ入れない。VoiceOver が 2 回読むため。
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock

from ui import ax_label


class _Ctrl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def SetName(self, text: str) -> None:
        self.calls.append(("name", text))

    def SetHint(self, text: str) -> None:
        self.calls.append(("hint", text))

    def SetToolTip(self, text: str) -> None:
        self.calls.append(("tip", text))


class TestLabelControl(unittest.TestCase):
    def test_no_insurance_when_the_name_is_set(self) -> None:
        ctrl = _Ctrl()
        with mock.patch.object(ax_label, "set_ax_label", return_value=True):
            ax_label.label_control(ctrl, "検索語")
        self.assertEqual(ctrl.calls, [("name", "検索語")])

    def test_insurance_when_the_name_cannot_be_set(self) -> None:
        ctrl = _Ctrl()
        with mock.patch.object(ax_label, "set_ax_label", return_value=False):
            ax_label.label_control(ctrl, "検索語")
        self.assertEqual(ctrl.calls, [("name", "検索語"), ("hint", "検索語"),
                                      ("tip", "検索語")])


@unittest.skipUnless(sys.platform == "darwin", "macOS の読み上げ名")
class TestRealControls(unittest.TestCase):
    def test_voiceover_gets_the_name_once(self) -> None:
        import objc
        import wx

        app = wx.App(False)
        frame = wx.Frame(None)
        panel = wx.Panel(frame)
        try:
            for ctrl in (wx.Button(panel, label="x"), wx.TextCtrl(panel),
                         wx.Choice(panel, choices=["a"]),
                         wx.ListBox(panel, choices=["a"])):
                with self.subTest(kind=type(ctrl).__name__):
                    ax_label.label_control(ctrl, "読み上げ名")
                    view = objc.objc_object(c_void_p=ctrl.GetHandle())
                    self.assertEqual(str(view.accessibilityLabel()), "読み上げ名")
                    self.assertFalse(view.accessibilityHelp())
                    self.assertFalse(ctrl.GetToolTipText())
                    if isinstance(ctrl, wx.TextCtrl):
                        self.assertEqual(ctrl.GetHint(), "")
        finally:
            frame.Destroy()
            del app


if __name__ == "__main__":
    unittest.main()
