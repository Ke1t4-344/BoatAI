#!/usr/bin/env python3
"""
health_check.py — データ品質・スクレイパー稼働状況の毎朝チェック

毎朝 9:00 に実行し、下記を検証する:
  1. course_stats 取得率（当日出走選手中何%がデータあるか）
  2. entries 整合性（各レース6艇そろっているか）
  3. スクレイパーログの鮮度（最終実行が24h以内か）
  4. DB整合性 quick check

問題があれば logs/health_check.log に CRITICAL/WARNING を出力する。
"""

import os
import sqlite3
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

BASE  = Path(__file__).parent
DB    = BASE / "boatai.db"
TODAY = date.today().strftime("%Y%m%d")

LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOG_DIR / "health_check.log",
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# コンソールにも出力
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", "%H:%M:%S"))
logging.getLogger().addHandler(console)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# チェック関数
# ──────────────────────────────────────────────────────────────────────

def check_course_stats(conn: sqlite3.Connection) -> bool:
    """今日の出走選手中、course_stats 取得率が閾値を超えているか"""
    THRESHOLD = 0.50   # 50%未満はCRITICAL

    total_players = conn.execute("""
        SELECT COUNT(DISTINCT e.player_no)
        FROM entries e JOIN races r ON r.id = e.race_id
        WHERE r.date = ?
    """, (TODAY,)).fetchone()[0]

    if total_players == 0:
        log.warning("[course_stats] 今日の出走選手が見つかりません (entries未取得?)")
        return False

    fetched = conn.execute(
        "SELECT COUNT(DISTINCT player_no) FROM course_stats WHERE fetched_date = ?", (TODAY,)
    ).fetchone()[0]

    rate = fetched / total_players
    msg = f"[course_stats] {fetched}/{total_players} ({rate*100:.1f}%)"

    if rate < THRESHOLD:
        log.critical("%s ← 取得率が低すぎます (閾値%.0f%%)。scraper.py の稼働を確認してください。", msg, THRESHOLD * 100)
        return False
    elif rate < 0.80:
        log.warning("%s ← やや低い（目標80%%以上）", msg)
        return True
    else:
        log.info("%s ✓", msg)
        return True


def check_entries(conn: sqlite3.Connection) -> bool:
    """今日のレースで6艇未満の不完全なentries行があるか"""
    bad_races = conn.execute("""
        SELECT r.venue_code, r.race_no, COUNT(e.id) as cnt
        FROM races r
        LEFT JOIN entries e ON e.race_id = r.id
        WHERE r.date = ?
        GROUP BY r.id
        HAVING cnt < 6
    """, (TODAY,)).fetchall()

    if bad_races:
        for venue, rno, cnt in bad_races[:5]:
            log.warning("[entries] 会場%s %dR: %d艇しか登録されていません", venue, rno, cnt)
        if len(bad_races) > 5:
            log.warning("[entries] ... 他%d件", len(bad_races) - 5)
        return False
    else:
        total = conn.execute(
            "SELECT COUNT(*) FROM races WHERE date = ?", (TODAY,)
        ).fetchone()[0]
        log.info("[entries] 全%dレース 6艇そろっています ✓", total)
        return True


def check_log_freshness() -> bool:
    """各スクレイパーログが直近24時間以内に更新されているか"""
    SCRAPERS = {
        "scraper.py (course_stats)": LOG_DIR / "scraper_nohup.log",
        "live_scraper.py":            LOG_DIR / "live_nohup.log",
        "morning_scraper.py":         LOG_DIR / "morning_nohup.log",
        "focused_scraper.py":         LOG_DIR / "focused_nohup.log",
    }
    now = datetime.now()
    all_ok = True

    for name, log_path in SCRAPERS.items():
        if not log_path.exists():
            log.warning("[scraper_log] %s: ログファイルが見つかりません (%s)", name, log_path.name)
            all_ok = False
            continue

        mtime = datetime.fromtimestamp(log_path.stat().st_mtime)
        age_h = (now - mtime).total_seconds() / 3600

        if age_h > 24:
            log.critical("[scraper_log] %s: 最終更新が %.1f時間前 — 停止している可能性があります (%s)",
                         name, age_h, log_path.name)
            all_ok = False
        elif age_h > 4:
            log.warning("[scraper_log] %s: 最終更新が %.1f時間前 (%s)", name, age_h, log_path.name)
        else:
            log.info("[scraper_log] %s: %.1f時間前 ✓", name, age_h)

    return all_ok


def check_db_integrity(conn: sqlite3.Connection) -> bool:
    """SQLite integrity_check (quick)"""
    result = conn.execute("PRAGMA quick_check").fetchone()[0]
    if result == "ok":
        log.info("[db_integrity] ok ✓")
        return True
    else:
        log.critical("[db_integrity] FAILED: %s", result)
        return False


def check_launchagents() -> None:
    """LaunchAgentsディレクトリに必要なplistがあるか確認"""
    REQUIRED = [
        "com.boatai.scraper.plist",
        "com.boatai.live.plist",
        "com.boatai.morning.plist",
        "com.boatai.focused.plist",
        "com.boatai.historical.plist",
    ]
    la_dir = Path.home() / "Library" / "LaunchAgents"
    for plist in REQUIRED:
        path = la_dir / plist
        if not path.exists():
            log.critical("[launchagent] %s が ~/Library/LaunchAgents/ に見つかりません。"
                         "install_agents.sh を実行してください。", plist)
        else:
            log.info("[launchagent] %s ✓", plist)


# ──────────────────────────────────────────────────────────────────────
# メイン
# ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 60)
    log.info("BoatAI ヘルスチェック 開始 (日付: %s)", TODAY)
    log.info("=" * 60)

    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")

    issues = 0

    # 1. DB整合性
    if not check_db_integrity(conn):
        issues += 1

    # 2. course_stats
    if not check_course_stats(conn):
        issues += 1

    # 3. entries
    if not check_entries(conn):
        issues += 1

    # 4. ログ鮮度
    if not check_log_freshness():
        issues += 1

    # 5. LaunchAgent登録確認
    check_launchagents()

    conn.close()

    log.info("-" * 60)
    if issues == 0:
        log.info("ヘルスチェック完了: 問題なし ✓")
    else:
        log.critical("ヘルスチェック完了: %d件の問題が検出されました。上記ログを確認してください。", issues)


if __name__ == "__main__":
    main()
