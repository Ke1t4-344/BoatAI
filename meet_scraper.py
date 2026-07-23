#!/usr/bin/env python3
"""
meet_scraper.py — 出走表ページから整備情報・選手コメントを収集

boatrace.jp /owpc/pc/race/racelist ページをスクレイピングし、
各艇の整備情報（整備回数・プロペラ変更・部品交換）と
選手コメント（感情スコア付き）を entry_notes テーブルに保存する。

LaunchAgent StartInterval=1800（30分おき）で実行。
8:00〜21:30 の時間帯のみ動作。
"""

import re
import sys
import time
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from scraper import DB_PATH, TODAY, VENUE_NAMES, BASE_URL, HEADERS, REQ_DELAY
from db_lock import acquire_write_lock, release_write_lock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# 感情分析キーワード（コメント）
# ────────────────────────────────────────────────────────────────────────────
_POS_WORDS = [
    "いい", "良い", "よく", "合って", "合った", "自信", "期待", "上がっ",
    "改善", "まとまっ", "まとまって", "伸び", "満足", "順調", "好調",
    "しっかり", "スムーズ", "楽しみ", "いける",
]
_NEG_WORDS = [
    "重い", "合わない", "悪い", "難しい", "苦し", "不安", "落ち",
    "厳し", "出ない", "出なく", "弱い", "つらい", "ダメ",
]


def _sentiment(text: str | None) -> float:
    """コメントの感情スコアを -1.0〜1.0 で返す（ルールベース）"""
    if not text:
        return 0.0
    pos = sum(1 for w in _POS_WORDS if w in text)
    neg = sum(1 for w in _NEG_WORDS if w in text)
    total = pos + neg
    if total == 0:
        return 0.0
    return round((pos - neg) / total, 2)


# ────────────────────────────────────────────────────────────────────────────
# DB
# ────────────────────────────────────────────────────────────────────────────
def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entry_notes (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id           INTEGER NOT NULL,
            boat_no           INTEGER NOT NULL,
            maintenance_text  TEXT,
            prop_changed      INTEGER NOT NULL DEFAULT 0,
            parts_changed     INTEGER NOT NULL DEFAULT 0,
            maintenance_count INTEGER NOT NULL DEFAULT 0,
            player_comment    TEXT,
            comment_sentiment REAL,
            scraped_at        TEXT NOT NULL,
            UNIQUE(race_id, boat_no)
        )
    """)
    conn.commit()


# ────────────────────────────────────────────────────────────────────────────
# HTTP
# ────────────────────────────────────────────────────────────────────────────
_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(HEADERS)
    return _session


def fetch_racelist(venue_code: str, race_no: int, date: str) -> BeautifulSoup | None:
    url = f"{BASE_URL}/racelist"
    params = {"rno": race_no, "jcd": venue_code, "hd": date}
    try:
        resp = _get_session().get(url, params=params, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        time.sleep(REQ_DELAY)
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        log.warning("取得失敗 %s %sR: %s", venue_code, race_no, e)
        return None


# ────────────────────────────────────────────────────────────────────────────
# パーサー
# ────────────────────────────────────────────────────────────────────────────
_ZEN_NUM = {"１": 1, "２": 2, "３": 3, "４": 4, "５": 5, "６": 6}
_MAINT_KW = ["整備", "プロペラ", "ペラ", "部品", "エンジン", "キャブ", "調整", "交換"]


def _int(s) -> int | None:
    try:
        return int(str(s).strip())
    except (ValueError, TypeError):
        return None


def parse_racelist_notes(soup: BeautifulSoup) -> list[dict]:
    """
    出走表ページをパースして整備情報・コメントを返す。

    boatrace.jp の出走表は「tbody.is-fs12」が1艇分。
    各tbodyは複数のtrを持ち、整備情報は全tbodyのテキストに含まれる。
    コメントは tbody の最終行 td に含まれることが多い。

    Returns: list of {boat_no, maintenance_text, prop_changed, parts_changed,
                      maintenance_count, player_comment, comment_sentiment}
    """
    results: list[dict] = []

    for tbody in soup.select("tbody.is-fs12"):
        trs = tbody.find_all("tr")
        if not trs:
            continue

        # 艇番（最初の行の最初のtd）
        first_tds = trs[0].find_all("td")
        if not first_tds:
            continue
        raw = first_tds[0].get_text(strip=True)
        boat_no = _ZEN_NUM.get(raw, _int(raw))
        if boat_no is None:
            continue

        # ── tbody 全テキストから整備情報を抽出 ──
        full_text = tbody.get_text(separator=" ", strip=True)

        maintenance_text = ""
        prop_changed     = 0
        parts_changed    = 0
        maintenance_count = 0

        # 整備回数: "整備 N回" or "整備N回"
        m = re.search(r"整備\s*(\d+)\s*回", full_text)
        if m:
            maintenance_count = int(m.group(1))

        # 整備テキストが含まれる部分を切り出す
        for kw in _MAINT_KW:
            if kw in full_text:
                # キーワード周辺50文字を整備テキストとして記録
                idx = full_text.find(kw)
                snippet = full_text[max(0, idx - 5): idx + 60].strip()
                if len(snippet) > len(maintenance_text):
                    maintenance_text = snippet[:200]

        # プロペラ変更フラグ
        if any(w in full_text for w in ["プロペラ", "ペラ調整", "ペラ交換", "Ｐ交換"]):
            prop_changed = 1

        # 部品交換フラグ
        if any(w in full_text for w in ["部品交換", "エンジン交換", "キャブ交換"]):
            parts_changed = 1

        # ── コメント抽出 ──
        # 戦略1: class名に "comment" を含む td
        player_comment = ""
        comment_td = tbody.find("td", class_=re.compile(r"comment", re.I))
        if comment_td:
            player_comment = comment_td.get_text(separator="", strip=True)[:300]

        # 戦略2: 最終行の最終tdがコメントのことが多い（整備キーワードがある場合は除外）
        if not player_comment and len(trs) >= 3:
            last_tr = trs[-1]
            tds = last_tr.find_all("td")
            for td in reversed(tds):
                txt = td.get_text(separator="", strip=True)
                # コメントっぽい条件: 10文字以上かつ整備キーワードを含まない
                if (len(txt) >= 10
                        and not any(kw in txt for kw in _MAINT_KW)
                        and not re.match(r"^[\d\s.]+$", txt)):
                    player_comment = txt[:300]
                    break

        # 戦略3: tbody内の全テキストから「。」「！」などで終わる文を探す
        if not player_comment:
            sentences = re.findall(r"[^。！？\n]{10,50}[。！？]", full_text)
            if sentences:
                player_comment = sentences[-1][:300]

        results.append({
            "boat_no":          boat_no,
            "maintenance_text": maintenance_text,
            "prop_changed":     prop_changed,
            "parts_changed":    parts_changed,
            "maintenance_count": maintenance_count,
            "player_comment":   player_comment,
            "comment_sentiment": _sentiment(player_comment),
        })

    return results


# ────────────────────────────────────────────────────────────────────────────
# DB 書き込み
# ────────────────────────────────────────────────────────────────────────────
def save_entry_notes(conn: sqlite3.Connection, race_id: int, notes: list[dict]) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    count = 0
    for n in notes:
        conn.execute("""
            INSERT INTO entry_notes (
                race_id, boat_no,
                maintenance_text, prop_changed, parts_changed, maintenance_count,
                player_comment, comment_sentiment, scraped_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(race_id, boat_no) DO UPDATE SET
                maintenance_text   = excluded.maintenance_text,
                prop_changed       = excluded.prop_changed,
                parts_changed      = excluded.parts_changed,
                maintenance_count  = excluded.maintenance_count,
                player_comment     = excluded.player_comment,
                comment_sentiment  = excluded.comment_sentiment,
                scraped_at         = excluded.scraped_at
        """, (
            race_id, n["boat_no"],
            n["maintenance_text"], n["prop_changed"], n["parts_changed"],
            n["maintenance_count"], n["player_comment"], n["comment_sentiment"],
            now,
        ))
        count += 1
    conn.commit()
    return count


# ────────────────────────────────────────────────────────────────────────────
# メイン
# ────────────────────────────────────────────────────────────────────────────
def main() -> None:
    now = datetime.now()
    if not (8 <= now.hour < 21 or (now.hour == 21 and now.minute <= 30)):
        sys.exit(0)

    conn = open_db()
    ensure_tables(conn)

    # 当日の出走表あり・未取得or2時間以上更新なしのレースを対象
    races = conn.execute("""
        SELECT r.id, r.venue_code, r.race_no, r.scheduled_time
        FROM races r
        LEFT JOIN race_result_entries rre ON rre.race_id = r.id AND rre.rank = 1
        WHERE r.date = ?
          AND rre.id IS NULL
          AND EXISTS (SELECT 1 FROM entries e WHERE e.race_id = r.id)
          AND NOT EXISTS (
              SELECT 1 FROM entry_notes en
              WHERE en.race_id = r.id
                AND en.scraped_at >= datetime('now', '-2 hours')
          )
        ORDER BY r.scheduled_time, r.venue_code, r.race_no
    """, (TODAY,)).fetchall()
    conn.close()

    if not races:
        log.info("取得対象レースなし → 終了")
        return

    log.info("=== meet_scraper 開始 (%d件) ===", len(races))
    total_saved = total_maint = total_comment = 0

    for race_id, vcode, rno, stime in races:
        vname = VENUE_NAMES.get(vcode, vcode)
        soup  = fetch_racelist(vcode, rno, TODAY)
        if not soup:
            continue

        notes = parse_racelist_notes(soup)
        if not notes:
            log.warning("  %s %dR: パース結果なし（HTML構造要確認）", vname, rno)
            continue

        acquire_write_lock(wait=True, timeout=60)
        try:
            c = open_db()
            saved = save_entry_notes(c, race_id, notes)
            c.close()
        finally:
            release_write_lock()

        n_maint   = sum(1 for n in notes if n["maintenance_count"] > 0 or n["prop_changed"])
        n_comment = sum(1 for n in notes if n["player_comment"])
        total_saved   += saved
        total_maint   += n_maint
        total_comment += n_comment
        log.info("  %s %dR: 保存 %d艇 (整備あり:%d / コメントあり:%d)",
                 vname, rno, saved, n_maint, n_comment)

    log.info("=== meet_scraper 完了 (合計 %d艇, 整備:%d, コメント:%d) ===",
             total_saved, total_maint, total_comment)


if __name__ == "__main__":
    main()
