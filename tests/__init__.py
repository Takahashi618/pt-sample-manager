"""テスト全体の前提。

**テストの最中に VoiceOver が読み出さないようにする。** 能動アナウンスは
アプリ自身から VoiceOver へ通知を送る（`ui/announce.py`）。テストで
ダイアログを組み立てると本物の通知が飛ぶので、環境変数で止める。
音（`SM_SILENT`）も止める。
"""

import os

os.environ.setdefault("SM_NO_ANNOUNCE", "1")
os.environ.setdefault("SM_SILENT", "1")
# テストは日本語の文言で書いてある（英語は tests/test_i18n.py が別のプロセスで見る）
os.environ["SM_LANG"] = "ja"
