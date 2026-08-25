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

_http = requests.Session()
_http.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; shinsum-monitor-st/1.0)"
})

# 1レース1回だけ送信
seen = set()

# 予測配分
WEIGHT_ST_TREND = 0.45
WEIGHT_F = 0.35
WEIGHT_EXHIBITION = 0.20

# F補正
F1_PENALTY = 0.025
F2_PENALTY = 0.045


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


def within60(text):
    d = deadline(text)
    if not d:
        return False

    h, m = map(int, d.split(":"))
    t = now().replace(hour=h, minute=m, second=0, microsecond=0)
    return timedelta(minutes=-1) <= t - now() <= timedelta(minutes=60)


def candidate_links(page):
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1000)

    host = urlparse(BASE_URL).netloc
    out = []
    aa = page.locator("a")

    for i in range(aa.count()):
        a = aa.nth(i)
        try:
            href = a.get_attribute("href")
            if not href or href.startswith("#") or href.startswith("javascript:"):
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
    m = re.search(r"(?<!\d)([1-9]|1[0-2])\s*R\b", text[:1800], re.I)
    return m.group(1) + "R" if m else ""


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
    except:
        return None

    if lo is not None and v < lo:
        return None
    if hi is not None and v > hi:
        return None

    return v


def fetch_official_exhibition(venue, race_no):
    """BOATRACE公式から展示ST・展示タイム取得"""
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
        print(f"[EXHIBITION-ERR] {venue}{race_no}R {e!r}", flush=True)
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
    """現在のF有無はBOATRACE公式を正とする"""
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
    except Exception as e:
        print(f"[F-ERR] {venue}{race_no}R {e!r}", flush=True)
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


def fetch_hiyori_data(parent_page, venue, race_no):
    """
    ボートレース日和から、今回使うものだけ取得:
      - 当地ST順位
      - 最終日ST順位
      - F持ちST順位
      - 節間平均ST
      - トップST率

    使わない:
      - 直近1ヶ月
      - 直近3ヶ月
      - 初日
      - コース補正
      - トップST時1着率
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
        except:
            hp.wait_for_timeout(2500)

        body = hp.locator("body").inner_text(timeout=10000)

        rows = hp.locator("tr").evaluate_all(
            """els => els.map(tr =>
                Array.from(tr.querySelectorAll('th,td'))
                    .map(x => (x.innerText || x.textContent || '').trim())
            )"""
        )

        # ---- ST順位 / 節間平均ST ----
        for cells in rows:
            if not cells:
                continue

            label = re.sub(r"\s+", "", cells[0])

            def six_values(lo=None, hi=None):
                vals = [_num_or_none(x, lo, hi) for x in cells[1:7]]
                return vals if len(vals) == 6 else None

            if label == "当地" or label.startswith("当地ST"):
                vals = six_values(1.0, 6.0)
                if vals:
                    for b, v in enumerate(vals, 1):
                        if v is not None:
                            out[b]["local_rank"] = float(v)

            elif label == "最終日" or label.startswith("最終日ST"):
                vals = six_values(1.0, 6.0)
                if vals:
                    for b, v in enumerate(vals, 1):
                        if v is not None:
                            out[b]["finalday_rank"] = float(v)

            elif label == "F持" or label == "F持ち" or label.startswith("F持"):
                vals = six_values(1.0, 6.0)
                if vals:
                    for b, v in enumerate(vals, 1):
                        if v is not None:
                            out[b]["f_rank"] = float(v)

            elif label == "平均ST":
                vals = six_values(0.01, 0.40)
                if vals:
                    for b, v in enumerate(vals, 1):
                        if v is not None:
                            out[b]["session_avg_st"] = float(v)

        # ---- トップスタート率 ----
        # 表記が変わっても拾いやすいようにDOM本文とtrを併用
        # "トップスタート分析"の近辺に6艇分の%がある場合を想定
        top_rows = []
        for cells in rows:
            joined = " ".join(cells)
            if "トップスタート" in joined or "トップST" in joined:
                top_rows.append(cells)

        # 典型ケース: 各艇行に確率がある
        for cells in top_rows:
            joined = " ".join(cells)
            boat_m = re.search(r"(?<!\d)([1-6])号艇", joined)
            if not boat_m:
                continue

            b = int(boat_m.group(1))
            pcts = re.findall(r"(\d+(?:\.\d+)?)\s*%", joined)
            if pcts:
                out[b]["top_start_rate"] = float(pcts[0])

        # まだ足りなければ、本文から "1号艇 ... 34.4%" 形式を探す
        for b in range(1, 7):
            if "top_start_rate" in out[b]:
                continue

            m = re.search(
                rf"{b}号艇[\s\S]{{0,450}}?(\d+(?:\.\d+)?)\s*%",
                body
            )
            if m:
                out[b]["top_start_rate"] = float(m.group(1))

        return out

    except Exception as e:
        print(f"[HIYORI-ERR] {venue}{race_no}R {e!r}", flush=True)
        return {}

    finally:
        hp.close()


def merge_data(official_ex, official_f, hiyori):
    out = {i: {} for i in range(1, 7)}

    for b in range(1, 7):
        out[b].update(hiyori.get(b, {}))
        out[b].update(official_ex.get(b, {}))

        # F有無は公式で最後に上書き
        out[b]["f_count"] = int(
            official_f.get(b, {}).get("f_count", 0)
        )

    return out


def rank_to_st(rank):
    """
    ST順位を本番STの基準値へ変換。
    2.0 -> 0.115
    3.0 -> 0.145
    4.0 -> 0.175
    5.0 -> 0.205
    """
    if rank is None:
        return None

    return 0.055 + 0.030 * float(rank)


def predict_st(d):
    """
    本番ST予測
      ST傾向45%
      F補正35%
      展示ST20%

    ST傾向の中身:
      最終日ST順位
      当地ST順位
      節間平均ST
      トップST率

    使わない:
      直近1ヶ月
      直近3ヶ月
      初日
      コース補正
      トップST時1着率
    """
    final_est = rank_to_st(d.get("finalday_rank"))
    local_est = rank_to_st(d.get("local_rank"))
    session = d.get("session_avg_st")
    top_rate = d.get("top_start_rate")

    trend_parts = []

    # ST傾向45%の中での内訳
    if final_est is not None:
        trend_parts.append((final_est, 0.35))

    if local_est is not None:
        trend_parts.append((local_est, 0.25))

    if session is not None:
        trend_parts.append((float(session), 0.25))

    if top_rate is not None:
        ts = max(0.0, min(100.0, float(top_rate))) / 100.0

        # トップST率が高いほど速い基準値
        # 0% -> .19 / 50% -> .135 / 100% -> .08
        top_est = 0.190 - 0.110 * ts
        trend_parts.append((top_est, 0.15))

    if trend_parts:
        sw = sum(w for _, w in trend_parts)
        trend_st = sum(v * w for v, w in trend_parts) / sw
    else:
        trend_st = 0.16

    # ---- F補正35% ----
    f_rank_est = rank_to_st(d.get("f_rank"))
    f_st = f_rank_est if f_rank_est is not None else trend_st

    f_count = int(d.get("f_count", 0))

    if f_count >= 2:
        f_st += F2_PENALTY
    elif f_count == 1:
        f_st += F1_PENALTY

    # ---- 展示ST20% ----
    exst = d.get("ex_st")

    if exst is None:
        ex_st = trend_st
    elif exst < 0:
        # 展示Fをそのまま本番F予測にしない
        # 本番は安全側へ戻す
        ex_st = max(0.08, trend_st - 0.02)
    else:
        ex_st = float(exst)

    pred = (
        WEIGHT_ST_TREND * trend_st
        + WEIGHT_F * f_st
        + WEIGHT_EXHIBITION * ex_st
    )

    return max(0.03, min(0.30, round(pred, 2)))


def is_final_day_from_hiyori(parent_page, venue, race_no):
    """
    ボートレース日和のページ上部に最終日表記があるか判定。
    最終日以外は通知しない。
    """
    jcd = VENUE_CODES.get(venue)
    if not jcd:
        return False

    hd = now().strftime("%Y%m%d")
    url = (
        "https://kyoteibiyori.com/race_shusso.php"
        f"?place_no={jcd}&race_no={race_no}&hiduke={hd}"
    )

    hp = parent_page.context.new_page()

    try:
        hp.goto(url, wait_until="domcontentloaded", timeout=30000)
        hp.wait_for_timeout(1800)

        body = hp.locator("body").inner_text(timeout=10000)

        head = "\n".join(body.splitlines()[:180])

        if "最終日" in head:
            return True

        return False

    except Exception as e:
        print(f"[FINALDAY-ERR] {venue}{race_no}R {e!r}", flush=True)
        return False

    finally:
        hp.close()


def build_st_image(page, venue, race, deadline_value, data):
    """
    本番ST予測だけを表示する通知画像。
    「○号艇有利」
    「最終1着率」
    「判定理由」
    は表示しない。
    """
    race_no = int(re.sub(r"\D", "", str(race)) or 0)

    pred = {
        b: predict_st(data[b])
        for b in range(1, 7)
    }

    vals = list(pred.values())
    lo, hi = min(vals), max(vals)

    spread = max(hi - lo, 0.05)

    best = min(pred.values())

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

        fire = "🔥" if abs(pred[b] - best) < 0.0001 else ""

        lanes.append(
            f"""
            <div class="lane">
              <div class="num">{b}</div>

              <div class="track">
                <div class="start"></div>

                <div class="boat" style="left:{x:.1f}%">
                  ⛵▶ {fire}
                </div>
              </div>

              <div class="stat">
                <b>予測 {pred[b]:.02f}</b>
                <span>展示ST {exst_txt}</span>
              </div>
            </div>
            """
        )

    doc = f"""
    <html>
    <head>
      <meta charset="utf-8">

      <style>
        *{{box-sizing:border-box}}

        body{{
          margin:0;
          padding:30px;
          width:920px;
          background:#08111f;
          color:#eef4ff;
          font-family:
            "Noto Sans CJK JP",
            "Noto Sans JP",
            "Yu Gothic",
            sans-serif;
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
          font-size:16px;
          color:#90a4c2;
          margin-top:6px;
        }}

        .title-right{{
          font-size:27px;
          font-weight:900;
          color:#ffcf63;
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
          margin-bottom:14px;
        }}

        .lane{{
          display:grid;
          grid-template-columns:42px 1fr 175px;
          gap:10px;
          align-items:center;
          min-height:72px;
          border-top:1px solid #1d2c42;
        }}

        .lane:first-of-type{{
          border-top:none;
        }}

        .num{{
          width:34px;
          height:34px;
          border-radius:50%;
          border:1px solid #607798;
          display:flex;
          align-items:center;
          justify-content:center;
          font-weight:900;
        }}

        .track{{
          position:relative;
          height:52px;
        }}

        .track:before{{
          content:"";
          position:absolute;
          left:0;
          right:0;
          top:26px;
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
          font-size:27px;
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
          font-size:18px;
          color:#f2f6ff;
        }}

        .arrow{{
          text-align:center;
          margin-top:12px;
          color:#a6b8d1;
          font-weight:800;
        }}

        .note{{
          margin-top:14px;
          color:#8fa5c4;
          font-size:13px;
          line-height:1.6;
        }}

        .foot{{
          margin-top:20px;
          color:#6f83a2;
          font-size:12px;
          text-align:right;
        }}
      </style>
    </head>

    <body>

      <div class="top">

        <div>
          <div class="race">
            {html.escape(venue)} {html.escape(str(race))}
          </div>

          <div class="sub">
            最終日 / 締切 {html.escape(str(deadline_value))}
          </div>
        </div>

        <div class="title-right">
          本番ST予測
        </div>

      </div>

      <div class="card">

        <div class="title">
          本番ST予測スリット
        </div>

        {''.join(lanes)}

        <div class="arrow">
          進行方向 →　　STARTは赤線
        </div>

        <div class="note">
          右に出ているほど本番先行予測<br>
          ST傾向45% / F補正35% / 展示ST20%
        </div>

      </div>

      <div class="foot">
        使用: 最終日ST順位・当地ST順位・節間平均ST・トップST率・F情報・展示ST
      </div>

    </body>
    </html>
    """

    ip = page.context.new_page()

    try:
        ip.set_viewport_size({
            "width": 920,
            "height": 1000
        })

        ip.set_content(
            doc,
            wait_until="load"
        )

        ip.wait_for_timeout(300)

        path = (
            f"/tmp/st_predict_"
            f"{venue}_{race_no}_"
            f"{int(time.time())}.png"
        )

        ip.screenshot(
            path=path,
            full_page=True
        )

        return path

    finally:
        ip.close()


def send_image_ntfy(image_path, message):
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
            headers={
                "Content-Type": "image/png"
            },
            timeout=30,
        )

    r.raise_for_status()


def inspect_st(page):
    """
    Shinsumページから
    場・R・締切だけ取得。

    ST予測データは
    BOATRACE公式 + ボートレース日和から取得する。
    """
    text = page.locator("body").inner_text(timeout=10000)

    v = actual_venue(text)
    r = actual_race(text)

    if not v or not r:
        return None

    # 1〜12Rすべて対象
    race_no = int(re.sub(r"\D", "", r) or 0)

    if race_no < 1 or race_no > 12:
        return None

    # 最終日のみ
    if not is_final_day_from_hiyori(
        page,
        v,
        race_no
    ):
        return None

    # 展示が出る前は通知しない
    ex = fetch_official_exhibition(
        v,
        race_no
    )

    ex_count = sum(
        1
        for b in range(1, 7)
        if ex.get(b, {}).get("ex_st") is not None
    )

    if ex_count < 4:
        return None

    # 締切60分以内
    if not within60(text):
        return None

    d = deadline(text)

    official_f = fetch_official_f(
        v,
        race_no
    )

    hiyori = fetch_hiyori_data(
        page,
        v,
        race_no
    )

    data = merge_data(
        ex,
        official_f,
        hiyori
    )

    pred = {
        b: predict_st(data[b])
        for b in range(1, 7)
    }

    print(
        f"[ST-PREDICT] {v} {r} / "
        + " / ".join(
            f"{b}={pred[b]:.02f}"
            for b in range(1, 7)
        ),
        flush=True
    )

    return {
        "venue": v,
        "race": r,
        "deadline": d,
        "data": data,
        "key": (
            f"{now():%Y-%m-%d}|"
            f"{v}|{r}|ST"
        )
    }


def cycle(page, initial=False):
    links = candidate_links(page)

    print(
        f"詳細候補リンク数: {len(links)}",
        flush=True
    )

    current = {}

    for u in links[:120]:
        try:
            page.goto(
                u,
                wait_until="domcontentloaded",
                timeout=20000
            )

            page.wait_for_timeout(300)

            item = inspect_st(page)

            if item:
                current[item["key"]] = item

        except Exception as e:
            print(
                "詳細ページ確認失敗:",
                u,
                repr(e),
                flush=True
            )

    # 起動直後は既読登録だけ
    # すでに展示済みのレースを大量送信しない
    if initial:
        seen.update(current.keys())

        print(
            f"初期既読登録: {len(current)}件",
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
            "新規ST予測通知なし",
            flush=True
        )

    for x in new_items:
        image_path = None

        try:
            image_path = build_st_image(
                page,
                x["venue"],
                x["race"],
                x["deadline"],
                x["data"]
            )

            send_image_ntfy(
                image_path,
                f"{x['venue']} {x['race']} / 本番ST予測"
            )

            print(
                f"[ST-NOTIFY] "
                f"{x['venue']} {x['race']} / "
                f"締切 {x['deadline']}",
                flush=True
            )

        except Exception as e:
            print(
                f"[ST-NOTIFY-ERR] "
                f"{x['venue']} {x['race']} / "
                f"{e!r}",
                flush=True
            )

        finally:
            if image_path:
                try:
                    os.remove(image_path)
                except:
                    pass

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
            f"Shinsum Monitor "
            f"最終日1〜12R 本番ST予測開始",
            flush=True
        )

        print(
            "予測配分: "
            "ST傾向45% / "
            "F補正35% / "
            "展示ST20%",
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
