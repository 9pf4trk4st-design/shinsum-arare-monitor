import os
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo
from html.parser import HTMLParser
import html

import requests
from playwright.sync_api import sync_playwright

BASE_URL = "https://boatrace-shinsum.com/"

USER = os.environ["SHINSUM_USER"]
PASSWORD = os.environ["SHINSUM_PASSWORD"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))
JST = ZoneInfo("Asia/Tokyo")

TARGET_VENUES = [
    "平和島", "児島", "戸田", "多摩川",
    "蒲郡", "びわこ", "三国", "鳴門",
    "宮島", "徳山", "下関", "若松",
    "芦屋", "唐津", "大村", "住之江"
]


VENUE_CODES = {
    "戸田": 2, "平和島": 4, "多摩川": 5, "蒲郡": 7,
    "三国": 10, "びわこ": 11, "住之江": 12, "鳴門": 14,
    "児島": 16, "宮島": 17, "徳山": 18, "下関": 19,
    "若松": 20, "芦屋": 21, "唐津": 23, "大村": 24,
}
NIGHT_VENUES = {"蒲郡", "住之江", "下関", "若松", "大村"}

_http = requests.Session()
_http.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; shinsum-monitor-image/1.0)"
})

ALERT_TYPES = ("やや本命", "荒れ注意")

# -----------------------------
# 選別基準
# -----------------------------
# 1号艇有利
ONE_MIN = 8.0
ONE_GAP_VS_34 = 8.0
ONE_GAP_VS_SECOND = 5.0

# 3・4号艇有利
OUT_MIN = 10.0
OUT_GAP_VS_1 = 8.0
OUT_GAP_VS_OTHER = 5.0

# 独立バフ検知（やや本命/荒れ注意が出ていないレースも対象）
BUFF_TARGET_BOATS = (1, 2, 3, 4)
BUFF_MIN_CURRENT_DIFF = 0.50
BUFF_MIN_THEORY_1ST = 10.0
BUFF_MIN_CHECKER_1ST = 5.0

seen = set()


def now():
    return datetime.now(JST)


def active():
    return 8 <= now().hour < 23


def deadline(text):
    m = re.search(
        r"締切\s*[：:]?\s*([01]?\d|2[0-3]):([0-5]\d)",
        text
    )
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""



def parse_event_day(text):
    """
    ページ本文だけから開催何日目か判定。
    """
    t = re.sub(r"\s+", "", str(text))

    if "初日" in t:
        return 1

    m = re.search(r"(?:第)?([1-9]|1[0-2])日目", t)
    if m:
        return int(m.group(1))

    m = re.search(r"第([1-9]|1[0-2])日(?:目)?", t)
    if m:
        return int(m.group(1))

    return None


def fetch_event_day_official(venue, race_no):
    """
    BOATRACE公式レースページから開催日(初日=1, 2日目=2...)を取得。
    Shinsum詳細ページに開催日表記が無い場合の本命フォールバック。
    """
    jcd = VENUE_CODES.get(venue)
    if not jcd:
        return None

    hd = now().strftime("%Y%m%d")
    urls = [
        (
            "https://www.boatrace.jp/owpc/pc/race/racelist"
            f"?hd={hd}&jcd={jcd:02d}&rno={race_no}"
        ),
        (
            "https://www.boatrace.jp/owpc/pc/race/beforeinfo"
            f"?hd={hd}&jcd={jcd:02d}&rno={race_no}"
        ),
    ]

    for url in urls:
        try:
            r = _http.get(url, timeout=15)
            r.raise_for_status()
            plain = _clean_html(r.text)
            compact = re.sub(r"\s+", "", plain)

            if "初日" in compact:
                return 1

            # 例: 第2日 / 第2日目 / 2日目
            for pat in (
                r"第([1-9]|1[0-2])日目",
                r"第([1-9]|1[0-2])日",
                r"(?<!\d)([1-9]|1[0-2])日目",
            ):
                m = re.search(pat, compact)
                if m:
                    return int(m.group(1))

        except Exception as e:
            print(
                f"[DAY-FETCH-ERR] {venue}{race_no}R {e!r}",
                flush=True
            )

    return None


def get_event_day(text, venue, race):
    """
    1) Shinsum詳細ページ本文
    2) BOATRACE公式
    の順で開催日を判定。
    """
    day = parse_event_day(text)
    if day is not None:
        return day, "shinsum"

    race_no = int(re.sub(r"\D", "", str(race)) or 0)
    if race_no:
        day = fetch_event_day_official(venue, race_no)
        if day is not None:
            return day, "official"

    return None, "unknown"



def within15(text):
    d = deadline(text)
    if not d:
        return False

    h, m = map(int, d.split(":"))
    t = now().replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0
    )

    return timedelta(minutes=-1) <= t - now() <= timedelta(minutes=15)


def candidate_links(page):
    page.goto(
        BASE_URL,
        wait_until="domcontentloaded",
        timeout=30000
    )
    page.wait_for_timeout(1000)

    host = urlparse(BASE_URL).netloc
    out = []
    aa = page.locator("a")

    for i in range(aa.count()):
        a = aa.nth(i)

        try:
            href = a.get_attribute("href")

            if (
                not href
                or href.startswith("#")
                or href.startswith("javascript:")
            ):
                continue

            full = urljoin(BASE_URL, href)

            if urlparse(full).netloc != host:
                continue

            txt = ""

            try:
                txt = a.inner_text(timeout=250) or ""
            except:
                pass

            try:
                txt += "\n" + a.locator(
                    "xpath=ancestor::*[self::div or self::td or self::li or self::section][1]"
                ).inner_text(timeout=250)
            except:
                pass

            if (
                any(v in txt for v in TARGET_VENUES)
                or re.search(r"([1-9]|1[0-2])\s*R", txt)
                or "race" in full.lower()
                or "detail" in full.lower()
                or "sum" in full.lower()
            ):
                out.append(full)

        except:
            pass

    return list(dict.fromkeys(out))


def actual_venue(text):
    head = text[:1800]
    matches = [v for v in TARGET_VENUES if v in head]
    return matches[0] if len(matches) == 1 else ""


def actual_race(text):
    m = re.search(
        r"(?<!\d)([1-9]|1[0-2])\s*R\b",
        text[:1800],
        re.I
    )
    return m.group(1) + "R" if m else ""


def alert_near_deadline(text, d):
    pos = -1

    for p in (
        f"締切 {d}",
        f"締切{d}",
        f"締切：{d}",
        f"締切: {d}"
    ):
        pos = text.find(p)

        if pos >= 0:
            break

    if pos < 0:
        return None

    local = text[
        max(0, pos - 250):
        min(len(text), pos + 350)
    ]

    return next(
        (a for a in ALERT_TYPES if a in local),
        None
    )


def parse_base_1st(text):
    """
    ページ上部の「選手名・1着率」から元1着率を6艇分取得。
    例: {1: 34.0, 2: 16.0, ...}
    """
    start = text.find("選手名・1着率")
    if start < 0:
        return {}

    end_candidates = [
        p for p in (
            text.find("戦法別上昇率", start),
            text.find("スリット隊形", start),
        )
        if p > start
    ]
    end = min(end_candidates) if end_candidates else min(len(text), start + 5000)

    section = text[start:end]
    lines = [x.strip() for x in section.splitlines() if x.strip()]
    result = {}

    for boat in range(1, 7):
        for i, line in enumerate(lines):
            if line != str(boat):
                continue

            window = "\n".join(lines[i:i + 10])
            pcts = re.findall(r"(\d+(?:\.\d+)?)\s*%", window)
            if pcts:
                result[boat] = float(pcts[0])
                break

    return result


def parse_theory_1st(text):
    """
    「シンsum理論」表そのものの1着補正を6艇全部取得する。
    上部の「← シンsum理論に戻る」を誤って拾わないよう、
    「シンsumチェッカー」の直前にある最後のシンsum理論を使う。

    例:
      1号艇 -1%
      2号艇 -8%
      3号艇 +1%
    """
    checker_pos = text.find("シンsumチェッカー")
    if checker_pos < 0:
        checker_pos = len(text)

    start = text.rfind("シンsum理論", 0, checker_pos)
    if start < 0:
        return {}

    section = text[start:checker_pos]
    lines = [x.strip() for x in section.splitlines() if x.strip()]
    result = {}

    for boat in range(1, 7):
        for i, line in enumerate(lines):
            if line != str(boat):
                continue

            # 艇番→登録番号→型→平均との差→1着/2着/3着...
            window = "\n".join(lines[i:i + 18])
            pcts = re.findall(
                r"([+-]?\d+(?:\.\d+)?)\s*%",
                window
            )
            if pcts:
                result[boat] = float(pcts[0])
                break

    return result


def parse_checker_1st_all(text, current_diffs):
    """
    1〜6号艇すべてについて、現在の「平均との差」ゾーンの
    シンsumチェッカー1着補正を取得する。
    """
    start = text.find("シンsumチェッカー")
    if start < 0:
        return {}

    section = text[start:]
    compact = re.sub(r"\s+", "", section)
    result = {}

    for boat in range(1, 7):
        if boat not in current_diffs:
            continue

        token = f"{boat}号艇"
        pos = compact.find(token)
        if pos < 0:
            continue

        next_positions = []
        for other in range(1, 7):
            if other == boat:
                continue
            p = compact.find(f"{other}号艇", pos + len(token))
            if p >= 0:
                next_positions.append(p)

        end = min(next_positions) if next_positions else min(len(compact), pos + 3500)
        card = compact[pos:end]

        zone = checker_zone(current_diffs[boat])
        variants = {
            "+0.5以上": ("+0.5以上",),
            "0〜+0.5": ("0〜+0.5", "0~+0.5", "0～+0.5"),
            "-0.5〜0": ("-0.5〜0", "-0.5~0", "-0.5～0"),
            "-0.5未満": ("-0.5未満",),
        }[zone]

        zpos = -1
        for v in variants:
            zpos = card.find(v)
            if zpos >= 0:
                break

        if zpos < 0:
            continue

        row = card[zpos:zpos + 260]
        pcts = re.findall(r"([+-]?\d+(?:\.\d+)?)%", row)
        if not pcts:
            continue

        result[boat] = {
            "zone": zone,
            "checker_1st": float(pcts[0]),
        }

    return result


def build_final_1st_rates(base_1st, theory_1st, checker_all):
    """
    最終1着率 = 元1着率 + シンsum理論1着補正 + シンsumチェッカー1着補正
    """
    out = {}
    for boat in range(1, 7):
        base = base_1st.get(boat)
        theory = theory_1st.get(boat)
        checker_row = checker_all.get(boat)
        checker = checker_row.get("checker_1st") if checker_row else None

        if base is None or theory is None or checker is None:
            out[boat] = {
                "base": base,
                "theory": theory,
                "checker": checker,
                "final": None,
            }
            continue

        out[boat] = {
            "base": float(base),
            "theory": float(theory),
            "checker": float(checker),
            "final": float(base) + float(theory) + float(checker),
        }

    return out


def parse_current_diffs(text):
    """
    シンsum理論表の「平均との差」を6艇分取得する。
    例: {1: +0.24, 2: -0.31, 3: +0.61, ...}
    """
    start = text.find("シンsum理論")
    if start < 0:
        return {}

    end = text.find("シンsumチェッカー", start)
    section = text[start:(end if end > start else start + 6000)]
    lines = [x.strip() for x in section.splitlines() if x.strip()]

    result = {}
    for boat in range(1, 7):
        for i, line in enumerate(lines):
            if line != str(boat):
                continue

            # 艇番の直後に出る「+0.61」「-0.31」等を拾う。
            # %付きの理論値は除外する。
            for candidate in lines[i + 1:i + 12]:
                if "%" in candidate:
                    continue
                m = re.fullmatch(r"([+-]\d+(?:\.\d+)?)", candidate)
                if m:
                    result[boat] = float(m.group(1))
                    break
            break

    return result


def checker_zone(current_diff):
    if current_diff >= 0.5:
        return "+0.5以上"
    if current_diff >= 0:
        return "0〜+0.5"
    if current_diff >= -0.5:
        return "-0.5〜0"
    return "-0.5未満"


def parse_checker_1st(text, current_diffs):
    """
    既存の独立バフ判定用。
    全艇を取得した後、BUFF_TARGET_BOATSだけ返す。
    """
    all_rows = parse_checker_1st_all(text, current_diffs)
    return {
        b: all_rows[b]
        for b in BUFF_TARGET_BOATS
        if b in all_rows
    }


def classify_buff(theory, current_diffs, checker):
    """
    やや本命/荒れ注意が無くても通知する独立バフ判定。

    条件:
      - 1〜4号艇
      - 現在の平均との差が +0.50以上
      - シンsum理論の1着補正が +10%以上
      - シンsumチェッカーの現在ゾーンの1着率が +5%以上
    """
    buffs = []

    for boat in BUFF_TARGET_BOATS:
        diff = current_diffs.get(boat)
        theory_1st = theory.get(boat)
        c = checker.get(boat)

        if diff is None or theory_1st is None or not c:
            continue

        checker_1st = c["checker_1st"]

        if (
            diff >= BUFF_MIN_CURRENT_DIFF
            and theory_1st >= BUFF_MIN_THEORY_1ST
            and checker_1st >= BUFF_MIN_CHECKER_1ST
        ):
            buffs.append({
                "boat": boat,
                "current_diff": diff,
                "theory_1st": theory_1st,
                "checker_1st": checker_1st,
                "zone": c["zone"],
            })

    if not buffs:
        return None

    # 強い順（チェッカー1着率 → 理論1着率）
    buffs.sort(
        key=lambda x: (x["checker_1st"], x["theory_1st"]),
        reverse=True
    )

    focus = [x["boat"] for x in buffs]
    best = buffs[0]

    return {
        "type": "独立バフ",
        "focus": focus,
        "buffs": buffs,
        "reason": (
            f"{best['boat']}号艇を中心にバフ検知。"
            f"平均との差 {best['current_diff']:+.2f}、"
            f"理論1着 {best['theory_1st']:+.1f}%、"
            f"チェッカー1着 {best['checker_1st']:+.1f}%"
        )
    }


def classify_theory(values):
    """
    6艇全部を比較して、
    1号艇有利 or 3・4号艇有利 のどちらかだけ返す。
    """

    if len(values) < 6:
        return None

    vals = list(values.values())

    b1 = values[1]
    b3 = values[3]
    b4 = values[4]

    best_all = max(vals)
    second_best = sorted(vals, reverse=True)[1]

    # -----------------------------
    # 1号艇有利
    # -----------------------------
    best34 = max(b3, b4)

    if (
        b1 == best_all
        and b1 >= ONE_MIN
        and (b1 - best34) >= ONE_GAP_VS_34
        and (b1 - second_best) >= ONE_GAP_VS_SECOND
    ):
        return {
            "type": "1号艇有利",
            "focus": [1],
            "reason": (
                f"1号艇が6艇中トップ。"
                f"3・4号艇の最大値より {b1 - best34:+.1f}pt、"
                f"2番手より {b1 - second_best:+.1f}pt 優勢"
            )
        }

    # -----------------------------
    # 3・4号艇有利
    # -----------------------------
    best_boat = 3 if b3 >= b4 else 4
    best_value = values[best_boat]

    # 3・4以外の最大値
    best_other = max(
        values[1],
        values[2],
        values[5],
        values[6]
    )

    if (
        best_value == best_all
        and best_value >= OUT_MIN
        and (best_value - b1) >= OUT_GAP_VS_1
        and (best_value - best_other) >= OUT_GAP_VS_OTHER
    ):
        focus = [best_boat]

        # 3号艇・4号艇の両方が強く、
        # 数値も近ければ両方注目表示
        other_boat = 4 if best_boat == 3 else 3

        if (
            values[other_boat] >= OUT_MIN
            and abs(
                values[best_boat] - values[other_boat]
            ) <= 8
        ):
            focus = [3, 4]

        return {
            "type": "3・4号艇有利",
            "focus": focus,
            "reason": (
                f"{best_boat}号艇が6艇中トップ。"
                f"1号艇より {best_value - b1:+.1f}pt、"
                f"3・4以外の最上位より "
                f"{best_value - best_other:+.1f}pt 優勢"
            )
        }

    return None



class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self.table = None
        self.row = None
        self.cell = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "table":
            self.depth += 1
            if self.depth == 1:
                self.table = []
        elif self.depth and tag == "tr":
            self.row = []
        elif self.depth and tag in ("td", "th"):
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.depth and tag in ("td", "th") and self.cell is not None:
            if self.row is not None:
                self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.cell = None
        elif self.depth and tag == "tr":
            if self.table is not None and self.row:
                self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.depth:
            self.depth -= 1
            if self.depth == 0:
                if self.table:
                    self.tables.append(self.table)
                self.table = None


def _tables(raw):
    p = _TableParser()
    p.feed(raw)
    return p.tables


def _clean_html(raw):
    raw = re.sub(r"<script\b[^>]*>.*?</script>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<style\b[^>]*>.*?</style>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def _parse_st_token(s):
    s = str(s).strip().upper().replace(" ", "")
    m = re.search(r"F\.?(\d{1,2})", s)
    if m:
        return -int(m.group(1)) / 100.0
    m = re.search(r"0\.(\d{2})", s)
    if m:
        return int(m.group(1)) / 100.0
    m = re.search(r"(?<!\d)\.?(\d{2})(?!\d)", s)
    if m:
        return int(m.group(1)) / 100.0
    return None


def _num_or_none(s, lo=None, hi=None):
    s = re.sub(r"\s+", "", str(s))
    if s in ("", "-", "－", "—"):
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except Exception:
        return None
    if lo is not None and v < lo:
        return None
    if hi is not None and v > hi:
        return None
    return v


def fetch_official_exhibition(venue, race_no):
    """公式直前情報から展示タイムとスタート展示を取得。"""
    jcd = VENUE_CODES.get(venue)
    if not jcd:
        return {}

    hd = now().strftime("%Y%m%d")
    url = (
        "https://www.boatrace.jp/owpc/pc/race/beforeinfo"
        f"?hd={hd}&jcd={jcd:02d}&rno={race_no}"
    )

    try:
        r = _http.get(url, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"[IMAGE] 展示取得失敗 {venue}{race_no}R {e!r}", flush=True)
        return {}

    out = {i: {} for i in range(1, 7)}

    for tb in _tables(r.text):
        flat = " ".join(" ".join(row) for row in tb)

        if "展示タイム" in flat:
            for row in tb:
                s = " ".join(row)
                bm = re.search(r"(?<!\d)([1-6])(?!\d)", s)
                tm = re.search(r"(?<!\d)(6\.\d{2}|7\.\d{2})(?!\d)", s)
                if bm and tm:
                    out[int(bm.group(1))]["ex_time"] = float(tm.group(1))

        if "スタート展示" in flat or ("ST" in flat and "進入" in flat):
            for row in tb:
                s = " ".join(row)
                bm = re.search(r"(?<!\d)([1-6])(?!\d)", s)
                if not bm:
                    continue
                st = _parse_st_token(s)
                if st is not None:
                    out[int(bm.group(1))]["ex_st"] = st

    return out


def fetch_official_f(venue, race_no):
    """現在のF有無だけは公式BOATRACEを正とする。"""
    jcd = VENUE_CODES.get(venue)
    if not jcd:
        return {}

    hd = now().strftime("%Y%m%d")
    url = (
        "https://www.boatrace.jp/owpc/pc/race/racelist"
        f"?hd={hd}&jcd={jcd:02d}&rno={race_no}"
    )

    try:
        r = _http.get(url, timeout=15)
        r.raise_for_status()
    except Exception:
        return {}

    out = {i: {"f_count": 0} for i in range(1, 7)}
    for tb in _tables(r.text):
        flat = " ".join(" ".join(row) for row in tb)
        if "平均ST" not in flat:
            continue
        for row in tb:
            s = " ".join(row)
            bm = re.search(r"^\s*([1-6])(?:\s|$)", s)
            if not bm:
                continue
            b = int(bm.group(1))
            fm = re.search(r"F\s*([0-9])", s)
            out[b]["f_count"] = int(fm.group(1)) if fm else 0
    return out


def fetch_hiyori_st(parent_page, venue, race_no):
    """
    ボートレース日和の描画後DOMからST順位を取得。
    当地・初日・ナイター(該当場)・F持ち順位を使用。
    """
    jcd = VENUE_CODES.get(venue)
    if not jcd:
        return {}

    hd = now().strftime("%Y%m%d")
    url = (
        "https://kyoteibiyori.com/race_shusso.php"
        f"?place_no={jcd}&race_no={race_no}&hiduke={hd}"
    )

    out = {i: {} for i in range(1, 7)}
    hp = parent_page.context.new_page()

    try:
        hp.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            hp.wait_for_function(
                """() => {
                    const t = document.body?.innerText || '';
                    return t.includes('ST順位');
                }""",
                timeout=10000,
            )
        except Exception:
            hp.wait_for_timeout(2500)

        rows = hp.locator("tr").evaluate_all(
            """els => els.map(tr =>
                Array.from(tr.querySelectorAll('th,td'))
                    .map(x => (x.innerText || x.textContent || '').trim())
            )"""
        )

        for cells in rows:
            if not cells:
                continue

            label = re.sub(r"\s+", "", cells[0])

            def six_values(lo=1.0, hi=6.0):
                vals = [_num_or_none(x, lo, hi) for x in cells[1:7]]
                return vals if len(vals) == 6 else None

            key = None
            if label == "当地" or label.startswith("当地ST"):
                key = "local_rank"
            elif label == "初日" or label.startswith("初日ST"):
                key = "firstday_rank"
            elif venue in NIGHT_VENUES and (
                label == "ナイター" or label.startswith("ナイターST")
            ):
                key = "night_rank"
            elif label == "F持" or label == "F持ち" or label.startswith("F持"):
                key = "f_rank"

            if key:
                vals = six_values()
                if vals:
                    for b, v in enumerate(vals, 1):
                        if v is not None:
                            out[b][key] = float(v)

        return out

    except Exception as e:
        print(f"[IMAGE] 日和ST取得失敗 {venue}{race_no}R {e!r}", flush=True)
        return {}
    finally:
        hp.close()


def merge_slit_data(official_ex, official_f, hiyori):
    out = {i: {} for i in range(1, 7)}
    for b in range(1, 7):
        out[b].update(hiyori.get(b, {}))
        out[b].update(official_ex.get(b, {}))
        # F有無だけは公式を最後に上書き
        out[b]["f_count"] = int(official_f.get(b, {}).get("f_count", 0))
    return out


def slit_rank(d):
    vals = [d.get("local_rank"), d.get("firstday_rank")]

    if d.get("night_rank") is not None:
        vals.append(d.get("night_rank"))

    if d.get("f_count", 0) >= 1 and d.get("f_rank") is not None:
        vals.append(d.get("f_rank"))

    vals = [float(v) for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def predicted_st(d):
    """
    スリット図のための相対予測ST。
    数字は確定予測ではなく、6艇の前後関係を描くための指標。
    """
    rank = slit_rank(d)

    if rank is None:
        p = 0.15
    else:
        p = 0.15 + (rank - 3.5) * 0.012

    exst = d.get("ex_st")
    if exst is not None:
        if exst >= 0:
            # 展示STは単発なので35%反映
            p = p * 0.65 + float(exst) * 0.35
        else:
            # 展示Fをそのまま本番Fとして扱わない
            fdepth = abs(float(exst))
            if rank is not None and rank <= 2.8 and fdepth <= 0.03:
                p -= 0.012
            else:
                p += 0.006

    # F持ち艇はF持ち順位が悪い時だけ少し慎重にする
    if d.get("f_count", 0) >= 1 and d.get("f_rank") is not None:
        if d["f_rank"] >= 4.0:
            p += 0.008
        elif d["f_rank"] <= 2.5:
            p -= 0.004

    return max(0.04, min(0.24, p))


def build_shinsum_image(page, venue, race, deadline_value, theory_values, classification, final_rates):
    """
    Shinsum Monitor専用の画像通知。
    予測スリット + シンsum理論 + 注目艇を1枚にする。
    """
    race_no = int(re.sub(r"\D", "", str(race)) or 0)
    if not race_no:
        raise ValueError("race number parse failed")

    ex = fetch_official_exhibition(venue, race_no)
    official_f = fetch_official_f(venue, race_no)
    hiyori = fetch_hiyori_st(page, venue, race_no)
    data = merge_slit_data(ex, official_f, hiyori)

    pred = {b: predicted_st(data[b]) for b in range(1, 7)}
    vals = list(pred.values())
    lo, hi = min(vals), max(vals)
    spread = max(hi - lo, 0.05)

    focus = set(classification.get("focus", []))

    lanes = []
    for b in range(1, 7):
        rel = (hi - pred[b]) / spread
        x = 24 + max(0.0, min(1.0, rel)) * 54

        d = data[b]
        exst = d.get("ex_st")
        if exst is None:
            exst_txt = "-"
        elif exst < 0:
            exst_txt = f"F{abs(exst):.02f}"
        else:
            exst_txt = f"{exst:.02f}"

        ext = d.get("ex_time")
        ext_txt = f"{ext:.2f}" if ext is not None else "-"
        hot = " hot" if b in focus else ""
        fire = "🔥" if b in focus else ""

        lanes.append(f"""
        <div class="lane{hot}">
          <div class="num">{b}</div>
          <div class="track">
            <div class="start"></div>
            <div class="boat" style="left:{x:.1f}%">⛵▶ {fire}</div>
          </div>
          <div class="stat">
            <b>予測 {pred[b]:.02f}</b>
            <span>展示ST {exst_txt}</span>
            <span>展示 {ext_txt}</span>
          </div>
        </div>
        """)

    theory_rows = []
    for b in range(1, 7):
        mark = " ← 注目" if b in focus else ""
        fr = final_rates.get(b, {})
        base = fr.get("base")
        th = fr.get("theory")
        ck = fr.get("checker")
        final = fr.get("final")

        if final is None:
            detail = (
                f"元{base:.1f}% / 理論{th:+.1f}% / チェッカー未取得"
                if base is not None and th is not None
                else "最終1着率 計算データ不足"
            )
        else:
            detail = (
                f"<strong>{final:.1f}%</strong>　"
                f"= 元{base:.1f}% + 理論{th:+.1f}% + チェッカー{ck:+.1f}%"
            )

        theory_rows.append(
            f'<div class="trow"><b>{b}号艇</b>'
            f'<span>{detail}{mark}</span></div>'
        )

    buff_rows = []
    if classification.get("type") == "独立バフ":
        for x in classification.get("buffs", []):
            buff_rows.append(
                f'<div class="buff"><b>{x["boat"]}号艇</b>'
                f'<span>平均との差 {x["current_diff"]:+.2f} / '
                f'理論 {x["theory_1st"]:+.1f}% / '
                f'チェッカー {x["checker_1st"]:+.1f}%</span></div>'
            )

    kind = classification.get("type", "注目")
    reason = classification.get("reason", "")

    doc = f"""
    <html><head><meta charset="utf-8">
    <style>
      *{{box-sizing:border-box}}
      body{{
        margin:0;padding:30px;width:920px;background:#08111f;color:#eef4ff;
        font-family:"Noto Sans CJK JP","Noto Sans JP","Yu Gothic",sans-serif;
      }}
      .top{{display:flex;justify-content:space-between;align-items:flex-end;
        padding-bottom:18px;border-bottom:1px solid #283850}}
      .race{{font-size:30px;font-weight:900}}
      .kind{{font-size:30px;font-weight:900;color:#ffcf63}}
      .sub{{font-size:15px;color:#90a4c2;margin-top:4px}}
      .card{{margin-top:24px;background:#0d192a;border:1px solid #263957;
        border-radius:18px;padding:20px}}
      .title{{font-size:21px;font-weight:900;margin-bottom:14px}}
      .lane{{display:grid;grid-template-columns:42px 1fr 175px;
        gap:10px;align-items:center;min-height:66px;border-top:1px solid #1d2c42}}
      .lane:first-of-type{{border-top:none}}
      .lane.hot{{background:linear-gradient(90deg,rgba(255,190,0,.10),transparent)}}
      .num{{width:34px;height:34px;border-radius:50%;border:1px solid #607798;
        display:flex;align-items:center;justify-content:center;font-weight:900}}
      .track{{position:relative;height:46px}}
      .track:before{{content:"";position:absolute;left:0;right:0;top:23px;height:2px;background:#263956}}
      .start{{position:absolute;left:84%;top:2px;bottom:2px;width:3px;background:#ff5252}}
      .boat{{position:absolute;top:8px;transform:translateX(-50%);font-size:25px;
        white-space:nowrap;font-weight:900}}
      .stat{{display:flex;flex-direction:column;font-size:13px;color:#99acc7}}
      .stat b{{font-size:16px;color:#f2f6ff}}
      .arrow{{text-align:center;margin-top:10px;color:#a6b8d1;font-weight:800}}
      .note{{margin-top:12px;color:#8fa5c4;font-size:13px}}
      .trow,.buff{{display:grid;grid-template-columns:85px 1fr;gap:10px;
        padding:10px 0;border-top:1px solid #22324a}}
      .trow:first-of-type,.buff:first-of-type{{border-top:none}}
      .trow span,.buff span{{color:#c5d1e2}}
      .reason{{font-size:15px;line-height:1.6;color:#c5d1e2}}
      .foot{{margin-top:16px;color:#6f83a2;font-size:12px;text-align:right}}
    </style></head>
    <body>
      <div class="top">
        <div>
          <div class="race">{html.escape(venue)} {html.escape(str(race))}</div>
          <div class="sub">締切 {html.escape(str(deadline_value))}</div>
        </div>
        <div class="kind">{html.escape(kind)}</div>
      </div>

      <div class="card">
        <div class="title">本番予測スリット</div>
        {''.join(lanes)}
        <div class="arrow">進行方向 →　　STARTは赤線</div>
        <div class="note">右に出ているほど本番先行予測 / 🔥は注目艇</div>
      </div>

      <div class="card">
        <div class="title">最終1着率（元1着率＋シンsum理論＋シンsumチェッカー）</div>
        {''.join(theory_rows)}
      </div>

      {f'<div class="card"><div class="title">シンsumチェッカー</div>{"".join(buff_rows)}</div>' if buff_rows else ''}

      <div class="card">
        <div class="title">判定理由</div>
        <div class="reason">{html.escape(reason)}</div>
      </div>

      <div class="foot">
        ※予測STは当地/初日/ナイター/F持ち順位とスタート展示から算出した相対予測
      </div>
    </body></html>
    """

    ip = page.context.new_page()
    try:
        ip.set_viewport_size({"width": 920, "height": 1200})
        ip.set_content(doc, wait_until="load")
        ip.wait_for_timeout(300)
        path = f"/tmp/shinsum_{venue}_{race_no}_{int(time.time())}.png"
        ip.screenshot(path=path, full_page=True)
        return path
    finally:
        ip.close()


def send_image_ntfy(image_path, message):
    """
    ntfy画像添付。
    HTTPヘッダーに日本語を入れずUnicodeEncodeErrorを防ぐ。
    """
    filename = os.path.basename(image_path)
    with open(image_path, "rb") as f:
        r = requests.put(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            params={
                "filename": filename,
                "title": "Shinsum Monitor",
                "message": message,
                "priority": "5",
                "tags": "ship",
            },
            data=f,
            headers={"Content-Type": "image/png"},
            timeout=30,
        )
    r.raise_for_status()


def notify_selected(
    page,
    alert,
    venue,
    race,
    deadline_value,
    theory_values,
    classification,
    final_rates
):
    kind = classification["type"]
    focus = set(classification["focus"])

    if kind == "1号艇有利":
        symbol = "🟢"
    elif kind == "独立バフ":
        symbol = "🚀"
    else:
        symbol = "🔥"

    lines = []
    for boat in range(1, 7):
        mark = " ←注目" if boat in focus else ""
        fr = final_rates.get(boat, {})
        final = fr.get("final")

        if final is None:
            lines.append(f"{boat}号艇 最終1着率=取得不足{mark}")
        else:
            lines.append(
                f"{boat}号艇 {final:.1f}% "
                f"(元{fr['base']:.1f} "
                f"+理論{fr['theory']:+.1f} "
                f"+チェッカー{fr['checker']:+.1f})"
                f"{mark}"
            )

    if kind == "独立バフ":
        buff_lines = []
        for b in classification["buffs"]:
            buff_lines.append(
                f"{b['boat']}号艇 "
                f"平均との差 {b['current_diff']:+.2f} / "
                f"理論1着 {b['theory_1st']:+.1f}% / "
                f"チェッカー1着 {b['checker_1st']:+.1f}%"
            )

        body = (
            f"{symbol} シンsum独立バフ検知\n"
            f"{venue} {race}\n\n"
            + "\n".join(buff_lines)
            + "\n\n"
            + "最終1着率（元＋理論＋チェッカー）\n"
            + "\n".join(lines)
            + "\n\n"
            + f"{classification['reason']}\n"
            + f"締切 {deadline_value}"
        )
    else:
        body = (
            f"{symbol} {kind}\n"
            f"{venue} {race}【{alert}】\n\n"
            f"最終1着率（元＋理論＋チェッカー）\n"
            + "\n".join(lines)
            + "\n\n"
            + f"{classification['reason']}\n"
            + f"締切 {deadline_value}"
        )

    # まず画像付き通知を試す。失敗した時だけ従来のテキスト通知へ。
    try:
        image_path = build_shinsum_image(
            page,
            venue,
            race,
            deadline_value,
            theory_values,
            classification,
            final_rates,
        )
        send_image_ntfy(
            image_path,
            f"{venue} {race} / {kind}"
        )
        try:
            os.remove(image_path)
        except Exception:
            pass

        print(
            f"画像付き選別通知送信: {kind} / {venue} / {race}",
            flush=True
        )
        return

    except Exception as e:
        print(
            f"画像通知失敗: {venue} / {race} / {repr(e)} "
            "→ テキスト通知へフォールバック",
            flush=True
        )

    x = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={
            "Priority": "high",
            "Tags": "ship"
        },
        timeout=15
    )

    x.raise_for_status()

    print(
        f"選別通知送信: "
        f"{kind} / "
        f"{alert or '通常レース'} / "
        f"{venue} / "
        f"{race} / "
        f"締切 {deadline_value}",
        flush=True
    )


def inspect(page):
    text = page.locator("body").inner_text(
        timeout=10000
    )

    v = actual_venue(text)
    r = actual_race(text)

    if not v or not r:
        return None

    # 初日・2日目限定
    event_day, day_source = get_event_day(text, v, r)

    if event_day not in (1, 2):
        if event_day is not None:
            print(
                f"[DAY-SKIP] {v} / {r} / {event_day}日目 "
                f"(source={day_source}) → 対象外",
                flush=True
            )
        else:
            # ここで全レースを落とすと監視不能になるので、
            # 判定不能時はログを出して監視継続。通知直前でも再確認できる。
            print(
                f"[DAY-WARN] {v} / {r} / 開催日判定不能 "
                f"(source={day_source}) → 監視継続",
                flush=True
            )
    else:
        print(
            f"[DAY-OK] {v} / {r} / "
            + ("初日" if event_day == 1 else "2日目")
            + f" (source={day_source})",
            flush=True
        )

    if not within15(text):
        return None

    d = deadline(text)
    a = alert_near_deadline(text, d)

    base_1st = parse_base_1st(text)
    theory = parse_theory_1st(text)
    current_diffs = parse_current_diffs(text)
    checker_all = parse_checker_1st_all(text, current_diffs)
    final_rates = build_final_1st_rates(
        base_1st,
        theory,
        checker_all,
    )

    print(
        "[1ST-RATE] "
        + " / ".join(
            (
                f"{b}号艇="
                f"{final_rates[b]['final']:.1f}% "
                f"(元{final_rates[b]['base']:.1f}"
                f"+理論{final_rates[b]['theory']:+.1f}"
                f"+CHK{final_rates[b]['checker']:+.1f})"
            )
            if final_rates.get(b, {}).get("final") is not None
            else f"{b}号艇=計算不足"
            for b in range(1, 7)
        ),
        flush=True,
    )

    # 6艇全部取れないレースは誤判定防止のため除外
    if len(theory) < 6:
        print(
            f"シンsum理論6艇取得失敗: "
            f"{v} / {r} / "
            f"取得 {len(theory)}艇",
            flush=True
        )
        return None

    # -----------------------------
    # A. 従来: やや本命 / 荒れ注意から選別
    # -----------------------------
    if a:
        classification = classify_theory(theory)

        if not classification:
            print(
                f"選別対象外: "
                f"{a} / {v} / {r} / "
                f"1={theory[1]:+g}% "
                f"2={theory[2]:+g}% "
                f"3={theory[3]:+g}% "
                f"4={theory[4]:+g}% "
                f"5={theory[5]:+g}% "
                f"6={theory[6]:+g}%",
                flush=True
            )
            return None

        return {
            "venue": v,
            "race": r,
            "deadline": d,
            "alert": a,
            "theory": theory,
            "final_rates": final_rates,
            "classification": classification,
            "key": (
                f"{now():%Y-%m-%d}|"
                f"{v}|{r}|{a}|{classification['type']}"
            )
        }

    # -----------------------------
    # B. 新規: 通常レースでも1〜4号艇の独立バフを検知
    # -----------------------------
    checker = {
        b: checker_all[b]
        for b in BUFF_TARGET_BOATS
        if b in checker_all
    }
    classification = classify_buff(
        theory,
        current_diffs,
        checker
    )

    if not classification:
        return None

    print(
        f"独立バフ候補: {v} / {r} / "
        + ", ".join(
            f"{x['boat']}号艇 "
            f"差{x['current_diff']:+.2f} "
            f"理論{x['theory_1st']:+.1f}% "
            f"チェッカー{x['checker_1st']:+.1f}%"
            for x in classification["buffs"]
        ),
        flush=True
    )

    return {
        "venue": v,
        "race": r,
        "deadline": d,
        "alert": "",
        "theory": theory,
        "final_rates": final_rates,
        "classification": classification,
        "key": (
            f"{now():%Y-%m-%d}|"
            f"{v}|{r}|BUFF|"
            + "-".join(str(x["boat"]) for x in classification["buffs"])
        )
    }


def cycle(page, initial=False):
    links = candidate_links(page)

    print(
        f"詳細候補リンク数: {len(links)}",
        flush=True
    )

    current = {}

    for u in links[:100]:
        try:
            page.goto(
                u,
                wait_until="domcontentloaded",
                timeout=20000
            )

            page.wait_for_timeout(350)

            item = inspect(page)

            if item:
                current[item["key"]] = item

        except Exception as e:
            print(
                "詳細ページ確認失敗:",
                u,
                repr(e),
                flush=True
            )

    # 起動直後に既に存在している通知を
    # 大量送信しないため既読登録
    if initial:
        seen.update(current.keys())

        print(
            f"初期既読登録: {len(current)}件",
            flush=True
        )

        for x in current.values():
            c = x["classification"]

            print(
                f"既読: "
                f"{c['type']} / "
                f"{x['alert'] or '通常レース'} / "
                f"{x['venue']} / "
                f"{x['race']} / "
                f"締切 {x['deadline']}",
                flush=True
            )

        return

    new_items = [
        x
        for k, x in current.items()
        if k not in seen
    ]

    if not new_items:
        print(
            "新規選別判定なし",
            flush=True
        )

    for x in new_items:
        notify_selected(
            page,
            x["alert"],
            x["venue"],
            x["race"],
            x["deadline"],
            x["theory"],
            x["classification"],
            x["final_rates"]
        )

    seen.update(
        current.keys()
    )


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(
            headless=True
        )

        c = b.new_context(
            http_credentials={
                "username": USER,
                "password": PASSWORD
            }
        )

        page = c.new_page()

        if not active():
            print(
                "監視時間外（23:00〜08:00 JST）。終了します。",
                flush=True
            )
            return

        print(
            f"[{now():%Y-%m-%d %H:%M:%S}] "
            f"やや本命/荒れ注意選別 + "
            f"1〜4号艇 独立バフ監視開始",
            flush=True
        )

        cycle(
            page,
            initial=True
        )

        while active():
            print(
                f"{CHECK_INTERVAL}秒後に再チェック",
                flush=True
            )

            time.sleep(
                CHECK_INTERVAL
            )

            print(
                f"[{now():%Y-%m-%d %H:%M:%S}] "
                f"再チェック",
                flush=True
            )

            cycle(
                page,
                initial=False
            )


if __name__ == "__main__":
    main()
