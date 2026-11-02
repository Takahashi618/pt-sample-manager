"""操作の結果を短い音で知らせる。

読み上げだけだと、**時間のかかる処理（スキャン・配置）が終わったことに
気づきにくい。** 読み上げが始まる前に短い音を鳴らして、
「何か終わった」と先に分かるようにする。

設計:
- macOS 標準のシステム音を `afplay` で鳴らす。**追加の依存なし。**
- **非同期で鳴らす。** 音の再生を待って UI が止まらないようにする。
- 成功と失敗で音を変える。聞き分けられることが目的なので、
  短く、はっきり違うものを選ぶ。
- **読み上げより先に鳴らす。** 音が読み上げに重なると聞き取りにくい。
- `SM_SILENT=1` で無音にできる（検証時や、音が邪魔なとき）。

**システムのミュートは使わない。** VoiceOver まで黙るため。音を止めたいときはこの環境変数を使う。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# macOS 標準のシステム音。役割ごとに使い分ける
_SOUND_DIR = Path("/System/Library/Sounds")
_SOUNDS = {
    "success": "Glass.aiff",   # 明るい。完了
    "failure": "Basso.aiff",   # 低い。失敗・中断
    "start": "Tink.aiff",      # ごく短い。開始
}

_ENV_SILENT = "SM_SILENT"

# 読み上げ文言の頭の印のうち、失敗の音を鳴らすもの（日本語と英語）
FAILURE_PREFIXES = ("[エラー]", "[警告]", "[Error]", "[Warning]")


def is_silent() -> bool:
    """音を鳴らさない設定になっているか。"""
    return os.environ.get(_ENV_SILENT, "") not in ("", "0", "false", "False")


def sound_path(kind: str) -> Path | None:
    """種類に対応する音のパス。無ければ None。"""
    name = _SOUNDS.get(kind)
    if name is None:
        return None
    path = _SOUND_DIR / name
    return path if path.exists() else None


def play(kind: str) -> bool:
    """短い通知音を鳴らす。

    戻り値は「鳴らそうとしたか」。**失敗しても例外は投げない**
    （音が出ないことでアプリが止まるほうが困る）。
    """
    if is_silent() or sys.platform != "darwin":
        return False
    path = sound_path(kind)
    if path is None:
        return False
    try:
        # 待たない。UI と読み上げを止めないため
        subprocess.Popen(
            ["afplay", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError:
        return False


def play_for_message(message: str) -> bool:
    """読み上げ文言から成否を判断して鳴らす。

    文言は `[情報]` / `[警告]` / `[エラー]` で始まる規約になっている
    （`core/placement_planner.py`）。**警告とエラーは同じ音**にする。
    細かく鳴らし分けても聞き分けられないため。
    """
    if message.startswith(FAILURE_PREFIXES):
        return play("failure")
    return play("success")
