import os
import re
import time
import html
from html.parser import HTMLParser
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

# ============================================================
# Shinsum Monitor - 初日/最終日専用 本番ST予測
# ============================================================
# 通知対象:
#   ・指定17場のうち「ボートレース日和で当日が初日または最終日と確認できた場」だけ
#   ・1R〜12Rのうち締切前のレースだけ
#   ・締切済みレースは展示/ST/日和取得をせず完全スキップ
#   ・展示STが6艇分そろった後に各R 1回だけ通知
#
# 本番ST予測に使う材料:
#   1. 当地ST順位
#   2. 初日ST順位
#   3. 最終日ST順位（最終日のみ）
#   4. F有無 / F持ちST順位
#   5. 今節平均ST（最終日のみ。初日は使わない）
#   6. トップST分析（直近1年）
#      - トップST確率
#      - トップST時1着率
#   7. 展示ST
#
# 「直近1か月」「直近3か月」は予測に使わない。
# 「コース補正」も使わない。
# 2着予想・最終1着率も出さない。
# ============================================================

JST = ZoneInfo("Asia/Tokyo")

NTFY_TOPIC = os.environ["NTFY_TOPIC"]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))

TARGET_VENUES = [
    "浜名湖",
    "平和島", "児島", "戸田", "多摩川",
    "蒲郡", "びわこ", "三国", "鳴門",
    "宮島", "徳山", "下関", "若松",
    "芦屋", "唐津", "大村", "住之江",
]

VENUE_CODES = {
    "浜名湖": 6,
    "戸田": 2, "平和島": 4, "多摩川": 5, "蒲郡": 7,
    "三国": 10, "びわこ": 11, "住之江": 12, "鳴門": 14,
    "児島": 16, "宮島": 17, "徳山": 18, "下関": 19,
    "若松": 20, "芦屋": 21, "唐津": 23, "大村": 24,
}

# ------------------------------------------------------------
# 予測配分
# ------------------------------------------------------------
# 展示前の「基礎ST傾向」部分を100として作り、
# 最後に展示STを20%だけ混ぜる。
#
# 基礎ST傾向の内訳:
#   当地ST順位        20%
#   初日ST順位        15%
#   最終日ST順位      15%
#   今節平均ST        25%
#   トップST分析      15%
#   F補正             10%
#
# 最終出力:
#   基礎傾向 80% + 展示ST 20%
#
# ※コース補正は入れない。
BASE_W_LOCAL = 0.20
BASE_W_FIRSTDAY = 0.15
BASE_W_FINALDAY = 0.15
BASE_W_SETSU = 0.25
BASE_W_TOPSTART = 0.15
BASE_W_F = 0.10
FINAL_W_BASE = 0.80
FINAL_W_EXHIBITION = 0.20

_http = requests.Session()
_http.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.7,en;q=0.5",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "close",
})

seen = set()
_final_day_cache = {}
_deadline_cache = {}


def now():
    return datetime.now(JST)


def hd():
    return now().strftime("%Y%m%d")


def active():
    return 8 <= now().hour < 23


def hiyori_url(venue, race_no):
    return (
        "https://kyoteibiyori.com/race_shusso.php"
        f"?place_no={VENUE_CODES[venue]}&race_no={race_no}&hiduke={hd()}"
    )


def official_before_url(venue, race_no):
    """
    展示情報はスマホ版BOATRACE公式を使用。
    PC版は展示更新直後に空欄HTMLを返す場合があるため使わない。
    キャッシュ回避用のtsも付ける。
    """
    return (
        "https://www.boatrace.jp/owsp/sp/race/beforeinfo"
        f"?hd={hd()}&jcd={VENUE_CODES[venue]:02d}&rno={race_no}"
        f"&ts={int(time.time())}"
    )


def official_before_pc_url(venue, race_no):
    """
    展示取得の予備系としてPC版も用意。
    """
    return (
        "https://www.boatrace.jp/owpc/pc/race/beforeinfo"
        f"?hd={hd()}&jcd={VENUE_CODES[venue]:02d}&rno={race_no}"
        f"&ts={int(time.time())}"
    )


def _float(s):
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def _pct(s):
    v = _float(s)
    return v


def _rank(s):
    v = _float(s)
    if v is None:
        return None
    if 1.0 <= v <= 6.0:
        return float(v)
    return None


def _st(s):
    """
    .12 / 0.12 / F.03 / F03 を秒へ。
    Fは負値で返す。
    """
    if s is None:
        return None
    s = str(s).upper().replace(" ", "")
    m = re.search(r"F\.?(\d{1,2})", s)
    if m:
        return -int(m.group(1)) / 100.0

    m = re.search(r"0\.(\d{2})", s)
    if m:
        return int(m.group(1)) / 100.0

    m = re.search(r"(?<!\d)\.(\d{2})(?!\d)", s)
    if m:
        return int(m.group(1)) / 100.0

    return None


def _selected_texts(page):
    try:
        return page.locator("select").evaluate_all(
            """sels => sels.map(s => {
                const o = s.selectedOptions && s.selectedOptions[0];
                return o ? (o.textContent || '').trim() : '';
            })"""
        )
    except Exception:
        return []


def get_hiyori_event_day(page, venue):
    """
    ボートレース日和の「当日選択欄」だけを見て、
    今日が 初日 / 最終日 / その他 のどれかを返す。

    ST順位表の中にある「初日」「最終日」という文字は、
    開催日判定には使わない。
    """
    cache_key = f"{hd()}|{venue}"
    if cache_key in _final_day_cache:
        return _final_day_cache[cache_key]

    p = page.context.new_page()
    try:
        p.goto(hiyori_url(venue, 1), wait_until="domcontentloaded", timeout=25000)
        p.wait_for_timeout(700)

        selected = _selected_texts(p)

        event_day = None

        # 日付selectと開催日selectが別々でも判定できるようにする。
        # 例: selected=["8月25日", "初日"] / ["8月30日", "最終日"]
        # 旧UIの "8月25日 / 初日" 形式にも対応する。
        for t in selected:
            normalized = re.sub(r"\s+", "", str(t))
            if normalized == "初日" or re.search(r"(?:/|／)初日$", normalized):
                event_day = "初日"
                break
            if normalized == "最終日" or re.search(r"(?:/|／)最終日$", normalized):
                event_day = "最終日"
                break

        # selectが独自UIで拾えない場合だけ、ページ上部を補助確認
        if event_day is None and not selected:
            body = p.locator("body").inner_text(timeout=7000)
            head = body[:1800]

            if re.search(r"\d{1,2}月\d{1,2}日\s*(?:/|／)\s*初日", head):
                event_day = "初日"
            elif re.search(r"\d{1,2}月\d{1,2}日\s*(?:/|／)\s*最終日", head):
                event_day = "最終日"

        _final_day_cache[cache_key] = event_day

        print(
            f"[EVENT-DAY] {venue} -> {event_day or '対象外'}"
            f" / selected={selected[:3]}",
            flush=True
        )

        return event_day

    except Exception as e:
        print(f"[EVENT-DAY-ERR] {venue}: {e!r} -> 対象外", flush=True)
        _final_day_cache[cache_key] = None
        return None
    finally:
        p.close()

def detect_target_venues(page):
    """
    16場を場単位で1回だけ判定。
    今日が「最終日」の場だけ返す。
    戻り値: {venue: "最終日"}
    """
    out = {}

    for venue in TARGET_VENUES:
        event_day = get_hiyori_event_day(page, venue)
        if event_day == "最終日":
            out[venue] = event_day

    print(
        "[TARGET-DAY] "
        + (
            ", ".join(f"{v}({d})" for v, d in out.items())
            if out else "なし"
        ),
        flush=True
    )

    return out

def _row_cells(page):
    """
    全trを二次元配列にして返す。
    """
    return page.locator("tr").evaluate_all(
        """els => els.map(tr =>
            Array.from(tr.querySelectorAll('th,td'))
              .map(x => (x.innerText || x.textContent || '').trim())
        )"""
    )


def fetch_hiyori_data(page, venue, race_no, event_day):
    """
    ボートレース日和から、その開催日に実際に使うデータだけ取得。

    初日:
      当地ST順位 / 初日ST順位 / F / トップST分析(直近1年)
      ※最終日ST順位・今節平均STは取得も使用もしない

    最終日:
      当地ST順位 / 最終日ST順位 / 今節平均ST / F / トップST分析(直近1年)
      ※初日ST順位は取得も使用もしない
    """
    p = page.context.new_page()
    out = {i: {} for i in range(1, 7)}

    try:
        p.goto(
            hiyori_url(venue, race_no),
            wait_until="domcontentloaded",
            timeout=25000
        )

        try:
            p.wait_for_function(
                """() => {
                    const t = document.body?.innerText || '';
                    return t.includes('ST順位');
                }""",
                timeout=9000
            )
        except Exception:
            p.wait_for_timeout(1000)

        rows = _row_cells(p)

        # ---------- ST順位 / 今節平均ST ----------
        for cells in rows:
            if len(cells) < 7:
                continue

            label = re.sub(r"\s+", "", str(cells[0]))
            vals = cells[1:7]

            key = None
            parser = _rank

            if label == "当地" or label.startswith("当地ST"):
                key = "local_rank"

            elif event_day == "初日" and (
                label == "初日" or label.startswith("初日ST")
            ):
                key = "firstday_rank"

            elif event_day == "最終日" and (
                label == "最終日" or label.startswith("最終日ST")
            ):
                key = "finalday_rank"

            elif label in ("F持", "F持ち") or label.startswith("F持"):
                key = "f_rank"

            elif event_day == "最終日" and label == "平均ST":
                key = "setsu_avg_st"
                parser = _float

            if key:
                for boat, raw in enumerate(vals[:6], 1):
                    v = parser(raw)
                    if v is None:
                        continue
                    if key == "setsu_avg_st" and not (0.05 <= v <= 0.35):
                        continue
                    out[boat][key] = float(v)

        # ---------- トップスタート分析（直近1年） ----------
        # popup/hidden DOMでも取れるよう innerText ではなく textContent を使用。
        all_rows = p.locator("tr").evaluate_all(
            """els => els.map(tr =>
                Array.from(tr.querySelectorAll('th,td'))
                    .map(x => (x.textContent || '').replace(/\\s+/g,' ').trim())
            )"""
        )

        # 「トップスタート分析」見出しを含むDOM全体のテキスト
        body_text = p.locator("body").evaluate(
            """el => el.textContent || ''"""
        )
        compact_body = re.sub(r"\s+", "", body_text)
        top_anchor = compact_body.find("トップスタート分析")

        candidates = []
        for cells in all_rows:
            if len(cells) < 5:
                continue

            joined = " ".join(cells)
            compact = re.sub(r"\s+", "", joined)
            pcts = re.findall(r"(\d+(?:\.\d+)?)\s*%", joined)

            # TopST確率 + 1着率 の2つの%がある行
            if len(pcts) < 2:
                continue

            # 可能ならトップスタート分析より後ろに存在する行だけ採用
            if top_anchor >= 0:
                token = compact[:10]
                pos = compact_body.find(token, top_anchor) if token else -1
                if pos < 0:
                    continue

            candidates.append(
                (float(pcts[0]), float(pcts[1]))
            )

        if len(candidates) >= 6:
            for boat, (prob, win_rate) in enumerate(candidates[:6], 1):
                out[boat]["top_start_prob"] = prob
                out[boat]["top_start_win"] = win_rate

        # ---------- F有無 ----------
        for boat in range(1, 7):
            out[boat]["has_f"] = out[boat].get("f_rank") is not None

        # 念のため不要データを物理削除
        if event_day == "初日":
            for boat in range(1, 7):
                out[boat].pop("finalday_rank", None)
                out[boat].pop("setsu_avg_st", None)
        elif event_day == "最終日":
            for boat in range(1, 7):
                out[boat].pop("firstday_rank", None)

        # 実際に予測へ使う項目だけログに表示
        if event_day == "初日":
            print(
                f"[HIYORI] {venue}{race_no}R 初日 "
                + " / ".join(
                    f"{boat}:当地={out[boat].get('local_rank')},"
                    f"初日={out[boat].get('firstday_rank')},"
                    f"TOP={out[boat].get('top_start_prob')},"
                    f"TOP1着={out[boat].get('top_start_win')},"
                    f"F={'Y' if out[boat].get('has_f') else 'N'}"
                    for boat in range(1, 7)
                ),
                flush=True
            )
        else:
            print(
                f"[HIYORI] {venue}{race_no}R 最終日 "
                + " / ".join(
                    f"{boat}:当地={out[boat].get('local_rank')},"
                    f"最終={out[boat].get('finalday_rank')},"
                    f"節ST={out[boat].get('setsu_avg_st')},"
                    f"TOP={out[boat].get('top_start_prob')},"
                    f"TOP1着={out[boat].get('top_start_win')},"
                    f"F={'Y' if out[boat].get('has_f') else 'N'}"
                    for boat in range(1, 7)
                ),
                flush=True
            )

        missing_top = [
            boat for boat in range(1, 7)
            if out[boat].get("top_start_prob") is None
        ]
        if missing_top:
            print(
                f"[TOPSTART-INCOMPLETE] {venue}{race_no}R / "
                f"不足艇={missing_top}",
                flush=True
            )

        return out

    except Exception as e:
        print(f"[HIYORI-ERR] {venue}{race_no}R {e!r}", flush=True)
        return out
    finally:
        p.close()



def _official_tables(raw_html):
    p = _OfficialTableParser()
    p.feed(raw_html)
    return p.tables


def _extract_exhibition_from_html(raw_html):
    """
    BOATRACE公式のraw HTMLをtable構造で解析。
    艇番と同じ行の値を紐付けるので、単純な「6個順番取り」より安全。

    戻り値:
      {1: {"ex_st": .16, "ex_time": 6.78}, ...}
    """
    out = {i: {} for i in range(1, 7)}
    tables = _official_tables(raw_html)

    # -------- 展示タイム --------
    # 「調整重量」「展示タイム」等を含む選手表。
    for tb in tables:
        flat = " ".join(" ".join(row) for row in tb)
        if "展示タイム" not in flat and "調整重量" not in flat:
            continue

        for row in tb:
            joined = " ".join(row)

            # 行頭またはセル単体の艇番
            boat = None
            for cell in row[:3]:
                m = re.fullmatch(r"\s*([1-6])\s*", str(cell))
                if m:
                    boat = int(m.group(1))
                    break
            if boat is None:
                m = re.match(r"\s*([1-6])(?:\s|$)", joined)
                if m:
                    boat = int(m.group(1))

            if boat is None:
                continue

            vals = re.findall(r"(?<!\d)(6\.\d{2}|7\.\d{2})(?!\d)", joined)
            if vals:
                v = float(vals[0])
                if 6.20 <= v <= 7.80:
                    out[boat]["ex_time"] = v

    # -------- スタート展示 --------
    # スタート展示tableだけを選び、各行の艇番とSTを対応づける。
    for tb in tables:
        flat = " ".join(" ".join(row) for row in tb)
        if "スタート展示" not in flat and not ("コース" in flat and "ST" in flat):
            continue

        for row in tb:
            joined = " ".join(row)

            boat = None
            for cell in row[:3]:
                m = re.fullmatch(r"\s*([1-6])\s*", str(cell))
                if m:
                    boat = int(m.group(1))
                    break
            if boat is None:
                m = re.match(r"\s*([1-6])(?:\s|$)", joined)
                if m:
                    boat = int(m.group(1))

            if boat is None:
                continue

            # 艇番の数字をSTと誤認しないよう .xx / 0.xx / F.xx だけ許可
            m = re.search(
                r"F\.?\d{1,2}|0\.\d{2}|(?<!\d)\.\d{2}(?!\d)",
                joined,
                flags=re.I,
            )
            if not m:
                continue

            v = _st(m.group(0))
            if v is not None:
                out[boat]["ex_st"] = float(v)

    return out


def _html_to_text(raw):
    raw = re.sub(r"<script\b[^>]*>.*?</script>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<style\b[^>]*>.*?</style>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    raw = re.sub(r"</(?:tr|td|th|div|p|li|section|table)>", "\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = raw.replace("\r", "")
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n\s*\n+", "\n", raw)
    return raw.strip()


def _exhibition_complete(parsed):
    return all(
        parsed.get(b, {}).get("ex_st") is not None
        for b in range(1, 7)
    )


def _extract_exhibition_from_text(body):
    """
    BOATRACE公式スマホ版の表示済み本文から
    6艇分の展示STと展示タイムを取得する。

    展示ST:
      「スタート展示」以降だけを対象にし、
      .16 / F.05 等を順番に6個取得。

    展示タイム:
      「スタート展示」より前のレーサー表部分から、
      6.20〜7.80の値を順番に6個取得。
    """
    out = {i: {} for i in range(1, 7)}
    body = str(body).replace("\r", "")

    st_pos = body.find("スタート展示")

    # ---------- 展示ST ----------
    if st_pos >= 0:
        st_section = body[st_pos:st_pos + 2500]

        sts = []
        for tok in re.findall(
            r"F\.?\d{1,2}|(?<!\d)\.\d{2}(?!\d)|0\.\d{2}",
            st_section,
            flags=re.I,
        ):
            v = _st(tok)
            if v is not None:
                sts.append(float(v))
                if len(sts) == 6:
                    break

        if len(sts) == 6:
            for boat, v in enumerate(sts, 1):
                out[boat]["ex_st"] = v

    # ---------- 展示タイム ----------
    # スタート展示より前だけを見ることで、
    # 気温・水温・前走ST等の誤拾いを減らす。
    before = body[:st_pos] if st_pos >= 0 else body
    time_pos = before.find("展示タイム")
    if time_pos >= 0:
        time_section = before[time_pos:time_pos + 5000]
    else:
        time_section = before

    ex_times = []
    for m in re.finditer(
        r"(?<!\d)(6\.\d{2}|7\.\d{2})(?!\d)",
        time_section,
    ):
        v = float(m.group(1))
        if 6.20 <= v <= 7.80:
            ex_times.append(v)
            if len(ex_times) == 6:
                break

    if len(ex_times) == 6:
        for boat, v in enumerate(ex_times, 1):
            out[boat]["ex_time"] = v

    return out

def fetch_official_exhibition(page, venue, race_no):
    """
    6艇取得を最優先にした展示情報取得。

    順番:
      1) PC版 requests × 最大3回
      2) スマホ版 requests × 最大2回
      3) PC版 Playwright × 1回

    「6艇の展示STが揃った」時だけ READY。
    1〜5艇しか取れていない場合は絶対に通知へ進めない。
    """
    out = {i: {} for i in range(1, 7)}

    def merge(parsed):
        for boat in range(1, 7):
            row = parsed.get(boat, {})
            if row.get("ex_st") is not None:
                out[boat]["ex_st"] = row["ex_st"]
            if row.get("ex_time") is not None:
                out[boat]["ex_time"] = row["ex_time"]

    def got_st():
        return [b for b in range(1, 7) if out[b].get("ex_st") is not None]

    def got_time():
        return [b for b in range(1, 7) if out[b].get("ex_time") is not None]

    def log(source):
        print(
            f"[EXHIBITION-{source}] {venue} {race_no}R / "
            f"ST={got_st()} ({len(got_st())}/6) / "
            f"TIME={got_time()} ({len(got_time())}/6)",
            flush=True,
        )

    # --------------------------------------------------------
    # 1. PC版 requests
    # BOATRACE公式のbeforeinfoはPC版も展示STをHTMLに持っている。
    # Playwrightより軽く、GitHub Actionsでもまずこちらを試す。
    # --------------------------------------------------------
    for attempt in range(1, 4):
        url = official_before_pc_url(venue, race_no)
        try:
            r = _http.get(
                url,
                timeout=(4, 8),
                headers={
                    "Referer": "https://www.boatrace.jp/",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            r.raise_for_status()

            raw = r.text
            parsed = _extract_exhibition_from_html(raw)
            merge(parsed)

            # table構造で取れないサイト差分に備えて本文方式も併用
            if len(got_st()) < 6:
                text_parsed = _extract_exhibition_from_text(_html_to_text(raw))
                merge(text_parsed)

            log(f"PC-HTTP-{attempt}")

            if len(got_st()) == 6:
                break

        except Exception as e:
            print(
                f"[EXHIBITION-PC-HTTP-{attempt}-ERR] "
                f"{venue} {race_no}R {e!r}",
                flush=True,
            )

        if attempt < 3:
            time.sleep(0.6)

    # --------------------------------------------------------
    # 2. スマホ版 requests
    # --------------------------------------------------------
    if len(got_st()) < 6:
        for attempt in range(1, 3):
            url = official_before_url(venue, race_no)
            try:
                r = _http.get(
                    url,
                    timeout=(4, 8),
                    headers={
                        "Referer": "https://www.boatrace.jp/",
                        "Cache-Control": "no-cache",
                        "Pragma": "no-cache",
                    },
                )
                r.raise_for_status()

                raw = r.text
                parsed = _extract_exhibition_from_html(raw)
                merge(parsed)

                if len(got_st()) < 6:
                    text_parsed = _extract_exhibition_from_text(_html_to_text(raw))
                    merge(text_parsed)

                log(f"MOBILE-HTTP-{attempt}")

                if len(got_st()) == 6:
                    break

            except Exception as e:
                print(
                    f"[EXHIBITION-MOBILE-HTTP-{attempt}-ERR] "
                    f"{venue} {race_no}R {e!r}",
                    flush=True,
                )

            if attempt < 2:
                time.sleep(0.6)

    # --------------------------------------------------------
    # 3. 最終手段: PC版Playwright
    # gotoの完全ロードを待たず、表示されたDOMから直接tableを読む。
    # --------------------------------------------------------
    if len(got_st()) < 6:
        p = page.context.new_page()
        try:
            url = official_before_pc_url(venue, race_no)

            try:
                p.goto(
                    url,
                    wait_until="commit",
                    timeout=9000,
                )
            except Exception as e:
                print(
                    f"[EXHIBITION-PC-PW-GOTO] {venue} {race_no}R "
                    f"{e!r} / DOM確認続行",
                    flush=True,
                )

            try:
                p.wait_for_selector("body", timeout=3500)
            except Exception:
                pass

            # 「スタート展示」がDOMに現れるのを短時間だけ待つ
            try:
                p.wait_for_function(
                    """() => {
                        const t = document.body?.innerText || '';
                        return t.includes('スタート展示');
                    }""",
                    timeout=4500,
                )
            except Exception:
                pass

            # DOMそのもののHTMLを解析
            try:
                raw = p.content()
            except Exception:
                raw = ""

            if raw:
                merge(_extract_exhibition_from_html(raw))

            if len(got_st()) < 6:
                try:
                    body = p.locator("body").inner_text(timeout=2500)
                except Exception:
                    body = ""
                if body:
                    merge(_extract_exhibition_from_text(body))

            log("PC-PW")

        except Exception as e:
            print(
                f"[EXHIBITION-PC-PW-ERR] {venue} {race_no}R {e!r}",
                flush=True,
            )
        finally:
            p.close()

    # --------------------------------------------------------
    # 厳格判定
    # --------------------------------------------------------
    if len(got_st()) == 6:
        print(
            f"[EXHIBITION-READY] {venue} {race_no}R / "
            + " / ".join(
                f"{b}="
                + (
                    f"F{abs(out[b]['ex_st']):.02f}"
                    if out[b]["ex_st"] < 0
                    else f"{out[b]['ex_st']:.02f}"
                )
                for b in range(1, 7)
            )
            + f" / 展示TIME={len(got_time())}/6",
            flush=True,
        )
    else:
        missing = [b for b in range(1, 7) if out[b].get("ex_st") is None]
        print(
            f"[EXHIBITION-INCOMPLETE] {venue} {race_no}R / "
            f"ST={got_st()} ({len(got_st())}/6) / "
            f"不足艇={missing} / "
            f"TIME={got_time()} ({len(got_time())}/6) "
            "-> 通知しない",
            flush=True,
        )

    return out


# ============================================================
# 予測ロジック
# ============================================================

def rank_to_st(rank):
    """
    ST順位 1〜6 を相対STへ変換。
    1位≈0.105 / 6位≈0.195 のスケール。
    """
    if rank is None:
        return None
    return 0.105 + (float(rank) - 1.0) * 0.018


def topstart_to_st(prob, win_rate):
    """
    トップST分析を相対STへ。
    主役はトップST確率。トップST時1着率は補助（20%）。
    """
    if prob is None:
        return None

    prob = max(0.0, min(50.0, float(prob)))
    # 0% -> .195 / 50% -> .105
    st_prob = 0.195 - (prob / 50.0) * 0.090

    if win_rate is None:
        return st_prob

    win_rate = max(0.0, min(100.0, float(win_rate)))
    # 0% -> .185 / 100% -> .115
    st_win = 0.185 - (win_rate / 100.0) * 0.070

    return st_prob * 0.80 + st_win * 0.20


def f_adjusted_st(d):
    """
    F補正。
    Fなしはニュートラル(.150)。
    F持ちはF持ちST順位に応じて慎重/攻めを表す。
    """
    if not d.get("has_f"):
        return 0.150

    fr = d.get("f_rank")
    if fr is None:
        return 0.165

    # F持ちでも順位が良い選手は過度に遅くしない。
    base = rank_to_st(fr)
    return min(0.185, max(0.125, base + 0.008))


def weighted_available(parts):
    """
    欠損データがあっても、ある項目だけで重みを再正規化。
    """
    valid = [(v, w) for v, w in parts if v is not None and w > 0]
    if not valid:
        return 0.150

    sw = sum(w for _, w in valid)
    return sum(v * w for v, w in valid) / sw


def predict_st(d, event_day):
    """
    初日/最終日の本番ST予測。

    初日:
      - 当地ST順位 30%
      - 初日ST順位 30%
      - トップST分析(1年) 25%
      - F関連 15%
      - 今節平均STは使わない
      - 最終日ST順位も使わない
      → 基礎80% + 展示ST20%

    最終日:
      - 当地ST順位 25%
      - 最終日ST順位 25%
      - 今節平均ST 25%
      - トップST分析(1年) 15%
      - F関連 10%
      - 初日ST順位は使わない
      → 基礎80% + 展示ST20%

    直近1か月/3か月、コース補正は使わない。
    """
    local = rank_to_st(d.get("local_rank"))
    firstday = rank_to_st(d.get("firstday_rank"))
    finalday = rank_to_st(d.get("finalday_rank"))

    top = topstart_to_st(
        d.get("top_start_prob"),
        d.get("top_start_win")
    )
    f_st = f_adjusted_st(d)

    if event_day == "初日":
        base = weighted_available([
            (local, 0.30),
            (firstday, 0.30),
            (top, 0.25),
            (f_st, 0.15),
        ])
    else:
        setsu = d.get("setsu_avg_st")
        if setsu is not None:
            setsu = max(0.07, min(0.28, float(setsu)))

        base = weighted_available([
            (local, 0.25),
            (finalday, 0.25),
            (setsu, 0.25),
            (top, 0.15),
            (f_st, 0.10),
        ])

    ex = d.get("ex_st")
    if ex is None:
        pred = base
    else:
        # 展示Fをそのまま本番Fとは予測しない
        ex_for_calc = (
            max(0.04, float(ex))
            if ex >= 0
            else max(0.04, 0.06 - abs(float(ex)))
        )
        pred = base * FINAL_W_BASE + ex_for_calc * FINAL_W_EXHIBITION

    return max(0.04, min(0.25, pred))


# ============================================================
# 通知画像
# ============================================================

def build_image(page, venue, race_no, deadline_text, data, event_day):
    pred = {b: predict_st(data[b], event_day) for b in range(1, 7)}

    best = min(pred, key=pred.get)

    # 1M到達時の予測艇身差。
    # ST差を、スタート後の概算速度 約80km/h・艇長約2.9m で艇身換算。
    # 1秒あたり約7.66艇身。最速予測艇を0.00艇身とする。
    BOAT_LENGTHS_PER_SEC = (80_000 / 3600) / 2.9
    one_m_gap = {
        b: max(0.0, (pred[b] - pred[best]) * BOAT_LENGTHS_PER_SEC)
        for b in range(1, 7)
    }

    # 右に出ているほど速い。
    vals = list(pred.values())
    lo, hi = min(vals), max(vals)
    spread = max(hi - lo, 0.04)

    lanes = []
    for b in range(1, 7):
        rel = (hi - pred[b]) / spread
        x = 28 + max(0.0, min(1.0, rel)) * 52

        ex = data[b].get("ex_st")
        ext = data[b].get("ex_time")

        if ex is None:
            ex_txt = "-"
        elif ex < 0:
            ex_txt = f"F{abs(ex):.02f}".replace("0.", ".")
        else:
            ex_txt = f"{ex:.02f}"

        ext_txt = f"{ext:.2f}" if ext is not None else "-"
        fire = "🔥" if b == best else ""

        lanes.append(f"""
        <div class="lane">
          <div class="num">{b}</div>
          <div class="track">
            <div class="start"></div>
            <div class="boat" style="left:{x:.1f}%">⛵▶ {fire}</div>
          </div>
          <div class="stat">
            <b>予測 {pred[b]:.02f}</b>
            <span>展示ST {html.escape(ex_txt)}</span>
            <span>展示 {html.escape(ext_txt)}</span>
            <span>1M差 {one_m_gap[b]:.02f}艇身</span>
          </div>
        </div>
        """)

    doc = f"""
    <html><head><meta charset="utf-8">
    <style>
      *{{box-sizing:border-box}}
      body{{
        margin:0;padding:26px;width:920px;background:#08111f;color:#eef4ff;
        font-family:-apple-system,BlinkMacSystemFont,"Noto Sans JP","Yu Gothic",sans-serif;
      }}
      .top{{padding-bottom:18px;border-bottom:1px solid #283850}}
      .race{{font-size:31px;font-weight:900}}
      .sub{{font-size:15px;color:#90a4c2;margin-top:6px}}
      .card{{margin-top:22px;background:#0d192a;border:1px solid #263957;
        border-radius:18px;padding:20px}}
      .title{{font-size:22px;font-weight:900;margin-bottom:14px}}
      .lane{{display:grid;grid-template-columns:42px 1fr 175px;gap:10px;
        align-items:center;min-height:70px;border-top:1px solid #1d2c42}}
      .lane:first-of-type{{border-top:none}}
      .num{{width:34px;height:34px;border-radius:50%;border:1px solid #607798;
        display:flex;align-items:center;justify-content:center;font-weight:900}}
      .track{{position:relative;height:48px}}
      .track:before{{content:"";position:absolute;left:0;right:0;top:24px;height:2px;background:#263956}}
      .start{{position:absolute;left:84%;top:2px;bottom:2px;width:3px;background:#ff5252}}
      .boat{{position:absolute;top:8px;transform:translateX(-50%);font-size:25px;
        white-space:nowrap;font-weight:900}}
      .stat{{display:flex;flex-direction:column;font-size:13px;color:#99acc7}}
      .stat b{{font-size:17px;color:#f2f6ff}}
      .arrow{{text-align:center;margin-top:12px;color:#a6b8d1;font-weight:800}}
      .note{{margin-top:12px;color:#8fa5c4;font-size:13px}}
      .foot{{margin-top:18px;color:#7185a5;font-size:12px;text-align:right}}
    </style></head>
    <body>
      <div class="top">
        <div class="race">{html.escape(venue)} {race_no}R</div>
        <div class="sub">{html.escape(event_day)} / 締切 {html.escape(deadline_text or "-")}</div>
      </div>

      <div class="card">
        <div class="title">本番ST予測</div>
        {''.join(lanes)}
        <div class="arrow">進行方向 →　　STARTは赤線</div>
        <div class="note">右に出ているほど本番先行予測 / 🔥は最速予測艇</div>
        <div class="note">1M差＝最速予測艇を0.00艇身とした1M到達時の予測艇身差（ST差から概算）</div>
      </div>

      <div class="foot">
        当地・最終日ST順位 / F / 今節平均ST / トップST分析 / 展示ST / 1M予測艇身差
      </div>
    </body></html>
    """

    p = page.context.new_page()
    try:
        p.set_viewport_size({"width": 920, "height": 1000})
        p.set_content(doc, wait_until="load")
        p.wait_for_timeout(200)
        path = f"/tmp/finalday_st_{venue}_{race_no}_{int(time.time())}.png"
        p.screenshot(path=path, full_page=True)
        return path, pred
    finally:
        p.close()


def send_image(path, venue, race_no, event_day):
    with open(path, "rb") as f:
        r = requests.put(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            params={
                "filename": os.path.basename(path),
                "title": "Shinsum Monitor",
                "message": f"{venue} {race_no}R / {event_day} / 本番ST予測",
                "priority": "5",
                "tags": "ship",
            },
            data=f,
            headers={"Content-Type": "image/png"},
            timeout=30,
        )
    r.raise_for_status()


def fetch_deadline(page, venue, race_no):
    """
    ボートレース日和の描画後DOMから、
    指定したRそのものの締切だけを取得する。

    requestsはrace_noと異なる1R相当HTMLを返す場合があるため使用しない。
    """
    cache_key = f"{hd()}|{venue}|{race_no}"
    if cache_key in _deadline_cache:
        return _deadline_cache[cache_key]

    url = hiyori_url(venue, race_no)
    p = page.context.new_page()

    try:
        p.goto(url, wait_until="domcontentloaded", timeout=12000)
        p.wait_for_timeout(1200)

        body = p.locator("body").inner_text(timeout=4000).replace("\r", "")

        patterns = (
            rf"(?m)^\s*{race_no}\s*R[^\n]*締切\s*([0-2]?\d:[0-5]\d)",
            rf"(?s)(?<!\d){race_no}\s*R\b.{{0,140}}?締切\s*([0-2]?\d:[0-5]\d)",
        )

        deadline_value = ""
        for pat in patterns:
            m = re.search(pat, body, flags=re.I)
            if m:
                deadline_value = m.group(1)
                break

        if not deadline_value:
            head = re.sub(r"\s+", " ", body[:600]).strip()
            print(
                f"[DEADLINE-MISMATCH] {venue} {race_no}R / "
                f"指定Rの締切を確認できず / head={head[:200]}",
                flush=True,
            )
            return ""

        _deadline_cache[cache_key] = deadline_value
        print(
            f"[DEADLINE] {venue} {race_no}R -> {deadline_value}",
            flush=True,
        )
        return deadline_value

    except Exception as e:
        print(
            f"[HIYORI-DEADLINE-ERR] {venue} {race_no}R {e!r}",
            flush=True,
        )
        return ""
    finally:
        p.close()

def find_current_race(page, venue, event_day):
    """
    現在の未締切Rを二分探索で特定する。

    締切取得に失敗した場合は推測しない。
    12R終了済みならその場は即終了。
    """
    d12 = fetch_deadline(page, venue, 12)

    if not d12:
        print(
            f"[CURRENT-RACE-ERR] {venue} {event_day} / "
            "12R締切取得失敗",
            flush=True,
        )
        return None, ""

    if race_is_closed(d12):
        return None, "FINISHED"

    lo, hi = 1, 12
    deadline_cache = {12: d12}

    while lo < hi:
        mid = (lo + hi) // 2

        d = deadline_cache.get(mid)
        if d is None:
            d = fetch_deadline(page, venue, mid)
            deadline_cache[mid] = d

        if not d:
            print(
                f"[CURRENT-RACE-ERR] {venue} {event_day} / "
                f"{mid}R締切取得失敗 -> この周期は判定しない",
                flush=True,
            )
            return None, ""

        if race_is_closed(d):
            lo = mid + 1
        else:
            hi = mid

    race_no = lo

    d = deadline_cache.get(race_no)
    if not d:
        d = fetch_deadline(page, venue, race_no)

    if not d:
        return None, ""

    while race_no <= 12:
        key = f"{hd()}|{venue}|{race_no}|{event_day}"

        if key not in seen:
            return race_no, d

        race_no += 1
        if race_no > 12:
            return None, "FINISHED"

        d = fetch_deadline(page, venue, race_no)
        if not d:
            return None, ""

        if race_is_closed(d):
            seen.add(f"{hd()}|{venue}|{race_no}|{event_day}")
            continue

    return None, "FINISHED"

def deadline_datetime(deadline_text):
    """
    今日の締切時刻をJSTのdatetimeに変換。
    """
    if not deadline_text:
        return None

    m = re.fullmatch(r"([0-2]?\d):([0-5]\d)", str(deadline_text).strip())
    if not m:
        return None

    h, minute = map(int, m.groups())
    if h > 23:
        return None

    n = now()
    return n.replace(
        hour=h,
        minute=minute,
        second=0,
        microsecond=0,
    )


def race_is_closed(deadline_text):
    """
    締切時刻を過ぎたレースは審査対象外。
    締切時刻ちょうども終了扱い。
    """
    dt = deadline_datetime(deadline_text)
    if dt is None:
        return False
    return now() >= dt


def race_complete(ex):
    got = [
        b for b in range(1, 7)
        if ex.get(b, {}).get("ex_st") is not None
    ]
    return len(got) == 6, got


def cycle(page, target_venues):
    """
    現在Rだけを監視する高速版。

    - BOATRACE公式へ締切取得アクセスしない
    - ボートレース日和の締切を使用
    - 二分探索で現在Rを特定
    - 終了済み1R〜過去Rを1つずつ審査しない
    - 12R終了済みの場は即終了
    """
    for venue, event_day in target_venues.items():

        race_no, deadline_text = find_current_race(
            page,
            venue,
            event_day,
        )

        if deadline_text == "FINISHED":
            print(
                f"[VENUE-FINISHED] {venue} {event_day} / "
                "12Rまで終了済み -> 審査しない",
                flush=True,
            )
            continue

        if race_no is None:
            print(
                f"[CURRENT-RACE-RETRY] {venue} {event_day} / "
                "現在Rを特定できず -> 次周期",
                flush=True,
            )
            continue

        key = f"{hd()}|{venue}|{race_no}|{event_day}"

        print(
            f"[CURRENT-RACE] {venue} {race_no}R "
            f"{event_day} / 締切 {deadline_text}",
            flush=True,
        )

        # 展示情報は「今の1レース」だけ公式から取得。
        ex = fetch_official_exhibition(
            page,
            venue,
            race_no,
        )
        complete, got = race_complete(ex)

        if not complete:
            print(
                f"[WAIT-EXHIBITION] {venue} {race_no}R "
                f"{event_day} / 締切 {deadline_text} / "
                f"展示ST取得={got} / 6艇未完了 -> 次周期",
                flush=True,
            )
            continue

        # 日和の詳細データも今の1レースだけ取得。
        hdata = fetch_hiyori_data(
            page,
            venue,
            race_no,
            event_day,
        )

        data = {i: {} for i in range(1, 7)}
        for b in range(1, 7):
            data[b].update(hdata.get(b, {}))
            data[b].update(ex.get(b, {}))

        top_missing = [
            b for b in range(1, 7)
            if data[b].get("top_start_prob") is None
        ]
        if top_missing:
            print(
                f"[WAIT-TOPSTART] {venue} {race_no}R {event_day} / "
                f"トップST率不足艇={top_missing} -> 通知しない",
                flush=True,
            )
            continue

        image_path, pred = build_image(
            page,
            venue,
            race_no,
            deadline_text,
            data,
            event_day,
        )

        try:
            send_image(
                image_path,
                venue,
                race_no,
                event_day,
            )
            seen.add(key)

            print(
                f"[ST-PREDICT] {venue} {race_no}R / "
                f"{event_day} / "
                + " / ".join(
                    f"{b}={pred[b]:.02f}"
                    for b in range(1, 7)
                ),
                flush=True,
            )
            print(
                f"[NOTIFY] {venue} {race_no}R "
                f"{event_day} 送信完了",
                flush=True,
            )

        finally:
            try:
                os.remove(image_path)
            except Exception:
                pass


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15"
        )
        page = context.new_page()

        if not active():
            print("監視時間外（23:00〜08:00 JST）。終了します。", flush=True)
            return

        print(
            f"[{now():%Y-%m-%d %H:%M:%S}] Shinsum Monitor",
            flush=True
        )
        print(
            "初日・最終日 1〜12R 本番ST予測開始",
            flush=True
        )
        print(
            "初日使用: 当地ST順位 / 初日ST順位 / "
            "F有無 / トップST分析(1年) / 展示ST",
            flush=True
        )
        print(
            "最終日使用: 当地ST順位 / 最終日ST順位 / "
            "F有無 / 今節平均ST / トップST分析(1年) / 展示ST",
            flush=True
        )
        print(
            "不使用: 直近1か月 / 直近3か月 / コース補正 / 2着予想 / 最終1着率",
            flush=True
        )

        # 開催日判定は起動時に1回だけ。
        target_venues = detect_target_venues(page)

        if not target_venues:
            print("本日の初日・最終日対象場なし。終了します。", flush=True)
            return

        while active():
            print(
                f"[{now():%Y-%m-%d %H:%M:%S}] 再チェック / "
                f"対象場: {', '.join(f'{v}({d})' for v, d in target_venues.items())}",
                flush=True
            )

            cycle(page, target_venues)

            time.sleep(CHECK_INTERVAL)

        browser.close()


if __name__ == "__main__":
    main()
