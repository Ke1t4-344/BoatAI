#!/usr/bin/env python3
"""
hit_honmei_5 / hit_chuana_10 / hit_ana_10 バックフィルスクリプト
既存の top5_honmei / top5_chuana / top5_ana JSON から再計算してUPDATE。
一度だけ実行すればOK。スクレイパー停止中に実行すること。
"""
import sqlite3, json
from pathlib import Path

DB_PATH = Path(__file__).parent / "boatai.db"

conn = sqlite3.connect(str(DB_PATH), timeout=60)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA busy_timeout=60000")

# カラム追加（なければ）
existing = {row[1] for row in conn.execute("PRAGMA table_info(predictions)").fetchall()}
for col in ["hit_honmei_5", "hit_chuana_10", "hit_ana_10"]:
    if col not in existing:
        conn.execute(f"ALTER TABLE predictions ADD COLUMN {col} INTEGER")
        print(f"カラム追加: {col}")
conn.commit()

# 対象: actual_combo あり・新hit列がNULL
rows = conn.execute("""
    SELECT id, actual_combo, top5_honmei, top5_chuana, top5_ana
    FROM predictions
    WHERE actual_combo IS NOT NULL
      AND (hit_honmei_5 IS NULL OR hit_chuana_10 IS NULL OR hit_ana_10 IS NULL)
""").fetchall()

print(f"バックフィル対象: {len(rows)}件")

def _hit_n(json_str, actual, n):
    if not json_str:
        return None
    return 1 if actual in json.loads(json_str)[:n] else 0

updated = 0
for row in rows:
    conn.execute("""
        UPDATE predictions
        SET hit_honmei_5=?, hit_chuana_10=?, hit_ana_10=?
        WHERE id=?
    """, (
        _hit_n(row["top5_honmei"], row["actual_combo"], 5),
        _hit_n(row["top5_chuana"], row["actual_combo"], 10),
        _hit_n(row["top5_ana"],    row["actual_combo"], 10),
        row["id"]
    ))
    updated += 1
    if updated % 500 == 0:
        conn.commit()
        print(f"  {updated}件完了...")

conn.commit()

# 確認
totals = conn.execute("""
    SELECT COUNT(*),
           SUM(hit_honmei_5), SUM(hit_chuana_10), SUM(hit_ana_10)
    FROM predictions WHERE actual_combo IS NOT NULL
""").fetchone()

print(f"\n=== 完了 ===")
print(f"更新: {updated}件")
print(f"全体: {totals[0]}件")
print(f"  本命5点的中:  {totals[1]}件 ({totals[1]/totals[0]*100:.1f}%)" if totals[0] else "")
print(f"  中穴10点的中: {totals[2]}件 ({totals[2]/totals[0]*100:.1f}%)" if totals[0] else "")
print(f"  穴10点的中:   {totals[3]}件 ({totals[3]/totals[0]*100:.1f}%)" if totals[0] else "")

conn.close()
