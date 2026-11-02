"""スクリーンリーダーへの能動アナウンス（OS別の薄い分岐）。

wxPython はフォーカス移動とラベルで多くの情報を SR に渡せるが、
「配置完了」のような、フォーカス移動を伴わない重要な状態変化は
能動的にアナウンスしないと読み上げられないことがある。

設計方針:
- macOS: **アプリ自身から AnnouncementRequested の通知を送る**
  （`NSAccessibilityPostNotificationWithUserInfo`。VoiceOver の正規の方法で、
  VoiceOver の声で読む。say のような二重読みにならない）。権限も要らない。
- Windows: accessible_output2 があれば利用（PC-Talker/NVDA/SAPI へ
  ルーティング）。未導入なら無音フォールバック。
- いずれも失敗しても例外を投げない（UI を止めない）。ステータス文字列の
  更新（wx 側）と併用する前提なので、アナウンス失敗＝即致命ではない。

### 2026-10-24 に直したこと

それまでは「System Events で、前にあるアプリの AXAnnouncement 属性を書く」
方法だけだった。**この Mac（macOS 15）では -10006（設定できません）で
断られ、何も読まれていなかった。** osascript のエラーを捨てていたので
気づけなかった（空の文で実際に試して確かめた）。
いま聞こえていたのは、フォーカスのある欄やボタンの値が変わったときに
VoiceOver が自分で読んだ分だけ。

System Events の方法は、通知を送れないとき（pyobjc が無い）だけの控えに
残した。**通知はメインスレッドから送る**（wx の CallAfter の中など。
このアプリと、借りているアプリはどれもそうしている）。

この層は wx に依存しない（呼び出しは main_frame から行う）。
**作者の別のアプリも `announce` を借りている。** 名前と引数は変えないこと。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading

# 能動アナウンスを止める環境変数。**テスト用**（テストの最中に VoiceOver が
# 読み出さないように。tests/conftest.py が立てる）。音を止める SM_SILENT とは
# 分けてある（音だけ止めたい利用者から読み上げまで奪わないため）
_ENV_NO_ANNOUNCE = "SM_NO_ANNOUNCE"


def _escape_applescript(text: str) -> str:
    """AppleScript 文字列リテラル用エスケープ（順序重要: \\ を先に）。"""
    text = text.replace("\\", "\\\\")
    text = text.replace('"', '\\"')
    return text


def _announce_native(message: str) -> bool:
    """macOS: アプリ自身から AnnouncementRequested の通知を送る。送れたら True。

    優先度は高（読み上げ中でも割り込む）。どれも利用者が頼んだ操作の結果で、
    聞き逃すと困るため。窓に向けて送り、窓が無ければアプリそのものに送る。

    **メインスレッドでなければ送らない**（AppKit はメインスレッド専用）。
    """
    if threading.current_thread() is not threading.main_thread():
        return False
    try:
        from AppKit import (
            NSAccessibilityAnnouncementKey,
            NSAccessibilityAnnouncementRequestedNotification,
            NSAccessibilityPostNotificationWithUserInfo,
            NSAccessibilityPriorityHigh,
            NSAccessibilityPriorityKey,
            NSApplication,
        )
    except Exception:
        return False
    try:
        app = NSApplication.sharedApplication()
        target = app.keyWindow() or app.mainWindow() or app
        NSAccessibilityPostNotificationWithUserInfo(
            target, NSAccessibilityAnnouncementRequestedNotification,
            {NSAccessibilityAnnouncementKey: message,
             NSAccessibilityPriorityKey: NSAccessibilityPriorityHigh})
        return True
    except Exception:
        return False


def _announce_macos(message: str) -> None:
    """macOS の控え: フロントプロセスの AXAnnouncement を System Events で書く。

    **macOS 15 では -10006 で断られる**（上の説明）。通知を送れないときだけ使う。
    """
    safe = _escape_applescript(message)
    script = (
        'tell application "System Events"\n'
        f'    set ann to "{safe}"\n'
        "    tell (first application process whose frontmost is true)\n"
        '        set value of attribute "AXAnnouncement" to ann\n'
        "    end tell\n"
        "end tell"
    )
    subprocess.Popen(
        ["osascript", "-e", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# Windows 用の accessible_output2 スピーカーは一度だけ生成して使い回す
_ao2_speaker = None
_ao2_tried = False


def _announce_windows(message: str) -> None:
    """Windows: accessible_output2 経由で PC-Talker/NVDA/SAPI へ。"""
    global _ao2_speaker, _ao2_tried
    if not _ao2_tried:
        _ao2_tried = True
        try:
            from accessible_output2.outputs.auto import Auto

            _ao2_speaker = Auto()
        except Exception:
            _ao2_speaker = None
    if _ao2_speaker is not None:
        try:
            _ao2_speaker.speak(message, interrupt=True)
        except Exception:
            pass


def announce(message: str) -> None:
    """現在の OS に応じて SR へ能動アナウンスする（失敗は握りつぶす）。"""
    if not message:
        return
    if os.environ.get(_ENV_NO_ANNOUNCE, "") not in ("", "0"):
        return
    try:
        if sys.platform == "darwin":
            if not _announce_native(message):
                _announce_macos(message)
        elif sys.platform.startswith("win"):
            _announce_windows(message)
        # それ以外の OS はアナウンス手段なし（ステータス更新のみに任せる）
    except Exception:
        pass
