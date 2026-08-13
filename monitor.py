import os, re, time, requests
from datetime import datetime
from playwright.sync_api import sync_playwright

URL = "https://boatrace-shinsum.com/"
USER = os.environ["SHINSUM_USER"]
PW = os.environ["SHINSUM_PASSWORD"]
TOPIC = os.environ["NTFY_TOPIC"]
INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))

TARGETS = {
    "平和島","児島","戸田","多摩川","蒲郡","びわこ","三国","鳴門",
    "宮島","徳山","下関","若松","芦屋","唐津","大村","住之江"
}
ALERTS = ("やや本命", "荒れ注意")
sent = set()

def scan(page):
    lines = [x.strip() for x in page.locator("body").inner_text().splitlines() if x.strip()]
    out = []
    for i, line in enumerate(lines):
        alert = next((a for a in ALERTS if a in line), None)
        if not alert:
            continue

        ctx = " | ".join(lines[max(0, i-7):min(len(lines), i+8)])
        venue = next((v for v in TARGETS if v in ctx), None)
        if not venue:
            continue

        m = re.search(r'(?<!\d)([1-9]|1[0-2])\s*R\b', ctx, re.I)
        race = m.group(1) + "R" if m else "R不明"

        tm = re.search(r'([01]?\d|2[0-3]):[0-5]\d', ctx)
        deadline = tm.group(0) if tm else ""

        key = f"{datetime.now():%Y-%m-%d}|{venue}|{race}|{alert}"
        out.append((key, alert, venue, race, deadline))
    return out

def notify(alert, venue, race, deadline):
    symbol = "🟡" if alert == "やや本命" else "🔴"
    msg = f"{venue} {race}\n{alert}"
    if deadline:
        msg += f"\n締切 {deadline}"

    r = requests.post(
        f"https://ntfy.sh/{TOPIC}",
        data=msg.encode("utf-8"),
        headers={
            "Title": f"{symbol} {alert}｜{venue} {race}",
            "Priority": "high"
        },
        timeout=15
    )
    r.raise_for_status()

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    # HTTP Basic認証をブラウザコンテキストに設定
    context = browser.new_context(
        http_credentials={
            "username": USER,
            "password": PW
        }
    )

    page = context.new_page()

    while True:
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=30000)

            for key, alert, venue, race, deadline in scan(page):
                if key not in sent:
                    notify(alert, venue, race, deadline)
                    sent.add(key)
                    print("通知:", alert, venue, race)

        except Exception as e:
            print("ERROR:", repr(e))

        time.sleep(INTERVAL)
