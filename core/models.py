"""ドメインモデル定義。

OS・フレームワーク・DBに非依存の純粋なデータ構造。
ここは GUI も Pro Tools も無しで単体テストできる core 層の中核。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath

from core.i18n import COMMA, tr

# 読み上げの語と書式。**起動時に 1 回だけ選ぶ**（検索の結果は 1 行ずつ、
# 数千回組み立てる）
_FAVORITE = tr("お気に入り", "favorite")
_PACK = tr("パック: ", "pack: ")
_TAGS = tr("タグ: ", "tags: ")
_LENGTH = tr("長さ{:.1f}秒", "length {:.1f} seconds")
_NO_LENGTH = tr("長さ不明", "length unknown")
_RATE = tr("サンプルレート{:.1f}キロヘルツ", "sample rate {:.1f} kilohertz")
_BITS = tr("{}ビット", "{} bit")
_MONO_STEREO = {1: tr("モノラル", "Mono"), 2: tr("ステレオ", "Stereo")}
_CHANNELS = tr("{}チャンネル", "{} channels")


def channel_label(channels: int) -> str:
    """チャンネル数の読み上げ（モノラル・ステレオ・N チャンネル）。"""
    return _MONO_STEREO.get(channels) or _CHANNELS.format(channels)


class SampleKind(enum.Enum):
    """サンプルの種別。ワンショットかループか。

    値は SQLite に文字列として保存するため、安定した文字列にしておく。
    """

    ONESHOT = "oneshot"
    LOOP = "loop"
    UNKNOWN = "unknown"

    @property
    def label_ja(self) -> str:
        """スクリーンリーダー向けのラベル。"""
        return _KIND_LABELS[self]


_KIND_LABELS = {
    SampleKind.ONESHOT: tr("ワンショット", "One-shot"),
    SampleKind.LOOP: tr("ループ", "Loop"),
    SampleKind.UNKNOWN: tr("種別不明", "Type unknown"),
}


@dataclass(frozen=True)
class AudioMetadata:
    """オーディオファイルの技術的メタデータ。

    metadata.py の解析結果を格納する。長さが取れない場合は
    duration_sec を None にし、UI 側で「長さ不明」と読み上げる。
    """

    duration_sec: float | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None
    channels: int | None = None

    def describe_ja(self) -> str:
        """スクリーンリーダー向けのサマリを返す。

        視覚情報に頼らず、すべてテキストで状態を伝えるための関数。
        """
        parts: list[str] = []
        if self.duration_sec is not None:
            parts.append(_LENGTH.format(self.duration_sec))
        else:
            parts.append(_NO_LENGTH)
        if self.sample_rate is not None:
            parts.append(_RATE.format(self.sample_rate / 1000))
        if self.bit_depth is not None:
            parts.append(_BITS.format(self.bit_depth))
        if self.channels is not None:
            parts.append(channel_label(self.channels))
        return COMMA.join(parts)


@dataclass
class Tag:
    """サンプルに付与するタグ。"""

    name: str
    id: int | None = None  # 未保存なら None


@dataclass
class Category:
    """サンプルのカテゴリ。今回はフラット構造。"""

    name: str
    id: int | None = None


class SourceKind(enum.Enum):
    """読み取り先フォルダの種類。

    Splice は `packs/<パック名>/…` という決まった構造を持つので、
    スキャン時にパック名を拾える。それ以外は素のフォルダとして扱う。
    """

    SPLICE = "splice"
    GENERIC = "generic"

    @property
    def label_ja(self) -> str:
        return {
            SourceKind.SPLICE: "Splice",
            SourceKind.GENERIC: tr("通常のフォルダ", "Regular folder"),
        }[self]


@dataclass
class Source:
    """サンプルの読み取り先フォルダ。

    複数のフォルダを登録しておき、検索時に切り替えて表示する
    （Splice のライブラリと自前の素材フォルダを分けて見るため）。
    """

    name: str
    root: str
    kind: SourceKind = SourceKind.GENERIC
    id: int | None = None

    def describe_ja(self) -> str:
        return tr(f"{self.name}（{self.kind.label_ja}）: {self.root}",
                  f"{self.name} ({self.kind.label_ja}): {self.root}")


@dataclass
class Sample:
    """管理対象のオーディオサンプル1件。

    path は絶対パスを保持する。filename は path から導出するが、
    検索の利便のため明示フィールドとして持つ。
    """

    path: str
    filename: str
    file_format: str  # 拡張子（小文字、ドットなし）例: "wav"
    kind: SampleKind = SampleKind.UNKNOWN
    metadata: AudioMetadata = field(default_factory=AudioMetadata)
    tags: list[Tag] = field(default_factory=list)
    category: Category | None = None
    source_id: int | None = None   # どの読み取り先から来たか
    pack: str = ""                 # Splice のパック名。それ以外は空
    # よく使う素材の目印。**これだけを絞り込んで出せる。**
    # 数千件の中から前に使ったものを探し直すのは手間なので
    favorite: bool = False
    # ファイル名から読んだ音楽情報。**DB には保存しない**（都度読む）。
    # 解析そのものは借りている部品（ptcore）が持つので、ここは値を受け取るだけ
    bpm: int | None = None
    key_label: str = ""            # "Aマイナー" のような読み上げ向け表記
    added_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    id: int | None = None

    @staticmethod
    def derive_filename(path: str) -> str:
        """パス文字列からファイル名部分を取り出す。

        Windows で macOS 由来のパス（/区切り）も扱えるよう、両方の
        セパレータに対応する。逆も同様。OS非依存にするための明示処理。
        """
        # Windows形式・POSIX形式どちらでも末尾要素を取得できるよう両方試す
        win_name = PureWindowsPath(path).name
        posix_name = PurePosixPath(path).name
        # より短い方（=実際に分割できた方）を採用する
        return win_name if len(win_name) <= len(posix_name) else posix_name

    @staticmethod
    def derive_format(filename: str) -> str:
        """ファイル名から拡張子（小文字・ドットなし）を取り出す。"""
        suffix = PurePosixPath(filename).suffix
        return suffix.lstrip(".").lower()

    def describe_ja(self) -> str:
        """結果リストで読み上げる1行サマリ。

        ファイル名・種別・主要メタデータを、視覚に頼らず伝える。
        """
        # **お気に入りは名前のすぐ後に置く。** 一覧を矢印で辿るときに
        # 真っ先に知りたい情報で、後ろだと聞き終える前に次へ進んでしまう
        parts = [self.filename]
        if self.favorite:
            parts.append(_FAVORITE)
        parts.append(self.kind.label_ja)
        # **音楽情報は前のほうに置く。** 素材を選ぶときに真っ先に要る
        # 情報なので、長さや形式より先に読み上げられるようにする
        if self.bpm is not None:
            parts.append(f"{self.bpm} BPM")
        if self.key_label:
            parts.append(self.key_label)
        parts.append(self.metadata.describe_ja())
        if self.pack:
            parts.append(_PACK + self.pack)
        if self.tags:
            parts.append(_TAGS + COMMA.join(t.name for t in self.tags))
        return COMMA.join(p for p in parts if p)
