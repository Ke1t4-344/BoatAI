#!/usr/bin/env python3
"""
DB安全差し替えユーティリティ

mvではなくSQLite backup APIを使ってインプレースでDBを置換する。
mvを使うと既存のスクレイパープロセスが古いinodeに書き込み続けてしまう。

使い方:
    python3 db_replace.py <new_db_path>

    例: python3 db_replace.py boatai_recovered.db

注意:
    - 実行前に全スクレイパーを停止すること
    - new_db_path の integrity_check が ok の場合のみ置換する
"""
import sqlite3
import sys
import os
from pathlib import Path

DB_PATH = Path(__file__).parent / "boatai.db"


def safe_replace(new_db_path: str) -> bool:
    """
    new_db_path の内容を boatai.db にインプレースで置換する。
    mvは使わない（既存プロセスが古いinodeに書き込み続けるため）。
    """
    new_path = Path(new_db_path)
    if not new_path.exists():
        print(f"エラー: {new_db_path} が存在しません")
        return False

    # 新DBの整合性チェック
    print(f"整合性チェック: {new_path.name}")
    src = sqlite3.connect(str(new_path))
    r = src.execute("PRAGMA integrity_check").fetchone()[0]
    if r != "ok":
        print(f"エラー: 整合性チェック失敗 ({r})")
        src.close()
        return False
    print(f"  → OK")

    # 件数確認
    for t in ["races", "race_result_entries", "scraped_history"]:
        try:
            cnt = src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t}: {cnt}件")
        except Exception:
            pass

    # SQLite backup APIでインプレース置換
    print(f"\nboatai.db に置換中...")
    dst = sqlite3.connect(str(DB_PATH))
    try:
        src.backup(dst)
        print("  → 完了")
    except Exception as e:
        print(f"  → エラー: {e}")
        src.close()
        dst.close()
        return False

    src.close()

    # VACUUM
    dst.execute("PRAGMA journal_mode=WAL")
    dst.execute("VACUUM")
    dst.commit()

    # 最終チェック
    r2 = dst.execute("PRAGMA integrity_check").fetchone()[0]
    print(f"最終integrity_check: {r2}")
    dst.close()

    return r2 == "ok"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("使い方: python3 db_replace.py <new_db_path>")
        sys.exit(1)

    success = safe_replace(sys.argv[1])
    sys.exit(0 if success else 1)
