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

    symbol = "🟢" if kind == "1号艇有利" else "🔥"

    lines = []

    for boat in range(1, 7):
        mark = " ←注目" if boat in focus else ""

        lines.append(
            f"{boat}号艇 "
            f"{theory_values[boat]:+g}%"
            f"{mark}"
        )

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
        f"{alert} / "
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

    a = alert_near_deadline(
        text,
        d
    )

    # 「やや本命」「荒れ注意」以外は完全除外
    if not a:
        return None

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

    classification = classify_theory(
        theory
    )

    # 1号艇有利 / 3・4号艇有利に
    # 当てはまらなければ通知しない
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
            f"{v}|"
            f"{r}|"
            f"{a}|"
            f"{classification['type']}"
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
                f"{x['alert']} / "
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
            f"やや本命/荒れ注意 → "
            f"1号艇 or 3・4号艇 選別監視開始",
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
