#!/usr/bin/env python3
"""
backup_db.py — boatai.db の自動スナップショットバックアップ

LaunchAgent (com.boatai.backup.plist) から毎朝 08:00 に実行。
- SQLite の online backup API を使用（WAL モードでも一貫性保証）
- backups/ ディレクトリに日付付きで保存
- 7日分を超えた古いバックアップは自動削除
"""

import sqlite3
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db_lock import acquire_write_lock, release_write_lock

SCRAPERS = [
    "com.boatai.scraper", "com.boatai.focused", "com.boatai.live",
    "com.boatai.odds", "com.boatai.meet", "com.boatai.morning",
]

def _emergency_stop_scrapers() -> None:
    """DB破損検知時にスクレイパーを緊急停止（これ以上の書き込みを防ぐ）"""
    log.error("DB破損検知 → 全スクレイパーを緊急停止します")
    for label in SCRAPERS:
        subprocess.run(["launchctl", "stop", label], capture_output=True)

DB_PATH     = Path(__file__).parent / "boatai.db"
BACKUP_DIR  = Path(__file__).parent / "backups"
KEEP_DAYS   = 7   # 保持する日数（時間ベース）
KEEP_MIN    = 48  # 最低保持件数（バースト時にクリーンな世代を残す）

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def main():
    BACKUP_DIR.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst_path  = BACKUP_DIR / f"boatai_{timestamp}.db"

    # ── 書き込みロック取得（TRUNCATE checkpointやスクレイパーの書き込みと競合させない） ──
    log.info("DB書き込みロック待機中...")
    acquire_write_lock(wait=True, timeout=300)
    try:
        # ── SQLite online backup API ──────────────────────────────────────
        src = sqlite3.connect(str(DB_PATH), timeout=30)
        dst = sqlite3.connect(str(dst_path))
        src.backup(dst, pages=500)   # 500ページずつコピー（I/O分散）
        dst.close()
        src.close()
        size_mb = dst_path.stat().st_size / 1024 / 1024
        log.info("バックアップ完了: %s (%.1f MB)", dst_path.name, size_mb)
    except Exception as e:
        log.error("バックアップ失敗: %s", e)
        sys.exit(1)
    finally:
        release_write_lock()

    # ── integrity_check ───────────────────────────────────────────────
    try:
        conn = sqlite3.connect(str(dst_path), timeout=10)
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.close()
        if result == "ok":
            log.info("integrity_check: ok")
        else:
            log.warning("integrity_check 異常: %s", result[:200])
            # 破損DBへの追加書き込みを即座に止める（破損悪化防止）
            _emergency_stop_scrapers()
    except Exception as e:
        log.warning("integrity_check 失敗: %s", e)
        _emergency_stop_scrapers()

    # ── 古いバックアップを削除（KEEP_DAYS日＆最低KEEP_MIN件を保持）──────
    # 件数ベースではなく時間ベースで削除することでバースト時にもクリーンな世代を残す
    from datetime import timedelta
    backups = sorted(BACKUP_DIR.glob("boatai_*.db"))
    cutoff = datetime.now() - timedelta(days=KEEP_DAYS)
    for old in backups:
        # 最低件数は確保する（削除後の残り件数が KEEP_MIN を下回らないようにする）
        remaining = [b for b in backups if b != old]
        if len(remaining) < KEEP_MIN:
            break
        # ファイル名からタイムスタンプを解析して古いものだけ削除
        try:
            name = old.stem  # boatai_YYYYMMDD_HHMMSS
            parts = name.split("_")
            ts = datetime.strptime(f"{parts[1]}_{parts[2]}", "%Y%m%d_%H%M%S")
            if ts < cutoff:
                old.unlink()
                backups.remove(old)
                log.info("古いバックアップ削除: %s", old.name)
        except Exception:
            pass

    log.info("=== バックアップ処理完了 ===")


if __name__ == "__main__":
    main()
