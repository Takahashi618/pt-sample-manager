"""macOS で読み上げ名を確実に設定する。

**wxPython の `SetName` は macOS の入力欄に効かない。**
ボタンや選択肢では読み上げ名になるのに、`wx.TextCtrl` や `wx.ListBox`
では AXTitle も AXDescription も空のままで、VoiceOver には
「テキストフィールド」としか伝わらない（実機のアクセシビリティ情報で確認）。

`SetHint`（AXPlaceholderValue）は空欄のときしか読まれず、
`SetToolTip`（AXHelp）は VoiceOver の設定に依存する。
**どちらもタブ移動で確実に名前を読ませる手段にはならない。**

そこで下地の NSView へ `setAccessibilityLabel_` を直接呼ぶ。
これは macOS のアクセシビリティ API そのものなので、
VoiceOver は AXDescription として必ず読む。

pyobjc が無い環境（Windows など）では黙って何もしない。
その場合も `SetName` / `SetHint` は効いているので、まったく読めなく
なるわけではない。

検証は、アクセシビリティの階層を書き出して確かめる。
"""

from __future__ import annotations


def set_ax_label(ctrl, label: str) -> bool:
    """コントロールの読み上げ名を設定する。

    成功したら True。**失敗しても例外は投げない**（読み上げ名が
    付かないことでアプリが起動しなくなるほうが困る）。
    """
    if not label:
        return False
    try:
        import objc
    except ImportError:
        return False

    try:
        handle = ctrl.GetHandle()
    except Exception:
        return False
    if not handle:
        return False

    try:
        view = objc.objc_object(c_void_p=handle)
    except Exception:
        return False
    if view is None:
        return False

    done = _apply(view, label)

    # **リストや複数行の入力はスクロールビューに包まれている。**
    # 外側に付けても、実際に読まれるのは中のビュー（NSTableView 等）なので
    # 両方に設定する
    try:
        inner = view.documentView() if hasattr(view, "documentView") else None
    except Exception:
        inner = None
    if inner is not None:
        done = _apply(inner, label) or done

    return done


def _apply(view, label: str) -> bool:
    """1 つのビューに読み上げ名を設定する。"""
    # NSView は macOS 10.10 以降 setAccessibilityLabel_ を持つ。
    # wx の実装によっては応答しないビューが返るので確かめる
    if not hasattr(view, "setAccessibilityLabel_"):
        return False
    try:
        view.setAccessibilityLabel_(label)
        return True
    except Exception:
        return False


def label_control(ctrl, label: str) -> None:
    """読み上げ名を、使える手段すべてで設定する。

    - `SetName`      … wx の標準。Windows の SR と、macOS のボタン類に効く
    - `setAccessibilityLabel_` … macOS で確実に読ませる本命
    - `SetToolTip`   … AXHelp。上が使えない環境での保険

    `SetHint` は入力欄にしか無いので、呼べるときだけ呼ぶ。
    """
    try:
        ctrl.SetName(label)
    except Exception:
        pass

    if hasattr(ctrl, "SetHint"):
        try:
            ctrl.SetHint(label)
        except Exception:
            pass

    try:
        ctrl.SetToolTip(label)
    except Exception:
        pass

    set_ax_label(ctrl, label)
