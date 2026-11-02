"""AppleScript 実行基盤（macOS 専用）。

設計方針:
- osascript をサブプロセスで実行する薄いラッパ。
- 文字列を AppleScript に埋め込む箇所は必ず escape_applescript() を通す
  （インジェクション対策。" と \\ を必ずエスケープ）。
- 失敗は AppleScriptError に正規化し、呼び出し側で扱いやすくする。
- run_script は -e の連結ではなく標準入力経由で渡し、長い/複数行
  スクリプトでもシェルクオート問題を避ける。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from core.i18n import tr


class AppleScriptError(RuntimeError):
    """AppleScript 実行失敗を表す例外。"""


@dataclass
class AppleScriptResult:
    """osascript の実行結果。"""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def escape_applescript(value: str) -> str:
    """文字列を AppleScript の二重引用符リテラルへ安全に埋め込む形にする。

    バックスラッシュと二重引用符をエスケープする。順序が重要で、
    先にバックスラッシュを処理してから引用符を処理する。
    返り値は前後の引用符を含まない（呼び出し側で "..." に挟む）。
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def quote(value: str) -> str:
    """escape して前後に二重引用符を付けた AppleScript 文字列リテラルを返す。"""
    return f'"{escape_applescript(value)}"'


def run_script(script: str, timeout: float = 30.0) -> AppleScriptResult:
    """AppleScript を実行して結果を返す。

    スクリプト本文は標準入力から osascript に渡す（-e 連結や
    シェルクオートの問題を回避するため）。
    """
    try:
        proc = subprocess.run(
            ["osascript", "-"],
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:  # osascript が無い=macOS以外
        raise AppleScriptError(
            tr("osascript が見つかりません。macOS でのみ利用可能です。",
               "osascript was not found. This works only on macOS.")
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AppleScriptError(
            tr(f"AppleScript がタイムアウトしました（{timeout}秒）。",
               f"AppleScript timed out ({timeout} seconds).")
        ) from exc

    return AppleScriptResult(
        returncode=proc.returncode,
        stdout=proc.stdout.strip(),
        stderr=proc.stderr.strip(),
    )


def run_script_checked(script: str, timeout: float = 30.0) -> str:
    """run_script の失敗時に AppleScriptError を送出する版。stdout を返す。"""
    result = run_script(script, timeout=timeout)
    if not result.ok:
        raise AppleScriptError(result.stderr or tr("AppleScript の実行に失敗しました",
                                                   "AppleScript failed"))
    return result.stdout
