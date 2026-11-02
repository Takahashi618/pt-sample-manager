# PTサンプルマネージャ（PT Sample Manager）

Splice や自分のフォルダのオーディオサンプルを、検索して、試聴して、Pro Tools のトラックへ置く macOS のアプリです。
置く前に、素材をセッションのテンポとキーに合わせて加工することもできます。
VoiceOver とキーボードだけで使えるように作っています。名前の「PT」は Pro Tools のことです。
画面と読み上げは、Mac の言語の設定に合わせて日本語か英語で出ます。

- 動く環境: Apple シリコン（M1 以降）の Mac、macOS 15 以降、Pro Tools 2025.6 以降、VoiceOver
- 使うだけなら: [Releases](https://github.com/Takahashi618/pt-sample-manager/releases) にある `PTSampleManager_1.1.0.zip`（表示名は PTサンプルマネージャ_1.1.0.zip）を開き、中の「はじめにお読みください.txt」を読んでください
- 使い方: [docs/manual.txt](docs/manual.txt)（HTML 版は [docs/manual.html](docs/manual.html)。ダウンロードしてブラウザで開きます）
- ライセンス: MIT ライセンス（Copyright (c) 2026 Takahashi）。全文は [LICENSE.txt](LICENSE.txt)。
  配布版のアプリに入っているほかの人の部品の条件は [同梱部品のライセンス.txt](同梱部品のライセンス.txt)
- このソフトウェアは、作者（Takahashi）の指示と判断のもとで、AI（Anthropic 社の Claude）を使って作りました。
  プログラムのコードと文章の大部分は AI が書いたものです
- Pro Tools は Avid Technology 社の、Splice は Splice 社の製品です。このアプリは、それらの会社とは関係ありません

## ソースから動かす

Python 3.14 と、[requirements.txt](requirements.txt) の部品を入れます。テンポとキーを合わせる機能には ffmpeg も要ります
（`brew install ffmpeg`。`brew install rubberband` もあれば音質が上がります）。

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

- テスト: `PYTHONPATH=. .venv/bin/python -m pytest tests/ -q`（Pro Tools には触りません）
- 配布版（zip）を作る: `PYTHONPATH=. .venv/bin/python tools/build_release.py`
  （[python.org](https://www.python.org/) の Python 3.14 に、PyInstaller と requirements.txt の部品を入れておきます。grpcio は組み立て用の環境に自動で入ります）
- マニュアルを直したら: `PYTHONPATH=. .venv/bin/python tools/doc_parts.py`（キーの一覧などを作り直し、txt も作ります）

### 中身

- `app.py`・`core/`・`ui/`・`audio/`・`integration/`: アプリ本体（wxPython）
- `core/i18n.py`: 表示の言語（Mac の言語の設定に合わせて日本語か英語。文言は `tr("日本語", "English")` と並べて書いています）
- `integration/ptsl_grpc.py`: Pro Tools（PTSL）へ grpcio で直接つなぐ部分。Avid の SDK は要りません
- `siblings/placement_core/`: 作者の別のプロジェクトから借りている部品の写し（素材の名前の解析、加工、セッション情報の読み取り）。コメントを除いた形で入れています
- `docs/`: マニュアルと「はじめにお読みください」の元
- コメントには、開発中に実機で測って分かったことを日付つきで残しています

## English

PT Sample Manager is a macOS app for finding, auditioning, and placing audio samples (from Splice or your own folders)
on Pro Tools tracks. It can also match a sample to the session tempo and key before placing it.
It is designed to be used with VoiceOver and the keyboard only. "PT" stands for Pro Tools.

The app's window and speech follow your Mac's language settings: English or Japanese.

- Requirements: a Mac with Apple silicon (M1 or later), macOS 15 or later, Pro Tools 2025.6 or later, VoiceOver
- To use it: open `PTSampleManager_1.1.0.zip` from [Releases](https://github.com/Takahashi618/pt-sample-manager/releases) and read “README (English).txt” inside
- Manual: [docs/manual_en.txt](docs/manual_en.txt) (HTML version: [docs/manual_en.html](docs/manual_en.html))
- License: MIT License (Copyright (c) 2026 Takahashi). Full text in [LICENSE.txt](LICENSE.txt).
  Third-party components in the packaged app: [同梱部品のライセンス.txt](同梱部品のライセンス.txt)
- This software was made by the author (Takahashi) using AI (Claude by Anthropic), under the author's direction and judgment.
  Most of the code and text was written by the AI
- Pro Tools is a product of Avid Technology and Splice is a product of Splice. This app is not affiliated with those companies

### Running from source

Install Python 3.14 and [requirements.txt](requirements.txt), then run `app.py` (see the commands above). Matching tempo and key
needs ffmpeg (`brew install ffmpeg`; `brew install rubberband` improves the sound). Code comments are mostly in Japanese.
Tests: `PYTHONPATH=. .venv/bin/python -m pytest tests/ -q` Packaging: `PYTHONPATH=. .venv/bin/python tools/build_release.py`
(install PyInstaller and requirements.txt into the python.org Python 3.14 first)
