#!/usr/bin/env python3
"""
venue_scraper.py — 各会場サイトからオリジナル展示タイムを取得

取得データ（会場公式サイト）:
  - 一周タイム  → before_info.lap_time
  - まわり足    → before_info.mawariashi_time  (新規カラム)
  - 直線タイム  → before_info.straight_time

対応会場:
  タイプA group-cyokuzen.php: 浜名湖・常滑・三国・尼崎・鳴門・下関・若松・芦屋・唐津
  タイプB cyokuzen.php      : 桐生・びわこ・福岡
  タイプC oriten.php        : 多摩川（独自形式）
  タイプD tenji.php         : 徳山（独自形式、直線なし）
  タイプE xml_toda          : 戸田（XML形式）
  タイプF miyajima          : 宮島（POST形式）
  タイプG suminoe           : 住之江（独自形式、直線なし）

非対応:
  江戸川(03), 津(09): 公式にオリジナル展示なし
  大村(24)         : 一周/まわり足はJavaScript描画のため取得不可（展示タイムのみ）
  平和島/蒲郡/児島/丸亀: 未調査 (Phase 3)
"""

import sqlite3
import time
import logging
from pathlib import Path
import sys

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from scraper import DB_PATH, TODAY, VENUE_NAMES, REQ_DELAY, _ensure_column, init_db
from db_lock import acquire_write_lock, release_write_lock

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# ── 会場ごとのURL設定 ────────────────────────────────────
# (domain, php_type)
# php_type:
#   "group"    → group-cyokuzen.php?kind=2  (XOOPS odd/even行、col5-7)
#   "single"   → cyokuzen.php?kind=2        (XOOPS odd/even行、col5-7)
#   "oriten"   → oriten.php                 (多摩川独自、plain行、col6-8)
#   "tenji"    → tenji.php                  (徳山独自、col1=艇番、col9=一周、col10=まわり足)
#   "xml_toda" → XML GET                    (戸田独自、teiban/rnd/cnr/str)
#   "miyajima" → POST kaisai_reload.php     (宮島独自、race=N&date=0、col0=枠、col5-7)
#   "suminoe"  → yoso05{RR:02d}.htm         (住之江独自、col0=枠、col5=一周、col6=まわり足、直線なし)
VENUE_ORITEN_CONFIG: dict[str, tuple[str, str]] = {
    # タイプA: group-cyokuzen.php
    "06": ("www.boatrace-hamanako.jp",    "group"),
    "08": ("www.boatrace-tokoname.jp",    "group"),
    "10": ("www.boatrace-mikuni.jp",      "group"),
    "13": ("www.boatrace-amagasaki.jp",   "group"),
    "14": ("www.n14.jp",                  "group"),
    "19": ("www.boatrace-shimonoseki.jp", "group"),
    "20": ("www.wmb.jp",                  "group"),
    "21": ("www.boatrace-ashiya.com",     "group"),
    "23": ("www.boatrace-karatsu.jp",     "group"),
    # タイプB: cyokuzen.php
    "01": ("www.kiryu-kyotei.com",        "single"),
    "11": ("www.boatrace-biwako.jp",      "single"),
    "22": ("www.boatrace-fukuoka.com",    "single"),
    # タイプC: oriten.php（多摩川独自）
    "05": ("www.boatrace-tamagawa.com",   "oriten"),
    # タイプD: tenji.php（徳山独自）一周/まわり足のみ（直線なし）
    "18": ("www.boatrace-tokuyama.jp",    "tenji"),
    # タイプE: XML（戸田独自）
    "02": ("www.boatrace-toda.jp",        "xml_toda"),
    # タイプF: POST kaisai_reload.php（宮島独自）
    "17": ("www.boatrace-miyajima.com",   "miyajima"),
    # タイプG: yoso05{RR:02d}.htm（住之江独自）一周/まわり足のみ（直線なし）
    "12": ("www.boatrace-suminoe.jp",     "suminoe"),
}


def build_url(venue_code: str, date_str: str, race_no: int) -> str | None:
    cfg = VENUE_ORITEN_CONFIG.get(venue_code)
    if not cfg:
        return None
    domain, php_type = cfg
    if php_type == "oriten":
        return f"https://{domain}/modules/yosou/oriten.php?day={date_str}&race={race_no}"
    if php_type == "tenji":
        return f"https://{domain}/modules/yosou/tenji.php?day={date_str}&race={race_no}"
    if php_type == "xml_toda":
        return f"https://{domain}/xml/kaisai/{date_str}/race_table_original_{race_no:02d}.xml"
    if php_type == "miyajima":
        # POST エンドポイント（race_no は scrape_oriten_for_races で使用）
        return f"https://{domain}/race_common/require/kaisai_reload.php"
    if php_type == "suminoe":
        # 住之江独自: /asp/kyogi/12/sp/yoso05{RR:02d}.htm
        return f"https://{domain}/asp/kyogi/12/sp/yoso05{race_no:02d}.htm"
    php = "group-cyokuzen" if php_type == "group" else "cyokuzen"
    return f"https://{domain}/modules/yosou/{php}.php?day={date_str}&race={race_no}&kind=2"


def fetch_venue(url: str) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            resp.encoding = resp.apparent_encoding or "utf-8"
            return BeautifulSoup(resp.text, "html.parser")
        log.warning("  HTTP %d: %s", resp.status_code, url)
    except Exception as e:
        log.warning("  fetch error: %s — %s", url, e)
    return None


def parse_oriten(soup: BeautifulSoup, php_type: str = "group") -> dict[int, dict]:
    """
    テーブルから オリジナル展示タイム を解析する。

    XOOPSスタイル（group/single）:
      HTML構造（三国 kind=2 で確認済み）:
        ヘッダー行: 枠 | 選手 | 体重 | チルト | 展示タイム | 一周 | まわり足 | 直線
        データ行(odd/even): 艇番 | 選手 | 体重 | チルト | 展示 | shukai | mawari | choku
        調整行: colspan行（調整重量）
      カラム: [0]=枠, [1]=選手, [2]=体重, [3]=チルト, [4]=展示, [5]=一周, [6]=まわり足, [7]=直線

    多摩川スタイル（oriten）:
      HTML構造（oriten.php で確認済み）:
        ヘッダー行: 枠 | 選手 | 体重 | 調整 | チルト | 展示タイム | 一周 | まわり足 | 直線
        データ行（クラスなし）: 艇番 | 選手 | 体重 | 調整 | チルト | 展示 | shukai | mawari | choku
      カラム: [0]=枠, [1]=選手, [2]=体重, [3]=調整, [4]=チルト, [5]=展示, [6]=一周, [7]=まわり足, [8]=直線

    徳山スタイル（tenji）:
      HTML構造（tenji.php で確認済み）:
        ヘッダー行: 気配 | 枠 | 選手名 | 体重 | 調整 | モーター番号 | 2連対率 | チルト | 展示タイム | 一周 | まわり足 | ...
        データ行（クラスなし）
      カラム: [1]=枠(艇番), [9]=一周, [10]=まわり足（直線なし）

    Returns: {boat_no: {lap_time, mawariashi_time, straight_time}}
    """
    table = soup.find("table")
    if not table:
        return {}

    results: dict[int, dict] = {}
    rows = table.find_all("tr")

    def _f(cell) -> float | None:
        txt = cell.get_text(strip=True)
        try:
            return float(txt)
        except ValueError:
            return None

    if php_type == "oriten":
        # 多摩川独自形式: odd/evenクラスなし、調整列あり（col offset +1）
        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) < 7:
                continue
            try:
                boat_no = int(cells[0].get_text(strip=True))
                if not (1 <= boat_no <= 6):
                    continue
            except (ValueError, IndexError):
                continue
            results[boat_no] = {
                "lap_time":        _f(cells[6]) if len(cells) > 6 else None,
                "mawariashi_time": _f(cells[7]) if len(cells) > 7 else None,
                "straight_time":   _f(cells[8]) if len(cells) > 8 else None,
            }
    elif php_type == "tenji":
        # 徳山独自形式: cells[1]=枠(艇番), cells[9]=一周, cells[10]=まわり足, 直線なし
        # ヘッダー: 気配|枠|選手名|体重|調整|モーター番号|2連対率|チルト|展示タイム|一周|まわり足|...
        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) < 11:
                continue
            try:
                boat_no = int(cells[1].get_text(strip=True))
                if not (1 <= boat_no <= 6):
                    continue
            except (ValueError, IndexError):
                continue
            results[boat_no] = {
                "lap_time":        _f(cells[9]),
                "mawariashi_time": _f(cells[10]),
                "straight_time":   None,
            }
    else:
        # XOOPSスタイル（group/single）: odd/evenクラスのデータ行
        for row in rows:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            row_cls = row.get("class", [])
            if not any(c in ("odd", "even") for c in row_cls):
                continue
            if len(cells) < 7:
                continue
            try:
                boat_no = int(cells[0].get_text(strip=True))
            except (ValueError, IndexError):
                continue
            results[boat_no] = {
                "lap_time":        _f(cells[5]) if len(cells) > 5 else None,
                "mawariashi_time": _f(cells[6]) if len(cells) > 6 else None,
                "straight_time":   _f(cells[7]) if len(cells) > 7 else None,
            }

    return results


def parse_oriten_toda(content: bytes) -> dict[int, dict]:
    """
    戸田独自形式: XML から展示タイムを解析。
    URL: /xml/kaisai/{DATE}/race_table_original_{RR:02d}.xml
    タグ: <teiban>=艇番, <rnd>=一周, <cnr>=まわり足, <str>=直線
    """
    def _f(tag) -> float | None:
        if not tag:
            return None
        try:
            return float(tag.get_text(strip=True))
        except ValueError:
            return None

    try:
        import warnings
        from bs4 import XMLParsedAsHTMLWarning
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
            soup = BeautifulSoup(content, "html.parser")
    except Exception:
        return {}

    results: dict[int, dict] = {}
    for record in soup.find_all("record"):
        teiban = record.find("teiban")
        if not teiban:
            continue
        try:
            boat_no = int(teiban.get_text(strip=True))
            if not (1 <= boat_no <= 6):
                continue
        except ValueError:
            continue
        results[boat_no] = {
            "lap_time":        _f(record.find("rnd")),
            "mawariashi_time": _f(record.find("cnr")),
            "straight_time":   _f(record.find("str")),
        }
    return results


def parse_oriten_miyajima(html: str) -> dict[int, dict]:
    """
    宮島独自形式: kaisai_reload.php の HTML レスポンスから展示タイムを解析。
    POST data: race={N}&date=0
    対象テーブル: class="top_playertable" のうち "まわり足" を含む最初のもの
    カラム: [0]=枠, [1]=選手名, [2]=体重, [3]=チルト, [4]=展示, [5]=一周, [6]=まわり足, [7]=直線
    調整行（1セル）はスキップ
    """
    soup = BeautifulSoup(html, "html.parser")
    target = None
    for t in soup.find_all("table", class_="top_playertable"):
        if "まわり足" in t.get_text():
            target = t
            break
    if not target:
        return {}

    def _f(cell) -> float | None:
        try:
            return float(cell.get_text(strip=True))
        except ValueError:
            return None

    results: dict[int, dict] = {}
    for row in target.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 8:
            continue
        try:
            boat_no = int(cells[0].get_text(strip=True))
            if not (1 <= boat_no <= 6):
                continue
        except (ValueError, IndexError):
            continue
        results[boat_no] = {
            "lap_time":        _f(cells[5]),
            "mawariashi_time": _f(cells[6]),
            "straight_time":   _f(cells[7]),
        }
    return results


def parse_oriten_suminoe(html: str) -> dict[int, dict]:
    """
    住之江独自形式: yoso05{RR:02d}.htm の「オリジナル展示」テーブルを解析。
    URL: https://www.boatrace-suminoe.jp/asp/kyogi/12/sp/yoso05{RR:02d}.htm
    テーブル構造（7カラム）:
      ヘッダー行: 枠 | 選手名 | 体重 | チルト | 展示 | 一周 | まわり足
      サブヘッダー: (empty) × 2 | 調整 | (empty) × 4
      データ行: [0]=枠番, [1]=選手名, [2]=体重, [3]=チルト, [4]=展示, [5]=一周, [6]=まわり足
      調整行: [0-1]=empty, [2]=調整重量, [3-6]=empty
    直線なし。"まわり足" テキストを含む最初のテーブルを対象とする。
    """
    soup = BeautifulSoup(html, "html.parser")
    target = None
    for t in soup.find_all("table"):
        if "まわり足" in t.get_text():
            target = t
            break
    if not target:
        return {}

    def _f(cell) -> float | None:
        try:
            return float(cell.get_text(strip=True))
        except ValueError:
            return None

    results: dict[int, dict] = {}
    for row in target.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 7:
            continue
        try:
            boat_no = int(cells[0].get_text(strip=True))
            if not (1 <= boat_no <= 6):
                continue
        except (ValueError, IndexError):
            continue
        results[boat_no] = {
            "lap_time":        _f(cells[5]),
            "mawariashi_time": _f(cells[6]),
            "straight_time":   None,
        }
    return results


def save_oriten(conn: sqlite3.Connection, race_id: int, oriten: dict[int, dict]) -> int:
    """
    oriten データを before_info テーブルに UPDATE する。
    mawariashi_time カラムが存在しない場合は追加してからリトライ。
    """
    saved = 0
    for boat_no, data in oriten.items():
        try:
            rows_affected = conn.execute("""
                UPDATE before_info
                   SET lap_time        = ?,
                       mawariashi_time = ?,
                       straight_time   = ?
                 WHERE race_id = ?
                   AND boat_no  = ?
            """, (
                data.get("lap_time"),
                data.get("mawariashi_time"),
                data.get("straight_time"),
                race_id,
                boat_no,
            )).rowcount
        except sqlite3.OperationalError as e:
            if "no such column" in str(e):
                # カラムが存在しない → 追加してリトライ
                ensure_oriten_columns(conn)
                try:
                    rows_affected = conn.execute("""
                        UPDATE before_info
                           SET lap_time        = ?,
                               mawariashi_time = ?,
                               straight_time   = ?
                         WHERE race_id = ?
                           AND boat_no  = ?
                    """, (
                        data.get("lap_time"),
                        data.get("mawariashi_time"),
                        data.get("straight_time"),
                        race_id,
                        boat_no,
                    )).rowcount
                except sqlite3.OperationalError as e2:
                    log.warning("  UPDATE 失敗 (boat_no=%d): %s", boat_no, e2)
                    rows_affected = 0
            else:
                log.warning("  UPDATE 失敗 (boat_no=%d): %s", boat_no, e)
                rows_affected = 0
        if rows_affected:
            saved += 1
    return saved


def needs_oriten(conn: sqlite3.Connection, race_id: int) -> bool:
    """まだ mawariashi_time が未取得なら True。カラム未作成時は True を返す。"""
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM before_info WHERE race_id=? AND mawariashi_time IS NOT NULL",
            (race_id,)
        ).fetchone()
        return row[0] == 0
    except sqlite3.OperationalError:
        # カラムが存在しない場合はまだ未取得扱い
        return True


# ── DB 初期化（カラム追加） ────────────────────────────────
def ensure_oriten_columns(conn: sqlite3.Connection) -> None:
    """
    mawariashi_time カラムが未存在なら追加する。
    DDL は autocommit 接続で行う（busy_timeout + WAL 確実適用のため別接続を使う）。
    """
    # まず読み取りで存在確認（ロック不要）
    existing = {row[1] for row in conn.execute("PRAGMA table_info(before_info)").fetchall()}
    if "mawariashi_time" in existing:
        return

    # DDL 用に autocommit モードの別接続を開く
    from db_connect import open_db_autocommit
    ddl_conn = open_db_autocommit()
    try:
        ddl_conn.execute("ALTER TABLE before_info ADD COLUMN mawariashi_time REAL")
        log.info("mawariashi_time カラムを追加しました")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e):
            pass  # 他プロセスが先に追加済み
        else:
            log.warning("カラム追加をスキップ: %s", e)
    finally:
        ddl_conn.close()


# ── メイン ─────────────────────────────────────────────
def _fetch_oriten_single(vcode: str, rno: int, race_id: int):
    """1レース分のオリジナル展示タイムをHTTP取得（DB書き込みなし）。"""
    cfg = VENUE_ORITEN_CONFIG.get(vcode)
    if not cfg:
        return None
    _, php_type = cfg

    vname = VENUE_NAMES.get(vcode, vcode)
    url = build_url(vcode, TODAY, rno)

    try:
        if php_type == "xml_toda":
            log.info("  %s %dR: オリジナル展示取得(XML) → %s", vname, rno, url)
            resp = requests.get(url, headers=HEADERS, timeout=15)
            time.sleep(REQ_DELAY)
            if resp.status_code != 200:
                log.warning("  HTTP %d: %s", resp.status_code, url)
                return None
            oriten = parse_oriten_toda(resp.content)

        elif php_type == "miyajima":
            log.info("  %s %dR: オリジナル展示取得(POST) → %s race=%d", vname, rno, url, rno)
            resp = requests.post(url, headers=HEADERS,
                                 data={"race": rno, "date": 0}, timeout=20)
            time.sleep(REQ_DELAY)
            if resp.status_code != 200:
                log.warning("  HTTP %d: %s", resp.status_code, url)
                return None
            resp.encoding = resp.apparent_encoding or "utf-8"
            oriten = parse_oriten_miyajima(resp.text)

        elif php_type == "suminoe":
            log.info("  %s %dR: オリジナル展示取得(住之江) → %s", vname, rno, url)
            resp = requests.get(url, headers=HEADERS, timeout=15)
            time.sleep(REQ_DELAY)
            if resp.status_code != 200:
                log.warning("  HTTP %d: %s", resp.status_code, url)
                return None
            resp.encoding = resp.apparent_encoding or "utf-8"
            oriten = parse_oriten_suminoe(resp.text)

        else:
            log.info("  %s %dR: オリジナル展示取得 → %s", vname, rno, url)
            soup = fetch_venue(url)
            time.sleep(REQ_DELAY)
            if not soup:
                return None
            oriten = parse_oriten(soup, php_type=php_type)

    except Exception as e:
        log.warning("  fetch error %s %dR: %s", vname, rno, e)
        return None

    if not oriten:
        log.warning("  %s %dR: データなし（展示前or対応ページなし）", vname, rno)
        return None

    return (vcode, rno, race_id, oriten)


def scrape_oriten_for_races(races: list[tuple[str, int, int]]) -> None:
    """
    races: [(venue_code, race_no, race_id), ...]
    展示タイムが存在する（before_info に exhibition_time あり）かつ
    mawariashi_time が未取得のレースにのみアクセスする。並列取得版。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from db_connect import open_db

    conn = open_db()
    ensure_oriten_columns(conn)

    # 取得対象を絞り込み（対応会場 & 未取得のみ）
    targets = []
    for vcode, rno, race_id in races:
        if not VENUE_ORITEN_CONFIG.get(vcode):
            log.debug("  %s: オリジナル展示非対応", VENUE_NAMES.get(vcode, vcode))
            continue
        if not needs_oriten(conn, race_id):
            log.info("  %s %dR: オリジナル展示取得済みスキップ",
                     VENUE_NAMES.get(vcode, vcode), rno)
            continue
        targets.append((vcode, rno, race_id))

    if not targets:
        conn.close()
        return

    # 並列HTTP取得
    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_fetch_oriten_single, v, rno, rid): (v, rno, rid)
                   for v, rno, rid in targets}
        for future in as_completed(futures):
            data = future.result()
            if data:
                results.append(data)

    # 順次DB書き込み（書き込みロック保持中）
    acquire_write_lock(wait=True, timeout=300)
    try:
        for vcode, rno, race_id, oriten in results:
            vname = VENUE_NAMES.get(vcode, vcode)
            saved = save_oriten(conn, race_id, oriten)
            if saved:
                conn.commit()
                log.info("  %s %dR: %d艇分保存", vname, rno, saved)
            else:
                log.warning("  %s %dR: before_info 行なし", vname, rno)
    finally:
        release_write_lock()

    conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    # スタンドアロン実行: 今日の未確定レースに対して実行
    from db_connect import open_db
    conn = open_db()
    ensure_oriten_columns(conn)

    pending = conn.execute("""
        SELECT r.venue_code, r.race_no, r.id
        FROM races r
        LEFT JOIN race_result_entries rre ON rre.race_id = r.id AND rre.rank = 1
        WHERE r.date = ?
          AND rre.id IS NULL
          AND EXISTS (SELECT 1 FROM entries e WHERE e.race_id = r.id)
          AND EXISTS (SELECT 1 FROM before_info bi WHERE bi.race_id = r.id AND bi.exhibition_time IS NOT NULL)
        ORDER BY r.venue_code, r.race_no
    """, (TODAY,)).fetchall()
    conn.close()

    log.info("オリジナル展示対象: %d レース", len(pending))
    scrape_oriten_for_races(pending)
    log.info("完了")
