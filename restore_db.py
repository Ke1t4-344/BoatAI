#!/usr/bin/env python3
"""
DB緊急復元スクリプト（SQLite backup API使用）

【重要】このスクリプトは以下の順で実行すること:
  1. 全スクレイパー停止（このスクリプトが自動実行）
  2. DBを復元
  3. スクレイパー再起動（手動）

使い方: python3 restore_db.py [backup_filename]
  例: python3 restore_db.py                    # 最新のcleanバックアップから復元
  例: python3 restore_db.py boatai_20260719_193000.db
"""
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).parent
TARGET = BASE / "boatai.db"
LOCK_FILE = BASE / "db_writer.lock"

SCRAPERS = [
    "com.boatai.scraper",
    "com.boatai.focused",
    "com.boatai.live",
    "com.boatai.odds",
    "com.boatai.meet",
    "com.boatai.backup",
    "com.boatai.morning",
]

def stop_all_scrapers():
    """全スクレイパーを停止（SIGINTのみ、SIGKILLは使わない）"""
    print("⏹  全スクレイパーを停止中...")
    for label in SCRAPERS:
        result = subprocess.run(
            ["launchctl", "stop", label],
            capture_output=True, text=True
        )
        status = "停止" if result.returncode == 0 else "未起動/スキップ"
        print(f"  {label}: {status}")
    print("  10秒待機（プロセス終了待ち）...")
    time.sleep(10)

    # ロックファイルが残っていれば削除
    if LOCK_FILE.exists():
        LOCK_FILE.unlink()
        print("  ロックファイル削除済み")

def find_best_backup(specified: str | None) -> Path:
    """指定がなければ最新のcleanバックアップを自動選択"""
    BACKUP_DIR = BASE / "backups"

    if specified:
        p = BACKUP_DIR / specified
        if not p.exists():
            p = Path(specified)  # フルパス指定も許容
        if not p.exists():
            print(f"❌ 指定バックアップが見つかりません: {specified}")
            sys.exit(1)
        return p

    # 自動選択: integrity okなバックアップを新しい順に探す
    candidates = sorted(BACKUP_DIR.glob("boatai_*.db"), reverse=True)
    candidates = [p for p in candidates if p.stat().st_size > 1_000_000]  # 1MB以上
    for cand in candidates:
        try:
            conn = sqlite3.connect(str(cand), timeout=10)
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            conn.close()
            if result == "ok":
                print(f"✅ 自動選択: {cand.name}")
                return cand
            else:
                print(f"  スキップ（破損）: {cand.name}")
        except Exception as e:
            print(f"  スキップ（読み取り失敗）: {cand.name}: {e}")

    print("❌ 有効なバックアップが見つかりません")
    sys.exit(1)

def main():
    # ── Step 0: 全スクレイパー停止（最重要・最初に行う）────────────────────
    # ※ スクレイパーが動いたまま restore すると、旧コードが即座に再書き込みしてDBを再破損させる
    stop_all_scrapers()

    # ── Step 1: バックアップファイル選択 ──────────────────────────────────
    specified = sys.argv[1] if len(sys.argv) > 1 else None
    BACKUP = find_best_backup(specified)

    # ── Step 2: バックアップの整合性確認 ─────────────────────────────────
    print(f"バックアップ確認中: {BACKUP.name}")
    src = sqlite3.connect(str(BACKUP), timeout=10)
    result = src.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        print(f"❌ バックアップが破損しています: {result[:200]}")
        src.close()
        sys.exit(1)
    entries = src.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    print(f"✅ バックアップ正常 (integrity=ok, entries={entries:,})")

    # ── Step 3: WALファイル削除（残っていれば）─────────────────────────────
    for ext in ["-shm", "-wal"]:
        wal = Path(str(TARGET) + ext)
        if wal.exists():
            wal.unlink()
            print(f"  削除: {wal.name}")

    # ── Step 4: SQLite backup APIで復元 ──────────────────────────────────
    print(f"復元中: {BACKUP.name} → boatai.db")
    dst = sqlite3.connect(str(TARGET), timeout=30)
    src.backup(dst, pages=1000)
    dst.close()
    src.close()
    print("✅ 復元完了")

    # ── Step 5: 復元後の整合性確認 ────────────────────────────────────────
    check = sqlite3.connect(str(TARGET), timeout=10)
    result2 = check.execute("PRAGMA integrity_check").fetchone()[0]
    entries2 = check.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    check.close()
    print(f"復元後確認: integrity={result2}, entries={entries2:,}")

    if result2 == "ok":
        print("""
✅ 復元成功！スクレイパーを再起動してください：

  launchctl start com.boatai.scraper com.boatai.focused com.boatai.live \\
    com.boatai.odds com.boatai.meet com.boatai.backup

⚠️  再起動前にコードの修正が完了していることを確認してください。
   （修正前に restart すると旧コードが再び破損を引き起こします）
"""
        )
    else:
        print(f"\n❌ 復元後も異常あり: {result2}")
        sys.exit(1)

if __name__ == "__main__":
    main()
