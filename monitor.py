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
    トップページからレース詳細候補URLを収集。

    V37:
    <a href> だけでなく、
    form/action・data-href・data-url・onclick・HTML内URLも探索する。
    サイト側がボタン/JS遷移に変わっていても拾えるようにする。
    """

    page.goto(
        BASE_URL,
        wait_until="domcontentloaded",
        timeout=30000,
    )
    page.wait_for_timeout(1500)

    host = urlparse(BASE_URL).netloc
    raw_candidates = []

    # --------------------------------------------------------
    # 1. DOM属性からURL候補を回収
    # --------------------------------------------------------
    selectors_and_attrs = (
        ("a[href]", "href"),
        ("[data-href]", "data-href"),
        ("[data-url]", "data-url"),
        ("form[action]", "action"),
        ("button[formaction]", "formaction"),
    )

    for selector, attr in selectors_and_attrs:
        loc = page.locator(selector)

        for i in range(loc.count()):
            el = loc.nth(i)

            try:
                value = el.get_attribute(attr)
            except Exception:
                continue

            if value:
                raw_candidates.append(value)

    # --------------------------------------------------------
    # 2. onclick 内のURL候補
    # --------------------------------------------------------
    onclicks = page.locator("[onclick]")

    for i in range(onclicks.count()):
        el = onclicks.nth(i)

        try:
            value = el.get_attribute("onclick") or ""
        except Exception:
            continue

        # location='...'
        # location.href='...'
        # window.location='...'
        # open('...')
        for m in re.finditer(
            r"""(?:
                location(?:\.href)?\s*=\s*
                |
                window\.location(?:\.href)?\s*=\s*
                |
                open\s*\(
            )
            ['"]([^'"]+)['"]""",
            value,
            re.I | re.X,
        ):
            raw_candidates.append(m.group(1))

    # --------------------------------------------------------
    # 3. HTMLソース内に埋め込まれた内部URLも拾う
    # --------------------------------------------------------
    try:
        html = page.content()
    except Exception:
        html = ""

    # 絶対URL
    for m in re.finditer(
        r"""https?://boatrace-shinsum\.com/[^\s"'<>]+""",
        html,
        re.I,
    ):
        raw_candidates.append(m.group(0))

    # 相対URL
    for m in re.finditer(
        r"""["'](/[^"'<> ]+)["']""",
        html,
        re.I,
    ):
        raw_candidates.append(m.group(1))

    # --------------------------------------------------------
    # 4. 正規化・同一ドメイン限定
    # --------------------------------------------------------
    normalized = []

    for value in raw_candidates:
        if not value:
            continue

        value = value.strip()

        if value.startswith(
            ("#", "javascript:", "mailto:", "tel:")
        ):
            continue

        full = urljoin(
            BASE_URL,
            value
        )

        parsed = urlparse(full)

        if parsed.netloc != host:
            continue

        low = full.lower()

        if re.search(
            r"\.(?:jpg|jpeg|png|gif|svg|css|js|ico|pdf|woff2?)(?:\?|$)",
            low
        ):
            continue

        if any(
            x in low
            for x in (
                "/logout",
                "/login",
                "/privacy",
                "/terms",
            )
        ):
            continue

        # トップページそのものは除外
        if (
            parsed.path in ("", "/")
            and not parsed.query
        ):
            continue

        normalized.append(full)

    normalized = list(
        dict.fromkeys(normalized)
    )

    print(
        f"内部URL候補総数: {len(normalized)}",
        flush=True,
    )

    # --------------------------------------------------------
    # 5. レース詳細っぽいURLを優先
    # --------------------------------------------------------
    preferred = []

    for full in normalized:
        low = full.lower()

        if any(
            word in low
            for word in (
                "race",
                "detail",
                "shinsum",
                "sum",
                "prediction",
                "race_no",
                "raceno",
            )
        ):
            preferred.append(full)

    # URL名だけで判別できないサイト構造もあるため、
    # preferred が0件なら同一ドメインの内部URLを全部確認する。
    result = (
        preferred
        if preferred
        else normalized
    )

    return result[:250]


# ============================================================
# スリットアラート解析
# ============================================================

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
    「スリットアラート」列をヘッダから特定し、
    その列だけを解析する。

    理論欄・平均との差・チェッカー等は一切使わない。
    """

    alerts = []

    tables = page.locator("table")

    for ti in range(tables.count()):
        table = tables.nth(ti)
        rows = table.locator("tr")

        alert_col = None

        # まずヘッダから「スリットアラート」の列番号を取得
        for ri in range(rows.count()):
            row = rows.nth(ri)
            cells = row.locator("th, td")

            texts = []

            for ci in range(cells.count()):
                try:
                    texts.append(
                        " ".join(
                            cells.nth(ci).inner_text(timeout=500).split()
                        )
                    )
                except Exception:
                    texts.append("")

            for ci, txt in enumerate(texts):
                if "スリットアラート" in txt:
                    alert_col = ci
                    break

            if alert_col is not None:
                break

        if alert_col is None:
            continue

        # 同じ表の各艇行を確認
        for ri in range(rows.count()):
            row = rows.nth(ri)
            cells = row.locator("th, td")

            if cells.count() <= alert_col:
                continue

            cell_texts = []

            for ci in range(cells.count()):
                try:
                    cell_texts.append(
                        " ".join(
                            cells.nth(ci).inner_text(timeout=500).split()
                        )
                    )
                except Exception:
                    cell_texts.append("")

            try:
                row_text = " ".join(
                    row.inner_text(timeout=700).split()
                )
            except Exception:
                row_text = " ".join(cell_texts)

            boat = extract_boat_number(
                cell_texts,
                row_text
            )

            if boat is None:
                continue

            alert = parse_alert_cell(
                cell_texts[alert_col]
            )

            if not alert:
                continue

            alert["boat"] = boat
            alerts.append(alert)

    # 艇番単位の重複除去
    unique = {}

    for a in alerts:
        unique[a["boat"]] = a

    return [
        unique[b]
        for b in sorted(unique)
    ]


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

    # トップページ自体にレース詳細が描画される構成にも対応
    try:
        inspect_race_page(page)
    except Exception:
        pass

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

    for url in links[:250]:
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
        f"スリットアラート専用監視開始 [V37 route-fix]",
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
