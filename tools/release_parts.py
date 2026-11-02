"""配布版に同梱する借り物（作者の別のプロジェクトの部品）。**ここ 1 か所で決める。**

`tools/release.spec`（PyInstaller の設定）・`tools/build_release.py`・
`tests/test_release.py` などが読む。

部品は中立の名前で呼ぶ。探し方は `core/parts.py`。配布版には隣のフォルダが無いので、
.app の中の `siblings/` に**同じ並びで**同梱し、起動時に `PLACEMENT_CORE_PATH` で
その場所を教える（`app._frozen_setup`）。探し方が変わるだけで、読み込み方は同じ。
"""

from __future__ import annotations

from pathlib import Path

from core.parts import PART_NAME, has_parts, parts_path

# integration/ が読む部品と、それらが読む部品（足りなければ tests/test_release.py が
# 気づく）。加工: splicename・prep（library を読む）・dsp・audiofile、
# 位置: placement・timeline、配置の確認: sessioninfo。
# **`__init__.py` は入れない**（使わないモジュールまで読む）。無くてもファイルを直接読める
PART_FILES = tuple(f"ptcore/{name}.py" for name in (
    "audiofile", "dsp", "library", "placement", "prep", "sessioninfo",
    "splicename", "timeline"))


def find_part() -> Path | None:
    """借りている部品の場所。見つからなければ None。"""
    base = Path(parts_path())
    return base if has_parts(base) else None


def part_files() -> list[tuple[Path, str]]:
    """同梱する部品の (元のファイル, siblings/ の下の場所)。足りなければ ValueError。"""
    base = find_part()
    if base is None:
        raise ValueError(f"借りている部品が見つかりません: {PART_NAME}")
    files = []
    for rel in PART_FILES:
        path = base / rel
        if not path.is_file():
            raise ValueError(f"借りている部品が見つかりません: {PART_NAME}/{rel}")
        files.append((path, f"{PART_NAME}/{rel}"))
    return files
