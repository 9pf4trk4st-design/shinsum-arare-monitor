import os
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

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

# 2〜4号艇のバフ判定では、1号艇の弱化も加味する。
# 「シンsum理論1着補正 + シンsumチェッカー1着補正」を合算し、
# マイナスなら1号艇弱化として評価する。
ONE_WEAK_TOTAL_THRESHOLD = 0.0

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


def parse_theory_1st(text):
    """
    シンsum理論の「1着」変化率を6艇全部取得。

    戻り値例:
    {
        1: -2.0,
        2: 5.0,
        3: 23.0,
        4: 12.0,
        5: -1.0,
        6: 3.0
    }
    """

    start = text.find("シンsum理論")

    if start < 0:
        return {}

    end = text.find("シンsumチェッカー", start)

    section = text[
        start:(end if end > start else start + 6000)
    ]

    lines = [
        x.strip()
        for x in section.splitlines()
        if x.strip()
    ]

    result = {}

    for boat in range(1, 7):
        for i, line in enumerate(lines):

            if line != str(boat):
                continue

            # 1艇分を広めに確認
            window = "\n".join(lines[i:i + 18])

            # シンsum理論は
            # 1着 / 2着 / 3着 / 3連 の順に%が出る想定。
            pcts = re.findall(
                r"([+-]?\d+(?:\.\d+)?)\s*%",
                window
            )

            if pcts:
                result[boat] = float(pcts[0])
                break

    return result


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
    各艇のシンsumチェッカーから、現在の「平均との差」が属するゾーンの
    1着率変化を取得する。

    例: 3号艇が +0.61 なら「+0.5以上」行の1着率（例 +6.4%）を返す。
    """
    start = text.find("シンsumチェッカー")
    if start < 0:
        return {}

    section = text[start:]
    compact = re.sub(r"\s+", "", section)
    result = {}

    for boat in BUFF_TARGET_BOATS:
        if boat not in current_diffs:
            continue

        token = f"{boat}号艇"
        pos = compact.find(token)
        if pos < 0:
            continue

        # 次の艇カードまでをこの艇の範囲とする。
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
        zpos = card.find(zone)
        if zpos < 0:
            # 表記ゆれ対策（波ダッシュ/全角チルダ等）
            variants = {
                "0〜+0.5": ("0~+0.5", "0～+0.5"),
                "-0.5〜0": ("-0.5~0", "-0.5～0"),
            }.get(zone, ())
            for v in variants:
                zpos = card.find(v)
                if zpos >= 0:
                    break

        if zpos < 0:
            continue

        # 該当行は「件数」の後に 1着率 / 2着率 / 3着率 / 3連対率 の順。
        row = card[zpos:zpos + 220]
        pcts = re.findall(r"([+-]?\d+(?:\.\d+)?)%", row)
        if not pcts:
            continue

        result[boat] = {
            "zone": zone,
            "checker_1st": float(pcts[0]),
        }

    return result


def classify_buff(theory, current_diffs, checker):
    """
    やや本命/荒れ注意が無くても通知する独立バフ判定。

    基本条件:
      - 1〜4号艇
      - 現在の平均との差が +0.50以上
      - シンsum理論の1着補正が +10%以上
      - シンsumチェッカーの現在ゾーンの1着率が +5%以上

    さらに2〜4号艇については、1号艇の弱化も加味する。
      1号艇合計補正 = 1号艇シンsum理論1着 + 1号艇チェッカー1着
    例: -3.0% + -1.7% = -4.7%

    1号艇合計補正がマイナスなら「1号艇弱化あり」として優先度を上げる。
    ただし、1号艇がマイナスでない場合でも対象艇自身のバフが十分強ければ
    通知対象からは外さない。
    """
    buffs = []

    one_theory = theory.get(1)
    one_checker_info = checker.get(1)
    one_checker = (
        one_checker_info["checker_1st"]
        if one_checker_info else None
    )
    one_total = (
        one_theory + one_checker
        if one_theory is not None and one_checker is not None
        else None
    )
    one_weak = (
        one_total is not None
        and one_total < ONE_WEAK_TOTAL_THRESHOLD
    )

    for boat in BUFF_TARGET_BOATS:
        diff = current_diffs.get(boat)
        theory_1st = theory.get(boat)
        c = checker.get(boat)

        if diff is None or theory_1st is None or not c:
            continue

        checker_1st = c["checker_1st"]
        total_boost = theory_1st + checker_1st

        if (
            diff >= BUFF_MIN_CURRENT_DIFF
            and theory_1st >= BUFF_MIN_THEORY_1ST
            and checker_1st >= BUFF_MIN_CHECKER_1ST
        ):
            edge_vs_one = (
                total_boost - one_total
                if boat != 1 and one_total is not None
                else None
            )

            buffs.append({
                "boat": boat,
                "current_diff": diff,
                "theory_1st": theory_1st,
                "checker_1st": checker_1st,
                "total_boost": total_boost,
                "zone": c["zone"],
                "one_theory": one_theory,
                "one_checker_1st": one_checker,
                "one_total": one_total,
                "one_weak": one_weak if boat != 1 else False,
                "edge_vs_one": edge_vs_one,
            })

    if not buffs:
        return None

    def buff_rank(x):
        edge = x["edge_vs_one"]
        return (
            1 if x["one_weak"] else 0,
            edge if edge is not None else -999.0,
            x["total_boost"],
            x["checker_1st"],
        )

    buffs.sort(key=buff_rank, reverse=True)

    focus = [x["boat"] for x in buffs]
    best = buffs[0]

    if best["boat"] != 1 and best["one_total"] is not None:
        weak_text = (
            f"1号艇合計 {best['one_total']:+.1f}%"
            f"（理論 {best['one_theory']:+.1f}%"
            f" + チェッカー {best['one_checker_1st']:+.1f}%）"
        )
        if best["one_weak"]:
            weak_text += "で弱化"
        reason = (
            f"{best['boat']}号艇を中心にバフ検知。"
            f"対象艇合計 {best['total_boost']:+.1f}%、"
            f"{weak_text}、"
            f"1号艇との差 {best['edge_vs_one']:+.1f}pt"
        )
    else:
        reason = (
            f"{best['boat']}号艇を中心にバフ検知。"
            f"平均との差 {best['current_diff']:+.2f}、"
            f"理論1着 {best['theory_1st']:+.1f}%、"
            f"チェッカー1着 {best['checker_1st']:+.1f}%、"
            f"合計 {best['total_boost']:+.1f}%"
        )

    return {
        "type": "独立バフ",
        "focus": focus,
        "buffs": buffs,
        "one_theory": one_theory,
        "one_checker_1st": one_checker,
        "one_total": one_total,
        "one_weak": one_weak,
        "reason": reason,
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


def notify_selected(
    alert,
    venue,
    race,
    deadline_value,
    theory_values,
    classification
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
        lines.append(
            f"{boat}号艇 "
            f"{theory_values[boat]:+g}%"
            f"{mark}"
        )

    if kind == "独立バフ":
        buff_lines = []
        for b in classification["buffs"]:
            extra = ""
            if b["boat"] != 1 and b["one_total"] is not None:
                extra = (
                    f" / 1号艇合計 {b['one_total']:+.1f}%"
                    f" / 差 {b['edge_vs_one']:+.1f}pt"
                )
                if b["one_weak"]:
                    extra += " ←1号艇弱化"

            buff_lines.append(
                f"{b['boat']}号艇 "
                f"平均との差 {b['current_diff']:+.2f} / "
                f"理論1着 {b['theory_1st']:+.1f}% / "
                f"チェッカー1着 {b['checker_1st']:+.1f}% / "
                f"合計 {b['total_boost']:+.1f}%"
                f"{extra}"
            )

        one_breakdown = ""
        if classification.get("one_total") is not None:
            one_breakdown = (
                "\n1号艇の弱化チェック\n"
                f"理論1着 {classification['one_theory']:+.1f}% "
                f"+ チェッカー1着 {classification['one_checker_1st']:+.1f}% "
                f"= 合計 {classification['one_total']:+.1f}%"
            )
            if classification.get("one_weak"):
                one_breakdown += " ←弱化"

        body = (
            f"{symbol} シンsum独立バフ検知\n"
            f"{venue} {race}\n\n"
            + "\n".join(buff_lines)
            + one_breakdown
            + "\n\n"
            + "シンsum理論・1着\n"
            + "\n".join(lines)
            + "\n\n"
            + f"{classification['reason']}\n"
            + f"締切 {deadline_value}"
        )
    else:
        body = (
            f"{symbol} {kind}\n"
            f"{venue} {race}【{alert}】\n\n"
            f"シンsum理論・1着\n"
            + "\n".join(lines)
            + "\n\n"
            + f"{classification['reason']}\n"
            + f"締切 {deadline_value}"
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

    if not within15(text):
        return None

    d = deadline(text)
    a = alert_near_deadline(text, d)

    theory = parse_theory_1st(text)

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
            "classification": classification,
            "key": (
                f"{now():%Y-%m-%d}|"
                f"{v}|{r}|{a}|{classification['type']}"
            )
        }

    # -----------------------------
    # B. 新規: 通常レースでも1〜4号艇の独立バフを検知
    # -----------------------------
    current_diffs = parse_current_diffs(text)
    checker = parse_checker_1st(text, current_diffs)
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
            x["alert"],
            x["venue"],
            x["race"],
            x["deadline"],
            x["theory"],
            x["classification"]
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
