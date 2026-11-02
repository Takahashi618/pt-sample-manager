"""ライブラリ管理（SQLite 永続化・検索・絞り込み）。

主機能であるタグ・カテゴリ検索を高速に行うため SQLite を採用。
スキーマにインデックスを張り、数万件規模でも絞り込みが効くようにする。

設計方針:
- sqlite3 は標準ライブラリ。追加依存なし。
- パスの一意性を UNIQUE 制約で担保（同一ファイルの二重登録を防ぐ）。
- 検索は AND 条件（テキスト・種別・カテゴリ・タグ集合）で合成する。
- 接続は1インスタンス1接続。row_factory で辞書風アクセス。
"""

from __future__ import annotations

import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    AudioMetadata,
    Category,
    Sample,
    SampleKind,
    Source,
    SourceKind,
    Tag,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    root TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'generic'
);

CREATE TABLE IF NOT EXISTS categories (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,
    filename    TEXT NOT NULL,
    file_format TEXT NOT NULL,
    kind        TEXT NOT NULL,
    duration_sec REAL,
    sample_rate  INTEGER,
    bit_depth    INTEGER,
    channels     INTEGER,
    category_id  INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    source_id    INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    pack         TEXT NOT NULL DEFAULT '',
    added_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sample_tags (
    sample_id INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    tag_id    INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (sample_id, tag_id)
);
"""

# **インデックスはマイグレーションのあとで作る。** 既存 DB では
# CREATE TABLE IF NOT EXISTS が何もしないため、あとから足した列
# （source_id / pack）がまだ無い状態でインデックスを張ろうとすると失敗する。
_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_samples_filename ON samples(filename);
CREATE INDEX IF NOT EXISTS idx_samples_kind     ON samples(kind);
CREATE INDEX IF NOT EXISTS idx_samples_category ON samples(category_id);
CREATE INDEX IF NOT EXISTS idx_samples_source   ON samples(source_id);
CREATE INDEX IF NOT EXISTS idx_samples_pack     ON samples(pack);
CREATE INDEX IF NOT EXISTS idx_samples_channels ON samples(channels);
CREATE INDEX IF NOT EXISTS idx_samples_favorite ON samples(favorite);
CREATE INDEX IF NOT EXISTS idx_sample_tags_tag  ON sample_tags(tag_id);
"""

# 既存 DB に後から足した列。**すでに使われている DB を壊さない**ために、
# CREATE TABLE ではなく ALTER TABLE で追加する。
# （列名, 定義）。存在すれば黙って飛ばす
_MIGRATIONS = (
    ("source_id", "INTEGER REFERENCES sources(id) ON DELETE SET NULL"),
    ("pack", "TEXT NOT NULL DEFAULT ''"),
    ("favorite", "INTEGER NOT NULL DEFAULT 0"),
)


class Library:
    """サンプルライブラリの永続化と検索を担うリポジトリ。

    使用例:
        lib = Library(":memory:")  # またはファイルパス
        lib.add_sample(sample)
        results = lib.search(text="kick", kind=SampleKind.ONESHOT)
        lib.close()
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        # スキャンをワーカースレッドで行う際、同じ DB ファイルへ別接続を
        # 張れるよう保存先を公開しておく（sqlite3 接続はスレッド間共有不可）
        self.db_path = str(db_path)
        # detect_types は使わず、日時は ISO 文字列で明示的に扱う
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        # 検索でファイル名を比べるための関数（理由は fold_text を参照）
        self._conn.create_function("sm_fold", 1, _fold_sql,
                                   deterministic=True)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()          # インデックスより先。理由は _INDEXES を参照
        self._conn.executescript(_INDEXES)
        self._conn.commit()

    def _migrate(self) -> None:
        """後から足した列を既存 DB に追加する。

        **すでに使われている DB を作り直さない。** 列が無ければ足すだけで、
        中身はそのまま残る。既に足してあれば何もしない。
        """
        cur = self._conn.execute("PRAGMA table_info(samples)")
        existing = {row["name"] for row in cur.fetchall()}
        for column, definition in _MIGRATIONS:
            if column in existing:
                continue
            self._conn.execute(
                f"ALTER TABLE samples ADD COLUMN {column} {definition}"
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Library":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # カテゴリ / タグ
    # ------------------------------------------------------------------
    def get_or_create_category(self, name: str) -> Category:
        cur = self._conn.execute(
            "SELECT id, name FROM categories WHERE name = ?", (name,)
        )
        row = cur.fetchone()
        if row is not None:
            return Category(id=row["id"], name=row["name"])
        cur = self._conn.execute(
            "INSERT INTO categories(name) VALUES (?)", (name,)
        )
        self._conn.commit()
        return Category(id=cur.lastrowid, name=name)

    def get_or_create_tag(self, name: str) -> Tag:
        cur = self._conn.execute(
            "SELECT id, name FROM tags WHERE name = ?", (name,)
        )
        row = cur.fetchone()
        if row is not None:
            return Tag(id=row["id"], name=row["name"])
        cur = self._conn.execute("INSERT INTO tags(name) VALUES (?)", (name,))
        self._conn.commit()
        return Tag(id=cur.lastrowid, name=name)

    def list_categories(self) -> list[Category]:
        cur = self._conn.execute(
            "SELECT id, name FROM categories ORDER BY name"
        )
        return [Category(id=r["id"], name=r["name"]) for r in cur.fetchall()]

    def list_tags(self) -> list[Tag]:
        cur = self._conn.execute("SELECT id, name FROM tags ORDER BY name")
        return [Tag(id=r["id"], name=r["name"]) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # 読み取り先フォルダ（ソース）
    # ------------------------------------------------------------------
    def add_source(self, source: Source) -> Source:
        """読み取り先を登録する。同じ名前かパスが既にあればそれを返す。"""
        existing = self.get_source_by_root(source.root)
        if existing is not None:
            return existing
        cur = self._conn.execute(
            "SELECT id, name, root, kind FROM sources WHERE name = ?",
            (source.name,),
        )
        row = cur.fetchone()
        if row is not None:
            return _row_to_source(row)

        cur = self._conn.execute(
            "INSERT INTO sources(name, root, kind) VALUES (?, ?, ?)",
            (source.name, source.root, source.kind.value),
        )
        self._conn.commit()
        source.id = cur.lastrowid
        return source

    def get_source_by_root(self, root: str) -> Source | None:
        cur = self._conn.execute(
            "SELECT id, name, root, kind FROM sources WHERE root = ?", (root,)
        )
        row = cur.fetchone()
        return _row_to_source(row) if row is not None else None

    def list_sources(self) -> list[Source]:
        """読み取り先の一覧。**登録した順**（id の順）。

        コマンドと数字の切り替え（2 から先が登録した順）がこの並びを使う。
        以前は名前の順で、キー一覧とマニュアルの「登録した順」と違っていた。
        名前の順だと、あとから「A…」の読み取り先を足しただけで、覚えた番号が
        全部ずれる。
        """
        cur = self._conn.execute(
            "SELECT id, name, root, kind FROM sources ORDER BY id"
        )
        return [_row_to_source(r) for r in cur.fetchall()]

    def remove_source(self, source_id: int,
                      delete_samples: bool = True) -> int:
        """読み取り先を削除する。戻り値は消えたサンプル数。

        `delete_samples` が偽なら、サンプルは残して所属だけ外す
        （フォルダを移しただけのときに使える）。
        """
        if delete_samples:
            cur = self._conn.execute(
                "DELETE FROM samples WHERE source_id = ?", (source_id,)
            )
            removed = cur.rowcount
        else:
            self._conn.execute(
                "UPDATE samples SET source_id = NULL WHERE source_id = ?",
                (source_id,),
            )
            removed = 0
        self._conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        self._conn.commit()
        return removed

    def attach_to_source(self, sample_id: int, source_id: int,
                         pack: str) -> None:
        """登録済みのサンプルに読み取り先とパックを結びつける。

        **再スキャンで既存のサンプルにも遡って付けるために要る。**
        読み取り先という考え方はあとから足したので、それ以前に
        登録したサンプルは所属が空のままになっている。
        """
        self._conn.execute(
            "UPDATE samples SET source_id = ?, pack = ? WHERE id = ?",
            (source_id, pack, sample_id),
        )
        self._conn.commit()

    def list_packs(self, source_id: int | None = None, **filters) -> list[str]:
        """パック名の一覧。Splice の読み取り先で使う。

        パックが付いていないサンプル（通常のフォルダ）は含めない。
        """
        return [name for name, _
                in self.list_packs_with_counts(source_id, **filters)]

    def list_packs_with_counts(
        self, source_id: int | None = None, **filters
    ) -> list[tuple[str, int]]:
        """パック名と、その中のサンプル数。

        **数が分かると選びやすい。** Splice は 1 ファイル単位で落とすので、
        1 件しか入っていないパックが大量にできる。

        `filters` に検索と同じ条件（kind・channels・favorite_only など）を
        渡すと、**その条件に合うサンプルだけを数える。** 渡さないと
        「選んでも 0 件」のパックが選択肢に並ぶ（実機で「ループのみ」に
        したとき 527 パック中 341 件がそうなっていた）。

        `pack` は無視する。**自分自身で絞ると 1 つしか出てこない。**
        """
        filters.pop("pack", None)
        body, params = self._filter_sql(source_id=source_id, **filters)

        # サブクエリにするのは、タグ指定時の GROUP BY s.id と
        # パックの GROUP BY がぶつからないようにするため
        sql = (
            "SELECT pack, COUNT(*) AS n FROM ("
            "SELECT s.id AS id, s.pack AS pack FROM samples s "
            f"{body}) AS m "
            "WHERE m.pack <> '' "
            # **中身の多い順に並べる。** 1 件だけのパックが大量にできるので、
            # 名前順だとその中に本命が埋もれる。同数なら名前順で安定させる
            "GROUP BY m.pack ORDER BY n DESC, m.pack"
        )
        cur = self._conn.execute(sql, params)
        return [(r["pack"], r["n"]) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # サンプル登録
    # ------------------------------------------------------------------
    def add_sample(self, sample: Sample) -> Sample:
        """サンプルを登録する。既存パスなら既存IDを返す（重複登録防止）。

        スキャンで同じファイルを再投入しても増殖しないよう、
        UNIQUE(path) を利用して冪等にする。
        """
        existing = self.get_sample_by_path(sample.path)
        if existing is not None:
            return existing

        category_id = None
        if sample.category is not None:
            cat = self.get_or_create_category(sample.category.name)
            category_id = cat.id

        added_at = sample.added_at.astimezone(timezone.utc).isoformat()
        cur = self._conn.execute(
            """
            INSERT INTO samples(
                path, filename, file_format, kind,
                duration_sec, sample_rate, bit_depth, channels,
                category_id, source_id, pack, added_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sample.path,
                sample.filename,
                sample.file_format,
                sample.kind.value,
                sample.metadata.duration_sec,
                sample.metadata.sample_rate,
                sample.metadata.bit_depth,
                sample.metadata.channels,
                category_id,
                sample.source_id,
                sample.pack,
                added_at,
            ),
        )
        sample_id = cur.lastrowid

        for tag in sample.tags:
            self._attach_tag(sample_id, tag.name)

        self._conn.commit()
        sample.id = sample_id
        return sample

    def _attach_tag(self, sample_id: int, tag_name: str) -> None:
        tag = self.get_or_create_tag(tag_name)
        self._conn.execute(
            "INSERT OR IGNORE INTO sample_tags(sample_id, tag_id) "
            "VALUES (?, ?)",
            (sample_id, tag.id),
        )

    def set_tags(self, sample_id: int, tag_names: list[str]) -> None:
        """サンプルのタグを与えられた集合に置き換える（手動タグ付け用）。"""
        self._conn.execute(
            "DELETE FROM sample_tags WHERE sample_id = ?", (sample_id,)
        )
        for name in tag_names:
            self._attach_tag(sample_id, name)
        self._conn.commit()

    def set_category(self, sample_id: int, category_name: str | None) -> None:
        category_id = None
        if category_name is not None:
            category_id = self.get_or_create_category(category_name).id
        self._conn.execute(
            "UPDATE samples SET category_id = ? WHERE id = ?",
            (category_id, sample_id),
        )
        self._conn.commit()

    def set_favorite(self, sample_id: int, value: bool) -> None:
        """お気に入りの目印を付け外しする。"""
        self._conn.execute(
            "UPDATE samples SET favorite = ? WHERE id = ?",
            (1 if value else 0, sample_id),
        )
        self._conn.commit()

    def count_favorites(self) -> int:
        """お気に入りの件数。読み上げで「何件あるか」を伝えるのに使う。"""
        cur = self._conn.execute(
            "SELECT COUNT(*) AS n FROM samples WHERE favorite = 1")
        return int(cur.fetchone()["n"])

    def set_kind(self, sample_id: int, kind: SampleKind) -> None:
        self._conn.execute(
            "UPDATE samples SET kind = ? WHERE id = ?",
            (kind.value, sample_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # 取得 / 検索
    # ------------------------------------------------------------------
    def get_sample_by_path(self, path: str) -> Sample | None:
        cur = self._conn.execute(
            "SELECT * FROM samples WHERE path = ?", (path,)
        )
        row = cur.fetchone()
        return self._row_to_sample(row) if row is not None else None

    def get_sample(self, sample_id: int) -> Sample | None:
        cur = self._conn.execute(
            "SELECT * FROM samples WHERE id = ?", (sample_id,)
        )
        row = cur.fetchone()
        return self._row_to_sample(row) if row is not None else None

    def count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) AS n FROM samples")
        return int(cur.fetchone()["n"])

    def _filter_sql(
        self,
        text: str | None = None,
        kind: SampleKind | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        channels: int | None = None,
        source_id: int | None = None,
        pack: str | None = None,
        favorite_only: bool = False,
    ) -> tuple[str, list]:
        """絞り込みの SQL（JOIN + WHERE + タグの HAVING）を組み立てる。

        **検索とパックの集計で必ず共用すること。** 別々に書くと条件が
        食い違い、「選んでも 0 件」のパックが選択肢に並ぶ
        （実機で 527 パック中 341 件がそうなっていた）。
        """
        clauses: list[str] = []
        params: list[object] = []
        joins: list[str] = []

        if text:
            # サンプル名は _ だらけ（AB_EN_Dabow_90_…）。エスケープしないと
            # LIKE が _ を「任意の 1 文字」と解釈し、検索が正確でなくなる。
            # 比べる前に両方を fold_text で揃える（日本語のファイル名と
            # 全角の入力のため）。エスケープは揃えたあと（全角の ％ が
            # 半角の % になるため）
            clauses.append("sm_fold(s.filename) LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(fold_text(text))}%")
        if kind is not None:
            clauses.append("s.kind = ?")
            params.append(kind.value)
        if category is not None:
            joins.append("JOIN categories c ON c.id = s.category_id")
            clauses.append("c.name = ?")
            params.append(category)
        if channels is not None:
            clauses.append("s.channels = ?")
            params.append(channels)
        if source_id is not None:
            clauses.append("s.source_id = ?")
            params.append(source_id)
        if pack:
            clauses.append("s.pack = ?")
            params.append(pack)
        if favorite_only:
            clauses.append("s.favorite = 1")
        if tags:
            # 指定タグをすべて持つ = タグ一致件数が指定数と等しい
            placeholders = ",".join("?" for _ in tags)
            joins.append(
                "JOIN sample_tags st ON st.sample_id = s.id "
                "JOIN tags t ON t.id = st.tag_id"
            )
            clauses.append(f"t.name IN ({placeholders})")
            params.extend(tags)

        sql = " ".join(joins)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if tags:
            sql += " GROUP BY s.id HAVING COUNT(DISTINCT t.name) = ?"
            params.append(len(tags))
        return sql, params

    def search(
        self,
        text: str | None = None,
        kind: SampleKind | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        channels: int | None = None,
        source_id: int | None = None,
        pack: str | None = None,
        favorite_only: bool = False,
        limit: int | None = None,
    ) -> list[Sample]:
        """AND 条件で絞り込む。

        - text:      ファイル名の部分一致（大文字小文字無視）
        - kind:      種別の完全一致（ワンショット / ループ）
        - category:  カテゴリ名の完全一致
        - tags:      指定タグを「すべて」持つもの（AND）
        - channels:  チャンネル数の完全一致（1 = モノラル、2 = ステレオ）
        - source_id: 読み取り先の完全一致
        - pack:      パック名の完全一致（Splice）
        - favorite_only: 真ならお気に入りだけ
        指定のない条件は無視される。
        """
        body, params = self._filter_sql(
            text=text, kind=kind, category=category, tags=tags,
            channels=channels, source_id=source_id, pack=pack,
            favorite_only=favorite_only)

        sql = "SELECT s.* FROM samples s " + body + " ORDER BY s.filename"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        cur = self._conn.execute(sql, params)
        return [self._row_to_sample(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # 内部: 行 → モデル変換
    # ------------------------------------------------------------------
    def _row_to_sample(self, row: sqlite3.Row) -> Sample:
        metadata = AudioMetadata(
            duration_sec=row["duration_sec"],
            sample_rate=row["sample_rate"],
            bit_depth=row["bit_depth"],
            channels=row["channels"],
        )
        category = None
        if row["category_id"] is not None:
            cur = self._conn.execute(
                "SELECT id, name FROM categories WHERE id = ?",
                (row["category_id"],),
            )
            crow = cur.fetchone()
            if crow is not None:
                category = Category(id=crow["id"], name=crow["name"])

        tags = self._tags_for_sample(row["id"])
        try:
            kind = SampleKind(row["kind"])
        except ValueError:
            kind = SampleKind.UNKNOWN

        return Sample(
            id=row["id"],
            path=row["path"],
            filename=row["filename"],
            file_format=row["file_format"],
            kind=kind,
            metadata=metadata,
            tags=tags,
            category=category,
            source_id=_column(row, "source_id"),
            pack=_column(row, "pack") or "",
            favorite=bool(_column(row, "favorite")),
            added_at=datetime.fromisoformat(row["added_at"]),
        )

    def _tags_for_sample(self, sample_id: int) -> list[Tag]:
        cur = self._conn.execute(
            """
            SELECT t.id, t.name FROM tags t
            JOIN sample_tags st ON st.tag_id = t.id
            WHERE st.sample_id = ?
            ORDER BY t.name
            """,
            (sample_id,),
        )
        return [Tag(id=r["id"], name=r["name"]) for r in cur.fetchall()]


def fold_text(text: str) -> str:
    """検索で比べる形にそろえる（互換の正規化 NFKC と、大文字小文字の畳み込み）。

    **Mac のアプリが作ったファイル名は、濁点を分けた形（NFD）で保存される。**
    「ドラム」が「ト」と濁点の 2 文字になっていて、日本語入力で打った
    「ドラム」（NFC）とは文字列として一致しない。以前は SQLite の LOWER
    （英字しか小文字にしない）で比べていたので、日本語のファイル名は
    検索で見つからなかった。

    NFKC にすると、全角の英数字（ｋｉｃｋ）や半角のカタカナも、
    ふつうの形と同じに扱える（日本語入力のまま打っても見つかる）。
    """
    return unicodedata.normalize("NFKC", text).casefold()


def _fold_sql(value) -> str:
    """SQLite に登録する版。NULL は空文字にする。"""
    return fold_text(value) if isinstance(value, str) else ""


def _escape_like(text: str) -> str:
    """LIKE のメタ文字（% _ \\）を文字どおりの意味に打ち消す。"""
    return (text.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_"))


def _column(row: sqlite3.Row, name: str):
    """行に列が無くても落ちないように読む。

    マイグレーション前の古い接続が残っている場合に備えた保険。
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _row_to_source(row: sqlite3.Row) -> Source:
    try:
        kind = SourceKind(row["kind"])
    except ValueError:
        kind = SourceKind.GENERIC
    return Source(
        id=row["id"], name=row["name"], root=row["root"], kind=kind
    )
