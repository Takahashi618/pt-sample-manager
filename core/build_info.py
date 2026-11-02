"""ビルド情報の読み取り（`PTサンプルマネージャ.app` の Info.plist から）。

**「今動いているのは最新版か」を利用者が自分で確かめられるようにする。**
このアプリは凍結ビルドをしないため、`.app` を見ただけでは中身がいつの
ものか分からない。そこで作者の手元のビルドの道具が、ビルドのたびに Info.plist へ
日時とコミットを書き込み、ここでそれを読んでヘルプメニューから読み上げる。

**動いているコードは常に最新**（ランチャがソースを直接実行する）。
ビルド情報が古いのは「検証を通していない」という意味であって、
「古いコードが動いている」という意味ではない。文言もそう区別する。

Info.plist が無い・壊れていても例外は投げない（起動を止めないため）。
wx にも PTSL にも依存しないので単体テストできる。

### 配布版（2026-10-24 から）

配布版は PyInstaller で固めた .app で、**中身は組み立てた時点で決まる**
（ランチャではない）。`tools/build_release.py` が Info.plist に `SMFlavor`
（「配布版」）を入れる。配布版では「ソースが更新されています」の判定を
しない（ソースが無い）代わりに、AI で作ったことを添える。
"""

from __future__ import annotations

import plistlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.i18n import APP_NAME, COMMA, tr

# ビルドの道具が書き込むキー。CFBundle 系は macOS 標準のもの
KEY_VERSION = "CFBundleShortVersionString"
KEY_BUILD = "CFBundleVersion"
KEY_BUILT_AT = "SMBuildDate"        # 読み上げ用の文字列
KEY_BUILT_STAMP = "SMBuildTimestamp"  # 比較用の UNIX 時刻
KEY_COMMIT = "SMGitCommit"
KEY_FLAVOR = "SMFlavor"             # 配布版なら下の印。手元の版には無い

FLAVOR_DISTRIBUTION = "distribution"

# 読み上げの頭
_HEAD = tr("[情報] ", "[Info] ") + APP_NAME

# 配布版のバージョン情報に添える文（マニュアル・ライセンスと同じ内容）
AI_NOTICE = tr("このソフトウェアは、作者 Takahashi の指示と判断のもとで、AI（Anthropic 社の Claude）"
               "を使って作りました。",
               "This software was made with AI (Anthropic's Claude) under "
               "the direction and decisions of the author, Takahashi. ")

# ビルドの新しさを比べる対象。生成物や設定は見ない
SOURCE_DIRS = ("core", "audio", "ui", "integration")
SOURCE_FILES = ("app.py",)


@dataclass(frozen=True)
class BuildInfo:
    """`.app` に記録されたビルドの情報。"""

    version: str = ""
    build: str = ""
    built_at: str = ""
    built_stamp: float = 0.0
    commit: str = ""
    flavor: str = ""

    @property
    def known(self) -> bool:
        """ビルド情報が記録されているか。"""
        return bool(self.built_at or self.built_stamp)

    @property
    def distribution(self) -> bool:
        """配布版（PyInstaller で固めた .app）か。"""
        return self.flavor == FLAVOR_DISTRIBUTION


def read_build_info(app_bundle: str | Path) -> BuildInfo:
    """`.app` の Info.plist からビルド情報を読む。

    読めなければ空の BuildInfo を返す（例外は投げない）。
    """
    plist = Path(app_bundle) / "Contents" / "Info.plist"
    try:
        with open(plist, "rb") as f:
            data = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return BuildInfo()
    if not isinstance(data, dict):
        return BuildInfo()

    stamp = data.get(KEY_BUILT_STAMP)
    try:
        stamp_value = float(stamp)
    except (TypeError, ValueError):
        stamp_value = 0.0

    return BuildInfo(
        version=str(data.get(KEY_VERSION) or ""),
        build=str(data.get(KEY_BUILD) or ""),
        built_at=str(data.get(KEY_BUILT_AT) or ""),
        built_stamp=stamp_value,
        commit=str(data.get(KEY_COMMIT) or ""),
        flavor=str(data.get(KEY_FLAVOR) or ""),
    )


def latest_source_mtime(root: str | Path) -> float:
    """ソースの中でいちばん新しい更新日時を返す。

    ビルド後にコードを直したかどうかの判定に使う。
    読めないファイルは飛ばす（判定を止めない）。
    """
    base = Path(root)
    newest = 0.0
    paths = [base / name for name in SOURCE_FILES]
    for folder in SOURCE_DIRS:
        paths.extend((base / folder).rglob("*.py"))
    for path in paths:
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def is_stale(info: BuildInfo, root: str | Path) -> bool:
    """ビルド後にソースが更新されているか。

    ビルド情報が無い場合も「確かめていない」として真を返す。
    """
    if not info.known:
        return True
    return latest_source_mtime(root) > info.built_stamp


def describe_ja(info: BuildInfo, root: str | Path) -> str:
    """バージョン情報の読み上げ文言を組み立てる。

    **「動作が古い」と「検証が古い」を混同させない。** ランチャは
    いつでも最新のソースを起動するので、動くコードは常に最新。
    ビルドが古いのは検証を通していないという意味でしかない。
    """
    if info.distribution:
        return describe_distribution_ja(info)
    if not info.known:
        return tr(f"{_HEAD}。ビルド情報がありません。最新のソースで動作します。",
                  f"{_HEAD}. There is no build information. It runs from the "
                  "latest source.")

    parts = [_HEAD]
    if info.version:
        parts.append(tr(f"バージョン {info.version}", f"version {info.version}"))
    parts.append(tr(f"ビルド {info.built_at}", f"build {info.built_at}"))
    if info.commit:
        parts.append(tr(f"コミット {info.commit}", f"commit {info.commit}"))
    head = COMMA.join(parts)

    if is_stale(info, root):
        tail = tr("ビルドのあとにソースが更新されています。動作は最新ですが、検証は済んでいません。",
                  "The source has changed since the build. It runs the "
                  "latest code, but that code has not been verified.")
    else:
        tail = tr("ソースはビルド時点のままです。",
                  "The source has not changed since the build.")
    return tr(f"{head}。{tail}", f"{head}. {tail}")


def describe_distribution_ja(info: BuildInfo) -> str:
    """配布版のバージョン情報。版・組み立てた日時・コミットと、AI で作った旨。

    配布版は組み立てた時点のまま変わらないので、新しさの判定はしない。
    """
    parts = [tr(f"{_HEAD} 配布版", f"{_HEAD}, packaged version")]
    if info.version:
        parts.append(tr(f"バージョン {info.version}", f"version {info.version}"))
    if info.built_at:
        parts.append(tr(f"作成 {info.built_at}", f"built {info.built_at}"))
    if info.commit:
        parts.append(tr(f"コミット {info.commit}", f"commit {info.commit}"))
    joined = COMMA.join(parts)
    return tr(f"{joined}。{AI_NOTICE}ライセンスは MIT ライセンスです（Copyright "
              "(c) 2026 Takahashi）。",
              f"{joined}. {AI_NOTICE}It is released under the MIT "
              "License (Copyright (c) 2026 Takahashi).")


def format_build_number(when: datetime) -> str:
    """ビルド番号（CFBundleVersion）を日時から作る。

    **必ず増える形にする。** macOS はこの値でアプリの新旧を判断する。
    """
    return when.strftime("%Y.%m%d.%H%M")
