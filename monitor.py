import os,re,time
from datetime import datetime,timedelta
from urllib.parse import urljoin,urlparse
from zoneinfo import ZoneInfo
import requests
from playwright.sync_api import sync_playwright

BASE_URL="https://boatrace-shinsum.com/"
USER=os.environ["SHINSUM_USER"]
PASSWORD=os.environ["SHINSUM_PASSWORD"]
NTFY_TOPIC=os.environ["NTFY_TOPIC"]
CHECK_INTERVAL=int(os.getenv("CHECK_INTERVAL","120"))
JST=ZoneInfo("Asia/Tokyo")

TARGET_VENUES=["平和島","児島","戸田","多摩川","蒲郡","びわこ","三国","鳴門","宮島","徳山","下関","若松","芦屋","唐津","大村","住之江"]
ALERT_TYPES=("やや本命","荒れ注意")
seen=set()

def now(): return datetime.now(JST)
def active(): return 8 <= now().hour < 23

def notify(alert,venue,race,deadline):
    s="🟡" if alert=="やや本命" else "🔴"
    body=f"{s} {alert}\n{venue} {race}\n締切 {deadline}"
    x=requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={"Priority":"high","Tags":"ship"},
        timeout=15
    )
    x.raise_for_status()
    print(f"通知送信: {alert} / {venue} / {race} / 締切 {deadline}",flush=True)

def deadline(text):
    m=re.search(r"締切\s*[：:]?\s*([01]?\d|2[0-3]):([0-5]\d)",text)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""

def within15(text):
    d=deadline(text)
    if not d:return False
    h,m=map(int,d.split(":"))
    t=now().replace(hour=h,minute=m,second=0,microsecond=0)
    return timedelta(minutes=-1) <= t-now() <= timedelta(minutes=15)

def ready(text):
    i=text.find("シンsum理論")
    if i<0:return False
    j=text.find("シンsumチェッカー",i)
    sec=text[i:(j if j>i else i+3500)]
    return re.search(r"(?<!\d)[+-]?\d+\.\d+(?!\d)",sec) is not None

def candidate_links(page):
    page.goto(BASE_URL,wait_until="domcontentloaded",timeout=30000)
    page.wait_for_timeout(1000)
    host=urlparse(BASE_URL).netloc
    out=[]
    aa=page.locator("a")
    for i in range(aa.count()):
        a=aa.nth(i)
        try:
            href=a.get_attribute("href")
            if not href or href.startswith("#") or href.startswith("javascript:"): continue
            full=urljoin(BASE_URL,href)
            if urlparse(full).netloc!=host: continue
            txt=""
            try: txt=a.inner_text(timeout=250) or ""
            except: pass
            try:
                txt+="\n"+a.locator("xpath=ancestor::*[self::div or self::td or self::li or self::section][1]").inner_text(timeout=250)
            except: pass
            if (any(v in txt for v in TARGET_VENUES)
                or re.search(r"([1-9]|1[0-2])\s*R",txt)
                or "race" in full.lower()
                or "detail" in full.lower()
                or "sum" in full.lower()):
                out.append(full)
        except: pass
    return list(dict.fromkeys(out))

def actual_venue(text):
    head=text[:1800]
    m=[v for v in TARGET_VENUES if v in head]
    return m[0] if len(m)==1 else ""

def actual_race(text):
    m=re.search(r"(?<!\d)([1-9]|1[0-2])\s*R\b",text[:1800],re.I)
    return m.group(1)+"R" if m else ""

def alert_near_deadline(text,d):
    pos=-1
    for p in (f"締切 {d}",f"締切{d}",f"締切：{d}",f"締切: {d}"):
        pos=text.find(p)
        if pos>=0: break
    if pos<0:return None
    local=text[max(0,pos-250):min(len(text),pos+350)]
    return next((a for a in ALERT_TYPES if a in local),None)

def inspect(page):
    text=page.locator("body").inner_text(timeout=10000)
    v=actual_venue(text)
    r=actual_race(text)
    if not v or not r:return None
    if not within15(text):return None
    if not ready(text):return None
    d=deadline(text)
    a=alert_near_deadline(text,d)
    if not a:return None
    return {"venue":v,"race":r,"deadline":d,"alert":a,
            "key":f"{now():%Y-%m-%d}|{v}|{r}|{a}"}

def cycle(page,initial=False):
    links=candidate_links(page)
    print(f"詳細候補リンク数: {len(links)}",flush=True)
    current={}
    for u in links[:100]:
        try:
            page.goto(u,wait_until="domcontentloaded",timeout=20000)
            page.wait_for_timeout(350)
            item=inspect(page)
            if item:
                current[item["key"]]=item
        except Exception as e:
            print("詳細ページ確認失敗:",u,repr(e),flush=True)

    if initial:
        seen.update(current.keys())
        print(f"初期既読登録: {len(current)}件",flush=True)
        for x in current.values():
            print(f"既読: {x['alert']} / {x['venue']} / {x['race']} / 締切 {x['deadline']}",flush=True)
        return

    new=[x for k,x in current.items() if k not in seen]
    if not new:
        print("新規判定なし",flush=True)
    for x in new:
        notify(x["alert"],x["venue"],x["race"],x["deadline"])
    seen.update(current.keys())

def main():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        c=b.new_context(http_credentials={"username":USER,"password":PASSWORD})
        page=c.new_page()

        if not active():
            print("監視時間外（23:00〜08:00 JST）。終了します。",flush=True)
            return

        print(f"[{now():%Y-%m-%d %H:%M:%S}] 荒れ注意/やや本命監視開始",flush=True)
        cycle(page,initial=True)

        while active():
            print(f"{CHECK_INTERVAL}秒後に再チェック",flush=True)
            time.sleep(CHECK_INTERVAL)
            print(f"[{now():%Y-%m-%d %H:%M:%S}] 再チェック",flush=True)
            cycle(page,initial=False)

if __name__=="__main__":
    main()
