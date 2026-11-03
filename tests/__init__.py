"""テスト全体の前提。

**テストの最中に VoiceOver が読み出さないようにする。** 能動アナウンスは
アプリ自身から VoiceOver へ通知を送る（`ui/announce.py`）。テストで
ダイアログを組み立てると本物の通知が飛ぶので、環境変数で止める。
音（`SM_SILENT`）も止める。

**利用者のクリップボードに触らない。** パスのコピーは `pbcopy` を動かすので、
偽物を PATH の先頭に置く（別のプロセスの tests/en_probe.py も PATH を引き継ぐ）。
これが無かった間は、テストとビルドのたびに、利用者のコピーした内容がテストの
パスに書き換わっていた（2026-11-03 に、作者の別のアプリで同じ事故を起こして調べて分かった）。
"""

import atexit
import os
import shutil
import tempfile

os.environ.setdefault("SM_NO_ANNOUNCE", "1")
os.environ.setdefault("SM_SILENT", "1")
# テストは日本語の文言で書いてある（英語は tests/test_i18n.py が別のプロセスで見る）
os.environ["SM_LANG"] = "ja"

FAKE_CLIPBOARD_DIR = tempfile.mkdtemp(prefix="sm_fake_clipboard_")
atexit.register(shutil.rmtree, FAKE_CLIPBOARD_DIR, ignore_errors=True)
for _name, _body in (("pbcopy", 'cat > "$(dirname "$0")/clip"'),
                     ("pbpaste", 'cat "$(dirname "$0")/clip"')):
    _path = os.path.join(FAKE_CLIPBOARD_DIR, _name)
    with open(_path, "w", encoding="utf-8") as _f:
        _f.write(f"#!/bin/sh\n{_body}\n")
    os.chmod(_path, 0o755)
os.environ["PATH"] = (FAKE_CLIPBOARD_DIR + os.pathsep
                      + os.environ.get("PATH", ""))
