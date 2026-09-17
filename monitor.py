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
    V39:
    1) tableの「スリットアラート」列を読む
    2) tableとして取れない場合は、実際に画面に表示されている
       「シンsum理論」の文字列を艇ごとのブロックに分けて読む

    今回の実ページ例:
      3
      3385
      -0.08        ← 平均との差
      +0.1         ← スリットアラート
      1着 +7%      ← 上昇幅
      -1% ...      ← 通常の理論補正
    """
    alerts = []

    # --------------------------------------------------------
    # 1. 通常のtable構造
    # --------------------------------------------------------
    try:
        tables = page.locator("table")

        for ti in range(tables.count()):
            table = tables.nth(ti)
            rows = table.locator("tr")
            alert_col = None

            for ri in range(rows.count()):
                cells = rows.nth(ri).locator("th, td")
                texts = []
                for ci in range(cells.count()):
                    try:
                        texts.append(" ".join(cells.nth(ci).inner_text(timeout=500).split()))
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

            for ri in range(rows.count()):
                row = rows.nth(ri)
                cells = row.locator("th, td")
                if cells.count() <= alert_col:
                    continue

                cell_texts = []
                for ci in range(cells.count()):
                    try:
                        cell_texts.append(" ".join(cells.nth(ci).inner_text(timeout=500).split()))
                    except Exception:
                        cell_texts.append("")

                try:
                    row_text = " ".join(row.inner_text(timeout=700).split())
                except Exception:
                    row_text = " ".join(cell_texts)

                boat = extract_boat_number(cell_texts, row_text)
                if boat is None:
                    continue

                alert = parse_alert_cell(cell_texts[alert_col])
                if alert:
                    alert["boat"] = boat
                    alerts.append(alert)

    except Exception as e:
        print(f"table方式解析失敗: {repr(e)}", flush=True)

    # --------------------------------------------------------
    # 2. 実表示テキスト方式（今回のサイト構造用）
    # --------------------------------------------------------
    if not alerts:
        try:
            body = page.locator("body").inner_text(timeout=10000)
        except Exception:
            body = ""

        # 上部に「←シンsum理論に戻る」があるので、
        # 「場平均」が存在する本物の理論表付近を優先して切り出す。
        marker = body.rfind("シンsum理論")
        section = body[marker:] if marker >= 0 else body

        # 表の下の説明文より先だけで十分
        note = section.find("※スリットアラート")
        if note >= 0:
            section = section[:note]

        lines = [" ".join(x.split()) for x in section.splitlines() if x.strip()]

        # 艇ごとの開始位置 = 「1〜6」の単独行の直後に4桁登録番号がある箇所
        starts = []
        for i in range(len(lines) - 1):
            if re.fullmatch(r"[1-6]", lines[i]) and re.fullmatch(r"\d{4}", lines[i + 1]):
                starts.append((i, int(lines[i])))

        for n, (st, boat) in enumerate(starts):
            en = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
            block = lines[st:en]

            # 艇番・登録番号・平均との差の次から、1着/2着/3着/3連の
            # 通常補正が始まる前までにある +0.1 等だけを見る。
            # 「1着 +○%」が直後に伴うものだけアラート扱い。
            joined = "\n".join(block)

            m = re.search(
                r"(?:SUPER\s*)?([+]0\.\d+)\s*\n\s*1着\s*([+]\d+(?:\.\d+)?)\s*%",
                joined,
                re.I,
            )

            if not m:
                # SUPERが別行/記号付きでも拾えるよう少し緩める
                m = re.search(
                    r"(?:⚡\s*)?(SUPER\s*)?\n?\s*([+]0\.\d+)\s*\n\s*1着\s*([+]\d+(?:\.\d+)?)\s*%",
                    joined,
                    re.I,
                )
                if m:
                    slit = m.group(2)
                    boost = float(m.group(3))
                    is_super = bool(m.group(1)) or "SUPER" in joined.upper()
                else:
                    continue
            else:
                slit = m.group(1)
                boost = float(m.group(2))
                is_super = "SUPER" in joined.upper()

            alerts.append({
                "boat": boat,
                "super": is_super,
                "slit": slit,
                "boost": boost,
            })

        if alerts:
            print(f"表示テキスト方式で検出: {alerts}", flush=True)

    # 艇番単位で重複除去
    unique = {}
    for a in alerts:
        unique[a["boat"]] = a

    return [unique[b] for b in sorted(unique)]


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
        f"スリットアラート専用監視開始 [V39 slit-display-text-fix]",
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
