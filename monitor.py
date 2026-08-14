import os,re,time
from datetime import datetime,timedelta
from urllib.parse import urljoin,urlparse
from zoneinfo import ZoneInfo
import requests
from playwright.sync_api import sync_playwright

BASE_URL="https://boatrace-shinsum.com/"
USER=os.environ["SHINSUM_USER"]; PASSWORD=os.environ["SHINSUM_PASSWORD"]; NTFY_TOPIC=os.environ["NTFY_TOPIC"]
CHECK_INTERVAL=int(os.getenv("CHECK_INTERVAL","120")); JST=ZoneInfo("Asia/Tokyo")
TARGET_VENUES=["平和島","児島","戸田","多摩川","蒲郡","びわこ","三国","鳴門","宮島","徳山","下関","若松","芦屋","唐津","大村","住之江"]
ALERT_TYPES=("やや本命","荒れ注意")
seen=set(); venue_urls={}

def now(): return datetime.now(JST)
def active(): return 8<=now().hour<23

def notify(a,v,r,d):
    s="🟡" if a=="やや本命" else "🔴"
    body=f"{s} {a}\n{v} {r}\n締切 {d}"
    x=requests.post(f"https://ntfy.sh/{NTFY_TOPIC}",data=body.encode(),headers={"Priority":"high","Tags":"ship"},timeout=15)
    x.raise_for_status(); print(f"通知送信: {a} / {v} / {r} / 締切 {d}",flush=True)

def get_deadline(text):
    m=re.search(r"締切\s*[：:]?\s*([01]?\d|2[0-3]):([0-5]\d)",text)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""

def near15(d):
    if not d:return False
    h,m=map(int,d.split(":")); t=now().replace(hour=h,minute=m,second=0,microsecond=0)
    x=t-now(); return timedelta(minutes=-1)<=x<=timedelta(minutes=15)

def shinsum_ready(text):
    i=text.find("シンsum理論")
    if i<0:return False
    j=text.find("シンsumチェッカー",i)
    sec=text[i:(j if j>i else min(len(text),i+3500))]
    return re.search(r"(?<!\d)[+-]?\d+\.\d+(?!\d)",sec) is not None

def alert_near_deadline(text,d):
    pos=-1
    for p in (f"締切 {d}",f"締切{d}",f"締切：{d}",f"締切: {d}"):
        pos=text.find(p)
        if pos>=0:break
    if pos<0:return None
    local=text[max(0,pos-250):min(len(text),pos+350)]
    return next((a for a in ALERT_TYPES if a in local),None)

def discover_venues(page):
    page.goto(BASE_URL,wait_until="domcontentloaded",timeout=30000); page.wait_for_timeout(1000)
    found={}; aa=page.locator("a"); host=urlparse(BASE_URL).netloc
    for i in range(aa.count()):
        a=aa.nth(i)
        try:
            href=a.get_attribute("href")
            if not href:continue
            full=urljoin(BASE_URL,href)
            if urlparse(full).netloc!=host:continue
            txt=""
            try: txt=a.inner_text(timeout=200) or ""
            except: pass
            try: txt+="\n"+a.locator("xpath=ancestor::*[self::div or self::td or self::li or self::section][1]").inner_text(timeout=200)
            except: pass
            for v in TARGET_VENUES:
                if v in txt and v not in found: found[v]=full
        except: pass
    print("本日開催場URL:",sorted(found),flush=True); return found

def open_race(page,url,n):
    page.goto(url,wait_until="domcontentloaded",timeout=30000); page.wait_for_timeout(400)
    rt=f"{n}R"; aa=page.locator("a")
    for i in range(aa.count()):
        a=aa.nth(i)
        try:
            if (a.inner_text(timeout=120) or "").strip()!=rt:continue
            href=a.get_attribute("href")
            if href:
                page.goto(urljoin(page.url,href),wait_until="domcontentloaded",timeout=20000); page.wait_for_timeout(300); return True
        except: pass
    try:
        loc=page.get_by_text(rt,exact=True)
        if loc.count()>0:
            loc.first.click(timeout=3000); page.wait_for_timeout(400); return True
    except: pass
    return False

def inspect(page,v,n):
    text=page.locator("body").inner_text(timeout=10000); d=get_deadline(text)
    if not d or not near15(d) or not shinsum_ready(text): return None
    a=alert_near_deadline(text,d)
    if not a:return None
    r=f"{n}R"; k=f"{now():%Y-%m-%d}|{v}|{r}|{a}"
    return {"key":k,"venue":v,"race":r,"deadline":d,"alert":a}

def collect(page):
    out={}
    for v,u in venue_urls.items():
        for n in range(1,13):
            try:
                if not open_race(page,u,n):continue
                item=inspect(page,v,n)
                if item:out[item["key"]]=item
            except Exception as e: print(f"確認失敗: {v} {n}R / {repr(e)}",flush=True)
    print(f"現在の条件成立レース: {len(out)}件",flush=True); return out

def main():
    global venue_urls
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        c=b.new_context(http_credentials={"username":USER,"password":PASSWORD}); page=c.new_page()
        if not active(): print("監視時間外（23:00〜08:00 JST）。終了します。",flush=True); return
        print(f"[{now():%Y-%m-%d %H:%M:%S}] 監視開始",flush=True)
        venue_urls=discover_venues(page)
        if not venue_urls: print("本日の開催場URLを取得できませんでした。",flush=True); return
        cur=collect(page); seen.update(cur.keys()); print(f"初期既読登録: {len(cur)}件",flush=True)
        while active():
            print(f"{CHECK_INTERVAL}秒後に再チェック",flush=True); time.sleep(CHECK_INTERVAL)
            print(f"[{now():%Y-%m-%d %H:%M:%S}] 再チェック",flush=True)
            cur=collect(page); new=[x for k,x in cur.items() if k not in seen]
            if not new: print("新規判定なし",flush=True)
            for x in new: notify(x["alert"],x["venue"],x["race"],x["deadline"])
            seen.update(cur.keys())

if __name__=="__main__": main()
