#!/usr/bin/env python3
"""
migrate_add_indexes.py — Turso read 削減のためのインデックス追加

なぜ必要か:
  race_result_entries (181万行) / entries (186万行) に対して
  player_no / motor_no でフィルタするクエリが毎回フルスキャンしていた。

実行:
    /Users/miyoshikeita/miniconda3/envs/boatai/bin/python3 migrate_add_indexes.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db_connect import open_db

INDEXES = [
    # ── race_result_entries (181万行) ─────────────────────────────────
    # player_no でフィルタ: rre_raw, _all_hist_rows, meet_player_stats, _load_models() × 4クエリ
    # → 181万行フルスキャン を index lookup に変換（最大の削減効果）
    ("idx_rre_player_no",
     "CREATE INDEX IF NOT EXISTS idx_rre_player_no ON race_result_entries(player_no)"),

    # start_course でフィルタ: _load_models()の会場別1コース1着率集計
    ("idx_rre_start_course",
     "CREATE INDEX IF NOT EXISTS idx_rre_start_course ON race_result_entries(start_course)"),

    # ── entries (186万行) ─────────────────────────────────────────────
    # motor_no でフィルタ: ml_predict.py meet_motor_stats
    ("idx_entries_motor_no",
     "CREATE INDEX IF NOT EXISTS idx_entries_motor_no ON entries(motor_no)"),

    # player_no でフィルタ: ml_predict.py player_no検索
    ("idx_entries_player_no",
     "CREATE INDEX IF NOT EXISTS idx_entries_player_no ON entries(player_no)"),

    # ── races (31.2万行) ──────────────────────────────────────────────
    # venue_code でフィルタ: get_venues()の開催日数計算クエリ（現状312K行フルスキャン）
    # 既存のUNIQUEは(date, venue_code, race_no)でdateが先頭 → venue_code検索は非効率
    ("idx_races_venue_date",
     "CREATE INDEX IF NOT EXISTS idx_races_venue_date ON races(venue_code, date DESC)"),

    # ── st_history ───────────────────────────────────────────────────
    # race_date でフィルタ: STばらつき計算の日付レンジ検索
    ("idx_st_history_race_date",
     "CREATE INDEX IF NOT EXISTS idx_st_history_race_date ON st_history(race_date)"),

    # ── predictions (30.7万行) ────────────────────────────────────────
    # actual_combo IS NOT NULL フィルタ: 予測精度集計クエリ
    ("idx_predictions_actual",
     "CREATE INDEX IF NOT EXISTS idx_predictions_actual ON predictions(actual_combo)"),
]


def main():
    print("Turso read削減インデックス追加マイグレーション")
    print("=" * 50)
    conn = open_db()

    for name, sql in INDEXES:
        print(f"  作成中: {name} ... ", end="", flush=True)
        try:
            conn.execute(sql)
            print("OK")
        except Exception as e:
            print(f"ERROR: {e}")

    conn.close()
    print("=" * 50)
    print("完了。Tursoダッシュボードで Rows Read が減少するか確認してください。")


if __name__ == "__main__":
    main()
