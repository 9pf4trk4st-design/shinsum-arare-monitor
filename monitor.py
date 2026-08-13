import os
import re
import time
from datetime import datetime
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
    "平和島", "児島", "戸田", "多摩川", "蒲郡", "びわこ", "三国", "鳴門",
    "宮島", "徳山", "下関", "若松", "芦屋", "唐津", "大村", "住之江"
]

# 通知する判定はこの2つだけ
ALERT_TYPES = ("やや本命", "荒れ注意")

# この実行中に一度でも確認した判定
# 起動直後に既に出ている判定もここへ入れ、通知しない
ever_seen = set()


def now_jst():
    return datetime.now(JST)


def active_hours():
    # 23:00〜08:00は監視しない
    h = now_jst().hour
    return 8 <= h < 23


def send_ntfy(alert, venue, race, deadline):
    symbol = "🟡" if alert == "やや本命" else "🔴"

    # 日本語・絵文字はHTTPヘッダーではなく本文に入れる
    body = f"{symbol} {alert}\n{venue} {race}"
    if deadline:
        body += f"\n締切 {deadline}"

    r = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={
            "Priority": "high",
            "Tags": "ship"
        },
        timeout=15
    )
    r.raise_for_status()

    print(
        f"通知送信: {alert} / {venue} / {race} / 締切 {deadline or '不明'}",
        flush=True
    )


def extract_deadline(text):
    m = re.search(r"締切\s*[：:]?\s*([01]?\d|2[0-3]):([0-5]\d)", text)
    if not m:
        return ""
    return f"{m.group(1)}:{m.group(2)}"


def extract_race(text):
    m = re.search(r"(?<!\d)([1-9]|1[0-2])\s*R\b", text, re.I)
    if not m:
        return ""
    return m.group(1) + "R"


def extract_venue(text):
    return next((v for v in TARGET_VENUES if v in text), "")


def make_key(venue, race, alert):
    return f"{now_jst():%Y-%m-%d}|{venue}|{race}|{alert}"


def parse_alert_from_context(text):
    alert = next((a for a in ALERT_TYPES if a in text), None)
    if not alert:
        return None

    venue = extract_venue(text)
    race = extract_race(text)
    deadline = extract_deadline(text)

    if not venue or not race:
        return None

    return {
        "key": make_key(venue, race, alert),
        "alert": alert,
        "venue": venue,
        "race": race,
        "deadline": deadline,
    }


def collect_alerts_from_page(page):
    """
    現在ページに表示されている
    「やや本命」「荒れ注意」を収集する。
    ここでは通知しない。
    """
    results = {}

    try:
        text = page.locator("body").inner_text(timeout=10000)
    except Exception as e:
        print("本文取得失敗:", repr(e), flush=True)
        return results

    lines = [x.strip() for x in text.splitlines() if x.strip()]

    for i, line in enumerate(lines):
        if not any(alert in line for alert in ALERT_TYPES):
            continue

        # 判定の前後を広めに取り、場・R・締切も拾う
        context = "\n".join(
            lines[max(0, i - 18): min(len(lines), i + 19)]
        )

        parsed = parse_alert_from_context(context)
        if not parsed:
            continue

        results[parsed["key"]] = parsed

    return results


def candidate_links(page):
    """
    トップページから対象場・レース詳細らしい内部リンクを集める。
    """
    base_host = urlparse(BASE_URL).netloc
    found = []

    anchors = page.locator("a")

    for i in range(anchors.count()):
        a = anchors.nth(i)

        try:
            href = a.get_attribute("href")

            if not href:
                continue
            if href.startswith("#") or href.startswith("javascript:"):
                continue

            full = urljoin(BASE_URL, href)

            if urlparse(full).netloc != base_host:
                continue

            text = ""

            try:
                text = a.inner_text(timeout=300) or ""
            except:
                pass

            try:
                parent_text = a.locator(
                    "xpath=ancestor::*[self::div or self::td or self::li or self::section][1]"
                ).inner_text(timeout=300)
                text += "\n" + parent_text
            except:
                pass

            relevant = (
                any(v in text for v in TARGET_VENUES)
                or re.search(r"([1-9]|1[0-2])\s*R", text)
                or "シンsum" in text
                or "race" in full.lower()
                or "sum" in full.lower()
                or "detail" in full.lower()
            )

            if relevant:
                found.append(full)

        except:
            pass

    # 順序を保ったまま重複削除
    return list(dict.fromkeys(found))


def collect_all_current_alerts(page):
    """
    トップ＋詳細候補ページを巡回して、
    今この瞬間に表示されている通知対象判定を全部集める。
    """
    all_alerts = {}

    page.goto(
        BASE_URL,
        wait_until="domcontentloaded",
        timeout=30000
    )
    page.wait_for_timeout(1000)

    # トップページ
    all_alerts.update(collect_alerts_from_page(page))

    # 詳細ページ
    links = candidate_links(page)
    print(f"詳細候補リンク数: {len(links)}", flush=True)

    for url in links[:80]:
        try:
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=20000
            )
            page.wait_for_timeout(400)

            page_alerts = collect_alerts_from_page(page)
            all_alerts.update(page_alerts)

        except Exception as e:
            print(
                "詳細ページ確認失敗:",
                url,
                repr(e),
                flush=True
            )

    return all_alerts


def initialize_baseline(page):
    """
    起動時点ですでに出ている判定は通知しない。
    既読として登録する。
    """
    current = collect_all_current_alerts(page)

    ever_seen.update(current.keys())

    print(
        f"初期既読登録: {len(current)}件 "
        "（起動時点で既に出ている判定は通知しません）",
        flush=True
    )

    for item in current.values():
        print(
            f"既読: {item['alert']} / {item['venue']} / "
            f"{item['race']} / 締切 {item['deadline'] or '不明'}",
            flush=True
        )


def check_for_new_alerts(page):
    """
    2回目以降。
    起動後に新しく出現した判定だけ通知する。
    """
    current = collect_all_current_alerts(page)

    new_keys = [
        key for key in current.keys()
        if key not in ever_seen
    ]

    if not new_keys:
        print("新規判定なし", flush=True)
    else:
        for key in new_keys:
            item = current[key]

            send_ntfy(
                item["alert"],
                item["venue"],
                item["race"],
                item["deadline"]
            )

    # 一度でも確認した判定は、消えて再表示されても再通知しない
    ever_seen.update(current.keys())


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # Safariで表示されたユーザ名/パスワード方式に対応
        context = browser.new_context(
            http_credentials={
                "username": USER,
                "password": PASSWORD
            }
        )

        page = context.new_page()

        if not active_hours():
            print(
                "監視時間外（23:00〜08:00 JST）。終了します。",
                flush=True
            )
            return

        print(
            f"[{now_jst():%Y-%m-%d %H:%M:%S}] 監視開始",
            flush=True
        )

        # 起動時点に出ているものは通知しない
        try:
            initialize_baseline(page)
        except Exception as e:
            print("初期化エラー:", repr(e), flush=True)
            return

        while active_hours():
            print(
                f"{CHECK_INTERVAL}秒後に再チェック",
                flush=True
            )
            time.sleep(CHECK_INTERVAL)

            try:
                print(
                    f"[{now_jst():%Y-%m-%d %H:%M:%S}] 再チェック",
                    flush=True
                )
                check_for_new_alerts(page)

            except Exception as e:
                print(
                    "監視エラー:",
                    repr(e),
                    flush=True
                )

        print(
            "23:00になったため監視を終了します。",
            flush=True
        )


if __name__ == "__main__":
    main()
