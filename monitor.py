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

ALERT_TYPES = ("やや本命", "荒れ注意")
sent = set()

def now_jst():
    return datetime.now(JST)

def active_hours():
    h = now_jst().hour
    return 8 <= h < 23

def send_ntfy(alert, venue, race, deadline):
    symbol = "🟡" if alert == "やや本命" else "🔴"
    body = f"{symbol} {alert}\n{venue} {race}"
    if deadline:
        body += f"\n締切 {deadline}"

    r = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={"Priority": "high", "Tags": "ship"},
        timeout=15
    )
    r.raise_for_status()
    print(f"通知送信: {alert} / {venue} / {race} / 締切 {deadline or '不明'}", flush=True)

def extract_deadline(text):
    m = re.search(r"締切\s*[：:]?\s*([01]?\d|2[0-3]):([0-5]\d)", text)
    return f"{m.group(1)}:{m.group(2)}" if m else ""

def extract_race(text):
    m = re.search(r"(?<!\d)([1-9]|1[0-2])\s*R\b", text, re.I)
    return m.group(1) + "R" if m else ""

def extract_venue(text):
    return next((v for v in TARGET_VENUES if v in text), "")

def shinsum_ready(text):
    normalized = re.sub(r"\s+", " ", text)
    if any(alert in normalized for alert in ALERT_TYPES):
        return True
    if "シンsum理論" in normalized:
        m = re.search(r"シンsum理論.{0,60}?[-+]?\d+(?:\.\d+)?", normalized)
        if m:
            return True
    return False

def parse_alert_from_context(text):
    alert = next((a for a in ALERT_TYPES if a in text), None)
    if not alert:
        return None

    venue = extract_venue(text)
    race = extract_race(text)
    deadline = extract_deadline(text)

    if not venue or not race:
        return None

    return alert, venue, race, deadline

def scan_page(page):
    text = page.locator("body").inner_text(timeout=10000)

    if not shinsum_ready(text):
        print("シンsum理論: 未更新", flush=True)
        return

    lines = [x.strip() for x in text.splitlines() if x.strip()]

    for i, line in enumerate(lines):
        if not any(alert in line for alert in ALERT_TYPES):
            continue

        context = "\n".join(lines[max(0, i - 18): min(len(lines), i + 19)])
        parsed = parse_alert_from_context(context)
        if not parsed:
            continue

        alert, venue, race, deadline = parsed
        key = f"{now_jst():%Y-%m-%d}|{venue}|{race}|{alert}"

        if key in sent:
            continue

        send_ntfy(alert, venue, race, deadline)
        sent.add(key)

def candidate_links(page):
    base_host = urlparse(BASE_URL).netloc
    found = []
    anchors = page.locator("a")

    for i in range(anchors.count()):
        a = anchors.nth(i)
        try:
            href = a.get_attribute("href")
            if not href or href.startswith("#") or href.startswith("javascript:"):
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

    return list(dict.fromkeys(found))

def one_cycle(page):
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1000)

    print(f"[{now_jst():%Y-%m-%d %H:%M:%S}] 監視開始", flush=True)
    scan_page(page)

    links = candidate_links(page)
    print(f"詳細候補リンク数: {len(links)}", flush=True)

    for url in links[:80]:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(400)
            scan_page(page)
        except Exception as e:
            print("詳細ページ確認失敗:", url, repr(e), flush=True)

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            http_credentials={"username": USER, "password": PASSWORD}
        )
        page = context.new_page()

        while True:
            if not active_hours():
                print("監視時間外（23:00〜08:00 JST）。終了します。", flush=True)
                break

            try:
                one_cycle(page)
            except Exception as e:
                print("監視エラー:", repr(e), flush=True)

            print(f"{CHECK_INTERVAL}秒後に再チェック", flush=True)
            time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
