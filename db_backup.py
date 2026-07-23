"""
db_backup.py — SQLite オンラインバックアップユーティリティ

SQLite の conn.backup() API を使用するため、DB稼働中・WALモード中でも安全。
バックアップは backups/ ディレクトリに保存し、古いものを自動削除する。

使い方:
    from db_backup import backup_db
    backup_db(conn, label="today")   # backups/boatai_YYYYMMDD_HHMMSS_today.db
"""

import sqlite3
import logging
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH     = Path(__file__).parent / "boatai.db"
BACKUP_DIR  = Path(__file__).parent / "backups"
KEEP_COUNT  = 5   # ラベルごとに保持するバックアップ数


def backup_db(
    conn: sqlite3.Connection,
    label: str = "auto",
    keep: int = KEEP_COUNT,
) -> Path | None:
    """
    conn に接続中の DB を backups/ にバックアップする。

    Parameters
    ----------
    conn  : 既存の sqlite3.Connection（バックアップ元）
    label : ファイル名に付けるラベル（例: "today", "historical", "focused"）
    keep  : 同じラベルのバックアップを何件残すか

    Returns
    -------
    保存したバックアップファイルのパス（失敗時は None）
    """
    BACKUP_DIR.mkdir(exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"boatai_{ts}_{label}.db"

    try:
        with sqlite3.connect(dest) as backup_conn:
            conn.backup(backup_conn, pages=100)   # 100ページずつコピー（他の書き込みをブロックしない）
        log.info(f"[backup] 完了: {dest.name} ({dest.stat().st_size // 1024}KB)")
        _rotate(label, keep)
        return dest
    except Exception as e:
        log.warning(f"[backup] 失敗: {e}")
        return None


def _rotate(label: str, keep: int) -> None:
    """同じラベルの古いバックアップを削除して keep 件だけ残す"""
    pattern = re.compile(rf"boatai_\d{{8}}_\d{{6}}_{re.escape(label)}\.db$")
    files = sorted(
        [f for f in BACKUP_DIR.iterdir() if pattern.match(f.name)],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    for old in files[keep:]:
        try:
            old.unlink()
            log.info(f"[backup] 古いバックアップ削除: {old.name}")
        except Exception as e:
            log.warning(f"[backup] 削除失敗: {old.name} — {e}")
