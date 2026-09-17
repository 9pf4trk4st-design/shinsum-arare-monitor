import os
import re
import time
from datetime import datetime, timedelta
from email.header import Header
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright


# ============================================================
# 基本設定
# ============================================================

BASE_URL = "https://boatrace-shinsum.com/"

SHINSUM_USER = os.environ["SHINSUM_USER"]
SHINSUM_PASSWORD = os.environ["SHINSUM_PASSWORD"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))
ALERT_WINDOW_MIN = int(os.getenv("ALERT_WINDOW_MIN", "15"))

JST = ZoneInfo("Asia/Tokyo")

# 通知対象場
TARGET_VENUES = (
    "戸田",
    "多摩川",
    "びわこ",
    "浜名湖",
    "平和島",
    "福岡",
    "蒲郡",
    "下関",
    "大村",
)

# 同一実行中の重複通知防止
SENT = set()


# ============================================================
# 時刻
# ============================================================

def now():
    return datetime.now(JST)


def active():
    # JST 08:00〜23:00
    return 8 <= now().hour < 23


def get_deadline(text):
    m = re.search(
        r"締切\s*[：:]?\s*([01]?\d|2[0-3]):([0-5]\d)",
        text
    )
    if not m:
        return ""

    return f"{int(m.group(1)):02d}:{m.group(2)}"


def within_alert_window(text):
    d = get_deadline(text)

    if not d:
        return False

    h, m = map(int, d.split(":"))

    deadline_dt = now().replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0,
    )

    diff = deadline_dt - now()

    return (
        timedelta(minutes=-1)
        <= diff
        <= timedelta(minutes=ALERT_WINDOW_MIN)
    )


# ============================================================
# レース情報
# ============================================================

def get_venue(text):
    head = text[:2500]

    for venue in TARGET_VENUES:
        if venue in head:
            return venue

    return ""


def get_race(text):
    head = text[:2500]

    m = re.search(
        r"(?<!\d)([1-9]|1[0-2])\s*R\b",
        head,
        re.IGNORECASE,
    )

    if not m:
        return ""

    return f"{m.group(1)}R"


# ============================================================
# 詳細ページ候補
# ============================================================

def candidate_links(page):
    """
    以前の監視ツールで使っていた候補リンク抽出方式をそのまま採用。
    a[href] を走査し、対象場/R/race/detail/sum を含むリンクを拾う。

    V38では、認証失敗を見逃さないよう HTTP status も確認する。
    """

    response = page.goto(
        BASE_URL,
        wait_until="domcontentloaded",
        timeout=30000
    )
    page.wait_for_timeout(1000)

    if response is None:
        print(
            "トップページ応答なし",
            flush=True
        )
        return []

    status = response.status

    print(
        f"トップページHTTP: {status}",
        flush=True
    )

    if status in (401, 403):
        print(
            "認証失敗: SHINSUM_USER / SHINSUM_PASSWORD を確認",
            flush=True
        )
        return []

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


# ============================================================
# スリットアラート解析
# ============================================================

def extract_theory_section(text):
    """
    旧監視ツールで実際に使えていた方式。
    ページ内の全「シンsum理論」候補を調べ、
    4桁登録番号が最も多く並ぶ箇所を本物の理論表として採用する。
    「←シンsum理論に戻る」を誤認しない。
    """
    starts = [
        m.start()
        for m in re.finditer(r"シン\s*sum理論", text)
    ]

    best_section = ""
    best_score = -1

    for start in starts:
        end = text.find("シンsumチェッカー", start)

        section = text[
            start:(
                end
                if end > start
                else min(len(text), start + 12000)
            )
        ]

        regs = re.findall(
            r"(?<!\d)(\d{4})(?!\d)",
            section
        )
        regs_unique = list(dict.fromkeys(regs))

        diff_like = re.findall(
            r"(?<![\d.])([+-]\d+(?:\.\d+)?)(?!\s*%)",
            section
        )

        score = (
            len(regs_unique) * 100
            + len(diff_like)
        )

        if score > best_score:
            best_score = score
            best_section = section

    return best_section



def parse_alert_cell(cell_text):
    """
    スリットアラート欄の1セルだけを解析。

    例:
      +0.2
      1着 +10%

      ⚡ SUPER
      +0.1
      1着 +15%
    """

    text = " ".join(cell_text.split())

    if "1着" not in text:
        return None

    slit_match = re.search(
        r"([+-]\d+(?:\.\d+)?)",
        text
    )

    boost_match = re.search(
        r"1着\s*([+-]\d+(?:\.\d+)?)\s*%",
        text
    )

    if not slit_match or not boost_match:
        return None

    slit = slit_match.group(1)
    boost = float(boost_match.group(1))

    # スリット差は +0.1 などの小数値。
    # 通常の1着補正セルだけを誤検知しないため1.0未満に限定。
    try:
        slit_num = float(slit)
    except ValueError:
        return None

    if abs(slit_num) >= 1.0:
        return None

    return {
        "super": "SUPER" in text.upper(),
        "slit": slit,
        "boost": boost,
    }


def extract_boat_number(cell_texts, row_text):
    # まず先頭付近のセルから艦番を取得
    for txt in cell_texts[:3]:
        cleaned = " ".join(txt.split())

        m = re.fullmatch(
            r"([1-6])(?:号艇)?",
            cleaned
        )

        if m:
            return int(m.group(1))

    # DOM差異用フォールバック
    m = re.search(
        r"(?:^|\s)([1-6])(?:号艇)?\s+\d{4}(?:\s|$)",
        row_text
    )

    if m:
        return int(m.group(1))

    return None


def parse_slit_alerts(page):
    """
    V45:
    旧監視ツールで実績のある extract_theory_section() を使う。

    1. 本物のシンsum理論表を特定
    2. 4桁登録番号を上から6個取得
    3. 登録番号〜次の登録番号までを、その艇だけのブロックにする
    4. ブロック内の
         +0.1  1着 +9%
         SUPER +0.1  1着 +15%
       を直接検出

    艇番は登録番号の出現順 = 1〜6号艇なので、
    前のような1艇ズレを起こさない。
    """
    try:
        body = page.locator(
            "body"
        ).inner_text(timeout=10000)
    except Exception as e:
        print(
            f"本文取得失敗: {repr(e)}",
            flush=True,
        )
        return []

    section = extract_theory_section(body)

    if not section:
        return []

    # 登録番号を出現順に6艇分取得
    regs = re.findall(
        r"(?<!\d)(\d{4})(?!\d)",
        section
    )
    regs = list(dict.fromkeys(regs))

    if len(regs) < 6:
        print(
            f"理論表登録番号不足: "
            f"{len(regs)}件 / {regs}",
            flush=True,
        )
        return []

    regs = regs[:6]

    mapping = {
        reg: boat
        for boat, reg
        in enumerate(regs, start=1)
    }

    print(
        f"理論表 艇番対応: {mapping}",
        flush=True,
    )

    alerts = []

    for boat, reg in enumerate(
        regs,
        start=1,
    ):
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
                next_pos = (
                    mreg.end()
                    + mn.start()
                )

        block = section[
            mreg.end():next_pos
        ]

        # 改行や空白をまとめる
        compact = " ".join(
            block.split()
        )

        # SUPERがある場合もない場合も対応。
        # 「平均との差 +0.xx」は直後に1着+○%が来ないので拾わない。
        m = re.search(
            r"(?:(?:⚡\s*)?SUPER\s*)?"
            r"(\+0\.\d+)\s*"
            r"1着\s*"
            r"(\+\d+(?:\.\d+)?)\s*%",
            compact,
            re.I,
        )

        if not m:
            continue

        slit = m.group(1)
        boost = float(
            m.group(2)
        )

        # アラート直前付近にSUPERがあるか確認
        alert_start = m.start()
        before = compact[
            max(0, alert_start - 30):
            alert_start + 5
        ]

        is_super = (
            "SUPER" in before.upper()
            or "SUPER" in m.group(0).upper()
        )

        alerts.append({
            "boat": boat,
            "super": is_super,
            "slit": slit,
            "boost": boost,
        })

        print(
            f"スリット検出: "
            f"{boat}号艇 / 登録{reg} / "
            f"{('SUPER / ' if is_super else '')}"
            f"{slit} / 1着{boost:+g}%",
            flush=True,
        )

    return alerts


# ============================================================
# ntfy通知
# ============================================================

def send_ntfy(title, body):
    encoded_title = Header(
        title,
        "utf-8"
    ).encode()

    r = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={
            "Title": encoded_title,
            "Priority": "high",
            "Tags": "zap,ship",
        },
        timeout=15,
    )

    r.raise_for_status()


def notify_slit_alert(
    venue,
    race,
    deadline,
    alerts,
):
    new_alerts = []

    for a in alerts:
        key = (
            now().strftime("%Y-%m-%d"),
            venue,
            race,
            a["boat"],
            a["super"],
            a["slit"],
            a["boost"],
        )

        if key in SENT:
            continue

        SENT.add(key)
        new_alerts.append(a)

    if not new_alerts:
        return

    has_super = any(
        a["super"]
        for a in new_alerts
    )

    title = (
        "⚡ SUPERスリットアラート"
        if has_super
        else "🚤 スリットアラート"
    )

    lines = [
        f"{venue} {race}",
        "",
    ]

    for a in new_alerts:
        parts = [
            f"{a['boat']}号艇"
        ]

        if a["super"]:
            parts.append("⚡SUPER")

        parts.append(a["slit"])
        parts.append(
            f"1着{a['boost']:+g}%"
        )

        lines.append(
            "  ".join(parts)
        )

    if deadline:
        lines.extend([
            "",
            f"締切 {deadline}",
        ])

    body = "\n".join(lines)

    send_ntfy(
        title,
        body
    )

    print(
        f"通知送信: {venue} {race} / "
        f"{[a['boat'] for a in new_alerts]}号艇",
        flush=True,
    )


# ============================================================
# 1レース確認
# ============================================================

def inspect_race_page(page):
    try:
        text = page.locator(
            "body"
        ).inner_text(timeout=10000)
    except Exception:
        return

    venue = get_venue(text)
    race = get_race(text)

    if not venue or not race:
        return

    if venue not in TARGET_VENUES:
        return

    if not within_alert_window(text):
        return

    alerts = parse_slit_alerts(page)

    if not alerts:
        return

    d = get_deadline(text)

    print(
        f"スリットアラート検知: "
        f"{venue} {race} / {alerts}",
        flush=True,
    )

    notify_slit_alert(
        venue=venue,
        race=race,
        deadline=d,
        alerts=alerts,
    )


# ============================================================
# 監視サイクル
# ============================================================

def cycle(page):
    links = candidate_links(page)

    print(
        f"詳細候補リンク数: {len(links)}",
        flush=True,
    )

    if not links:
        print(
            "詳細候補リンクなし",
            flush=True,
        )
        return

    for url in links[:150]:
        try:
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=20000,
            )

            page.wait_for_timeout(350)

            inspect_race_page(page)

        except Exception as e:
            print(
                f"詳細ページ確認失敗: "
                f"{url} / {repr(e)}",
                flush=True,
            )


# ============================================================
# メイン
# ============================================================

def main():
    if not active():
        print(
            "監視時間外（23:00〜08:00 JST）。終了します。",
            flush=True,
        )
        return

    print(
        f"[{now():%Y-%m-%d %H:%M:%S}] "
        f"スリットアラート専用監視開始 [V45 old-theory-section-parser]",
        flush=True,
    )

    print(
        "対象場: "
        + " / ".join(TARGET_VENUES),
        flush=True,
    )

    print(
        f"通知条件: サイトの「スリットアラート」欄に"
        f"実表示あり + 締切{ALERT_WINDOW_MIN}分以内",
        flush=True,
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True
        )

        context = browser.new_context(
            http_credentials={
                "username": SHINSUM_USER,
                "password": SHINSUM_PASSWORD,
                "origin": BASE_URL.rstrip("/"),
                "send": "always",
            }
        )

        page = context.new_page()

        while active():
            print(
                f"[{now():%Y-%m-%d %H:%M:%S}] チェック",
                flush=True,
            )

            cycle(page)

            if not active():
                break

            print(
                f"{CHECK_INTERVAL}秒後に再チェック",
                flush=True,
            )

            time.sleep(
                CHECK_INTERVAL
            )

        browser.close()


if __name__ == "__main__":
    main()
