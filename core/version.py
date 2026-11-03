"""PTサンプルマネージャの版と名前。**ここ 1 か所で決める。**

使うところ: `.app` の Info.plist、配布物の名前
（`PTサンプルマネージャ_<版>.zip`）、
マニュアルの版の表記（`tools/doc_parts.py` が合わせる）。

- 1.1.0（2026-11-02）: 画面・読み上げ・マニュアルを英語でも出す
- 1.0.0（2026-11-02）: 初めての配布版
"""

VERSION = "1.1.1"
# .app と配布物のフォルダの本当の名前（英語の Mac でも変えない。
# Finder での見え方は resources/*.lproj で決まる）
BUNDLE_NAME = "PTサンプルマネージャ"
ENGLISH_NAME = "PT Sample Manager"
