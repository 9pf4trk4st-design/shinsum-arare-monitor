# -*- coding: utf-8 -*-
"""
中穴・大穴BOT v1
目的:
  2〜6号艇を「自艇 vs 左隣艇」で比較し、
  本番でスタートして展開を作る艇 / その外で展開利を受ける艇を通知する。

重要ルール
- 主役: ST力 + 今節平均ST + スタート展示 + 展示タイム
- ST順位: 当地 / 初日。ナイター場だけナイターSTも加味
- 直近1ヶ月/3ヶ月ST順位は使わない
- スタート展示Fは一律減点しない
  * 元々STが速い艇は F.01〜F.03 を許容
  * 元々STが遅い艇のFは「展示だけ踏み込んだ」可能性として信頼度を下げる
- 展示タイムが左隣より良ければ追加加点
- 2〜6号艇すべて同じロジック
- 全開催日対象（ANA_ALLOWED_DAYSで必要なら絞り込み可）
"""

import os
import re
import time
import html
from datetime import datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import requests

JST = ZoneInfo("Asia/Tokyo")
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))
ALLOWED_DAYS = {
    int(x) for x in os.getenv("ANA_ALLOWED_DAYS", "1,2,3,4,5,6,7").split(",") if x.strip().isdigit()
}

# 中穴/大穴の通知閾値。ログを貯めて後で調整可能。
MID_SCORE = float(os.getenv("ANA_MID_SCORE", "5.0"))
BIG_SCORE = float(os.getenv("ANA_BIG_SCORE", "7.0"))

# 「元々スタートが速い」判定
FAST_ST = float(os.getenv("ANA_FAST_ST", "0.16"))
VERY_FAST_ST = float(os.getenv("ANA_VERY_FAST_ST", "0.14"))
# 元々速い艇ならスタート展示F.01〜F.03を許容
ALLOW_EX_F = float(os.getenv("ANA_ALLOW_EX_F", "0.03"))

# 左隣との展示タイム差
EX_TIME_GOOD = float(os.getenv("ANA_EX_TIME_GOOD", "0.03"))
EX_TIME_STRONG = float(os.getenv("ANA_EX_TIME_STRONG", "0.07"))

# 左隣とのST差
ST_EDGE_GOOD = float(os.getenv("ANA_ST_EDGE_GOOD", "0.02"))
ST_EDGE_STRONG = float(os.getenv("ANA_ST_EDGE_STRONG", "0.04"))

TARGET_VENUES = {
    "戸田":2, "平和島":4, "多摩川":5, "蒲郡":7, "三国":10, "びわこ":11,
    "住之江":12, "鳴門":14, "児島":16, "宮島":17, "徳山":18, "下関":19,
    "若松":20, "芦屋":21, "唐津":23, "大村":24,
}
# 今回の対象場のうちナイターSTを使う場
NIGHT_VENUES = {"蒲郡", "住之江", "下関", "若松", "大村"}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ana-bot/1.0)"}
session = requests.Session()
session.headers.update(HEADERS)

_seen = set()
_day_cache = {}


def now():
    return datetime.now(JST)


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


def tables(raw):
    p = TableParser()
    p.feed(raw)
    return p.tables


def clean_text(raw):
    raw = re.sub(r"<script\b[^>]*>.*?</script>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<style\b[^>]*>.*?</style>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def meeting_day(venue):
    """公式トップの開催期間から何日目かを判定。開催中なら全日対象。"""
    key = (now().strftime("%Y%m%d"), venue)
    if key in _day_cache:
        return _day_cache[key]

    hd = now().strftime("%Y%m%d")
    try:
        r = session.get(f"https://www.boatrace.jp/owpc/pc/race/index?hd={hd}", timeout=12)
        r.raise_for_status()
        txt = clean_text(r.text)
        pos = txt.find(venue)
        if pos < 0:
            _day_cache[key] = None
            return None
        block = txt[pos:pos+2000]
        m = re.search(r"(\d{1,2})\s*/\s*(\d{1,2})\s*[-～〜~－ー]\s*(\d{1,2})\s*/\s*(\d{1,2})", block)
        if not m:
            _day_cache[key] = None
            return None
        sm, sd, _, _ = map(int, m.groups())
        today = now().date()
        start = today.replace(month=sm, day=sd)
        if start > today and sm == 12 and today.month == 1:
            start = start.replace(year=today.year - 1)
        day = (today - start).days + 1
        _day_cache[key] = day if day > 0 else None
        return _day_cache[key]
    except Exception as e:
        print("開催日判定失敗", venue, repr(e), flush=True)
        return None


def parse_st_token(s):
    """F.02 -> -0.02 / .08 -> 0.08 / 0.08 -> 0.08"""
    s = str(s).strip().upper().replace(" ", "")
    m = re.search(r"F\.?(\d{1,2})", s)
    if m:
        return -int(m.group(1)) / 100.0
    m = re.search(r"(?<!\d)\.?(\d{2})(?!\d)", s)
    if m:
        return int(m.group(1)) / 100.0
    m = re.search(r"0\.(\d{2})", s)
    if m:
        return int(m.group(1)) / 100.0
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


def classify_race(venue, race, data):
    makers = []
    for b in range(2, 7):
        score, reasons = evaluate_boat(b, data, venue)
        if score >= MID_SCORE:
            makers.append({"boat": b, "score": score, "reasons": reasons})

    if not makers:
        return None

    makers.sort(key=lambda x: x["score"], reverse=True)
    best = makers[0]

    # 大穴: 展開作り指数が高い、または4〜6号艇が強い
    level = "💣大穴" if best["score"] >= BIG_SCORE or (best["boat"] >= 4 and best["score"] >= MID_SCORE + 1) else "🔥中穴"

    # 展開利: 攻め役の外側。特に5→6は明示。
    beneficiaries = []
    for m in makers:
        b = m["boat"]
        if b < 6:
            outer = b + 1
            od = data.get(outer, {})
            # 外艇自身にST/展示の根拠がある時だけ展開穴にする
            good_ex = od.get("ex_time") is not None and data.get(b, {}).get("ex_time") is not None and od["ex_time"] <= data[b]["ex_time"] + 0.03
            good_st = od.get("avg_st") is not None and od["avg_st"] <= FAST_ST
            if good_ex or good_st:
                beneficiaries.append(outer)

    return level, makers, sorted(set(beneficiaries))


def notify(venue, race, day, result, data):
    level, makers, beneficiaries = result
    best = makers[0]
    lines = [
        f"{level}候補 {venue}{race}R / {day}日目",
        f"展開作り本命: {best['boat']}号艇  指数{best['score']:.1f}",
    ]
    for m in makers[:3]:
        lines.append(f"{m['boat']}号艇: " + " / ".join(m["reasons"][:5]))
    if beneficiaries:
        lines.append("展開穴: " + "・".join(f"{b}号艇" for b in beneficiaries))

    # 確認用に主要値を通知へ
    lines.append("----")
    for b in range(1, 7):
        d = data.get(b, {})
        st = d.get("series_st", d.get("avg_st"))
        exst = d.get("ex_st")
        ext = d.get("ex_time")
        rr = avg_rank(d)
        exst_s = "-" if exst is None else (f"F{abs(exst):.02f}" if exst < 0 else f"{exst:.02f}")
        lines.append(
            f"{b}: ST {st if st is not None else '-'} / ST順 {rr:.2f}" if rr is not None
            else f"{b}: ST {st if st is not None else '-'} / ST順 -"
        )
        lines[-1] += f" / 展示ST {exst_s} / 展示 {ext if ext is not None else '-'}"

    body = "\n".join(lines)
    r = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={"Priority": "high", "Tags": "fire"},
        timeout=12,
    )
    r.raise_for_status()
    print("通知", venue, race, level, flush=True)


def inspect_race(venue, race, day):
    ex = official_exhibition(venue, race)
    # 展示未公開ならまだ判定しない
    if sum("ex_time" in v for v in ex.values()) < 5:
        return

    official = official_avg_st_f(venue, race)
    hiyori = hiyori_st_data(venue, race)
    data = merge_data(official, hiyori, ex)

    # 最低限の信頼性チェック
    have_st = sum(data[b].get("avg_st") is not None or data[b].get("series_st") is not None for b in range(1,7))
    have_ex = sum(data[b].get("ex_time") is not None for b in range(1,7))
    if have_st < 5 or have_ex < 5:
        print(f"材料不足 {venue}{race}R ST={have_st} 展示={have_ex}", flush=True)
        return

    result = classify_race(venue, race, data)
    if not result:
        return

    key = (now().strftime("%Y%m%d"), venue, race, result[0], result[1][0]["boat"])
    if key in _seen:
        return
    notify(venue, race, day, result, data)
    _seen.add(key)


def cycle():
    for venue in TARGET_VENUES:
        day = meeting_day(venue)
        if day not in ALLOWED_DAYS:
            continue
        for race in range(1, 13):
            try:
                inspect_race(venue, race, day)
            except Exception as e:
                print("race error", venue, race, repr(e), flush=True)


def main():
    print(
        f"中穴・大穴BOT v1 開始 / 対象日={sorted(ALLOWED_DAYS)} / "
        f"中穴>={MID_SCORE} 大穴>={BIG_SCORE}",
        flush=True,
    )
    while 8 <= now().hour < 23:
        cycle()
        print(f"{CHECK_INTERVAL}秒後に再チェック", flush=True)
        time.sleep(CHECK_INTERVAL)
    print("監視時間外 23:00〜08:00 JST", flush=True)


if __name__ == "__main__":
    main()
