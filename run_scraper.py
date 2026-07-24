#!/usr/bin/env python3
"""
run_scraper.py — Railway Worker エントリーポイント

Mac の LaunchAgent（毎分起動・単発実行）の代替として、
Railway 上で scraper.main() を定期ループで実行し続ける。

起動方法:
  python run_scraper.py                  # デフォルト: 2分おき
  SCRAPER_INTERVAL_SEC=60 python run_scraper.py  # 1分おき
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ── 設定 ────────────────────────────────────────────────
_JST = timezone(timedelta(hours=9))
LOOP_INTERVAL = int(os.environ.get("SCRAPER_INTERVAL_SEC", "120"))  # デフォルト2分

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def _jst_now() -> datetime:
    return datetime.now(_JST)


def main() -> None:
    log.info("=== Railway Scraper Worker 起動 (interval=%ds) ===", LOOP_INTERVAL)

    # 環境変数確認ログ（秘密情報は出力しない）
    turso_url   = os.environ.get("TURSO_URL", "")
    turso_token = os.environ.get("TURSO_TOKEN", "")
    use_turso   = bool(turso_url and turso_token)
    log.info("DB接続モード: %s", "Turso (クラウド)" if use_turso else "⚠️ ローカルSQLite（TURSO_URL/TOKEN未設定）")

    import scraper

    while True:
        loop_start = time.monotonic()

        try:
            # TODAY を JST で最新化（真夜中をまたいでも正確な日付を使用）
            scraper.TODAY = _jst_now().strftime("%Y%m%d")
            log.info("--- ループ開始 (JST=%s, TODAY=%s) ---",
                     _jst_now().strftime("%H:%M:%S"), scraper.TODAY)
            scraper.main()

        except SystemExit as e:
            # PID チェックや時間外チェックで return した場合は無視して継続
            code = e.code if e.code is not None else 0
            if code != 0:
                log.warning("SystemExit(%s) — ループ継続", code)

        except Exception:
            log.exception("スクレイパー実行エラー（ループ継続）")

        elapsed = time.monotonic() - loop_start
        wait = max(0, LOOP_INTERVAL - elapsed)
        log.info("--- ループ完了 (%.1fs) / 次回まで %.0fs ---", elapsed, wait)
        if wait > 0:
            time.sleep(wait)


if __name__ == "__main__":
    main()
