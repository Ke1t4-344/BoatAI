#!/usr/bin/env python3
"""
boatai.db マイグレーション v2
新テーブル・カラムを追加する（既存データは保持）

追加内容:
  - entries: branch, national_3ring_rate, nige_rate, sashi_rate,
             makuri_rate, makuri_sashi_rate, teiko_rate, megumi_rate
  - 新テーブル: odds_tansho (単勝オッズ)
  - 新テーブル: odds_2t (2連単オッズ)
  - 新テーブル: st_history (選手ST履歴)
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "boatai.db"


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")

    print("=== boatai.db マイグレーション v2 開始 ===")

    # ── 1. entries テーブルにカラム追加 ──────────────────
    existing = {row[1] for row in conn.execute("PRAGMA table_info(entries)").fetchall()}

    new_columns = [
        ("branch",               "TEXT"),     # 支部
        ("national_3ring_rate",  "REAL"),     # 全国3連対率
        ("nige_rate",            "REAL"),     # 決まり手: 逃げ率
        ("sashi_rate",           "REAL"),     # 決まり手: 差し率
        ("makuri_rate",          "REAL"),     # 決まり手: まくり率
        ("makuri_sashi_rate",    "REAL"),     # 決まり手: まくり差し率
        ("teiko_rate",           "REAL"),     # 決まり手: 抵抗率
        ("megumi_rate",          "REAL"),     # 決まり手: 恵まれ率
    ]

    added = 0
    for col_name, col_type in new_columns:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE entries ADD COLUMN {col_name} {col_type}")
            print(f"  entries.{col_name} ({col_type}) 追加")
            added += 1
        else:
            print(f"  entries.{col_name} 既存 → スキップ")

    # ── 2. 単勝オッズテーブル ────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS odds_tansho (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id INTEGER NOT NULL,
            boat_no INTEGER NOT NULL,
            odds    REAL,
            UNIQUE (race_id, boat_no),
            FOREIGN KEY (race_id) REFERENCES races(id)
        )
    """)
    print("  odds_tansho テーブル 確認/作成済み")

    # ── 3. 2連単オッズテーブル ───────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS odds_2t (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id     INTEGER NOT NULL,
            combination TEXT    NOT NULL,
            odds        REAL,
            UNIQUE (race_id, combination),
            FOREIGN KEY (race_id) REFERENCES races(id)
        )
    """)
    print("  odds_2t テーブル 確認/作成済み")

    # ── 4. ST履歴テーブル ────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS st_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            player_no    TEXT    NOT NULL,
            race_id      INTEGER NOT NULL,
            race_date    TEXT    NOT NULL,
            venue_code   TEXT,
            race_no      INTEGER,
            start_course INTEGER,
            start_timing TEXT,
            finish_rank  INTEGER,
            UNIQUE (player_no, race_id),
            FOREIGN KEY (race_id) REFERENCES races(id)
        )
    """)
    print("  st_history テーブル 確認/作成済み")

    conn.commit()
    print(f"\n=== マイグレーション完了 (entries新規カラム: {added}個) ===")

    # ── 検証 ─────────────────────────────────────────────
    cols = [row[1] for row in conn.execute("PRAGMA table_info(entries)").fetchall()]
    print(f"entries カラム数: {len(cols)}")
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print(f"テーブル一覧: {tables}")


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH, timeout=60)
    try:
        migrate(conn)
    finally:
        conn.close()
