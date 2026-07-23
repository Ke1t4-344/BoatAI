#!/usr/bin/env python3
"""
db_lock.py — DB書き込み排他ロック（fcntl.flock版）

Turso 使用時（TURSO_URL 環境変数あり）は全関数が no-op。
Turso はサーバー側でトランザクション管理を行うため、
OS レベルのファイルロックは不要（かつ使用不可）。

ローカル SQLite 使用時は従来の fcntl.flock によるロックを継続。
"""

import logging
import os
from pathlib import Path

# Turso 使用中かどうかを環境変数で判定（db_connect.py と同じロジック）
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

_USE_TURSO = bool(os.environ.get("TURSO_URL", ""))

log = logging.getLogger(__name__)

LOCK_FILE = Path(__file__).parent / "db_writer.lock"

log = logging.getLogger(__name__)

LOCK_FILE = Path(__file__).parent / "db_writer.lock"

# ── Turso 使用時: 全関数 no-op ─────────────────────────────────────────────────
if _USE_TURSO:
    def acquire_write_lock(wait: bool = False, timeout: int = 60) -> None:
        """Turso 使用時は no-op（サーバー側でロック管理）"""
        log.debug("Turso モード: acquire_write_lock はスキップ")

    def release_write_lock() -> None:
        """Turso 使用時は no-op"""
        log.debug("Turso モード: release_write_lock はスキップ")

    def check_and_acquire(script_name: str = "") -> None:
        """Turso 使用時は no-op"""
        pass

# ── ローカル SQLite 使用時: fcntl.flock による従来のロック ────────────────────
else:
    import fcntl
    import sys
    import time

    # モジュールレベルでファイルディスクリプタを保持（プロセス内で唯一）
    _lock_fd: "int | None" = None

    def acquire_write_lock(wait: bool = False, timeout: int = 60) -> None:
        """
        書き込みロックを取得する（fcntl.flock使用）。

        wait=False（デフォルト）: 他プロセスがロック中なら即 sys.exit(1)
        wait=True: ロックが解放されるまで最大 timeout 秒待機
        """
        global _lock_fd

        if _lock_fd is not None:
            log.debug("DB書き込みロック: 既に保持中 (fd=%d)", _lock_fd)
            return

        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            deadline = time.monotonic() + (timeout if wait else 0)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    os.ftruncate(fd, 0)
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.write(fd, str(os.getpid()).encode())
                    _lock_fd = fd
                    log.debug("DB書き込みロック取得 (PID: %d)", os.getpid())
                    return
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        os.close(fd)
                        log.error(
                            "DB書き込みロック取得タイムアウト (%d秒)。"
                            "全スクレイパーを停止してから再実行してください。", timeout
                        )
                        sys.exit(1)
                    log.info("DB書き込みロック待機中... (%d秒)", int(deadline - time.monotonic()))
                    time.sleep(5)
        except Exception:
            os.close(fd)
            raise

    def release_write_lock() -> None:
        """書き込みロックを解放する。"""
        global _lock_fd
        if _lock_fd is None:
            return
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
            # ★ LOCK_FILE.unlink() は絶対に呼ばない ★
            # 同一inodeを使い回すことでTOCTOU競合を防ぐ
            log.debug("DB書き込みロック解放 (PID: %d)", os.getpid())
        except OSError as e:
            log.warning("DB書き込みロック解放エラー: %s", e)
        finally:
            _lock_fd = None

    def check_and_acquire(script_name: str = "") -> None:
        """ロック確認＋取得のショートカット。他が書き込み中なら即終了。"""
        acquire_write_lock(wait=False)
