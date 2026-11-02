"""借りている部品（ptcore）の場所と言葉。**ここ 1 か所に集める。**

配置の確認と音の加工に使う `ptcore` は、作者の別のプロジェクトから借りている。
中立の名前 `placement_core` で呼ぶ。

探す順番（中に部品があるところだけ。古い環境変数で動かなくならないように）:

1. 環境変数 `PLACEMENT_CORE_PATH`（配布版は、起動時に .app の中の場所を
   ここで教える。`app._frozen_setup`）
2. このプロジェクトの `siblings/placement_core/`（公開しているソース。
   部品の写しが入っている）
3. 作者の手元では、同じ親フォルダに並んだ別のプロジェクト（本当のフォルダ名は
   `sibling_dirs.json`。公開しない）

**見つからなくても落ちない。** 加工と配置の確認が使えなくなるだけで、
アプリは起動する。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from core.i18n import is_english

PART_NAME = "placement_core"
PARTS_ENV = "PLACEMENT_CORE_PATH"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUNDLED = PROJECT_ROOT / "siblings" / PART_NAME
DEV_DIRS_FILE = PROJECT_ROOT / "sibling_dirs.json"

# 部品が返す言葉（日本語だけ）→ 英語。部品は別のアプリも借りているので部品の側は
# 変えず、ここで言い換える。部品の文言が変わると英語に日本語が混ざるので、
# tests/test_i18n.py が部品のソースと突き合わせる。長い文を先に置く
_PART_WORDS = (
    ("移調やタイムストレッチに使える道具がありません。ffmpeg か rubberband を入れてください"
     "（brew install ffmpeg rubberband）",
     "No tool for transposing and time stretching was found. Install ffmpeg "
     "or rubberband (brew install ffmpeg rubberband)"),
    ("チャンネル数を合わせるには ffmpeg が要ります（brew install ffmpeg）",
     "Changing the channel count needs ffmpeg (brew install ffmpeg)"),
    ("チャンネル数を変えられませんでした: ", "Could not change the channel count: "),
    ("元のファイルがありません: ", "The source file does not exist: "),
    ("出力先を作れません: ", "Cannot create the output folder: "),
    ("コピーできません: ", "Cannot copy: "),
    ("出力ファイルができていません", "The output file was not created"),
    ("使えるバックエンドがありません", "No audio tool is available"),
    (" 秒で終わりませんでした", " seconds passed without finishing"),
    (" を起動できません: ", " could not be started: "),
    (" が失敗しました: ", " failed: "),
    ("終了コード ", "exit code "),
    ("チャンネル数は 1 以上です", "The channel count must be 1 or more"),
    ("キーとして解釈できません: ", "Cannot read this as a key: "),
    ("音名として解釈できません: ", "Cannot read this as a note: "),
    ("BPM は正の値である必要があります", "BPM must be positive"),
    ("拍子は正の値である必要があります", "The meter must be positive"),
    ("長さの比は正の値である必要があります", "The length ratio must be positive"),
    ("ループの小節数は正の値である必要があります", "The loop length in bars must be positive"),
    ("目標の小節数は正の値である必要があります", "The target length in bars must be positive"),
    ("atempo に 0 以下は指定できません", "atempo must be greater than 0"),
    # Node のブリッジ（作者の手元だけの控えの接続）
    ("node が見つかりません。Homebrew で入れるか、環境変数 PTSL_NODE に実行ファイルのパスを"
     "設定してください",
     "node was not found. Install it with Homebrew, or set the environment "
     "variable PTSL_NODE to its path"),
    ("。Pro Tools が起動しているか確認してください（PTSL サーバーは Pro Tools のプロセス内に"
     "あります）",
     ". Check that Pro Tools is running (the PTSL server runs inside the "
     "Pro Tools process)"),
    ("Pro Tools が起動しているか確認してください", "Check that Pro Tools is running"),
    (" 秒待っても応答がありません。Pro Tools がダイアログを出して止まっていないか"
     "確認してください",
     " seconds without a response. Check that Pro Tools is not waiting on "
     "a dialog"),
    ("ブリッジが見つかりません: ", "The Node bridge was not found: "),
    ("ブリッジが応答しませんでした。", "The Node bridge did not respond. "),
    ("ブリッジへ書き込めません: ", "Cannot write to the Node bridge: "),
    ("ブリッジからの応答が途切れました。", "The response from the Node bridge was cut off. "),
    ("Pro Tools につながりませんでした: ", "Could not connect to Pro Tools: "),
    ("Pro Tools が落ちた可能性があります", "Pro Tools may have quit"),
    ("理由は返っていません", "No reason was returned"),
    ("理由不明", "unknown reason"),
    ("、", ", "),
)


def has_parts(base: str | Path) -> bool:
    """本物の部品のフォルダか（同名の空フォルダや古い場所を掴まない）。"""
    return (Path(base) / "ptcore" / "splicename.py").exists()


def _dev_dir() -> Path | None:
    """作者の手元の隣のフォルダ。設定が無い・読めなければ None。"""
    try:
        data = json.loads(DEV_DIRS_FILE.read_text(encoding="utf-8"))
        return PROJECT_ROOT.parent / str(data[PART_NAME])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def parts_path() -> str:
    """部品のフォルダ。**無くても文字列を返す**（案内の文に載せるため）。"""
    override = os.environ.get(PARTS_ENV, "")
    for base in (override, BUNDLED, _dev_dir()):
        if base and has_parts(base):
            return str(base)
    return override or str(BUNDLED)


def add_ptcore_to_path(base: str) -> bool:
    """`<base>/ptcore` を sys.path に足す（部品はフラットに読む）。無ければ False。"""
    path = os.path.join(base, "ptcore")
    if not os.path.isdir(path):
        return False
    if path not in sys.path:
        sys.path.insert(0, path)
    return True


def part_message(text: str) -> str:
    """部品が返す文言を、いまの言語にする（表に無いファイル名などはそのまま残す）。"""
    if is_english():
        for ja, en in _PART_WORDS:
            text = text.replace(ja, en)
    return text
