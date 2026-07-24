#!/usr/bin/env python3
"""
db_connect.py — DB接続の統一エントリポイント（Turso HTTP API版）

libsql-experimental を使わず、requests で Turso HTTP API を直接叩く。
ネイティブライブラリ不要のため Streamlit Cloud でもビルド可能。

ローカル（TURSO_URL/TURSO_TOKEN 未設定）は従来通り SQLite にフォールバック。
"""

import os
import sqlite3
import base64
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

TURSO_URL   = os.environ.get("TURSO_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "")
DB_PATH     = Path(__file__).parent / "boatai.db"

USE_TURSO = bool(TURSO_URL and TURSO_TOKEN)


# ---------------------------------------------------------------------------
# 共通ユーティリティ
# ---------------------------------------------------------------------------

def _http_url(turso_url: str) -> str:
    """libsql:// または https:// → 正規化した https:// URL"""
    url = turso_url.strip()
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    return url.rstrip("/")


def _encode_arg(v):
    """Python 値 → Turso HTTP API (hrana) の型付き引数

    hrana プロトコル仕様:
      integer → value は文字列（64bit整数がJSONの精度を超えるため）
      float   → value は JSON 数値（文字列不可）
      text    → value は文字列
      blob    → base64 文字列
    """
    import math
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": "1" if v else "0"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        # NaN / Inf は JSON で表現できないので null に変換
        if math.isnan(v) or math.isinf(v):
            return {"type": "null"}
        return {"type": "float", "value": v}   # ← JSON 数値（文字列ではない）
    if isinstance(v, bytes):
        return {"type": "blob", "base64": base64.b64encode(v).decode()}
    return {"type": "text", "value": str(v)}


def _decode_val(cell):
    """Turso HTTP レスポンスのセル → Python 値"""
    if cell is None:
        return None
    t = cell.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(cell["value"])
    if t == "float":
        return float(cell["value"])
    if t == "blob":
        return base64.b64decode(cell.get("base64", ""))
    return cell.get("value")  # text


# ---------------------------------------------------------------------------
# _CursorProxy — conn.cursor() 互換アダプター
# Turso HTTP API は cursor() を持たないため、既存コードの移行を最小化するための
# 互換レイヤー。conn.cursor() → _CursorProxy を返す。
# ---------------------------------------------------------------------------

class _CursorProxy:
    """
    cursor = conn.cursor()
    cursor.execute(sql, params)
    cursor.fetchone() / cursor.fetchall()
    という既存コードをそのまま動かすためのアダプター。
    """
    def __init__(self, conn):
        self._conn = conn
        self._cur = None

    def execute(self, sql, params=()):
        self._cur = self._conn.execute(sql, params)
        return self

    def fetchone(self):
        return self._cur.fetchone() if self._cur else None

    def fetchall(self):
        return self._cur.fetchall() if self._cur else []

    def __iter__(self):
        return iter(self._cur) if self._cur else iter([])


# ---------------------------------------------------------------------------
# _DictRow — sqlite3.Row 互換ラッパー（全パスで統一）
# ---------------------------------------------------------------------------

class _DictRow:
    """
    sqlite3.Row 互換のラッパー。
    row["col"]  → 文字列キーアクセス
    row[0]      → 数値インデックスアクセス
    dict(row)   → dict 変換
    iter(row)   → 値の順次列挙
    row.get()   → デフォルト値付きアクセス
    """
    __slots__ = ("_data", "_keys", "_vals")

    def __init__(self, cols: list, vals):
        self._keys = list(cols)
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


# ---------------------------------------------------------------------------
# Turso HTTP API カーソル
# ---------------------------------------------------------------------------

class _HttpCursor:
    """Turso HTTP API のレスポンスを Cursor 互換インターフェースで提供"""

    def __init__(self, use_dict_row: bool = True):
        self._cols: list = []
        self._rows: list = []
        self._pos: int = 0
        self._lastrowid = None
        self._rowcount: int = -1
        self.description = None
        self._use_dict_row = use_dict_row

    def _load(self, result: dict):
        cols_info = result.get("cols", [])
        self._cols = [c["name"] for c in cols_info]
        self.description = tuple(
            (c["name"], None, None, None, None, None, None) for c in cols_info
        )
        raw_rows = result.get("rows", [])
        self._rows = [tuple(_decode_val(cell) for cell in row) for row in raw_rows]
        self._rowcount = result.get("affected_row_count", len(self._rows))
        self._lastrowid = result.get("last_insert_rowid")
        self._pos = 0

    @property
    def lastrowid(self):
        try:
            return int(self._lastrowid) if self._lastrowid is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def rowcount(self):
        return self._rowcount

    def _wrap(self, row):
        if row is None:
            return None
        if self._use_dict_row and self._cols:
            return _DictRow(self._cols, row)
        return row

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return self._wrap(row)

    def fetchall(self):
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return [self._wrap(r) for r in rows]

    def __iter__(self):
        while self._pos < len(self._rows):
            yield self._wrap(self._rows[self._pos])
            self._pos += 1


# ---------------------------------------------------------------------------
# Turso HTTP API 接続
# ---------------------------------------------------------------------------

class _HttpConn:
    """
    Turso HTTP API を sqlite3.Connection 互換インターフェースで提供。

    execute() は 1 クエリ = 1 HTTP リクエスト。
    executemany() は複数ステートメントを 1 HTTP リクエストにまとめる。
    commit() / rollback() は HTTP API の自動コミットに合わせてノーオペレーション。
    """

    def __init__(self, base_url: str, token: str, use_dict_row: bool = True):
        import requests as _req
        self._base_url = base_url
        self._token = token
        self._use_dict_row = use_dict_row
        self._session = _req.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    @property
    def row_factory(self):
        return sqlite3.Row if self._use_dict_row else None

    @row_factory.setter
    def row_factory(self, value):
        self._use_dict_row = (value is not None)

    def _pipeline(self, stmts: list) -> list:
        """
        stmts: [{"type": "execute", "stmt": {...}}, ...]
        → results リストを返す（close エントリは除外済み）
        """
        payload = {"requests": stmts + [{"type": "close"}]}
        resp = self._session.post(
            f"{self._base_url}/v2/pipeline",
            json=payload,
            timeout=30,
        )
        if not resp.ok:
            raise Exception(
                f"Turso HTTP {resp.status_code} {resp.reason}: {resp.text[:500]}"
            )
        data = resp.json()
        results = data.get("results", [])
        for r in results:
            if r.get("type") == "error":
                msg = r.get("error", {}).get("message", "Unknown Turso error")
                raise Exception(f"Turso error: {msg}")
        # close 結果（最後）を除いて返す
        return results[:-1] if results else []

    def execute(self, sql: str, params=()):
        stmt: dict = {"sql": sql}
        if params:
            stmt["args"] = [_encode_arg(v) for v in params]
        results = self._pipeline([{"type": "execute", "stmt": stmt}])
        cur = _HttpCursor(self._use_dict_row)
        if results and results[0].get("type") == "ok":
            cur._load(results[0]["response"]["result"])
        return cur

    def executemany(self, sql: str, seq):
        stmts = []
        for params in seq:
            s: dict = {"sql": sql}
            if params:
                s["args"] = [_encode_arg(v) for v in params]
            stmts.append({"type": "execute", "stmt": s})
        if stmts:
            self._pipeline(stmts)

    def executescript(self, sql: str):
        for stmt_str in sql.split(";"):
            s = stmt_str.strip()
            if s:
                self.execute(s)

    def cursor(self):
        return _CursorProxy(self)

    def commit(self):
        pass  # HTTP API は自動コミット

    def rollback(self):
        pass  # HTTP API はロールバック不可

    def close(self):
        try:
            self._session.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self.close()
        except Exception:
            pass
        return False  # 例外を伝播


# ---------------------------------------------------------------------------
# ローカル SQLite ラッパー（フォールバック用）
# ---------------------------------------------------------------------------

class _TursoCursor:
    """ローカル SQLite cursor を _DictRow で包む"""

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
        return [_DictRow(cols, r) for r in self._cursor.fetchall()] if cols else self._cursor.fetchall()

    def __iter__(self):
        cols = self._cols() if self._use_dict_row else []
        for row in self._cursor:
            yield _DictRow(cols, row) if cols else row


class _TursoConn:
    """ローカル SQLite 接続を _DictRow で包む"""

    def __init__(self, conn, use_dict_row: bool = True):
        self._conn = conn
        self._use_dict_row = use_dict_row

    @property
    def row_factory(self):
        return sqlite3.Row if self._use_dict_row else None

    @row_factory.setter
    def row_factory(self, value):
        self._use_dict_row = (value is not None)

    def execute(self, sql: str, params=()):
        cursor = self._conn.execute(sql, tuple(params)) if params else self._conn.execute(sql)
        return _TursoCursor(cursor, self._use_dict_row)

    def executemany(self, sql: str, seq):
        return self._conn.executemany(sql, seq)

    def executescript(self, sql: str):
        return self._conn.executescript(sql)

    def cursor(self):
        return _CursorProxy(self)

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
        try:
            self.close()
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------

def open_db(row_factory=sqlite3.Row):
    """
    DB 接続を返す（sqlite3 互換インターフェース）。

    TURSO_URL / TURSO_TOKEN が設定されていれば Turso HTTP API、
    なければローカル SQLite にフォールバック。
    """
    if USE_TURSO:
        return _HttpConn(
            _http_url(TURSO_URL),
            TURSO_TOKEN,
            use_dict_row=(row_factory is not None),
        )
    else:
        conn = sqlite3.connect(str(DB_PATH), timeout=60)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
        return _TursoConn(conn, use_dict_row=(row_factory is not None))


def open_db_autocommit():
    """DDL（CREATE TABLE / ALTER TABLE）用接続。"""
    if USE_TURSO:
        return _HttpConn(_http_url(TURSO_URL), TURSO_TOKEN, use_dict_row=False)
    else:
        return _TursoConn(
            sqlite3.connect(str(DB_PATH), timeout=30, isolation_level=None),
            use_dict_row=False,
        )
