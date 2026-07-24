#!/usr/bin/env python3
"""
migrate_add_scheduled_time.py
races テーブルに scheduled_time カラムを追加するマイグレーション。
一度だけ実行してください。

実行方法:
  python migrate_add_scheduled_time.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from db_connect import open_db

def main():
    conn = open_db()
    try:
        conn.execute("ALTER TABLE races ADD COLUMN scheduled_time TEXT")
        print("✅ scheduled_time カラムを追加しました")
    except Exception as e:
        msg = str(e).lower()
        if "duplicate column" in msg or "already exists" in msg:
            print("⚠️  scheduled_time カラムはすでに存在します（スキップ）")
        else:
            print(f"❌ エラー: {e}")
            sys.exit(1)

if __name__ == "__main__":
    main()
