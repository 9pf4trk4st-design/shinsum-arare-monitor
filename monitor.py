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
# 最終補正1着率で比較する。
# 最終補正1着率 =
#   元の1着率
# + シンsum理論欄の1着補正
# + シンsumチェッカー該当ゾーンの1着補正

# 1号艇有利
ONE_MIN = float(os.getenv("ONE_MIN", "8.0"))
ONE_GAP_VS_34 = float(os.getenv("ONE_GAP_VS_34", "8.0"))
ONE_GAP_VS_SECOND = float(os.getenv("ONE_GAP_VS_SECOND", "5.0"))

# 3・4号艇有利
OUT_MIN = float(os.getenv("OUT_MIN", "10.0"))
OUT_GAP_VS_1 = float(os.getenv("OUT_GAP_VS_1", "8.0"))
OUT_GAP_VS_OTHER = float(os.getenv("OUT_GAP_VS_OTHER", "5.0"))

# 独立バフ検知
BUFF_TARGET_BOATS = (2, 3, 4)

# 最終比較のため1〜6号艇すべてチェッカー解析
CHECKER_PARSE_BOATS = (1, 2, 3, 4, 5, 6)

# 「理論補正 + チェッカー補正」がこの値を超えた2〜4号艇をバフ候補にする
BUFF_MIN_TOTAL = float(os.getenv("BUFF_MIN_TOTAL", "0.0"))

# 1号艇の「理論補正 + チェッカー補正」が0未満なら弱化扱い
ONE_WEAK_TOTAL_THRESHOLD = float(os.getenv("ONE_WEAK_TOTAL_THRESHOLD", "0.0"))

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


def parse_base_1st_rates(text):
    """
    ページ上部の「選手名・1着率」から元の1着率を6艇分取得する。

    DOMの改行位置が変わっても取れるように、
    「選手名・1着率」〜「戦法別上昇率」の範囲に出る
    符号なしの%を上から6個、1〜6号艇として読む。

    例:
      17%, 15%, 11%, 51%, 2%, 2%
      -> {1:17.0, 2:15.0, 3:11.0, 4:51.0, 5:2.0, 6:2.0}
    """
    start = text.find("選手名・1着率")
    if start < 0:
        start = text.find("選手名")
    if start < 0:
        return {}

    end_candidates = []

    for marker in (
        "戦法別上昇率",
        "スリット隊形",
        "シンsum理論",
    ):
        p = text.find(marker, start + 1)
        if p > start:
            end_candidates.append(p)

    end = min(end_candidates) if end_candidates else min(len(text), start + 5000)
    section = text[start:end]

    # +5%, -8% のような補正値は除外。
    # 元1着率は符号なしなので、それだけを順番に取る。
    pcts = re.findall(
        r"(?<![+\-\d.])(\d+(?:\.\d+)?)\s*%",
        section
    )

    if len(pcts) < 6:
        print(
            "元1着率候補不足:",
            pcts,
            "section=",
            repr(section[:1200]),
            flush=True
        )
        return {}

    values = [float(x) for x in pcts[:6]]

    if any(x < 0 or x > 100 for x in values):
        return {}

    return {
        boat: values[boat - 1]
        for boat in range(1, 7)
    }


def parse_theory_adjustments(text):
    """
    シンsum理論表の「1着」補正を6艇全部取得する。

    例:
    {
        1: +2.0,
        2: +3.0,
        3: +4.0,
        4: -1.0,
        5: -2.0,
        6: -1.0
    }
    """
    start = text.find("シンsum理論")
    if start < 0:
        return {}

    end = text.find("シンsumチェッカー", start)
    section = text[start:(end if end > start else start + 7000)]
    lines = [x.strip() for x in section.splitlines() if x.strip()]

    result = {}

    for boat in range(1, 7):
        for i, line in enumerate(lines):
            if line != str(boat):
                continue

            # 艇番直後の1艇分を確認。
            # %値は「1着 / 2着 / 3着 / 3連」の順と想定。
            window = "\n".join(lines[i:i + 20])
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
    例: {1: +0.52, 2: +0.31, 3: +0.45, ...}
    """
    start = text.find("シンsum理論")
    if start < 0:
        return {}

    end = text.find("シンsumチェッカー", start)
    section = text[start:(end if end > start else start + 7000)]
    lines = [x.strip() for x in section.splitlines() if x.strip()]

    result = {}

    for boat in range(1, 7):
        for i, line in enumerate(lines):
            if line != str(boat):
                continue

            for candidate in lines[i + 1:i + 14]:
                if "%" in candidate:
                    continue

                m = re.fullmatch(
                    r"([+-]?\d+(?:\.\d+)?)",
                    candidate
                )

                if not m:
                    continue

                value = float(m.group(1))

                # 平均との差として現実的な範囲だけ採用し、
                # 登録番号などの誤取得を防止
                if -5.0 <= value <= 5.0:
                    result[boat] = value
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


def parse_checker_1st(text, current_diffs):
    """
    各艇のシンsumチェッカーから、
    現在の「平均との差」が属するゾーンの1着率補正を取得。

    例:
    1号艇 差+0.52 → +0.5以上 → +24.1%
    """
    start = text.find("シンsumチェッカー")
    if start < 0:
        return {}

    section = text[start:]
    compact = re.sub(r"\s+", "", section)
    result = {}

    for boat in CHECKER_PARSE_BOATS:
        if boat not in current_diffs:
            continue

        token = f"{boat}号艇"
        pos = compact.find(token)

        if pos < 0:
            continue

        # 次の艇カードまで
        next_positions = []

        for other in range(1, 7):
            if other == boat:
                continue

            p = compact.find(
                f"{other}号艇",
                pos + len(token)
            )

            if p >= 0:
                next_positions.append(p)

        end = (
            min(next_positions)
            if next_positions
            else min(len(compact), pos + 4500)
        )

        card = compact[pos:end]
        zone = checker_zone(current_diffs[boat])

        zpos = -1
        matched_zone_text = None

        for variant in _zone_variants(zone):
            zpos = card.find(variant)
            if zpos >= 0:
                matched_zone_text = variant
                break

        if zpos < 0:
            continue

        # 該当行の後ろ側を確認
        row = card[zpos:zpos + 320]

        # 「件数」の数字を%と誤認しないよう、%付きだけを拾う
        pcts = re.findall(
            r"([+-]?\d+(?:\.\d+)?)%",
            row
        )

        if not pcts:
            continue

        result[boat] = {
            "zone": zone,
            "matched_zone_text": matched_zone_text,
            "checker_1st": float(pcts[0]),
        }

    return result


def build_final_rates(base_rates, theory_adj, checker):
    """
    最終補正1着率を作る。

    最終補正1着率 =
      元の1着率
      + シンsum理論1着補正
      + シンsumチェッカー1着補正
    """
    result = {}

    for boat in range(1, 7):
        base = base_rates.get(boat)
        theory = theory_adj.get(boat)
        checker_info = checker.get(boat)

        if base is None or theory is None or not checker_info:
            continue

        checker_1st = checker_info["checker_1st"]

        result[boat] = {
            "base": base,
            "theory": theory,
            "checker": checker_1st,
            "zone": checker_info["zone"],
            "total_adjustment": theory + checker_1st,
            "final": base + theory + checker_1st,
        }

    return result


def classify_buff(final_rates, current_diffs):
    """
    2・3・4号艇の独立バフ判定。

    ・バフ量 = 理論補正 + チェッカー補正
    ・最終1着率 = 元1着率 + バフ量
    ・1号艇も同じ方式で最終1着率を出し、比較する
    """
    buffs = []

    one = final_rates.get(1)

    if one:
        one_total_adjustment = one["total_adjustment"]
        one_final = one["final"]
        one_weak = (
            one_total_adjustment < ONE_WEAK_TOTAL_THRESHOLD
        )
    else:
        one_total_adjustment = None
        one_final = None
        one_weak = False

    for boat in BUFF_TARGET_BOATS:
        info = final_rates.get(boat)
        diff = current_diffs.get(boat)

        if not info or diff is None:
            continue

        total_boost = info["total_adjustment"]

        if total_boost <= BUFF_MIN_TOTAL:
            continue

        edge_vs_one_final = (
            info["final"] - one_final
            if one_final is not None
            else None
        )

        buffs.append({
            "boat": boat,
            "current_diff": diff,
            "zone": info["zone"],
            "base_1st": info["base"],
            "theory_1st": info["theory"],
            "checker_1st": info["checker"],
            "total_boost": total_boost,
            "final_1st": info["final"],
            "one_final_1st": one_final,
            "one_total_adjustment": one_total_adjustment,
            "one_weak": one_weak,
            "edge_vs_one_final": edge_vs_one_final,
        })

    if not buffs:
        return None

    # 1号艇弱化を優先しつつ、
    # その後は「最終補正1着率」「1号艇との差」で順位付け
    def buff_rank(x):
        edge = x["edge_vs_one_final"]

        return (
            1 if x["one_weak"] else 0,
            x["final_1st"],
            edge if edge is not None else -999.0,
            x["total_boost"],
        )

    buffs.sort(key=buff_rank, reverse=True)

    best = buffs[0]
    focus = [x["boat"] for x in buffs]

    if one:
        reason = (
            f"{best['boat']}号艇: 元 {best['base_1st']:.1f}% "
            f"+ 理論 {best['theory_1st']:+.1f}% "
            f"+ チェッカー {best['checker_1st']:+.1f}% "
            f"= 最終 {best['final_1st']:.1f}%。"
            f"1号艇は 元 {one['base']:.1f}% "
            f"+ 理論 {one['theory']:+.1f}% "
            f"+ チェッカー {one['checker']:+.1f}% "
            f"= 最終 {one['final']:.1f}%。"
        )

        if best["edge_vs_one_final"] is not None:
            reason += (
                f" 最終1着率の差 "
                f"{best['edge_vs_one_final']:+.1f}pt"
            )

        if one_weak:
            reason += (
                f"。1号艇の補正合計 "
                f"{one_total_adjustment:+.1f}%で弱化"
            )
    else:
        reason = (
            f"{best['boat']}号艇: 元 {best['base_1st']:.1f}% "
            f"+ 理論 {best['theory_1st']:+.1f}% "
            f"+ チェッカー {best['checker_1st']:+.1f}% "
            f"= 最終 {best['final_1st']:.1f}%"
        )

    return {
        "type": "独立バフ",
        "focus": focus,
        "buffs": buffs,
        "one": one,
        "one_weak": one_weak,
        "reason": reason,
    }


def classify_final_rates(final_rates):
    """
    6艇すべての「最終補正1着率」を比較して、
    1号艇有利 or 3・4号艇有利 を返す。
    """
    if len(final_rates) < 6:
        return None

    values = {
        boat: final_rates[boat]["final"]
        for boat in range(1, 7)
    }

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
                f"最終補正1着率で1号艇が6艇中トップ。"
                f"3・4号艇の最大値より {b1 - best34:+.1f}pt、"
                f"2番手より {b1 - second_best:+.1f}pt 優勢"
            )
        }

    # -----------------------------
    # 3・4号艇有利
    # -----------------------------
    best_boat = 3 if b3 >= b4 else 4
    best_value = values[best_boat]

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
                f"最終補正1着率で{best_boat}号艇が6艇中トップ。"
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
    final_rates,
    classification
):
    kind = classification["type"]
    focus = set(classification["focus"])

    # やや本命/荒れ注意の主判定とは別に、
    # 2・3・4号艇でプラス補正が出ている艇を「バフ注目」として併記する。
    secondary_buffs = classification.get("secondary_buffs", [])
    secondary_focus = {
        x["boat"] for x in secondary_buffs
        if x["boat"] not in focus
    }

    if kind == "1号艇有利":
        symbol = "🟢"
    elif kind == "独立バフ":
        symbol = "🚀"
    else:
        symbol = "🔥"

    rate_lines = []

    for boat in range(1, 7):
        x = final_rates.get(boat)

        if not x:
            continue

        if boat in focus:
            mark = " ←主注目"
        elif boat in secondary_focus:
            mark = " ←バフ注目"
        else:
            mark = ""

        rate_lines.append(
            f"{boat}号艇 "
            f"{x['base']:.1f}% "
            f"+ 理論{x['theory']:+.1f}% "
            f"+ チェッカー{x['checker']:+.1f}% "
            f"= {x['final']:.1f}%"
            f"{mark}"
        )

    if kind == "独立バフ":
        buff_lines = []

        for b in classification["buffs"]:
            extra = ""

            if b["edge_vs_one_final"] is not None:
                extra = (
                    f" / 1号艇最終 {b['one_final_1st']:.1f}%"
                    f" / 差 {b['edge_vs_one_final']:+.1f}pt"
                )

                if b["one_weak"]:
                    extra += " ←1号艇弱化"

            buff_lines.append(
                f"{b['boat']}号艇 "
                f"平均との差 {b['current_diff']:+.2f} ({b['zone']})\n"
                f"元 {b['base_1st']:.1f}% "
                f"+ 理論 {b['theory_1st']:+.1f}% "
                f"+ チェッカー {b['checker_1st']:+.1f}% "
                f"= 最終 {b['final_1st']:.1f}%"
                f"{extra}"
            )

        body = (
            f"{symbol} シンsum独立バフ検知\n"
            f"{venue} {race}\n\n"
            + "\n\n".join(buff_lines)
            + "\n\n全艇・最終補正1着率\n"
            + "\n".join(rate_lines)
            + "\n\n"
            + f"{classification['reason']}\n"
            + f"締切 {deadline_value}"
        )
    else:
        secondary_text = ""

        if secondary_buffs:
            secondary_lines = []
            for b in secondary_buffs:
                secondary_lines.append(
                    f"{b['boat']}号艇 "
                    f"平均との差 {b['current_diff']:+.2f} ({b['zone']}) / "
                    f"理論 {b['theory_1st']:+.1f}% + "
                    f"チェッカー {b['checker_1st']:+.1f}% "
                    f"= 補正 {b['total_boost']:+.1f}% / "
                    f"最終 {b['final_1st']:.1f}%"
                )

            secondary_text = (
                "\n\n🚀 2・3・4号艇のバフも確認\n"
                + "\n".join(secondary_lines)
            )

        body = (
            f"{symbol} {kind}\n"
            f"{venue} {race}【{alert}】\n\n"
            f"全艇・最終補正1着率\n"
            + "\n".join(rate_lines)
            + secondary_text
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

    # -----------------------------
    # 3種類のデータを取得
    # -----------------------------
    base_rates = parse_base_1st_rates(text)
    theory_adj = parse_theory_adjustments(text)
    current_diffs = parse_current_diffs(text)
    checker = parse_checker_1st(text, current_diffs)

    if len(base_rates) < 6:
        print(
            f"元1着率6艇取得失敗: "
            f"{v} / {r} / "
            f"取得 {len(base_rates)}艇 / {base_rates}",
            flush=True
        )
        return None

    if len(theory_adj) < 6:
        print(
            f"シンsum理論1着補正6艇取得失敗: "
            f"{v} / {r} / "
            f"取得 {len(theory_adj)}艇 / {theory_adj}",
            flush=True
        )
        return None

    if len(current_diffs) < 6:
        print(
            f"平均との差6艇取得失敗: "
            f"{v} / {r} / "
            f"取得 {len(current_diffs)}艇 / {current_diffs}",
            flush=True
        )
        return None

    if len(checker) < 6:
        print(
            f"シンsumチェッカー6艇取得失敗: "
            f"{v} / {r} / "
            f"取得 {len(checker)}艇 / {checker}",
            flush=True
        )
        return None

    final_rates = build_final_rates(
        base_rates,
        theory_adj,
        checker
    )

    if len(final_rates) < 6:
        print(
            f"最終補正1着率6艇計算失敗: "
            f"{v} / {r} / "
            f"取得 {len(final_rates)}艇",
            flush=True
        )
        return None

    print(
        f"最終補正1着率: {v} / {r} / "
        + " | ".join(
            f"{boat}号艇 "
            f"{final_rates[boat]['base']:.1f}"
            f"{final_rates[boat]['theory']:+.1f}"
            f"{final_rates[boat]['checker']:+.1f}"
            f"={final_rates[boat]['final']:.1f}%"
            for boat in range(1, 7)
        ),
        flush=True
    )

    # 全レース共通で、2〜4号艇のバフを先に計算しておく。
    # これにより「4号艇が主役だが2号艇にも強いバフ」のようなケースを
    # やや本命/荒れ注意の通知内でも見落とさない。
    buff_classification = classify_buff(
        final_rates,
        current_diffs
    )

    # -----------------------------
    # A. やや本命 / 荒れ注意
    #    → 最終補正1着率で主判定
    #    ＋ 2〜4号艇のバフを副注目として同時表示
    # -----------------------------
    if a:
        classification = classify_final_rates(
            final_rates
        )

        if classification:
            if buff_classification:
                classification["secondary_buffs"] = buff_classification["buffs"]

            buff_key = ""
            if buff_classification:
                buff_key = "|B" + "-".join(
                    str(x["boat"])
                    for x in buff_classification["buffs"]
                )

            return {
                "venue": v,
                "race": r,
                "deadline": d,
                "alert": a,
                "final_rates": final_rates,
                "classification": classification,
                "key": (
                    f"{now():%Y-%m-%d}|"
                    f"{v}|{r}|{a}|{classification['type']}"
                    f"{buff_key}"
                )
            }

        print(
            f"最終補正で主選別は対象外、独立バフを続けて確認: "
            f"{a} / {v} / {r}",
            flush=True
        )

    # -----------------------------
    # B. 全レース共通
    #    2〜4号艇の独立バフ
    # -----------------------------
    classification = buff_classification

    if not classification:
        return None

    print(
        f"独立バフ候補: {v} / {r} / "
        + ", ".join(
            f"{x['boat']}号艇 "
            f"差{x['current_diff']:+.2f} "
            f"元{x['base_1st']:.1f}% "
            f"理論{x['theory_1st']:+.1f}% "
            f"チェッカー{x['checker_1st']:+.1f}% "
            f"最終{x['final_1st']:.1f}%"
            for x in classification["buffs"]
        ),
        flush=True
    )

    return {
        "venue": v,
        "race": r,
        "deadline": d,
        "alert": "",
        "final_rates": final_rates,
        "classification": classification,
        "key": (
            f"{now():%Y-%m-%d}|"
            f"{v}|{r}|BUFF|"
            + "-".join(
                str(x["boat"])
                for x in classification["buffs"]
            )
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

    # 起動直後に既に存在している通知を大量送信しない
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
            x["final_rates"],
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
            f"最終補正1着率 "
            f"(元1着率 + 理論補正 + チェッカー補正) "
            f"監視開始",
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
