"""フォルダ走査とサンプル自動登録。

指定フォルダを再帰的に走査し、対応形式のオーディオファイルを
検出して metadata 解析のうえ Library に登録する。

設計方針:
- 登録は冪等（Library.add_sample が UNIQUE(path) で重複を防ぐ）。
  再スキャンしても増殖しないので、フォルダ更新時に何度でも回せる。
- 1ファイルの解析失敗でスキャン全体を止めない（errors に記録して継続）。
- 種別（ワンショット/ループ）はこの段階では推定に留め、確定は手動。
  ファイル名に loop が含まれれば LOOP、それ以外は ONESHOT を初期値とする。
  （ユーザー方針: スキャン後に手動でタグ付け・種別補正する）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .library import Library, fold_text
from .metadata import extract_metadata
from .models import Sample, SampleKind, Source, SourceKind

from core.i18n import tr, plural

# 対応する拡張子（ドットなし・小文字）
SUPPORTED_FORMATS = {"wav", "aif", "aiff", "aifc", "flac", "mp3", "ogg"}

# ファイル名からループ素材を推定するためのヒント語
_LOOP_HINTS = ("loop", "ループ", "_lp", "groove", "beat")

# Splice のライブラリは packs/<パック名>/… という構造になっている。
# この階層を見つけたら、その次の階層名をパック名として拾う
PACKS_DIR = "packs"


@dataclass
class ScanResult:
    """スキャン結果のサマリ。スクリーンリーダー向けに件数を読み上げる。"""

    added: int = 0
    skipped: int = 0  # 既登録のため登録せず
    linked: int = 0   # 既登録だが読み取り先を後から結びつけた
    errors: list[tuple[str, str]] = field(default_factory=list)  # (path, 理由)

    def describe_ja(self) -> str:
        """読み上げ文言。**失敗があれば先頭で分かるようにする。**

        通知音は文言の先頭（[警告] か否か）で決まる（`ui/sounds.py`）。
        接頭辞が無いと、フォルダが無くて 0 件でも成功の音が鳴る。
        最初の失敗の理由も添える（件数だけでは直しようがない）。
        """
        msg = tr(f"{self.added}件を登録しました。",
                 f"Added {plural(self.added, 'sample')}. ")
        if self.linked:
            msg += tr(f"{self.linked}件は読み取り先に結びつけました。",
                      f"Linked {plural(self.linked, 'sample')} to the "
                      "source. ")
        if self.skipped:
            msg += tr(f"{self.skipped}件は登録済みのため省略しました。",
                      f"Skipped {plural(self.skipped, 'sample')} already "
                      "added. ")
        if self.errors:
            reason = self.errors[0][1]
            msg = tr(f"[警告] {msg}{len(self.errors)}件は読み込みに失敗しました（{reason}）。",
                     f"[Warning] {msg}{plural(len(self.errors), 'file')} "
                     f"could not be read ({reason}).")
        return msg.strip()


def guess_kind(filename: str) -> SampleKind:
    """ファイル名から種別の初期値を推定する。確定は手動補正前提。

    **比べる前に形をそろえる**（`fold_text`）。Mac のアプリが作った
    ファイル名は濁点・半濁点を分けた形（NFD）なので、そのままでは
    「ループ」の「プ」が一致せず、ワンショットに分類されていた。
    """
    lower = fold_text(filename)
    if any(hint in lower for hint in _LOOP_HINTS):
        return SampleKind.LOOP
    return SampleKind.ONESHOT


def looks_like_splice(folder: str | Path) -> bool:
    """Splice のライブラリらしいフォルダかどうか。

    `packs` という階層があるかで見る。フォルダを登録するときの
    種類の初期値に使う（利用者が変えられる）。
    """
    root = Path(folder)
    if root.name == PACKS_DIR:
        return True
    return ((root / PACKS_DIR).is_dir()
            or (root / "sounds" / PACKS_DIR).is_dir())


def extract_pack(file_path: str | Path, root: str | Path) -> str:
    """Splice のパス構造からパック名を取り出す。

    `…/packs/<パック名>/…/音.wav` の <パック名> を返す。
    `packs` 階層が無ければ空文字（パックなし）。

    **パック名の直下にあるファイルも拾える。** Splice には
    `packs/SAUCE.WAV` のようにパック階層を持たないものが混ざっている。
    """
    try:
        relative = Path(file_path).relative_to(Path(root))
    except ValueError:
        relative = Path(file_path)

    parts = relative.parts
    for i, part in enumerate(parts):
        if part.lower() != PACKS_DIR:
            continue
        # packs の次が最後の要素（＝ファイル名）ならパック無し
        if i + 2 <= len(parts) - 1:
            return parts[i + 1]
        return ""
    return ""


def iter_audio_files(folder: str | Path):
    """フォルダ配下の対応オーディオファイルを再帰的に列挙する。

    ジェネレータ。隠しファイル（先頭ドット）と macOS のリソース
    フォーク（._ 始まり）は除外する。
    """
    root = Path(folder)
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.name.startswith("."):  # 隠し・._AppleDouble を除外
            continue
        fmt = p.suffix.lstrip(".").lower()
        if fmt in SUPPORTED_FORMATS:
            yield p


def scan_folder(library: Library, folder: str | Path,
                source: Source | None = None) -> ScanResult:
    """フォルダを走査し、検出したサンプルを Library に登録する。

    `source` を渡すと、登録したサンプルにその読み取り先を紐づける。
    種類が Splice なら**パック名も記録する**（`packs/<パック名>/…`）。

    戻り値の ScanResult で登録・省略・失敗の件数を返す。
    """
    result = ScanResult()
    root = Path(folder)
    if not root.exists() or not root.is_dir():
        result.errors.append((str(folder), tr("フォルダが存在しません",
                                              "The folder does not exist")))
        return result

    source_id = source.id if source is not None else None
    want_pack = source is not None and source.kind is SourceKind.SPLICE

    for file_path in iter_audio_files(root):
        path_str = str(file_path.resolve())
        try:
            existing = library.get_sample_by_path(path_str)
            if existing is not None:
                # **登録済みでも、読み取り先が付いていなければ付ける。**
                # 読み取り先という考え方をあとから足したので、それ以前に
                # 登録したサンプルは所属が空のまま残っている
                if source_id is not None and existing.source_id is None:
                    pack = extract_pack(file_path, root) if want_pack else ""
                    library.attach_to_source(existing.id, source_id, pack)
                    result.linked += 1
                else:
                    result.skipped += 1
                continue

            filename = file_path.name
            sample = Sample(
                path=path_str,
                filename=filename,
                file_format=Sample.derive_format(filename),
                kind=guess_kind(filename),
                metadata=extract_metadata(file_path),
                source_id=source_id,
                pack=extract_pack(file_path, root) if want_pack else "",
            )
            library.add_sample(sample)
            result.added += 1
        except (OSError, ValueError) as exc:
            result.errors.append((path_str, str(exc)))

    return result
