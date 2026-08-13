import os, re, time, requests
from datetime import datetime
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

BASE_URL = "https://boatrace-shinsum.com/"
USER = os.environ["SHINSUM_USER"]
PASSWORD = os.environ["SHINSUM_PASSWORD"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))
JST = ZoneInfo("Asia/Tokyo")

TARGET_VENUES = [
    "平和島","児島","戸田","多摩川","蒲郡","びわこ","三国","鳴門",
    "宮島","徳山","下関","若松","芦屋","唐津","大村","住之江"
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
    title = f"{symbol} {alert}｜{venue} {race}"
    body = f"{venue} {race}\n{alert}"
    if deadline:
        body += f"\n締切 {deadline}"
    r = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": "high", "Tags": "ship"},
        timeout=15,
    )
    r.raise_for_status()
    print("通知送信:", alert, venue, race, deadline or "", flush=True)

def parse_and_notify(text):
    alert = next((a for a in ALERT_TYPES if a in text), None)
    venue = next((v for v in TARGET_VENUES if v in text), None)
    rm = re.search(r'(?<!\d)([1-9]|1[0-2])\s*R\b', text, re.I)
    tm = re.search(r'締切\s*([01]?\d|2[0-3]):[0-5]\d', text)
    if not (alert and venue and rm):
        return
    race = rm.group(1) + "R"
    deadline = tm.group(1) if tm else ""
    key = f"{now_jst():%Y-%m-%d}|{venue}|{race}|{alert}"
    if key in sent:
        return
    send_ntfy(alert, venue, race, deadline)
    sent.add(key)

def scan_page(page):
    text = page.locator("body").inner_text(timeout=10000)
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    for i, line in enumerate(lines):
        if any(a in line for a in ALERT_TYPES):
            ctx = "\n".join(lines[max(0, i-12):min(len(lines), i+13)])
            parse_and_notify(ctx)

def candidate_links(page):
    base_host = urlparse(BASE_URL).netloc
    out = []
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
            nearby = ""
            try:
                nearby = a.locator("xpath=ancestor::*[self::div or self::td or self::li][1]").inner_text(timeout=300)
            except:
                try:
                    nearby = a.inner_text(timeout=300)
                except:
                    pass
            relevant = (
                any(v in nearby for v in TARGET_VENUES)
                or re.search(r'([1-9]|1[0-2])\s*R', nearby)
                or "シンsum" in nearby
                or "sum" in full.lower()
                or "race" in full.lower()
            )
            if relevant:
                out.append(full)
        except:
            pass
    return list(dict.fromkeys(out))

def one_cycle(page):
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1000)
    print(f"[{now_jst():%Y-%m-%d %H:%M:%S}] トップ確認", flush=True)
    scan_page(page)
    links = candidate_links(page)
    print("候補リンク数:", len(links), flush=True)
    for url in links[:80]:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(400)
            scan_page(page)
        except Exception as e:
            print("詳細ページ失敗:", url, repr(e), flush=True)

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
            time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
