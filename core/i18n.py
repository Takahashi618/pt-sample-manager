"""表示の言語（日本語・英語）。**Mac の言語の設定に合わせる。**

海外の利用者向けに、画面・メニュー・読み上げを英語でも出す。言語は起動したときに
1 回だけ決める（途中で変えるなら、アプリを開き直す）。

### 決め方

1. 環境変数 `SM_LANG` が `ja` か `en` なら、それ（テストと、試しに切り替えるとき用）
2. Mac の「優先する言語」（`NSLocale.preferredLanguages`）を上から見て、
   **日本語と英語のうち先に出てくるほう**（配布版は、システム設定の「言語と地域」→
   「アプリケーション」で、このアプリだけ言語を変えたときもこれに入る）
3. どちらも無ければ英語（日本語以外の Mac は英語で出す）

**.app の `CFBundleDevelopmentRegion` を en にしてあるのは 3 と揃えるため。** ずれると、
日本語も英語も無い Mac で、画面は英語なのに Finder の名前と許可の説明が日本語になる。

### 書き方

文言は**日本語と英語を並べて書く**（`tr("日本語", "English")`）。f 文字列も
そのまま渡せる。訳を別の表に分けないのは、組み立てる文が多く、並べたほうが
取り違えないため。

**ほかのアプリも借りて読む部品（`ui/announce.py`・`ui/sounds.py`・
`ui/wx_keys.py`・`ui/ax_label.py`・`audio/player.py`）はこのモジュールを読まない。**
読み込むと、借りている側で読めなくなる。
"""

from __future__ import annotations

import os

from core.version import BUNDLE_NAME, ENGLISH_NAME

ENV_NAME = "SM_LANG"
SUPPORTED = ("ja", "en")
# OS のロケールの書き方（Windows は "Japanese_Japan"）
_LOCALE_NAMES = {"japanese": "ja", "english": "en"}


def preferred_languages() -> list[str]:
    """Mac の「優先する言語」（例: ["ja-JP", "en-JP"]）。

    読めなければ環境変数 LANG、それも無ければ OS のロケールから 1 つ。
    """
    try:
        from Foundation import NSLocale

        langs = [str(x) for x in (NSLocale.preferredLanguages() or [])]
        if langs:
            return langs
    except Exception:
        pass
    env = os.environ.get("LANG", "")
    if env:
        return [env]
    try:
        import locale

        name = locale.getlocale()[0]
    except Exception:
        return []
    return [name] if name else []


def pick_language(langs: list[str]) -> str:
    """並びの上から、日本語か英語の先に出てくるほう。どちらも無ければ英語。"""
    for lang in langs:
        head = lang.strip().lower().replace("_", "-").split("-")[0]
        head = _LOCALE_NAMES.get(head, head)
        if head in SUPPORTED:
            return head
    return "en"


def detect_language() -> str:
    forced = os.environ.get(ENV_NAME, "").strip().lower()
    if forced in SUPPORTED:
        return forced
    return pick_language(preferred_languages())


LANG = detect_language()


def is_english() -> bool:
    return LANG == "en"


def tr(ja, en):
    """いまの言語の文言（文言以外の組にも使える）。"""
    return en if LANG == "en" else ja


APP_NAME = tr(BUNDLE_NAME, ENGLISH_NAME)
# 項目を並べるときの区切り
COMMA = tr("、", ", ")


def plural(count, word: str) -> str:
    """英語の数え方（"1 sample"・"3 samples"・"1.5 bars"）。日本語の文では使わない。"""
    shown = f"{count:g}" if isinstance(count, float) else str(count)
    return f"{shown} {word}" if count == 1 else f"{shown} {word}s"


def sentences(parts) -> str:
    """文を並べて 1 つの読み上げにする（空の文は飛ばす）。

    英語は文の終わりにピリオドが要る。無いと「The source was not removed 2 sources.」
    のように続けて読まれる。日本語は空白で区切るだけでよい。
    """
    cleaned = [p.strip() for p in parts if p and p.strip()]
    if is_english():
        cleaned = [p if p.endswith((".", "!", "?", "…")) else p + "."
                   for p in cleaned]
    return " ".join(cleaned)
