import os
import re
import time
import html
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo
from html.parser import HTMLParser

import requests
from playwright.sync_api import sync_playwright

# =========================================================
# Shinsum Monitor
# 最終日限定 / 1R〜12R / 本番ST予測のみ
#
# 重要仕様
# - TARGET_VENUES のうち「今日が最終日」の場だけ監視
# - 最終日判定は場ごとに1回だけ公式BOATRACEから取得してキャッシュ
# - 最終日でない場は候補リンクの段階で除外
# - 展示STが6艇すべて揃うまで絶対に通知しない
# - 展示タイムは参考表示のみ。通知可否は6艇の展示STで判定
# - 2着予想 / ○号艇有利 / 最終1着率 は出さない
# - コース補正は入れない
# - 本番ST予測配分:
#     ST傾向 45%
#     F補正 35%
#     展示ST 20%
# - 直近1ヶ月 / 直近3ヶ月 / 初日 は予測に使わない
# =========================================================

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
    "User-Agent": "Mozilla/5.0 (compatible; shinsum-finalday-st-monitor/2.0)"
})

# 場単位キャッシュ
_FINAL_DAY_CACHE = {}
_FINAL_DAY_VENUES_CACHE = {
    "date": None,
    "venues": None,
}

# 公式ページの「最終日」という別欄を誤読する場合に備えた日付別の確定表。
# 値がある日は公式の自動判定よりこちらを優先する。
FINAL_DAY_OVERRIDES = {
    "20260825": ["芦屋"],
}

# そのプロセス内で通知済み
seen = set()


# ---------------------------------------------------------
# 基本
# ---------------------------------------------------------

def now():
    return datetime.now(JST)


def active():
    return 8 <= now().hour < 23


def today_hd():
    return now().strftime("%Y%m%d")


def deadline(text):
    m = re.search(
        r"締切\s*[：:]?\s*([01]?\d|2[0-3]):([0-5]\d)",
        text
    )
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


def deadline_dt(deadline_value):
    if not deadline_value:
        return None
    try:
        h, m = map(int, deadline_value.split(":"))
        return now().replace(
            hour=h,
            minute=m,
            second=0,
            microsecond=0
        )
    except Exception:
        return None


def race_is_still_relevant(deadline_value):
    """
    終了済みレースを拾わない。
    締切5分後まで許容。
    """
    t = deadline_dt(deadline_value)
    if t is None:
        return True
    return t >= now() - timedelta(minutes=5)


# ---------------------------------------------------------
# HTML
# ---------------------------------------------------------

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
                self.row.append(
                    re.sub(r"\s+", " ", "".join(self.cell)).strip()
                )
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
    raw = re.sub(
        r"<script\b[^>]*>.*?</script>",
        " ",
        raw,
        flags=re.I | re.S
    )
    raw = re.sub(
        r"<style\b[^>]*>.*?</style>",
        " ",
        raw,
        flags=re.I | re.S
    )
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(
        r"\s+",
        " ",
        html.unescape(raw)
    ).strip()


def _num_or_none(s, lo=None, hi=None):
    s = re.sub(r"\s+", "", str(s))
    if s in ("", "-", "－", "—"):
        return None

    m = re.search(r"(-?\d+(?:\.\d+)?)", s)
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


def _parse_st_token(s):
    """
    0.07 / .07 / F.02 / F02 をST値に変換。
    展示Fは負数で返す。
    """
    s = str(s).strip().upper().replace(" ", "")

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


# ---------------------------------------------------------
# 最終日判定
# ---------------------------------------------------------

def is_final_day_official(venue):
    """
    今日、その場が最終日かをBOATRACE公式で厳密判定。

    「最終日」という単語がページ内にあるだけではTrueにしない。
    出走表には成績欄として「最終日」が常時出る場合があるため、
    raceindex上の「今日の日付 + 開催日ラベル」を確認する。

    取得失敗・判定不能は誤通知防止でFalse。
    """
    key = f"{today_hd()}|{venue}"
    if key in _FINAL_DAY_CACHE:
        return _FINAL_DAY_CACHE[key]

    jcd = VENUE_CODES.get(venue)
    if not jcd:
        _FINAL_DAY_CACHE[key] = False
        return False

    url = (
        "https://www.boatrace.jp/owpc/pc/race/raceindex"
        f"?hd={today_hd()}&jcd={jcd:02d}"
    )

    final_day = False

    try:
        r = _http.get(url, timeout=7)
        r.raise_for_status()

        compact = re.sub(r"\s+", "", _clean_html(r.text))

        dt = now()
        today_label = f"{dt.month}月{dt.day}日"

        # 例:
        # 8月25日初日
        # 8月25日2日目
        # 8月25日最終日
        m = re.search(
            rf"{re.escape(today_label)}"
            r"(初日|(?:[1-9]|1[0-2])日目|最終日)",
            compact
        )

        if m:
            day_label = m.group(1)
            final_day = (day_label == "最終日")
            print(
                f"[FINAL-DAY-CHECK] {venue} "
                f"{today_label}{day_label}",
                flush=True
            )
        else:
            print(
                f"[FINAL-DAY-CHECK] {venue} "
                f"{today_label}の開催日ラベル取得不能 -> 対象外",
                flush=True
            )
            final_day = False

    except Exception as e:
        print(
            f"[FINAL-DAY-ERR] {venue}: {e!r}",
            flush=True
        )
        final_day = False

    _FINAL_DAY_CACHE[key] = final_day

    print(
        f"[FINAL-DAY] {venue} -> "
        f"{'最終日' if final_day else '対象外'}",
        flush=True
    )

    return final_day

def get_final_day_venues():
    """
    今日の最終日場だけ返す。

    日付別オーバーライドがある日はそれを最優先。
    これにより公式ページ内の別用途の「最終日」表記を
    誤って拾って他場を監視する事故を防ぐ。
    """
    d = today_hd()

    override = FINAL_DAY_OVERRIDES.get(d)
    if override is not None:
        venues = [v for v in override if v in TARGET_VENUES]
        _FINAL_DAY_VENUES_CACHE["date"] = d
        _FINAL_DAY_VENUES_CACHE["venues"] = venues

        print(
            "[FINAL-DAY-OVERRIDE] "
            + (", ".join(venues) if venues else "該当場なし"),
            flush=True
        )
        return venues

    if (
        _FINAL_DAY_VENUES_CACHE["date"] == d
        and _FINAL_DAY_VENUES_CACHE["venues"] is not None
    ):
        return _FINAL_DAY_VENUES_CACHE["venues"]

    venues = [
        v for v in TARGET_VENUES
        if is_final_day_official(v)
    ]

    _FINAL_DAY_VENUES_CACHE["date"] = d
    _FINAL_DAY_VENUES_CACHE["venues"] = venues

    print(
        "[FINAL-DAY-TARGET] "
        + (", ".join(venues) if venues else "該当場なし"),
        flush=True
    )
    return venues


# ---------------------------------------------------------
# Shinsumリンク収集
# ---------------------------------------------------------

def candidate_links(page, final_day_venues):
    """
    最終日の場に関係するリンクだけ候補化。
    ここで他場を落とすので処理が軽くなる。
    """
    if not final_day_venues:
        return []

    page.goto(
        BASE_URL,
        wait_until="domcontentloaded",
        timeout=30000
    )
    page.wait_for_timeout(800)

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
                txt = a.inner_text(timeout=200) or ""
            except Exception:
                pass

            try:
                txt += "\n" + a.locator(
                    "xpath=ancestor::*[self::div or self::td or self::li or self::section][1]"
                ).inner_text(timeout=200)
            except Exception:
                pass

            if not any(v in txt for v in final_day_venues):
                continue

            if (
                re.search(r"([1-9]|1[0-2])\s*R", txt)
                or "race" in full.lower()
                or "detail" in full.lower()
                or "sum" in full.lower()
            ):
                out.append(full)

        except Exception:
            pass

    return list(dict.fromkeys(out))


def actual_venue(text, final_day_venues):
    head = text[:2200]
    matches = [v for v in final_day_venues if v in head]
    return matches[0] if len(matches) == 1 else ""


def actual_race(text):
    m = re.search(
        r"(?<!\d)([1-9]|1[0-2])\s*R\b",
        text[:2200],
        re.I
    )
    return m.group(1) + "R" if m else ""


# ---------------------------------------------------------
# 公式BOATRACE
# ---------------------------------------------------------

def fetch_official_exhibition(venue, race_no):
    """
    公式直前情報から
    - 展示タイム
    - スタート展示ST
    を取得。
    """
    jcd = VENUE_CODES.get(venue)
    if not jcd:
        return {}

    url = (
        "https://www.boatrace.jp/owpc/pc/race/beforeinfo"
        f"?hd={today_hd()}&jcd={jcd:02d}&rno={race_no}"
    )

    try:
        r = _http.get(url, timeout=12)
        r.raise_for_status()
    except Exception as e:
        print(
            f"[EXHIBITION-ERR] {venue}{race_no}R {e!r}",
            flush=True
        )
        return {}

    out = {i: {} for i in range(1, 7)}

    for tb in _tables(r.text):
        flat = " ".join(" ".join(row) for row in tb)

        # 展示タイム
        if "展示タイム" in flat:
            for row in tb:
                s = " ".join(row)

                bm = re.search(
                    r"(?<!\d)([1-6])(?!\d)",
                    s
                )
                tm = re.search(
                    r"(?<!\d)(6\.\d{2}|7\.\d{2})(?!\d)",
                    s
                )

                if bm and tm:
                    out[int(bm.group(1))]["ex_time"] = float(
                        tm.group(1)
                    )

        # スタート展示
        if "スタート展示" in flat or ("ST" in flat and "進入" in flat):
            for row in tb:
                s = " ".join(row)

                bm = re.search(
                    r"(?<!\d)([1-6])(?!\d)",
                    s
                )
                if not bm:
                    continue

                st = _parse_st_token(s)

                if st is not None:
                    out[int(bm.group(1))]["ex_st"] = st

    return out


def exhibition_complete(ex):
    """
    本番ST予測を出してよいかのガード。

    6艇すべての「スタート展示ST」が取得できたらTrue。
    展示タイムは画像表示用の参考値であり、予測配分には使わないため
    展示タイム取得失敗だけで通知を止めない。

    展示開始前は6艇のex_stが揃わないのでFalseのまま。
    """
    if not ex or len(ex) < 6:
        return False

    for b in range(1, 7):
        d = ex.get(b, {})
        if d.get("ex_st") is None:
            return False

    return True

def fetch_official_f(venue, race_no):
    """
    F数はBOATRACE公式を正とする。
    """
    jcd = VENUE_CODES.get(venue)
    if not jcd:
        return {}

    url = (
        "https://www.boatrace.jp/owpc/pc/race/racelist"
        f"?hd={today_hd()}&jcd={jcd:02d}&rno={race_no}"
    )

    try:
        r = _http.get(url, timeout=12)
        r.raise_for_status()
    except Exception as e:
        print(
            f"[F-ERR] {venue}{race_no}R {e!r}",
            flush=True
        )
        return {}

    out = {
        i: {"f_count": 0}
        for i in range(1, 7)
    }

    for tb in _tables(r.text):
        flat = " ".join(" ".join(row) for row in tb)

        if "平均ST" not in flat:
            continue

        for row in tb:
            s = " ".join(row)

            bm = re.search(
                r"^\s*([1-6])(?:\s|$)",
                s
            )
            if not bm:
                continue

            b = int(bm.group(1))

            fm = re.search(
                r"F\s*([0-9])",
                s
            )

            out[b]["f_count"] = (
                int(fm.group(1))
                if fm
                else 0
            )

    return out


# ---------------------------------------------------------
# ボートレース日和
# ---------------------------------------------------------

def _extract_six_numeric(cells, lo=None, hi=None):
    vals = [
        _num_or_none(x, lo, hi)
        for x in cells[1:7]
    ]
    if len(vals) != 6:
        return None
    return vals


def fetch_hiyori_st(page, venue, race_no):
    """
    ボートレース日和のST予測用データ。

    使用:
    - 当地ST順位
    - 最終日ST順位
    - ナイターST順位（ナイター場のみ）
    - F持ST順位
    - 節間平均ST
    - トップスタート確率
    - トップスタート時1着率

    不使用:
    - 直近1ヶ月
    - 直近3ヶ月
    - 初日
    - コース補正
    """
    jcd = VENUE_CODES.get(venue)
    if not jcd:
        return {}

    url = (
        "https://kyoteibiyori.com/race_shusso.php"
        f"?place_no={jcd}&race_no={race_no}&hiduke={today_hd()}"
    )

    out = {i: {} for i in range(1, 7)}
    hp = page.context.new_page()

    try:
        hp.goto(
            url,
            wait_until="domcontentloaded",
            timeout=30000
        )

        try:
            hp.wait_for_function(
                """() => {
                    const t = document.body?.innerText || '';
                    return t.includes('ST順位');
                }""",
                timeout=10000
            )
        except Exception:
            hp.wait_for_timeout(2200)

        rows = hp.locator("tr").evaluate_all(
            """els => els.map(tr =>
                Array.from(tr.querySelectorAll('th,td'))
                    .map(x => (x.innerText || x.textContent || '').trim())
            )"""
        )

        # -------------------------
        # ST順位系・節間平均ST
        # -------------------------
        for cells in rows:
            if not cells:
                continue

            label = re.sub(r"\s+", "", cells[0])

            key = None

            if label == "当地" or label.startswith("当地ST"):
                key = "local_rank"

            elif label == "最終日" or label.startswith("最終日ST"):
                key = "finalday_rank"

            elif venue in NIGHT_VENUES and (
                label == "ナイター"
                or label.startswith("ナイターST")
            ):
                key = "night_rank"

            elif (
                label == "F持"
                or label == "F持ち"
                or label.startswith("F持")
            ):
                key = "f_rank"

            if key:
                vals = _extract_six_numeric(
                    cells,
                    1.0,
                    6.0
                )

                if vals:
                    for b, v in enumerate(vals, 1):
                        if v is not None:
                            out[b][key] = float(v)

            # 節間平均ST
            if (
                "節間平均ST" in label
                or label == "節間ST"
                or label.startswith("節間平均")
            ):
                vals = _extract_six_numeric(
                    cells,
                    0.00,
                    0.40
                )

                if vals:
                    for b, v in enumerate(vals, 1):
                        if v is not None:
                            out[b]["series_avg_st"] = float(v)

        # -------------------------
        # トップスタート分析
        # 画面の表:
        # 選手名 / 出走数 / 回数 / 確率 / 1着数 / 1着率
        # -------------------------
        body_text = hp.locator("body").inner_text(timeout=10000)

        if "トップスタート分析" in body_text:
            for cells in rows:
                if len(cells) < 5:
                    continue

                joined = " ".join(cells)

                # 登録番号を艇番に照合するのはページ上の順番が一番安定。
                # ここでは選手名/登録番号を含む行を上から6行だけ拾う。
                pct_tokens = re.findall(
                    r"(\d+(?:\.\d+)?)\s*%",
                    joined
                )

                if len(pct_tokens) < 1:
                    continue

                # 行内に登録番号4桁があること
                if not re.search(r"\b\d{4}\b", joined):
                    continue

                # 後で順番割当用
                out.setdefault("_top_rows", []).append({
                    "top_rate": float(pct_tokens[0]),
                    "top_win_rate": (
                        float(pct_tokens[1])
                        if len(pct_tokens) >= 2
                        else None
                    )
                })

            top_rows = out.pop("_top_rows", [])

            if len(top_rows) >= 6:
                for b in range(1, 7):
                    out[b]["top_rate"] = top_rows[b - 1]["top_rate"]
                    if top_rows[b - 1]["top_win_rate"] is not None:
                        out[b]["top_win_rate"] = top_rows[b - 1]["top_win_rate"]

        return out

    except Exception as e:
        print(
            f"[HIYORI-ERR] {venue}{race_no}R {e!r}",
            flush=True
        )
        return {}

    finally:
        hp.close()


# ---------------------------------------------------------
# ST予測
# ---------------------------------------------------------

def _rank_to_st(rank_value):
    """
    ST順位(1〜6)を予測STスケールへ。
    コース差は一切入れない。
    """
    if rank_value is None:
        return None

    # 1位≈0.11 / 6位≈0.19
    return 0.11 + (float(rank_value) - 1.0) * 0.016


def trend_estimate(d):
    """
    ST傾向45%の中身。
    直近1/3ヶ月・初日は使わない。
    """
    estimates = []

    # 当地
    x = _rank_to_st(d.get("local_rank"))
    if x is not None:
        estimates.append((x, 1.0))

    # 最終日
    x = _rank_to_st(d.get("finalday_rank"))
    if x is not None:
        estimates.append((x, 1.2))

    # ナイター
    x = _rank_to_st(d.get("night_rank"))
    if x is not None:
        estimates.append((x, 0.8))

    # 節間平均STは実STなので強め
    if d.get("series_avg_st") is not None:
        estimates.append((
            float(d["series_avg_st"]),
            1.5
        ))

    if not estimates:
        base = 0.15
    else:
        sw = sum(w for _, w in estimates)
        base = sum(v * w for v, w in estimates) / sw

    # トップST率は傾向の微調整
    top_rate = d.get("top_rate")
    if top_rate is not None:
        # 25%を中立、10ptごとに約0.008補正
        base -= (float(top_rate) - 25.0) * 0.0008

    # トップSTを決めた時の1着率は、
    # 「前を取った時の信頼度」として小さく補強。
    top_win_rate = d.get("top_win_rate")
    if top_win_rate is not None:
        base -= (float(top_win_rate) - 30.0) * 0.00015

    return max(0.06, min(0.24, base))


def f_adjusted_estimate(d, trend):
    """
    F補正35%。
    Fなし艇はトレンド値をそのまま使うため、
    Fなしなのに勝手にコース補正されることはない。
    """
    f_count = int(d.get("f_count", 0))

    if f_count <= 0:
        return trend

    f_rank = d.get("f_rank")

    if f_rank is not None:
        f_est = _rank_to_st(f_rank)
    else:
        f_est = trend + 0.012 * f_count

    # F2はF1よりさらに慎重
    if f_count >= 2:
        f_est += 0.010

    return max(0.06, min(0.26, f_est))


def exhibition_estimate(d, trend):
    """
    展示ST20%。
    展示Fをそのまま本番F予測にはしない。
    """
    ex_st = d.get("ex_st")

    if ex_st is None:
        return trend

    ex_st = float(ex_st)

    if ex_st >= 0:
        return max(0.02, min(0.30, ex_st))

    # 展示F:
    # 本番でも負値と決めつけず、
    # その艇の傾向より少しだけ踏み込む推定。
    depth = abs(ex_st)

    if depth <= 0.03:
        return max(0.04, trend - 0.015)

    if depth <= 0.06:
        return max(0.04, trend - 0.010)

    return max(0.05, trend - 0.005)


def predicted_st(d):
    """
    本番ST予測
      ST傾向 45%
      F補正 35%
      展示ST 20%
    """
    trend = trend_estimate(d)
    f_est = f_adjusted_estimate(d, trend)
    ex_est = exhibition_estimate(d, trend)

    p = (
        trend * 0.45
        + f_est * 0.35
        + ex_est * 0.20
    )

    return max(0.03, min(0.28, p))


def merge_data(exhibition, official_f, hiyori):
    out = {i: {} for i in range(1, 7)}

    for b in range(1, 7):
        out[b].update(hiyori.get(b, {}))
        out[b].update(exhibition.get(b, {}))

        # F有無は必ず公式で最後に上書き
        out[b]["f_count"] = int(
            official_f.get(b, {}).get("f_count", 0)
        )

    return out


# ---------------------------------------------------------
# 画像生成
# ---------------------------------------------------------

def build_st_image(page, venue, race, deadline_value, data):
    pred = {
        b: predicted_st(data[b])
        for b in range(1, 7)
    }

    vals = list(pred.values())
    lo = min(vals)
    hi = max(vals)
    spread = max(hi - lo, 0.04)

    # 最速予測艇だけ🔥
    best_boat = min(
        pred,
        key=pred.get
    )

    lanes = []

    for b in range(1, 7):
        # 右に出るほどSTが速い
        rel = (hi - pred[b]) / spread
        x = 22 + max(0.0, min(1.0, rel)) * 58

        d = data[b]

        exst = d.get("ex_st")
        if exst is None:
            exst_txt = "-"
        elif exst < 0:
            exst_txt = f"F{abs(exst):.02f}"
        else:
            exst_txt = f"{exst:.02f}"

        ext = d.get("ex_time")
        ext_txt = (
            f"{ext:.2f}"
            if ext is not None
            else "-"
        )

        fire = "🔥" if b == best_boat else ""

        lanes.append(f"""
        <div class="lane">
          <div class="num n{b}">{b}</div>
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

    doc = f"""
    <html>
    <head>
      <meta charset="utf-8">
      <style>
        *{{box-sizing:border-box}}
        body{{
          margin:0;
          padding:28px;
          width:900px;
          background:#08111f;
          color:#eef4ff;
          font-family:"Noto Sans CJK JP","Noto Sans JP","Yu Gothic",sans-serif;
        }}
        .top{{
          display:flex;
          justify-content:space-between;
          align-items:flex-end;
          padding-bottom:18px;
          border-bottom:1px solid #283850;
        }}
        .race{{
          font-size:31px;
          font-weight:900;
        }}
        .sub{{
          font-size:15px;
          color:#90a4c2;
          margin-top:5px;
        }}
        .card{{
          margin-top:24px;
          background:#0d192a;
          border:1px solid #263957;
          border-radius:18px;
          padding:20px;
        }}
        .title{{
          font-size:24px;
          font-weight:900;
          margin-bottom:12px;
        }}
        .lane{{
          display:grid;
          grid-template-columns:44px 1fr 170px;
          gap:10px;
          align-items:center;
          min-height:70px;
          border-top:1px solid #1d2c42;
        }}
        .lane:first-of-type{{
          border-top:none;
        }}
        .num{{
          width:35px;
          height:35px;
          border-radius:50%;
          border:1px solid #607798;
          display:flex;
          align-items:center;
          justify-content:center;
          font-weight:900;
        }}
        .track{{
          position:relative;
          height:48px;
        }}
        .track:before{{
          content:"";
          position:absolute;
          left:0;
          right:0;
          top:24px;
          height:2px;
          background:#263956;
        }}
        .start{{
          position:absolute;
          left:84%;
          top:2px;
          bottom:2px;
          width:3px;
          background:#ff5252;
        }}
        .boat{{
          position:absolute;
          top:8px;
          transform:translateX(-50%);
          font-size:25px;
          white-space:nowrap;
          font-weight:900;
        }}
        .stat{{
          display:flex;
          flex-direction:column;
          font-size:13px;
          color:#99acc7;
        }}
        .stat b{{
          font-size:17px;
          color:#f2f6ff;
        }}
        .arrow{{
          text-align:center;
          margin-top:10px;
          color:#a6b8d1;
          font-weight:800;
        }}
        .note{{
          margin-top:12px;
          color:#8fa5c4;
          font-size:13px;
        }}
        .foot{{
          margin-top:16px;
          color:#6f83a2;
          font-size:12px;
          text-align:right;
        }}
      </style>
    </head>
    <body>
      <div class="top">
        <div>
          <div class="race">{html.escape(venue)} {html.escape(str(race))}</div>
          <div class="sub">締切 {html.escape(str(deadline_value))}</div>
        </div>
      </div>

      <div class="card">
        <div class="title">本番ST予測</div>
        {''.join(lanes)}
        <div class="arrow">進行方向 →　　STARTは赤線</div>
        <div class="note">
          右に出ているほど本番先行予測 / 🔥は最速予測艇
        </div>
      </div>

      <div class="foot">
        予測配分：ST傾向45% / F補正35% / 展示ST20%
      </div>
    </body>
    </html>
    """

    ip = page.context.new_page()

    try:
        ip.set_viewport_size({
            "width": 900,
            "height": 950
        })

        ip.set_content(
            doc,
            wait_until="load"
        )

        ip.wait_for_timeout(250)

        safe_venue = re.sub(r"[^\w\-]+", "_", venue)
        path = (
            f"/tmp/st_{safe_venue}_"
            f"{re.sub(r'\\D', '', str(race))}_"
            f"{int(time.time())}.png"
        )

        ip.screenshot(
            path=path,
            full_page=True
        )

        return path, pred

    finally:
        ip.close()


# ---------------------------------------------------------
# ntfy
# ---------------------------------------------------------

def send_image_ntfy(image_path, venue, race):
    filename = os.path.basename(image_path)

    with open(image_path, "rb") as f:
        r = requests.put(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            params={
                "filename": filename,
                "title": "Shinsum Monitor",
                "message": f"{venue} {race} 本番ST予測",
                "priority": "5",
                "tags": "ship",
            },
            data=f,
            headers={
                "Content-Type": "image/png"
            },
            timeout=30,
        )

    r.raise_for_status()


# ---------------------------------------------------------
# 1レース判定
# ---------------------------------------------------------

def inspect(page, final_day_venues):
    text = page.locator(
        "body"
    ).inner_text(timeout=10000)

    venue = actual_venue(
        text,
        final_day_venues
    )
    race = actual_race(text)

    if not venue or not race:
        return None

    # 念のため二重ガード
    if venue not in final_day_venues:
        return None

    race_no = int(
        re.sub(r"\D", "", race) or 0
    )

    if not 1 <= race_no <= 12:
        return None

    d = deadline(text)

    if not race_is_still_relevant(d):
        return None

    # まず公式展示だけを見る
    exhibition = fetch_official_exhibition(
        venue,
        race_no
    )

    # ★ 6艇の展示STが揃っていなければ
    #    日和取得も画像生成もしない。通知もしない。
    if not exhibition_complete(exhibition):
        got_st = [
            b for b in range(1, 7)
            if exhibition.get(b, {}).get("ex_st") is not None
        ]
        print(
            f"[WAIT-EXHIBITION] {venue} {race} "
            f"展示ST取得={got_st} / 6艇未完了 → 通知しない",
            flush=True
        )
        return None

    official_f = fetch_official_f(
        venue,
        race_no
    )

    hiyori = fetch_hiyori_st(
        page,
        venue,
        race_no
    )

    data = merge_data(
        exhibition,
        official_f,
        hiyori
    )

    key = (
        f"{today_hd()}|"
        f"{venue}|{race}|ST"
    )

    return {
        "key": key,
        "venue": venue,
        "race": race,
        "deadline": d,
        "data": data,
    }


# ---------------------------------------------------------
# cycle
# ---------------------------------------------------------

def cycle(page):
    final_day_venues = get_final_day_venues()

    if not final_day_venues:
        print(
            "[CYCLE] 今日の対象最終日場なし",
            flush=True
        )
        return

    links = candidate_links(
        page,
        final_day_venues
    )

    print(
        f"詳細候補リンク数: {len(links)} "
        f"/ 最終日場: {', '.join(final_day_venues)}",
        flush=True
    )

    for u in links[:80]:
        try:
            page.goto(
                u,
                wait_until="domcontentloaded",
                timeout=20000
            )

            page.wait_for_timeout(250)

            item = inspect(
                page,
                final_day_venues
            )

            if not item:
                continue

            if item["key"] in seen:
                continue

            image_path = None

            try:
                image_path, pred = build_st_image(
                    page,
                    item["venue"],
                    item["race"],
                    item["deadline"],
                    item["data"],
                )

                send_image_ntfy(
                    image_path,
                    item["venue"],
                    item["race"],
                )

                print(
                    "[ST-PREDICT] "
                    f"{item['venue']} {item['race']} / "
                    + " / ".join(
                        f"{b}={pred[b]:.02f}"
                        for b in range(1, 7)
                    ),
                    flush=True
                )

                # 成功後だけ既読
                seen.add(item["key"])

            except Exception as e:
                print(
                    f"[NOTIFY-ERR] "
                    f"{item['venue']} {item['race']} "
                    f"{e!r}",
                    flush=True
                )

            finally:
                if image_path:
                    try:
                        os.remove(image_path)
                    except Exception:
                        pass

        except Exception as e:
            print(
                "詳細ページ確認失敗:",
                u,
                repr(e),
                flush=True
            )


# ---------------------------------------------------------
# main
# ---------------------------------------------------------

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True
        )

        context = browser.new_context(
            http_credentials={
                "username": USER,
                "password": PASSWORD
            }
        )

        page = context.new_page()

        if not active():
            print(
                "監視時間外（23:00〜08:00 JST）。終了します。",
                flush=True
            )
            return

        targets = get_final_day_venues()

        print(
            f"[{now():%Y-%m-%d %H:%M:%S}] "
            "Shinsum Monitor "
            "最終日1〜12R 本番ST予測開始（厳密最終日判定）",
            flush=True
        )

        print(
            "予測配分: "
            "ST傾向45% / F補正35% / 展示ST20%",
            flush=True
        )

        print(
            "監視対象: "
            + (", ".join(targets) if targets else "なし"),
            flush=True
        )

        # 起動直後から、
        # 展示完了済みの「まだ締切前」の対象レースは通知可能。
        cycle(page)

        while active():
            print(
                f"{CHECK_INTERVAL}秒後に再チェック",
                flush=True
            )

            time.sleep(
                CHECK_INTERVAL
            )

            print(
                f"[{now():%Y-%m-%d %H:%M:%S}] 再チェック",
                flush=True
            )

            cycle(page)

        browser.close()


if __name__ == "__main__":
    main()
