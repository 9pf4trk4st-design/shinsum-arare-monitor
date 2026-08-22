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

def hiyori_st_data(venue, race):
    """
    ボートレース日和出走表から ST順位系を取得。
    必須: 当地ST順位 / 初日ST順位
    ナイター場のみ: ナイターST順位
    今節平均STはページにあれば取得、無ければ公式平均STを補助に使う。
    """
    place = TARGET_VENUES[venue]
    hd = now().strftime("%Y%m%d")
    url = f"https://kyoteibiyori.com/race_shusso.php?place_no={place}&race_no={race}&hiduke={hd}"
    try:
        r = session.get(url, timeout=12)
        r.raise_for_status()
        txt = clean_text(r.text)
    except Exception as e:
        print("日和ST取得失敗", venue, race, repr(e), flush=True)
        return {}

    out = {i: {} for i in range(1, 7)}
    labels = {
        "local_rank": ["当地ST順位", "当地 ST順位", "当地"],
        "firstday_rank": ["初日ST順位", "初日 ST順位", "初日"],
    }
    if venue in NIGHT_VENUES:
        labels["night_rank"] = ["ナイターST順位", "ナイター ST順位", "ナイター"]

    for b in range(1, 7):
        for key, variants in labels.items():
            for lab in variants:
                v = _rank_after_label(txt, lab, b)
                if v is not None:
                    out[b][key] = v
                    break

    # 今節平均ST: ラベル周辺で艇別 .xx を拾う。取れない時は None。
    pos = txt.find("今節平均ST")
    if pos >= 0:
        block = txt[pos:pos+2200]
        for b in range(1, 7):
            m = re.search(rf"{b}\s*号艇.{{0,120}}?(0\.\d{{2}})", block, re.S)
            if m:
                out[b]["series_st"] = float(m.group(1))

    return out

def avg_rank(d):
    vals = [d.get("local_rank"), d.get("firstday_rank")]
    if d.get("night_rank") is not None:
        vals.append(d["night_rank"])
    vals = [v for v in vals if v is not None]
    return sum(vals)/len(vals) if vals else None

def evaluate_boat(boat, data, venue):
    """
    自艇 vs 左隣。
    正のscoreほど「左を叩いて展開を作れる」。
    """
    left = boat - 1
    me, le = data.get(boat, {}), data.get(left, {})
    score, reasons = 0.0, []

    # 1) 元々のスタート力 / 今節平均ST
    me_st = me.get("series_st", me.get("avg_st"))
    le_st = le.get("series_st", le.get("avg_st"))
    if me_st is not None and le_st is not None:
        edge = le_st - me_st  # +なら自艇が速い
        if edge >= ST_EDGE_STRONG:
            score += 2.5; reasons.append(f"ST左より{edge:.02f}速い")
        elif edge >= ST_EDGE_GOOD:
            score += 1.5; reasons.append(f"ST左より{edge:.02f}速い")
        elif edge <= -ST_EDGE_STRONG:
            score -= 2.0; reasons.append(f"ST左より{-edge:.02f}遅い")
        elif edge <= -ST_EDGE_GOOD:
            score -= 1.0

    # 2) ST順位: 当地 + 初日 + (ナイター時だけナイター)
    mr, lr = avg_rank(me), avg_rank(le)
    if mr is not None and lr is not None:
        rank_edge = lr - mr  # +なら自艇順位が良い
        if rank_edge >= 0.70:
            score += 2.0; reasons.append(f"ST順位左より{rank_edge:.2f}上")
        elif rank_edge >= 0.30:
            score += 1.0; reasons.append(f"ST順位左より{rank_edge:.2f}上")
        elif rank_edge <= -0.70:
            score -= 1.5

    # 3) スタート展示。「Fだから消し」はしない。
    exst = me.get("ex_st")
    naturally_fast = (me_st is not None and me_st <= FAST_ST) or (mr is not None and mr <= 2.8)
    if exst is not None:
        if exst < 0:
            fdepth = abs(exst)
            if naturally_fast and fdepth <= ALLOW_EX_F:
                score += 1.5
                reasons.append(f"展示F{fdepth:.02f}許容(元ST速い)")
            elif not naturally_fast:
                score -= 1.5
                reasons.append(f"展示F{fdepth:.02f}再現性注意")
            elif fdepth > ALLOW_EX_F:
                score -= 0.5
        elif exst <= 0.08:
            score += 1.0; reasons.append(f"展示ST{exst:.02f}")
        elif exst >= 0.18:
            score -= 0.8

    # 左隣の展示Fが「普段遅いのにF」なら、自艇には追い風
    lexst = le.get("ex_st")
    left_st = le_st
    left_rank = lr
    left_naturally_fast = (left_st is not None and left_st <= FAST_ST) or (left_rank is not None and left_rank <= 2.8)
    if lexst is not None and lexst < 0 and not left_naturally_fast:
        score += 1.0
        reasons.append("左艇は展示Fだが平常ST弱め")

    # 4) 展示タイム: 左より良ければ強い
    mt, lt = me.get("ex_time"), le.get("ex_time")
    if mt is not None and lt is not None:
        tedge = lt - mt  # +なら自艇が速い
        if tedge >= EX_TIME_STRONG:
            score += 2.5; reasons.append(f"展示左より{tedge:.02f}速い")
        elif tedge >= EX_TIME_GOOD:
            score += 1.5; reasons.append(f"展示左より{tedge:.02f}速い")
        elif tedge <= -EX_TIME_STRONG:
            score -= 1.2

    # 5) F持ちは軽い減点。ただし展示Fとは別物。
    if me.get("f_count", 0) >= 1:
        score -= 0.7
        reasons.append(f"F持ち{me['f_count']}")

    return score, reasons

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
    arr = []
    for b in range(2, 7):
        score, reasons = evaluate_boat(b, data, venue)
        print(
            f"[SCORE] {venue}{race}R {b}号艇={score:.1f} / "
            + " / ".join(reasons),
            flush=True,
        )
        if score >= CHANCE_SCORE:
            arr.append({"boat": b, "score": score, "reasons": reasons})
    arr.sort(key=lambda x: x["score"], reverse=True)
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

def data_ready(data):
    et = sum(data[b].get("ex_time") is not None for b in range(1, 7))
    es = sum(data[b].get("ex_st") is not None for b in range(1, 7))
    st = sum(
        data[b].get("series_st") is not None
        or data[b].get("avg_st") is not None
        or avg_rank(data[b]) is not None
        for b in range(1, 7)
    )
    print(f"[DATA] 展示={et}/6 展示ST={es}/6 ST系={st}/6", flush=True)
    return et >= 5 and es >= 5 and st >= 5

def notify_chance(venue, race, candidates, data, shin):
    best = candidates[0]
    title = f"🚤 {best['boat']}号艇 チャンス"

    lines = [
        f"{venue} {race}R",
        "",
        f"注目: {best['boat']}号艇 / 指数 {best['score']:.1f}",
        " / ".join(best["reasons"][:6]) or "総合評価",
    ]

    if len(candidates) > 1:
        lines.append(
            "他候補: "
            + "・".join(
                f"{x['boat']}号艇({x['score']:.1f})"
                for x in candidates[1:3]
            )
        )

    lines += ["", "【ST・展示】"]

    for b in range(1, 7):
        d = data.get(b, {})
        stv = d.get("series_st", d.get("avg_st"))
        exst = d.get("ex_st")
        ext = d.get("ex_time")
        rr = avg_rank(d)

        if exst is None:
            exs = "-"
        elif exst < 0:
            exs = f"F{abs(exst):.02f}"
        else:
            exs = f"{exst:.02f}"

        rr_text = f"{rr:.2f}" if rr is not None else "-"
        st_text = f"{stv:.2f}" if stv is not None else "-"
        ext_text = f"{ext:.2f}" if ext is not None else "-"

        lines.append(
            f"{b}号艇 ST={st_text} / ST順={rr_text} "
            f"/ 展示ST={exs} / 展示={ext_text}"
        )

    lines += ["", "【最後にシンsumで1着確認】"]

    for x in candidates[:3]:
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
                f"+ チェッカー{ck:+.1f}% = {s['final']:.1f}%"
            )
        else:
            lines.append(
                f"{b}号艇 元1着率表示なし / 理論{th:+.1f}% "
                f"+ チェッカー{ck:+.1f}% = 補正{s['total']:+.1f}pt"
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
    print(f"[NOTIFY] {title} / {venue}{race}R", flush=True)

def analyze_current_page(page, venue, race):
    ex = official_exhibition(venue, race)
    official = official_avg_st_f(venue, race)
    hiyori = hiyori_st_data(venue, race)
    data = merge_data(official, hiyori, ex)

    if not data_ready(data):
        return False

    candidates = chance_candidates(venue, race, data)

    if not candidates:
        print(f"[NO-CHANCE] {venue}{race}R", flush=True)
        return True

    boats = [x["boat"] for x in candidates]
    shin = shinsum_confirmation(page, venue, race, boats)

    notify_chance(venue, race, candidates, data, shin)
    return True

def remain_minutes_from_text(text):
    d = deadline(text)
    if not d:
        return None

    hh, mm = map(int, d.split(":"))
    t = now().replace(hour=hh, minute=mm, second=0, microsecond=0)
    return (t - now()).total_seconds() / 60.0

def cycle(page):
    links = candidate_links(page)
    print(f"[CYCLE] {now():%H:%M:%S} 詳細候補リンク={len(links)}", flush=True)

    today = now().strftime("%Y%m%d")

    for link in links:
        try:
            page.goto(link, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(350)

            text = page.locator("body").inner_text(timeout=10000)
            venue = actual_venue(text)
            rr = actual_race(text)

            if not venue or not rr:
                continue

            race = int(rr[:-1])
            remain = remain_minutes_from_text(text)

            if remain is None:
                continue

            key = (today, venue, race)
            state = _attempt_state.get(key, 0)

            if state == 9 or state >= 3:
                continue

            target = {0: 15.0, 1: 13.0, 2: 11.0}[state]

            print(
                f"[RACE] {venue}{race}R 残り{remain:.1f}分 state={state}",
                flush=True,
            )

            if remain > target:
                continue

            if remain < 9.0:
                _attempt_state[key] = 3
                continue

            attempt = state + 1

            print(
                f"[TRY] {venue}{race}R {attempt}回目 / "
                f"目標{int(target)}分前",
                flush=True,
            )

            if analyze_current_page(page, venue, race):
                _attempt_state[key] = 9
            else:
                _attempt_state[key] = attempt

                if attempt < 3:
                    nxt = 13 if attempt == 1 else 11
                    print(
                        f"[RETRY] {venue}{race}R → {nxt}分前",
                        flush=True,
                    )
                else:
                    print(
                        f"[GIVEUP] {venue}{race}R 3回目もデータ不足",
                        flush=True,
                    )

        except Exception as e:
            print(f"[ERR] {link} / {e!r}", flush=True)

def main():
    print(
        "ANA チャンスBOT開始 / 全開催日 / "
        "15→13→11分前 / 最後にシンsum1着確認",
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
