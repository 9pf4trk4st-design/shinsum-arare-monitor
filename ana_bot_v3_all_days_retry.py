# -*- coding: utf-8 -*-
"""
ANA チャンスBOT
- 全開催日
- 2〜6号艇を同一基準で評価
- ST / 今節ST / スタート展示 / 展示タイム / 左隣比較を主判定
- ナイター開催時だけナイターSTを加味
- 初日STは加味、直近1か月/3か月ST順位は不使用
- 展示Fは平常STとの組み合わせで評価
- 締切15分前 → データ不足なら13分前 → 11分前
- 通知名は「○号艇 チャンス」
- 最後にシンsum理論 + シンsumチェッカーで1着補正を確認
"""

import os
import re
import time
import html
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

JST = ZoneInfo("Asia/Tokyo")
BASE_URL = "https://boatrace-shinsum.com/"
SHINSUM_USER = os.environ["SHINSUM_USER"]
SHINSUM_PASSWORD = os.environ["SHINSUM_PASSWORD"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))

CHANCE_SCORE = float(os.getenv("ANA_CHANCE_SCORE", "5.0"))
FAST_ST = float(os.getenv("ANA_FAST_ST", "0.16"))
ALLOW_EX_F = float(os.getenv("ANA_ALLOW_EX_F", "0.03"))
EX_TIME_GOOD = float(os.getenv("ANA_EX_TIME_GOOD", "0.03"))
EX_TIME_STRONG = float(os.getenv("ANA_EX_TIME_STRONG", "0.07"))
ST_EDGE_GOOD = float(os.getenv("ANA_ST_EDGE_GOOD", "0.02"))
ST_EDGE_STRONG = float(os.getenv("ANA_ST_EDGE_STRONG", "0.04"))

TARGET_VENUES = {
    "戸田":2, "平和島":4, "多摩川":5, "蒲郡":7, "三国":10, "びわこ":11,
    "住之江":12, "鳴門":14, "児島":16, "宮島":17, "徳山":18, "下関":19,
    "若松":20, "芦屋":21, "唐津":23, "大村":24,
}
NIGHT_VENUES = {"蒲郡","住之江","下関","若松","大村"}
CHECKER_PARSE_BOATS = (1,2,3,4,5,6)
HEADERS = {"User-Agent":"Mozilla/5.0 (compatible; ana-chance-bot/2.0)"}
session = requests.Session()
session.headers.update(HEADERS)
_seen = set()
_attempt_state = {}
_pending_shinsum = {}
_shinsum_state = {}

def now():
    return datetime.now(JST)

def active():
    return 8 <= now().hour < 23

class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.table, self.row, self.cell = [], None, None, None
        self.depth = 0
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "table":
            self.depth += 1
            if self.depth == 1:
                self.table = []
        elif self.depth and tag == "tr":
            self.row = []
        elif self.depth and tag in ("td","th"):
            self.cell = []
    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)
    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.depth and tag in ("td","th") and self.cell is not None:
            if self.row is not None:
                self.row.append(re.sub(r"\s+"," ","".join(self.cell)).strip())
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

def tables(raw):
    p = TableParser()
    p.feed(raw)
    return p.tables

def clean_text(raw):
    raw = re.sub(r"<script\b[^>]*>.*?</script>"," ",raw,flags=re.I|re.S)
    raw = re.sub(r"<style\b[^>]*>.*?</style>"," ",raw,flags=re.I|re.S)
    raw = re.sub(r"<[^>]+>"," ",raw)
    return re.sub(r"\s+"," ",html.unescape(raw)).strip()

def parse_st_token(s):
    s = str(s).strip().upper().replace(" ","")
    m = re.search(r"F\.?(\d{1,2})", s)
    if m:
        return -int(m.group(1))/100.0
    m = re.search(r"0\.(\d{2})", s)
    if m:
        return int(m.group(1))/100.0
    m = re.search(r"(?<!\d)\.?(\d{2})(?!\d)", s)
    if m:
        return int(m.group(1))/100.0
    return None

def official_exhibition(venue, race):
    """公式展示ページから展示タイム/スタート展示STを取得。"""
    jcd = TARGET_VENUES[venue]
    hd = now().strftime("%Y%m%d")
    url = f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?hd={hd}&jcd={jcd:02d}&rno={race}"
    try:
        r = session.get(url, timeout=12)
        r.raise_for_status()
    except Exception as e:
        print("展示取得失敗", venue, race, repr(e), flush=True)
        return {}

    out = {i: {} for i in range(1, 7)}
    for tb in tables(r.text):
        flat = " ".join(" ".join(row) for row in tb)
        # 展示タイム表
        if "展示タイム" in flat:
            for row in tb:
                rowtxt = " ".join(row)
                bm = re.search(r"(?<!\d)([1-6])(?!\d)", rowtxt)
                tm = re.search(r"(?<!\d)(6\.\d{2}|7\.\d{2})(?!\d)", rowtxt)
                if bm and tm:
                    out[int(bm.group(1))]["ex_time"] = float(tm.group(1))
        # スタート展示表
        if "スタート展示" in flat or ("ST" in flat and "進入" in flat):
            for row in tb:
                rowtxt = " ".join(row)
                bm = re.search(r"(?<!\d)([1-6])(?!\d)", rowtxt)
                if not bm:
                    continue
                st = parse_st_token(rowtxt)
                if st is not None:
                    out[int(bm.group(1))]["ex_st"] = st

    # HTML構造変更時の補助パーサ
    txt = clean_text(r.text)
    if sum("ex_time" in x for x in out.values()) < 4:
        vals = [float(x) for x in re.findall(r"(?<!\d)([67]\.\d{2})(?!\d)", txt)]
        # 6艇連続の展示値らしい箇所だけ利用
        if len(vals) >= 6:
            vals = vals[-6:]
            for b, v in enumerate(vals, 1):
                out[b].setdefault("ex_time", v)

    return out

def official_avg_st_f(venue, race):
    """公式出走表から平均STと今期F数。"""
    jcd = TARGET_VENUES[venue]
    hd = now().strftime("%Y%m%d")
    url = f"https://www.boatrace.jp/owpc/pc/race/racelist?hd={hd}&jcd={jcd:02d}&rno={race}"
    try:
        r = session.get(url, timeout=12)
        r.raise_for_status()
    except Exception:
        return {}

    out = {}
    for tb in tables(r.text):
        flat = " ".join(" ".join(row) for row in tb)
        if "平均ST" not in flat:
            continue
        for row in tb:
            txt = " ".join(row)
            bm = re.search(r"^\s*([1-6])(?:\s|$)", txt)
            if not bm:
                continue
            b = int(bm.group(1))
            f = re.search(r"F\s*([0-9])", txt)
            sts = re.findall(r"(?<!\d)(0\.\d{2})(?!\d)", txt)
            if sts:
                out[b] = {"avg_st": float(sts[0]), "f_count": int(f.group(1)) if f else 0}
    return out

def _rank_after_label(text, label, boat):
    """
    競艇日和は表示変更があり得るため、ラベル周辺の艇別値を保守的に取得。
    取得できない項目は None にし、推測で埋めない。
    """
    pos = text.find(label)
    if pos < 0:
        return None
    block = text[pos:pos+1800]
    # 「3号艇 ... 2.81」等
    pats = [
        rf"{boat}\s*号艇.{{0,120}}?(\d(?:\.\d+)?)",
        rf"(?:^|\s){boat}(?:\s|号).{{0,100}}?(\d(?:\.\d+)?)",
    ]
    for pat in pats:
        m = re.search(pat, block, re.S)
        if m:
            v = float(m.group(1))
            if 1.0 <= v <= 6.0:
                return v
    return None

def _row_numbers(cells, lo, hi):
    vals = []
    for cell in cells:
        s = re.sub(r"\s+", " ", str(cell)).strip()
        for m in re.finditer(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)", s):
            try:
                v = float(m.group(1))
            except Exception:
                continue
            if lo <= v <= hi:
                vals.append(v)
    return vals


def _row_numbers(cells, lo, hi):
    vals = []
    for cell in cells:
        s = re.sub(r"\s+", " ", str(cell)).strip()
        for m in re.finditer(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)", s):
            try:
                v = float(m.group(1))
            except Exception:
                continue
            if lo <= v <= hi:
                vals.append(v)
    return vals


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


def hiyori_st_data(parent_page, venue, race):
    """
    ボートレース日和はST順位表がJavaScript描画されるため、
    requestsのHTMLではなくPlaywrightで描画後のtr/tdを読む。

    取得するもの:
      - 当地ST順位
      - 初日ST順位
      - ナイターST順位（ナイター場のみ）
      - F持ち順位（F持ち艇のみ）
      - 今節平均ST

    直近3ヶ月・直近1ヶ月・最終日は判定に使わない。
    """
    place = TARGET_VENUES[venue]
    hd = now().strftime("%Y%m%d")
    url = (
        "https://kyoteibiyori.com/race_shusso.php"
        f"?place_no={place}&race_no={race}&hiduke={hd}"
    )

    hp = parent_page.context.new_page()
    out = {i: {} for i in range(1, 7)}

    try:
        hp.goto(url, wait_until="domcontentloaded", timeout=30000)

        # ST順位/今節データがJSで埋まるのを待つ
        try:
            hp.wait_for_function(
                """() => {
                    const t = document.body?.innerText || '';
                    return t.includes('ST順位') && t.includes('平均ST');
                }""",
                timeout=10000,
            )
        except Exception:
            hp.wait_for_timeout(2500)

        # DOM上の全trを「セル配列」のまま取得。
        rows = hp.locator("tr").evaluate_all(
            """els => els.map(tr =>
                Array.from(tr.querySelectorAll('th,td'))
                     .map(x => (x.innerText || x.textContent || '').trim())
            )"""
        )

        # 表示上、行頭がラベル、後ろ6列が1〜6号艇。
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
                            out[b]["local_rank"] = v

            elif label == "初日" or label.startswith("初日ST"):
                vals = six_values(1.0, 6.0)
                if vals:
                    for b, v in enumerate(vals, 1):
                        if v is not None:
                            out[b]["firstday_rank"] = v

            elif venue in NIGHT_VENUES and (
                label == "ナイター" or label.startswith("ナイターST")
            ):
                vals = six_values(1.0, 6.0)
                if vals:
                    for b, v in enumerate(vals, 1):
                        if v is not None:
                            out[b]["night_rank"] = v

            elif label == "F持" or label == "F持ち" or label.startswith("F持"):
                # ST順位欄のF持ち行は、F持ち艇だけ数字、他艇は「-」。
                vals = six_values(1.0, 6.0)
                if vals:
                    for b, v in enumerate(vals, 1):
                        if v is not None:
                            out[b]["f_rank"] = v
                            # 注意:
                            # F持ち順位が表示されていても「現在F持ち」とは限らない。
                            # 現在のF有無は公式BOATRACEのF0/F1のみで判定する。

            elif label in ("平均ST", "今節平均ST"):
                vals = six_values(0.00, 0.40)
                if vals:
                    for b, v in enumerate(vals, 1):
                        if v is not None:
                            out[b]["series_st"] = v

        # 行が複雑な場合の補助:
        # innerTextを行単位で見て「平均ST 0.14 0.17 ...」等も拾う。
        body_text = hp.locator("body").inner_text(timeout=10000)
        lines = [re.sub(r"\s+", " ", x).strip() for x in body_text.splitlines()]

        def fallback_line(label, key, lo, hi, f_only=False):
            for i, line in enumerate(lines):
                if re.sub(r"\s+", "", line) != label:
                    continue
                # 次の数行も含め、6艇分の表示セルを順に拾う
                chunk = lines[i:i+10]
                raw = []
                for z in chunk[1:]:
                    zz = z.strip()
                    if zz in ("-", "－", "—"):
                        raw.append(None)
                    else:
                        v = _num_or_none(zz, lo, hi)
                        if v is not None:
                            raw.append(v)
                    if len(raw) >= 6:
                        break
                if len(raw) >= 6:
                    for b, v in enumerate(raw[:6], 1):
                        if v is not None and key not in out[b]:
                            out[b][key] = v
                            # f_onlyでもf_countは立てない。
                            # 現在Fは公式BOATRACE F0/F1を正とする。
                    return

        fallback_line("当地", "local_rank", 1.0, 6.0)
        fallback_line("初日", "firstday_rank", 1.0, 6.0)
        if venue in NIGHT_VENUES:
            fallback_line("ナイター", "night_rank", 1.0, 6.0)
        fallback_line("F持", "f_rank", 1.0, 6.0, f_only=True)
        fallback_line("平均ST", "series_st", 0.00, 0.40)

        local_n = sum(out[b].get("local_rank") is not None for b in range(1, 7))
        first_n = sum(out[b].get("firstday_rank") is not None for b in range(1, 7))
        night_n = sum(out[b].get("night_rank") is not None for b in range(1, 7))
        f_rank_n = sum(out[b].get("f_rank") is not None for b in range(1, 7))
        series_n = sum(out[b].get("series_st") is not None for b in range(1, 7))

        if venue in NIGHT_VENUES:
            print(
                f"[HIYORI-ST] {venue}{race}R 当地={local_n}/6 初日={first_n}/6 "
                f"ナイター={night_n}/6 今節ST={series_n}/6 "
                f"F持順位データ={f_rank_n}/6",
                flush=True,
            )
        else:
            print(
                f"[HIYORI-ST] {venue}{race}R 当地={local_n}/6 初日={first_n}/6 "
                f"今節ST={series_n}/6 "
                f"F持順位データ={f_rank_n}/6",
                flush=True,
            )

        return out

    except Exception as e:
        print(f"[HIYORI-ERR] {venue}{race}R {e!r}", flush=True)
        return {}
    finally:
        hp.close()

def avg_rank(d):
    """
    基本ST順位:
      当地 + 初日
      ナイター場では + ナイター

    F持ち艇だけ:
      + F持ち順位

    f_count が0なら F持ち順位は無視する。
    """
    vals = [
        d.get("local_rank"),
        d.get("firstday_rank"),
    ]

    if d.get("night_rank") is not None:
        vals.append(d.get("night_rank"))

    if d.get("f_count", 0) >= 1 and d.get("f_rank") is not None:
        vals.append(d.get("f_rank"))

    vals = [v for v in vals if v is not None]

    return sum(vals) / len(vals) if vals else None


def race_day_number(data):
    """
    merge済みdataから開催何日目を取得。
    official側の day / race_day / day_no があれば使用。
    無ければNone（安全側で今節平均STは使わない）。
    """
    for b in range(1, 7):
        d = data.get(b, {})
        for key in ("day_no", "race_day", "day"):
            v = d.get(key)
            if v is None:
                continue
            m = re.search(r"\d+", str(v))
            if m:
                return int(m.group())
    return None


def evaluate_boat(boat, data, venue):
    """
    「固定点数だけ」でチャンス判定しない。

    まず自艇が左隣艇を叩ける根拠を数える。
    必須根拠:
      A. 展示タイムが左より良い
      B. ST順位が左より良い
      C. スタート展示で左より本番再現性が高い
      D. 3日目以降のみ、今節平均STが左より良い

    最低2個の根拠がない艇は、点数が高くても候補にしない。
    scoreは候補同士の強弱を付ける補助値としてだけ使う。
    """
    left = boat - 1
    me = data.get(boat, {})
    le = data.get(left, {})

    score = 0.0
    reasons = []
    attack_evidence = []
    caution = []

    day_no = race_day_number(data)
    use_series_st = day_no is not None and day_no >= 3

    # --- ST順位 ---
    mr = avg_rank(me)
    lr = avg_rank(le)

    rank_edge = None
    if mr is not None and lr is not None:
        rank_edge = lr - mr  # +なら自艇が良い
        if rank_edge >= 0.70:
            score += 2.0
            attack_evidence.append("ST順位優勢")
            reasons.append(f"ST順位 左より{rank_edge:.2f}上")
        elif rank_edge >= 0.30:
            score += 1.0
            attack_evidence.append("ST順位優勢")
            reasons.append(f"ST順位 左より{rank_edge:.2f}上")
        elif rank_edge <= -0.70:
            score -= 1.5
            caution.append("ST順位劣勢")
            reasons.append(f"ST順位 左より{-rank_edge:.2f}下")

    # --- 展示タイム ---
    mt = me.get("ex_time")
    lt = le.get("ex_time")
    time_edge = None
    if mt is not None and lt is not None:
        time_edge = lt - mt  # +なら自艇が速い
        if time_edge >= EX_TIME_STRONG:
            score += 2.5
            attack_evidence.append("展示タイム優勢")
            reasons.append(f"展示 左より{time_edge:.02f}速い")
        elif time_edge >= EX_TIME_GOOD:
            score += 1.5
            attack_evidence.append("展示タイム優勢")
            reasons.append(f"展示 左より{time_edge:.02f}速い")
        elif time_edge <= -EX_TIME_STRONG:
            score -= 1.2
            caution.append("展示タイム劣勢")
            reasons.append(f"展示 左より{-time_edge:.02f}遅い")

    # --- スタート展示 ---
    exst = me.get("ex_st")
    lexst = le.get("ex_st")

    # Fは数値どおりに比較しない。
    # 左F・自艇非Fなら「左の展示STは本番再現性を割り引く」。
    if lexst is not None and lexst < 0:
        reasons.append(f"左艇 展示F{abs(lexst):.02f} → 本番再現性割引")

        if exst is not None and exst >= 0:
            attack_evidence.append("スタート展示再現性優勢")
            score += 1.5
            reasons.append("自艇は非F → 左より本番ST再現性あり")

    elif (
        exst is not None and lexst is not None
        and exst >= 0 and lexst >= 0
    ):
        # 非F同士なら0.03以上先行をスタート展示根拠とする。
        ex_edge = lexst - exst
        if ex_edge >= 0.03:
            attack_evidence.append("スタート展示優勢")
            score += 1.5
            reasons.append(f"展示ST 左より{ex_edge:.02f}先行")
        elif ex_edge <= -0.05:
            caution.append("スタート展示劣勢")
            score -= 0.8
            reasons.append(f"展示ST 左より{-ex_edge:.02f}遅れ")

    # 自艇が展示Fの場合は、平常のST順位から再現性を判定。
    if exst is not None and exst < 0:
        fdepth = abs(exst)
        naturally_fast = mr is not None and mr <= 2.8

        if naturally_fast and fdepth <= ALLOW_EX_F:
            score += 0.2
            reasons.append(
                f"自艇 展示F{fdepth:.02f}だがST順位良好 → 頭候補は維持"
            )
        else:
            score -= 1.2
            caution.append("自艇展示F再現性注意")
            reasons.append(f"自艇 展示F{fdepth:.02f} → 本番再現性注意")

    # 展示STで遅れてもST順位上位なら、本番修正候補。
    if exst is not None and exst >= 0.15 and mr is not None and mr <= 2.8:
        score += 0.5
        reasons.append("展示ST遅れだがST順位上位 → 本番修正候補")

    # --- 今節平均ST：3日目以降のみ ---
    me_series = me.get("series_st") if use_series_st else None
    le_series = le.get("series_st") if use_series_st else None

    if me_series is not None and le_series is not None:
        st_edge = le_series - me_series

        if st_edge >= ST_EDGE_STRONG:
            score += 2.0
            attack_evidence.append("今節ST優勢")
            reasons.append(f"今節ST 左より{st_edge:.02f}速い")
        elif st_edge >= ST_EDGE_GOOD:
            score += 1.0
            attack_evidence.append("今節ST優勢")
            reasons.append(f"今節ST 左より{st_edge:.02f}速い")
        elif st_edge <= -ST_EDGE_STRONG:
            score -= 1.5
            caution.append("今節ST劣勢")
            reasons.append(f"今節ST 左より{-st_edge:.02f}遅い")

    # --- F持ち艇だけF持ち順位を加味 ---
    if me.get("f_count", 0) >= 1:
        fr = me.get("f_rank")
        score -= 0.5
        reasons.append(f"F持ち{me.get('f_count', 0)}")

        if fr is not None:
            if fr <= 2.5:
                score += 1.0
                reasons.append(f"F持ち順位{fr:.2f}良好")
            elif fr >= 4.0:
                score -= 0.8
                caution.append("F持ち順位遅め")
                reasons.append(f"F持ち順位{fr:.2f}遅め")

    # 同じ種類は1根拠として数える。
    attack_evidence = list(dict.fromkeys(attack_evidence))
    caution = list(dict.fromkeys(caution))

    # 強い捲り形:
    # 左が展示F + 自艇非F + 展示タイム優勢 + ST順位優勢
    strong_pattern = (
        lexst is not None and lexst < 0
        and exst is not None and exst >= 0
        and "展示タイム優勢" in attack_evidence
        and "ST順位優勢" in attack_evidence
    )

    if strong_pattern:
        score += 1.5
        reasons.append("左展示F＋展示タイム＋ST順位優勢 → 捲り形強い")

    return {
        "score": score,
        "reasons": reasons,
        "attack_evidence": attack_evidence,
        "attack_hits": len(attack_evidence),
        "caution": caution,
        "strong_pattern": strong_pattern,
        "day_no": day_no,
    }

def merge_data(*sources):
    out = {i: {} for i in range(1, 7)}
    for src in sources:
        for b, vals in (src or {}).items():
            out.setdefault(int(b), {}).update(vals or {})
    return out


def deadline(text):
    m = re.search(
        r"締切\s*[：:]?\s*([01]?\d|2[0-3]):([0-5]\d)",
        text
    )
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""

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
            except Exception:
                pass

            try:
                txt += "\n" + a.locator(
                    "xpath=ancestor::*[self::div or self::td or self::li or self::section][1]"
                ).inner_text(timeout=250)
            except Exception:
                pass

            if (
                any(v in txt for v in TARGET_VENUES)
                or re.search(r"([1-9]|1[0-2])\s*R", txt)
                or "race" in full.lower()
                or "detail" in full.lower()
                or "sum" in full.lower()
            ):
                out.append(full)

        except Exception:
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

def parse_base_1st_rates(text):
    """
    ページ上部の「選手名・1着率」欄から、明示された元1着率だけを取得する。

    V18の重要修正:
    ページ上部には「←シンsum理論に戻る」があるため、
    ページ先頭から最初の「シンsum理論」を終了位置にすると
    選手一覧より前で切れてしまう。

    そこで、
      1. 締切表示より後にある最初の「選手名」を開始点
      2. その開始点より後にある
         「危険艇 / 戦法別上昇率 / スリット隊形 / シンsum理論」
         の最初を終了点
    として、選手一覧だけを切り出す。

    切り出した範囲の符号なし%を上から順に1〜6号艇へ割り当てる。
    「危険艇」以降の12/13/14/15/16の組み合わせ確率は含めない。
    """

    # 締切表示より後から選手一覧を探す。
    deadline_pos = -1
    m_deadline = re.search(
        r"締切\s*[：:]?\s*(?:[01]?\d|2[0-3]):[0-5]\d",
        text
    )
    if m_deadline:
        deadline_pos = m_deadline.end()

    # 「選手名・1着率」でも「選手名」単独でも対応。
    search_from = max(0, deadline_pos)
    m_header = re.search(
        r"選手名(?:\s*[・･]\s*1着率)?",
        text[search_from:]
    )

    if not m_header:
        print(
            "元1着率: 選手一覧開始位置を検出できず",
            flush=True
        )
        return {}

    section_start = search_from + m_header.start()

    # 必ず section_start より後だけを検索する。
    tail = text[section_start:]
    end_candidates = []

    for pattern in (
        r"危険艇",
        r"戦法別上昇率",
        r"スリット隊形",
        r"シン\s*sum理論",
    ):
        m = re.search(pattern, tail[1:])
        if m:
            end_candidates.append(
                section_start + 1 + m.start()
            )

    section_end = (
        min(end_candidates)
        if end_candidates
        else min(len(text), section_start + 7000)
    )

    section = text[section_start:section_end]

    # 元1着率は符号なしの%。
    # +11% / -8% のような上昇率・補正値は除外。
    values = [
        float(x)
        for x in re.findall(
            r"(?<![+\-\d.])(\d+(?:\.\d+)?)\s*%",
            section
        )
    ][:6]

    result = {
        boat: values[boat - 1]
        for boat in range(1, len(values) + 1)
    }

    print(
        f"元1着率候補(V18): {values} -> {result}",
        flush=True
    )

    # 取得失敗時は切り出した本文も一部出して原因確認できるようにする。
    if not result:
        print(
            "元1着率解析section:",
            repr(section[:1200]),
            flush=True
        )

    return result

def extract_theory_section(text):
    """
    実際のシンsum理論表だけを切り出す。

    ページ上部の「←シンsum理論に戻る」を誤認しないため、
    text内のすべての「シンsum理論」候補を調べ、
    その後に4桁登録番号が最も多く並ぶ候補を本表として採用する。
    """
    starts = [m.start() for m in re.finditer(r"シン\s*sum理論", text)]

    best_section = ""
    best_score = -1

    for start in starts:
        end = text.find("シンsumチェッカー", start)
        section = text[start:(end if end > start else min(len(text), start + 10000))]

        regs = re.findall(r"(?<!\d)([3-5]\d{3})(?!\d)", section)
        regs_unique = list(dict.fromkeys(regs))

        diff_like = re.findall(
            r"(?<![\d.])([+-]\d+(?:\.\d+)?)(?!\s*%)",
            section
        )

        score = len(regs_unique) * 100 + len(diff_like)

        if score > best_score:
            best_score = score
            best_section = section

    return best_section

def parse_theory_adjustments(text):
    """
    シンsum理論表の「1着」補正を6艇全部取得。

    各4桁登録番号を起点に、その艇ブロック内で最初に出る
    %付き数値を1着補正として読む。
    """
    section = extract_theory_section(text)
    if not section:
        return {}

    regs = re.findall(r"(?<!\d)([3-5]\d{3})(?!\d)", section)
    regs = list(dict.fromkeys(regs))

    if len(regs) < 6:
        return {}

    regs = regs[:6]
    result = {}

    for boat, reg in enumerate(regs, start=1):
        mreg = re.search(
            rf"(?<!\d){re.escape(reg)}(?!\d)",
            section
        )
        if not mreg:
            continue

        next_pos = len(section)
        if boat < 6:
            next_reg = regs[boat]
            mn = re.search(
                rf"(?<!\d){re.escape(next_reg)}(?!\d)",
                section[mreg.end():]
            )
            if mn:
                next_pos = mreg.end() + mn.start()

        block = section[mreg.end():next_pos]

        pcts = re.findall(
            r"([+-]?\d+(?:\.\d+)?)\s*%",
            block
        )

        if pcts:
            result[boat] = float(pcts[0])

    return result

def parse_current_diffs(text):
    """
    シンsum理論表の「平均との差」を6艇分取得する。

    重要:
    艇番の数字(1,2,3...)やヘッダーの「1着/2着/3着」を起点にしない。
    各艇の4桁登録番号を起点に、その直後に出る
    「符号付き・%なし」の数値を平均との差として読む。

    例:
      4836 -> +0.87
      3807 -> +0.03
      4419 -> +0.18
    """
    section = extract_theory_section(text)
    if not section:
        return {}

    # 登録番号を出現順に拾う。シンsum理論では上から1〜6号艇。
    regs = re.findall(r"(?<!\d)([3-5]\d{3})(?!\d)", section)

    regs = list(dict.fromkeys(regs))

    if len(regs) < 6:
        return {}

    regs = regs[:6]
    result = {}

    for boat, reg in enumerate(regs, start=1):
        mreg = re.search(
            rf"(?<!\d){re.escape(reg)}(?!\d)",
            section
        )
        if not mreg:
            continue

        # 次の登録番号までをその艇の範囲にする。
        next_pos = len(section)
        if boat < 6:
            next_reg = regs[boat]
            mn = re.search(
                rf"(?<!\d){re.escape(next_reg)}(?!\d)",
                section[mreg.end():]
            )
            if mn:
                next_pos = mreg.end() + mn.start()

        block = section[mreg.end():next_pos]

        # +0.87 / -0.02 のような符号付き数値。
        # %付きの理論補正は除外する。
        candidates = re.findall(
            r"(?<![\d.])([+-]\d+(?:\.\d+)?)(?!\s*%)",
            block
        )

        if not candidates:
            continue

        # 最初に出る値が平均との差。
        value = float(candidates[0])

        # 誤取得防止
        if -5.0 <= value <= 5.0:
            result[boat] = value

    return result

def parse_slit_alarms(text):
    """
    シンsum理論表の「スリットアラーム」を6艇分取得する。

    スリットアラームは左隣艇との展示タイム差をもとに表示される値。
    例: 3号艇 6.98 / 4号艇 6.77 -> 4号艇 +0.2

    理論表では各艇ブロック内に
      1) 平均との差 (+1.60 / -0.22 など)
      2) スリットアラーム (+0.2 / -0.1 など。表示がある艇のみ)
    の順で符号付き・%なし数値が出るため、2個目を採用する。
    表示がない艇は 0.0 とする。
    """
    section = extract_theory_section(text)
    if not section:
        return {}

    regs = re.findall(r"(?<!\d)([3-5]\d{3})(?!\d)", section)
    regs = list(dict.fromkeys(regs))
    if len(regs) < 6:
        return {}
    regs = regs[:6]

    result = {}
    for boat, reg in enumerate(regs, start=1):
        mreg = re.search(
            rf"(?<!\d){re.escape(reg)}(?!\d)",
            section
        )
        if not mreg:
            result[boat] = 0.0
            continue

        next_pos = len(section)
        if boat < 6:
            next_reg = regs[boat]
            mn = re.search(
                rf"(?<!\\d){re.escape(next_reg)}(?!\\d)",
                section[mreg.end():]
            )
            if mn:
                next_pos = mreg.end() + mn.start()

        block = section[mreg.end():next_pos]

        # %値（理論補正）を絶対に拾わないよう、
        # inner_text上で「その行全体が符号付き数値」のものだけを候補にする。
        # 例: -0.22 / +0.2 は対象、+1% / -3% は対象外。
        candidates = []
        for line in block.splitlines():
            t = line.strip()
            if re.fullmatch(r"[+-]\d+(?:\.\d+)?", t):
                candidates.append(t)

        # 1個目=平均との差、2個目=スリットアラーム。
        value = 0.0
        if len(candidates) >= 2:
            try:
                value = float(candidates[1])
            except Exception:
                value = 0.0

        # 表示上あり得る範囲だけ採用。異常値は0扱いにして誤判定防止。
        if -2.0 <= value <= 2.0:
            result[boat] = value
        else:
            result[boat] = 0.0

    return result

def build_adjusted_diffs(current_diffs, slit_alarms):
    """
    チェッカーのゾーン判定に使う平均との差を、
    「元の平均との差 + スリットアラーム」で補正する。

    例: 4号艇 平均との差 -0.22 / スリット +0.2 -> 補正後 -0.02
    """
    adjusted = {}
    for boat in range(1, 7):
        if boat not in current_diffs:
            continue
        raw = float(current_diffs[boat])
        slit = float(slit_alarms.get(boat, 0.0) or 0.0)
        adjusted[boat] = raw + slit
    return adjusted

def parse_registration_numbers(text):
    """
    シンsum理論欄の4桁登録番号を上から6個取得し、
    1〜6号艇に割り当てる。

    艇番の単独行を探さないため、
    ヘッダー数字を誤認しない。
    """
    section = extract_theory_section(text)
    if not section:
        return {}

    regs = re.findall(
        r"(?<!\d)([3-5]\d{3})(?!\d)",
        section
    )

    # 出現順を維持したまま重複除外
    regs = list(dict.fromkeys(regs))

    if len(regs) < 6:
        print(
            f"登録番号候補不足: {regs}",
            flush=True
        )
        return {}

    regs = regs[:6]

    return {
        boat: regs[boat - 1]
        for boat in range(1, 7)
    }

def parse_single_checker_1st(text, boat, current_diff):
    """
    現在表示中のシンsumチェッカーから、指定艇の該当ゾーン1着率を取得する。
    """
    start = text.find("シンsumチェッカー")
    if start < 0:
        return None

    section = text[start:]
    compact = re.sub(r"\s+", "", section)

    token = f"{boat}号艇"
    pos = compact.find(token)
    if pos < 0:
        return None

    # 選択中のカードだけ取れればよいので、次の艇カードか十分な長さまで
    next_positions = []
    for other in range(1, 7):
        if other == boat:
            continue
        p = compact.find(f"{other}号艇", pos + len(token))
        if p >= 0:
            next_positions.append(p)

    end = min(next_positions) if next_positions else min(len(compact), pos + 5000)
    card = compact[pos:end]

    zone = checker_zone(current_diff)
    zpos = -1
    matched = None

    for variant in _zone_variants(zone):
        zpos = card.find(variant)
        if zpos >= 0:
            matched = variant
            break

    if zpos < 0:
        return None

    row = card[zpos:zpos + 420]
    pcts = re.findall(r"([+-]?\d+(?:\.\d+)?)%", row)

    if not pcts:
        return None

    return {
        "zone": zone,
        "matched_zone_text": matched,
        "checker_1st": float(pcts[0]),
        # 該当ゾーン行は 1着率 / 2着率 / 3着率 / 3連対率 の順。
        # 2着率が無い場合は0.0として扱う。
        "checker_2nd": float(pcts[1]) if len(pcts) >= 2 else 0.0,
    }

def collect_checker_1st(page, current_diffs, registrations):
    """
    1〜6号艇の登録番号を順番にクリックしてチェッカーを取得する。

    V9:
    - クリック後450ms固定待ちを廃止
    - 最大4秒、250ms間隔で表示完了を待つ
    - 1回で出なければ最大3回まで再クリック
    - 「データが見つかりません」は正式なデータなしとして区別
    """
    result = {}

    for boat in CHECKER_PARSE_BOATS:
        diff = current_diffs.get(boat)
        reg = registrations.get(boat)

        if diff is None or not reg:
            print(
                f"チェッカー前提データ不足: {boat}号艇 / "
                f"差={diff} / 登録番号={reg}",
                flush=True
            )
            continue

        got = None
        no_data = False

        for attempt in range(1, 4):
            try:
                loc = page.locator("a").filter(
                    has_text=re.compile(
                        rf"^\s*{re.escape(reg)}\s*$"
                    )
                )

                if loc.count() == 0:
                    loc = page.get_by_text(reg, exact=True)

                if loc.count() == 0:
                    print(
                        f"登録番号リンク未検出: "
                        f"{boat}号艇 / {reg}",
                        flush=True
                    )
                    break

                try:
                    loc.first.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass

                loc.first.click(
                    timeout=4000,
                    force=(attempt >= 2)
                )

                # 固定450msではなく、チェッカー表示を最大4秒待つ
                for _ in range(16):
                    page.wait_for_timeout(250)

                    body = page.locator("body").inner_text(
                        timeout=10000
                    )

                    # サイト側が明示的に「データなし」と返した場合
                    if (
                        f"{boat}号艇のデータが見つかりません" in body
                        or "データが見つかりません" in body[
                            max(0, body.find("シンsumチェッカー")):
                        ]
                    ):
                        no_data = True
                        break

                    info = parse_single_checker_1st(
                        body,
                        boat,
                        diff
                    )

                    if info:
                        got = info
                        break

                if got or no_data:
                    break

                print(
                    f"チェッカー表示待ち再試行: "
                    f"{boat}号艇 / {reg} / "
                    f"{attempt}回目",
                    flush=True
                )

            except Exception as e:
                print(
                    f"チェッカークリック再試行: "
                    f"{boat}号艇 / {reg} / "
                    f"{attempt}回目 / {repr(e)}",
                    flush=True
                )

        if got:
            result[boat] = got
            print(
                f"チェッカー取得成功: {boat}号艇 / "
                f"{reg} / 差{diff:+.2f} / "
                f"{got['zone']} / "
                f"1着{got['checker_1st']:+.1f}% / 2着{got.get('checker_2nd', 0.0):+.1f}%",
                flush=True
            )
        elif no_data:
            print(
                f"チェッカーデータなし: "
                f"{boat}号艇 / {reg} / 差{diff:+.2f}",
                flush=True
            )
        else:
            print(
                f"チェッカー取得失敗: "
                f"{boat}号艇 / {reg} / 差{diff:+.2f} / "
                f"3回再試行しても表示確認できず",
                flush=True
            )

    return result

def checker_zone(current_diff):
    if current_diff >= 0.5:
        return "+0.5以上"
    if current_diff >= 0:
        return "0〜+0.5"
    if current_diff >= -0.5:
        return "-0.5〜0"
    return "-0.5未満"

def _zone_variants(zone):
    variants = {
        "+0.5以上": (
            "+0.5以上",
            "0.5以上",
        ),
        "0〜+0.5": (
            "0〜+0.5",
            "0～+0.5",
            "0~+0.5",
            "0～0.5",
            "0〜0.5",
            "0~0.5",
        ),
        "-0.5〜0": (
            "-0.5〜0",
            "-0.5～0",
            "-0.5~0",
        ),
        "-0.5未満": (
            "-0.5未満",
        ),
    }
    return variants.get(zone, (zone,))



def chance_candidates(venue, race, data):
    """
    候補条件:
      - 左艇を叩ける根拠が最低2個
      - scoreは順位付け用。固定5点を超えたかどうかだけでは決めない。

    これにより「点数だけ高いが、実際に左を叩ける根拠が薄い艇」を除外する。
    """
    arr = []

    for b in range(2, 7):
        ev = evaluate_boat(b, data, venue)

        print(
            f"[展開判定] {venue}{race}R {b}号艇 "
            f"根拠={ev['attack_hits']}個 "
            f"補助点={ev['score']:.1f} "
            f"根拠内容={','.join(ev['attack_evidence']) or '-'} "
            f"注意={','.join(ev['caution']) or '-'}",
            flush=True,
        )

        # ★ここが新しい必須条件
        if ev["attack_hits"] < 2:
            print(
                f"[除外] {venue}{race}R {b}号艇 "
                "左艇を叩ける根拠が2個未満",
                flush=True,
            )
            continue

        row = {
            "boat": b,
            "score": ev["score"],
            "reasons": ev["reasons"],
            "attack_evidence": ev["attack_evidence"],
            "attack_hits": ev["attack_hits"],
            "strong_pattern": ev["strong_pattern"],
            "caution": ev["caution"],
        }
        arr.append(row)

    arr.sort(
        key=lambda x: (
            1 if x["strong_pattern"] else 0,
            x["attack_hits"],
            x["score"],
        ),
        reverse=True,
    )

    return arr

def shinsum_confirmation(page, venue, race, boats):
    text = page.locator("body").inner_text(timeout=10000)
    if actual_venue(text) != venue or actual_race(text) != f"{race}R":
        return {}

    base = parse_base_1st_rates(text)
    theory = parse_theory_adjustments(text)
    raw_diffs = parse_current_diffs(text)
    slit = parse_slit_alarms(text)
    adjusted = build_adjusted_diffs(raw_diffs, slit)
    regs = parse_registration_numbers(text)

    if len(theory) < 6 or len(adjusted) < 6 or len(regs) < 6:
        print(
            f"[SHINSUM] 前提不足 {venue}{race}R "
            f"theory={len(theory)} diff={len(adjusted)} reg={len(regs)}",
            flush=True,
        )
        return {}

    checker = collect_checker_1st(page, adjusted, regs)
    out = {}
    for b in boats:
        th = theory.get(b)
        ck = checker.get(b, {}).get("checker_1st")
        br = base.get(b)

        if th is None or ck is None:
            out[b] = {
                "theory": th,
                "checker": ck,
                "base": br,
                "final": None,
                "total": None,
            }
            continue

        total = float(th) + float(ck)
        final = float(br) + total if br is not None else None
        out[b] = {
            "theory": float(th),
            "checker": float(ck),
            "base": br,
            "final": final,
            "total": total,
        }
    return out

def data_ready(data, venue):
    """
    必須データ:
      1) 展示タイム
      2) スタート展示
      3) ボートレース日和ST順位（当地 + 初日）
      4) ナイター場ならナイターST順位
      5) F持ち艇がいる場合、その艇のF持ち順位

    最初にF持ちかどうかを確認し、
    F持ち艇だけF持ち順位を必須にする。
    """
    ex_time = sum(data[b].get("ex_time") is not None for b in range(1, 7))
    ex_st = sum(data[b].get("ex_st") is not None for b in range(1, 7))
    series_st = sum(data[b].get("series_st") is not None for b in range(1, 7))
    local = sum(data[b].get("local_rank") is not None for b in range(1, 7))
    firstday = sum(data[b].get("firstday_rank") is not None for b in range(1, 7))
    night = sum(data[b].get("night_rank") is not None for b in range(1, 7))

    f_boats = [
        b for b in range(1, 7)
        if data[b].get("f_count", 0) >= 1
    ]

    missing_f_rank = [
        b for b in f_boats
        if data[b].get("f_rank") is None
    ]

    print(
        "[F-CHECK] F持ち=" +
        ("・".join(f"{b}号艇" for b in f_boats) if f_boats else "なし"),
        flush=True,
    )

    if f_boats:
        print(
            "[F-RANK] " +
            " / ".join(
                f"{b}号艇={data[b].get('f_rank', '-')}"
                for b in f_boats
            ),
            flush=True,
        )

    if venue in NIGHT_VENUES:
        print(
            f"[DATA] 展示={ex_time}/6 展示ST={ex_st}/6 今節ST={series_st}/6 "
            f"当地ST順位={local}/6 初日ST順位={firstday}/6 "
            f"ナイターST順位={night}/6 "
            f"F持順位不足={missing_f_rank}",
            flush=True,
        )
        rank_ready = local >= 5 and firstday >= 5 and night >= 5
    else:
        print(
            f"[DATA] 展示={ex_time}/6 展示ST={ex_st}/6 今節ST={series_st}/6 "
            f"当地ST順位={local}/6 初日ST順位={firstday}/6 "
            f"F持順位不足={missing_f_rank}",
            flush=True,
        )
        rank_ready = local >= 5 and firstday >= 5

    day_no = race_day_number(data)

    # 初日・2日目は今節平均STを完全に無視。
    # 開催日が取れない場合も、安全側で今節STを必須にしない。
    series_ready = True if day_no is None or day_no <= 2 else series_st >= 5

    print(
        f"[DAY] 開催日={day_no if day_no is not None else '不明'} "
        f"/ 今節ST使用={'する' if day_no is not None and day_no >= 3 else 'しない'}",
        flush=True,
    )

    return (
        ex_time >= 5
        and ex_st >= 5
        and series_ready
        and rank_ready
        and not missing_f_rank
    )

def final_chance_type(candidate, shin_row):
    """
    最終通知判定。

    前提:
      左艇を叩ける根拠2個以上は既に満たしている。

    1着チャンス:
      シンsum理論 + チェッカーの1着補正がプラス。

    展開チャンス:
      シンsumがプラスでなくても、
      ・強い捲り形
      または
      ・左艇を叩ける根拠3個以上
      のときだけ残す。

    単にscoreが高いだけでは通知しない。
    """
    hits = candidate.get("attack_hits", 0)
    strong = candidate.get("strong_pattern", False)

    if shin_row:
        total = shin_row.get("total")
        if total is not None and total > 0:
            return "1着チャンス"

    if strong or hits >= 3:
        return "展開チャンス"

    return None

def notify_chance(venue, race, selected, data, shin):
    """
    selected:
      [{"boat":4, "score":..., "reasons":[...], "kind":"1着チャンス"}, ...]
    """
    best = selected[0]
    display_title = f"🚤 {best['boat']}号艇 {best['kind']}"
    title = "ANA Chance Bot"

    lines = [
        display_title,
        f"{venue} {race}R",
        "",
        f"注目: {best['boat']}号艇 / {best['kind']}",
        f"左艇を叩ける根拠: {best.get('attack_hits', 0)}個",
        "【展開判断】",
        " / ".join(best["reasons"][:7]) or "総合評価",
    ]

    if len(selected) > 1:
        lines.append(
            "他候補: "
            + "・".join(
                f"{x['boat']}号艇 {x['kind']}"
                for x in selected[1:3]
            )
        )

    lines += ["", "【展示→スタート展示→ST順位】"]

    for b in range(1, 7):
        d = data.get(b, {})
        exst = d.get("ex_st")
        ext = d.get("ex_time")
        local = d.get("local_rank")
        firstday = d.get("firstday_rank")
        night = d.get("night_rank")
        f_count = d.get("f_count", 0)
        f_rank = d.get("f_rank")
        series_st = d.get("series_st")

        if exst is None:
            exs = "-"
        elif exst < 0:
            exs = f"F{abs(exst):.02f}"
        else:
            exs = f"{exst:.02f}"

        ext_text = f"{ext:.2f}" if ext is not None else "-"
        series_text = f"{series_st:.2f}" if series_st is not None else "-"
        local_text = f"{local:.2f}" if local is not None else "-"
        first_text = f"{firstday:.2f}" if firstday is not None else "-"

        if venue in NIGHT_VENUES:
            night_text = f"{night:.2f}" if night is not None else "-"
            f_text = (
                f" / F持ち={f_count} / F持順位={f_rank:.2f}"
                if f_count >= 1 and f_rank is not None
                else (f" / F持ち={f_count}" if f_count >= 1 else "")
            )
            lines.append(
                f"{b}号艇 展示={ext_text} / 展示ST={exs} / 今節ST={series_text} / "
                f"当地={local_text} / 初日={first_text} / ナイター={night_text}" + f_text
            )
        else:
            f_text = (
                f" / F持ち={f_count} / F持順位={f_rank:.2f}"
                if f_count >= 1 and f_rank is not None
                else (f" / F持ち={f_count}" if f_count >= 1 else "")
            )
            lines.append(
                f"{b}号艇 展示={ext_text} / 展示ST={exs} / 今節ST={series_text} / "
                f"当地={local_text} / 初日={first_text}" + f_text
            )

    lines += ["", "【最終確認：シンsum1着率】"]

    for x in selected[:3]:
        b = x["boat"]
        s = shin.get(b)

        if not s:
            lines.append(f"{b}号艇 シンsum確認データなし")
            continue

        th = s.get("theory")
        ck = s.get("checker")

        if th is None or ck is None:
            lines.append(f"{b}号艇 理論/チェッカー一部取得なし")
            continue

        if s.get("final") is not None:
            lines.append(
                f"{b}号艇 元{s['base']:.1f}% + 理論{th:+.1f}% "
                f"+ チェッカー{ck:+.1f}% = 最終{s['final']:.1f}% "
                f"(補正{s['total']:+.1f}pt)"
            )
        else:
            lines.append(
                f"{b}号艇 理論{th:+.1f}% + チェッカー{ck:+.1f}% "
                f"= 補正{s['total']:+.1f}pt"
            )

    body = "\n".join(lines)

    r = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": "high",
            "Tags": "boat",
        },
        timeout=12,
    )
    r.raise_for_status()
    print(f"[NOTIFY] {display_title} / {venue}{race}R", flush=True)

def analyze_current_page(page, venue, race):
    """
    前半判定だけ行う。
    ① 展示タイム
    ② スタート展示
    ③ ボートレース日和ST順位・今節ST・F持ち順位
    ④ 左隣比較から「捲れる/展開を作れる」候補抽出

    シンsum最終確認はここでは行わず、別フェーズに分離する。
    """
    ex = official_exhibition(venue, race)
    official = official_avg_st_f(venue, race)
    hiyori = hiyori_st_data(page, venue, race)
    data = merge_data(official, hiyori, ex)

    # 現在のF有無は公式BOATRACEを唯一の正とする。
    # 日和の「F持順位」は順位データとしてだけ利用する。
    for b in range(1, 7):
        if b in official:
            data[b]["f_count"] = int(official[b].get("f_count", 0))
        else:
            data[b]["f_count"] = 0

    if not data_ready(data, venue):
        return {"status": "not_ready"}

    candidates = chance_candidates(venue, race, data)

    if not candidates:
        print(f"[NO-CHANCE] {venue}{race}R 展開候補なし", flush=True)
        return {"status": "done_no_chance"}

    print(
        f"[PRESCREEN] {venue}{race}R 候補="
        + "・".join(f"{x['boat']}号艇({x['score']:.1f})" for x in candidates),
        flush=True,
    )

    return {
        "status": "candidate",
        "candidates": candidates,
        "data": data,
    }


def shinsum_is_ready(shin, candidates):
    """
    候補艇のうち少なくとも1艇で
    シンsum理論 + シンsumチェッカーの両方が取れれば最終審査可能。
    """
    for c in candidates:
        row = shin.get(c["boat"])
        if not row:
            continue
        if row.get("theory") is not None and row.get("checker") is not None:
            return True
    return False


def finalize_with_shinsum(page, venue, race, candidates, data):
    """
    シンsum理論 + シンsumチェッカーを取得し、
    反映済みなら最終判定・通知する。

    戻り値:
      "not_ready" : シンsum未反映
      "done"      : 最終判定完了（通知あり/なし）
    """
    boats = [x["boat"] for x in candidates]
    shin = shinsum_confirmation(page, venue, race, boats)

    if not shinsum_is_ready(shin, candidates):
        print(f"[SHINSUM-WAIT] {venue}{race}R 理論/チェッカー未反映", flush=True)
        return "not_ready"

    selected = []

    for c in candidates:
        srow = shin.get(c["boat"])
        kind = final_chance_type(c, srow)

        total = None
        if srow:
            total = srow.get("total")

        print(
            f"[FINAL] {venue}{race}R {c['boat']}号艇 "
            f"展開score={c['score']:.1f} / "
            f"シンsum補正={total if total is not None else '-'} / "
            f"判定={kind or '通知なし'}",
            flush=True,
        )

        if kind:
            row = dict(c)
            row["kind"] = kind
            selected.append(row)

    if not selected:
        print(f"[NO-NOTIFY] {venue}{race}R 最終条件届かず", flush=True)
        return "done"

    selected.sort(
        key=lambda x: (
            1 if x["kind"] == "1着チャンス" else 0,
            x["score"],
        ),
        reverse=True,
    )

    notify_chance(venue, race, selected, data, shin)
    return "done"

def remain_minutes_from_text(text):
    d = deadline(text)
    if not d:
        return None

    hh, mm = map(int, d.split(":"))
    t = now().replace(hour=hh, minute=mm, second=0, microsecond=0)
    return (t - now()).total_seconds() / 60.0

def cycle(page):
    """
    前半:
      締切15分前 → データ不足なら13分前 → 11分前
      展示/ST/日和ST順位/F持ち順位で候補を確定

    後半:
      候補が出たレースだけシンsumを確認
      未反映なら 9分前 → 7分前 → 5分前 に再取得

    終了済みレースはログにも出さず完全除外。
    """
    links = candidate_links(page)
    print(f"[CYCLE] {now():%H:%M:%S} 詳細候補リンク={len(links)}", flush=True)

    today = now().strftime("%Y%m%d")

    for link in links:
        try:
            page.goto(link, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(350)

            body = page.locator("body").inner_text(timeout=10000)
            venue = actual_venue(body)
            rr = actual_race(body)

            if not venue or not rr:
                continue

            race = int(rr[:-1])
            remain = remain_minutes_from_text(body)

            if remain is None:
                continue

            # 終了済みレースは完全除外。
            if remain <= 0:
                continue

            key = (today, venue, race)

            # ----------------------------------------------------------
            # A. シンsum待ちフェーズ（候補確定済み）
            # ----------------------------------------------------------
            if key in _pending_shinsum:
                ss_state = _shinsum_state.get(key, 0)

                if ss_state >= 3:
                    continue

                # シンsum再取得時刻: 9 → 7 → 5分前
                ss_target = {0: 9.0, 1: 7.0, 2: 5.0}[ss_state]

                # まだその時刻より早い
                if remain > ss_target:
                    continue

                # 5分前を大きく過ぎたら終了
                if remain < 3.0:
                    print(
                        f"[SHINSUM-GIVEUP] {venue}{race}R "
                        "5分前までに理論/チェッカー未反映",
                        flush=True,
                    )
                    _shinsum_state[key] = 3
                    _pending_shinsum.pop(key, None)
                    _attempt_state[key] = 9
                    continue

                pending = _pending_shinsum[key]
                ss_attempt = ss_state + 1

                print(
                    f"[SHINSUM-TRY] {venue}{race}R {ss_attempt}回目 "
                    f"/ 残り{remain:.1f}分 / 目標{int(ss_target)}分前",
                    flush=True,
                )

                result = finalize_with_shinsum(
                    page,
                    venue,
                    race,
                    pending["candidates"],
                    pending["data"],
                )

                if result == "done":
                    _shinsum_state[key] = 9
                    _pending_shinsum.pop(key, None)
                    _attempt_state[key] = 9
                else:
                    _shinsum_state[key] = ss_attempt
                    if ss_attempt < 3:
                        nxt = 7 if ss_attempt == 1 else 5
                        print(
                            f"[SHINSUM-RETRY] {venue}{race}R → {nxt}分前",
                            flush=True,
                        )
                    else:
                        print(
                            f"[SHINSUM-GIVEUP] {venue}{race}R "
                            "5分前でも未反映 → 今回は通知見送り",
                            flush=True,
                        )
                        _pending_shinsum.pop(key, None)
                        _attempt_state[key] = 9

                continue

            # ----------------------------------------------------------
            # B. 前半の展示/ST審査フェーズ
            # ----------------------------------------------------------
            state = _attempt_state.get(key, 0)

            if state == 9 or state >= 3:
                continue

            target = {0: 15.0, 1: 13.0, 2: 11.0}[state]

            # 15分前より早いレースはまだ何もしない
            if remain > target:
                continue

            # 前半判定の締切は11分前。そこを過ぎた新規レースは見送る。
            if remain < 9.0:
                _attempt_state[key] = 3
                continue

            print(
                f"[RACE] {venue}{race}R 残り{remain:.1f}分 state={state}",
                flush=True,
            )

            attempt = state + 1
            print(
                f"[TRY] {venue}{race}R {attempt}回目 / "
                f"目標{int(target)}分前",
                flush=True,
            )

            result = analyze_current_page(page, venue, race)
            status = result.get("status")

            if status == "not_ready":
                _attempt_state[key] = attempt

                if attempt < 3:
                    nxt = 13 if attempt == 1 else 11
                    print(
                        f"[RETRY] {venue}{race}R → {nxt}分前",
                        flush=True,
                    )
                else:
                    print(
                        f"[GIVEUP] {venue}{race}R "
                        "11分前でも展示/STデータ不足",
                        flush=True,
                    )

            elif status == "done_no_chance":
                _attempt_state[key] = 9

            elif status == "candidate":
                # 候補はここで保存。シンsumは「今すぐ1回」確認する。
                _pending_shinsum[key] = {
                    "candidates": result["candidates"],
                    "data": result["data"],
                }

                print(
                    f"[SHINSUM-CHECK] {venue}{race}R "
                    "候補確定 → シンsum理論/チェッカー確認",
                    flush=True,
                )

                ss_result = finalize_with_shinsum(
                    page,
                    venue,
                    race,
                    result["candidates"],
                    result["data"],
                )

                if ss_result == "done":
                    _attempt_state[key] = 9
                    _shinsum_state[key] = 9
                    _pending_shinsum.pop(key, None)
                else:
                    # 未反映なら前半審査は終了し、次は9分前まで待つ。
                    _attempt_state[key] = 3
                    _shinsum_state[key] = 0
                    print(
                        f"[SHINSUM-PENDING] {venue}{race}R "
                        "未反映 → 9→7→5分前に再確認",
                        flush=True,
                    )

        except Exception as e:
            print(f"[ERR] {link} / {e!r}", flush=True)

def main():
    print(
        "ANA チャンスBOT開始 / 全開催日 / "
        "前半15→13→11分 / シンsum未反映は9→7→5分で再確認",
        flush=True,
    )

    if not active():
        print("監視時間外 23:00〜08:00 JST", flush=True)
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            http_credentials={
                "username": SHINSUM_USER,
                "password": SHINSUM_PASSWORD,
            }
        )
        page = context.new_page()

        while active():
            cycle(page)
            print(f"{CHECK_INTERVAL}秒後に再チェック", flush=True)
            time.sleep(CHECK_INTERVAL)

        browser.close()

if __name__ == "__main__":
    main()
