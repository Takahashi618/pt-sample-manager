"""Pro Tools 連携（macOS 専用）。

連携方式:
- 方式C（パスコピー / copy_path_to_clipboard）:
  選択サンプルのパスをクリップボードへ。最も確実なフォールバック。
  pbcopy 経由なので Pro Tools の UI 状態に依存しない。
- 方式B（Import Audio ダイアログ経由 / import_via_dialog）:
  Shift+Cmd+I で Import Audio を開き、ファイルパスを指定して取り込む。
  キーボード完結・座標非依存だが、ダイアログの AX 階層が
  Pro Tools のバージョン/ロケールに依存するため、実機検証が前提。
  検証前は dry_run=True で「実行されるスクリプト」を返すだけにする。

設計方針:
- AppleScript 埋め込みは必ず applescript.quote() を通す。
- Pro Tools を前面化する activate を必ず先に行う。
- 方式B の UI 操作は実機で階層を確かめて確定するまで、本番実行をガードする。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .applescript import quote, run_script, run_script_checked

from core.i18n import tr

PRO_TOOLS_APP = "Pro Tools"


@dataclass
class ImportOutcome:
    """取り込み試行の結果。"""

    success: bool
    method: str           # "clipboard" | "dialog"
    message: str          # 読み上げ用の日本語メッセージ
    script: str = ""      # 実行した（or 実行予定の）AppleScript（dry_run時）


# ----------------------------------------------------------------------
# 方式C: パスコピー（確実なフォールバック）
# ----------------------------------------------------------------------
def copy_path_to_clipboard(path: str) -> ImportOutcome:
    """ファイルパスをクリップボードにコピーする（pbcopy）。

    Pro Tools 側で「ワークスペース等からペースト」する運用を想定した
    確実なフォールバック。UI 状態に依存しない。
    """
    try:
        proc = subprocess.run(
            ["pbcopy"], input=path, text=True, capture_output=True, timeout=10
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return ImportOutcome(
            success=False,
            method="clipboard",
            message=tr(f"クリップボードへのコピーに失敗しました: {exc}",
                       f"Could not copy to the clipboard: {exc}"),
        )
    if proc.returncode != 0:
        return ImportOutcome(
            success=False,
            method="clipboard",
            message=tr("クリップボードへのコピーに失敗しました",
                       "Could not copy to the clipboard"),
        )
    return ImportOutcome(
        success=True,
        method="clipboard",
        message=tr(f"パスをコピーしました: {path}", f"Copied the path: {path}"),
    )


# ----------------------------------------------------------------------
# Pro Tools 起動確認・前面化
# ----------------------------------------------------------------------
def is_pro_tools_running() -> bool:
    """Pro Tools が起動しているか確認する。"""
    script = (
        'tell application "System Events" to '
        f"return (exists process {quote(PRO_TOOLS_APP)})"
    )
    result = run_script(script)
    return result.ok and result.stdout.lower() == "true"


def activate_pro_tools() -> None:
    """Pro Tools を前面化する。"""
    run_script(f"tell application {quote(PRO_TOOLS_APP)} to activate")


# ----------------------------------------------------------------------
# 方式B: Import Audio ダイアログ経由
# ----------------------------------------------------------------------
def build_import_script(path: str) -> str:
    """Import Audio を開いてパスを流し込む AppleScript を組み立てる。

    手順:
      1. Pro Tools を前面化
      2. Shift+Cmd+I で Import Audio を開く
      3. Cmd+Shift+G で「フォルダへ移動」シートを開きパスを入力
      4. Enter で確定、Enter で選択、Done/Open へ

    注意: ステップ3以降の正確なキー列・ボタン名は Pro Tools の
    バージョンとロケールに依存する。実機 dump で確定するまで本番では
    使わない（import_via_dialog の dry_run ガードを参照）。
    """
    p = quote(path)
    return f"""
tell application {quote(PRO_TOOLS_APP)} to activate
delay 0.5
tell application "System Events"
    tell process {quote(PRO_TOOLS_APP)}
        -- open Import Audio (Shift+Cmd+I)
        keystroke "i" using {{command down, shift down}}
        delay 0.8
        -- open the Go to Folder sheet on the file dialog (Cmd+Shift+G)
        keystroke "g" using {{command down, shift down}}
        delay 0.5
        keystroke {p}
        delay 0.3
        keystroke return
        delay 0.5
        keystroke return
    end tell
end tell
""".strip()


def import_via_dialog(path: str, dry_run: bool = True) -> ImportOutcome:
    """方式B で取り込む。既定は dry_run（スクリプトを返すだけ）。

    実機 dump で UI 階層・キー列を確定するまでは dry_run=True のまま
    使うこと。dry_run=False は実機検証済みの環境でのみ指定する。
    """
    script = build_import_script(path)
    if dry_run:
        return ImportOutcome(
            success=False,
            method="dialog",
            message=tr("方式B（Import Audio）は実機検証が未完了です。UI の階層を確認してから "
                       "dry_run=False にしてください。",
                       "Method B (Import Audio) has not been verified on "
                       "real hardware. Check the UI hierarchy before using "
                       "dry_run=False."),
            script=script,
        )

    if not is_pro_tools_running():
        return ImportOutcome(
            success=False,
            method="dialog",
            message=tr("Pro Tools が起動していません。", "Pro Tools is not running."),
            script=script,
        )

    try:
        run_script_checked(script, timeout=20)
    except Exception as exc:  # AppleScriptError 等
        return ImportOutcome(
            success=False,
            method="dialog",
            message=tr(f"取り込みに失敗しました: {exc}", f"Import failed: {exc}"),
            script=script,
        )
    return ImportOutcome(
        success=True,
        method="dialog",
        message=tr(f"Import Audio 経由で取り込みを実行しました: {Path(path).name}",
                   f"Imported through Import Audio: {Path(path).name}"),
        script=script,
    )
