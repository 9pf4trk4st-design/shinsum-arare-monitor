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
seen=set(); today_open=set()

def now(): return datetime.now(JST)
def active(): return 8 <= now().hour < 23

def notify(a,v,r,d):
    s="🟡" if a=="やや本命" else "🔴"
    body=f"{s} {a}\n{v} {r}"+(f"\n締切 {d}" if d else "")
    x=requests.post(f"https://ntfy.sh/{NTFY_TOPIC}",data=body.encode("utf-8"),headers={"Priority":"high","Tags":"ship"},timeout=15)
    x.raise_for_status()
    print(f"通知送信: {a} / {v} / {r} / 締切 {d}",flush=True)

def deadline(text):
    m=re.search(r"締切\s*[：:]?\s*([01]?\d|2[0-3]):([0-5]\d)",text)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""

def race(text):
    m=re.search(r"(?<!\d)([1-9]|1[0-2])\s*R\b",text,re.I)
    return m.group(1)+"R" if m else ""

def venue(text):
    return next((v for v in TARGET_VENUES if v in text),"")

def near_deadline(d):
    if not d: return False
    h,m=map(int,d.split(":"))
    t=now().replace(hour=h,minute=m,second=0,microsecond=0)
    delta=t-now()
    return timedelta(minutes=-1) <= delta <= timedelta(minutes=15)

def shinsum_ready(text):
    i=text.find("シンsum理論")
    if i<0: return False
    sec=text[i:i+2500]
    # must contain a decimal value in the theory section
    if not re.search(r"[-+]?\d+\.\d+",sec): return False
    # reject obvious all-placeholder section
    if re.search(r"場平均\s*[:：]?\s*[-ー—]\b",sec): return False
    return True

def parse_detail(text):
    v=venue(text); r=race(text); d=deadline(text); a=next((x for x in ALERT_TYPES if x in text),None)
    if not (v and r and d and a): return None
    if v not in today_open: return None
    if not shinsum_ready(text): return None
    if not near_deadline(d): return None
    return {"key":f"{now():%Y-%m-%d}|{v}|{r}|{a}","venue":v,"race":r,"deadline":d,"alert":a}

def detect_open(page):
    page.goto(BASE_URL,wait_until="domcontentloaded",timeout=30000); page.wait_for_timeout(1000)
    text=page.locator("body").inner_text(timeout=10000)
    lines=[x.strip() for x in text.splitlines() if x.strip()]
    start=next((i for i,x in enumerate(lines) if "本日の開催場" in x),0)
    area="\n".join(lines[start:start+120])
    s={v for v in TARGET_VENUES if v in area}
    print("本日開催場:",sorted(s),flush=True)
    return s

def links(page):
    base=urlparse(BASE_URL).netloc; out=[]; aa=page.locator("a")
    for i in range(aa.count()):
        a=aa.nth(i)
        try:
            href=a.get_attribute("href")
            if not href or href.startswith("#") or href.startswith("javascript:"): continue
            full=urljoin(BASE_URL,href)
            if urlparse(full).netloc!=base: continue
            txt=""
            try: txt=a.inner_text(timeout=250) or ""
            except: pass
            try: txt+="\n"+a.locator("xpath=ancestor::*[self::div or self::td or self::li or self::section][1]").inner_text(timeout=250)
            except: pass
            if any(v in txt for v in today_open) or re.search(r"([1-9]|1[0-2])\s*R",txt) or "race" in full.lower() or "detail" in full.lower():
                out.append(full)
        except: pass
    return list(dict.fromkeys(out))

def collect(page):
    out={}
    page.goto(BASE_URL,wait_until="domcontentloaded",timeout=30000); page.wait_for_timeout(800)
    ls=links(page); print(f"詳細候補リンク数: {len(ls)}",flush=True)
    for u in ls[:100]:
        try:
            page.goto(u,wait_until="domcontentloaded",timeout=20000); page.wait_for_timeout(350)
            item=parse_detail(page.locator("body").inner_text(timeout=10000))
            if item: out[item["key"]]=item
        except Exception as e: print("詳細ページ確認失敗:",u,repr(e),flush=True)
    return out

def main():
    global today_open
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        c=b.new_context(http_credentials={"username":USER,"password":PASSWORD})
        page=c.new_page()
        if not active():
            print("監視時間外（23:00〜08:00 JST）。終了します。",flush=True); return
        print(f"[{now():%Y-%m-%d %H:%M:%S}] 監視開始",flush=True)
        today_open=detect_open(page)
        if not today_open:
            print("本日開催場を取得できなかったため終了します。",flush=True); return
        cur=collect(page); seen.update(cur.keys())
        print(f"初期既読登録: {len(cur)}件",flush=True)
        while active():
            print(f"{CHECK_INTERVAL}秒後に再チェック",flush=True); time.sleep(CHECK_INTERVAL)
            print(f"[{now():%Y-%m-%d %H:%M:%S}] 再チェック",flush=True)
            cur=collect(page)
            new=[x for k,x in cur.items() if k not in seen]
            if not new: print("新規判定なし",flush=True)
            for x in new: notify(x["alert"],x["venue"],x["race"],x["deadline"])
            seen.update(cur.keys())

if __name__=="__main__": main()
