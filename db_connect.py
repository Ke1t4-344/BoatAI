#!/usr/bin/env python3
"""
db_connect.py — DB接続の統一エントリポイント

環境変数 TURSO_URL / TURSO_TOKEN が設定されていれば Turso に接続。
なければローカルの boatai.db にフォールバック（開発・ローカルテスト用）。

libsql_experimental は sqlite3.Row 互換の row_factory をサポートしないため、
_TursoConn / _TursoCursor / _DictRow ラッパーで互換インターフェースを提供する。
"""

import os
import sqlite3
from pathlib import Path

# .env ファイルを自動ロード
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

TURSO_URL   = os.environ.get("TURSO_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "")
DB_PATH     = Path(__file__).parent / "boatai.db"   # ローカルフォールバック用

USE_TURSO = bool(TURSO_URL and TURSO_TOKEN)


# ---------------------------------------------------------------------------
# libsql-experimental 互換ラッパー
# ---------------------------------------------------------------------------

class _DictRow:
    """
    sqlite3.Row 互換のラッパー（libsql-experimental 用）。
    row["col"]  → 文字列キーアクセス
    row[0]      → 数値インデックスアクセス
    dict(row)   → dict 変換
    iter(row)   → 値の順次列挙
    """
    __slots__ = ("_data", "_keys", "_vals")

    def __init__(self, cols: list, vals: tuple):
        self._keys = cols
        self._vals = tuple(vals)
        self._data = dict(zip(cols, vals))

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._data[key]
        return self._vals[key]

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def keys(self):
        return self._keys

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __repr__(self):
        return f"<_DictRow {self._data}>"


class _TursoCursor:
    """libsql Cursor ラッパー — fetchone/fetchall が _DictRow を返す。"""

    def __init__(self, cursor, use_dict_row: bool):
        self._cursor = cursor
        self._use_dict_row = use_dict_row

    @property
    def description(self):
        return self._cursor.description

    @property
    def lastrowid(self):
        return getattr(self._cursor, "lastrowid", None)

    @property
    def rowcount(self):
        return getattr(self._cursor, "rowcount", -1)

    def _cols(self):
        desc = self._cursor.description
        return [d[0] for d in desc] if desc else []

    def _wrap(self, row):
        if row is None or not self._use_dict_row:
            return row
        cols = self._cols()
        return _DictRow(cols, row) if cols else row

    def fetchone(self):
        return self._wrap(self._cursor.fetchone())

    def fetchall(self):
        if not self._use_dict_row:
            return self._cursor.fetchall()
        cols = self._cols()
        if cols:
            return [_DictRow(cols, r) for r in self._cursor.fetchall()]
        return self._cursor.fetchall()

    def __iter__(self):
        if not self._use_dict_row:
            yield from self._cursor
            return
        cols = self._cols()
        if cols:
            for row in self._cursor:
                yield _DictRow(cols, row)
        else:
            yield from self._cursor


class _TursoConn:
    """
    libsql.Connection ラッパー — sqlite3.Connection 互換インターフェースを提供。

    with conn: ... → 成功時 commit、例外時 rollback（close はしない）
    conn.close()   → 明示的にクローズ
    """

    def __init__(self, conn, use_dict_row: bool = True):
        self._conn = conn
        self._use_dict_row = use_dict_row

    # row_factory 属性: sqlite3 互換のため存在させる（値はダミー）
    @property
    def row_factory(self):
        return sqlite3.Row if self._use_dict_row else None

    @row_factory.setter
    def row_factory(self, value):
        self._use_dict_row = (value is not None)

    def execute(self, sql, params=()):
        # libsql_experimental はリスト不可・タプル必須なので変換
        if params:
            cursor = self._conn.execute(sql, tuple(params))
        else:
            cursor = self._conn.execute(sql)
        return _TursoCursor(cursor, self._use_dict_row)

    def executemany(self, sql, seq):
        return self._conn.executemany(sql, seq)

    def executescript(self, sql):
        return self._conn.executescript(sql)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        try:
            self._conn.rollback()
        except Exception:
            pass

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            try:
                self.commit()
            except Exception:
                pass
        else:
            self.rollback()
        return False  # 例外を伝播させる


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------

def open_db(row_factory=sqlite3.Row):
    """
    DB接続を返す（sqlite3 互換インターフェース）。

    TURSO_URL / TURSO_TOKEN が設定されていれば Turso、
    なければローカル SQLite にフォールバックする。

    row_factory=sqlite3.Row（デフォルト）でカラム名アクセスが可能。
    row_factory=None で生タプル。
    """
    if USE_TURSO:
        import libsql_experimental as libsql
        conn = libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)
        return _TursoConn(conn, use_dict_row=(row_factory is not None))
    else:
        conn = sqlite3.connect(str(DB_PATH), timeout=60)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
        if row_factory is not None:
            conn.row_factory = row_factory
        return conn


def open_db_autocommit():
    """
    DDL（CREATE TABLE / ALTER TABLE）用のオートコミット接続。
    venue_scraper の ensure_oriten_columns 等で使用。
    """
    if USE_TURSO:
        import libsql_experimental as libsql
        conn = libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)
        return _TursoConn(conn, use_dict_row=False)
    else:
        return sqlite3.connect(str(DB_PATH), timeout=30, isolation_level=None)
