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
sent=set()

def now(): return datetime.now(JST)
def active(): return 8 <= now().hour < 23

def notify(v,r,b,reg,base,diff,shift,direction):
    sym="📈" if direction=="UP" else "⚠️"
    label="1着率上昇" if direction=="UP" else "1号艇 1着率低下"
    body=(f"{sym} {label}\n{v} {r}\n{b}号艇 / 登録番号 {reg}\n"
          f"通算1着率 {base:.1f}%\n平均との差 {diff:+.2f}\n1着率変化 {shift:+.1f}%")
    x=requests.post(f"https://ntfy.sh/{NTFY_TOPIC}",data=body.encode("utf-8"),
                    headers={"Priority":"high","Tags":"ship"},timeout=15)
    x.raise_for_status()
    print(f"通知送信: {v} {r} / {b}号艇 / {shift:+.1f}%",flush=True)

def deadline(text):
    m=re.search(r"締切\s*[：:]?\s*([01]?\d|2[0-3]):([0-5]\d)",text)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""

def within20(text):
    d=deadline(text)
    if not d:return False
    h,m=map(int,d.split(":"))
    t=now().replace(hour=h,minute=m,second=0,microsecond=0)
    return timedelta(minutes=-1) <= t-now() <= timedelta(minutes=20)

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
            if (any(v in txt for v in TARGET_VENUES) or
                re.search(r"([1-9]|1[0-2])\s*R",txt) or
                "race" in full.lower() or "detail" in full.lower() or "sum" in full.lower()):
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

def parse_boats(text):
    i=text.find("シンsum理論")
    if i<0:return {}
    j=text.find("シンsumチェッカー",i)
    sec=text[i:(j if j>i else i+3500)]
    lines=[x.strip() for x in sec.splitlines() if x.strip()]
    out={}
    for b in range(1,7):
        for k,line in enumerate(lines):
            if line!=str(b): continue
            w="\n".join(lines[k:k+12])
            reg=re.search(r"\b(\d{4})\b",w)
            diff=re.search(r"(?<!\d)([+-]?\d+\.\d+)(?!\d)",w)
            if reg and diff:
                out[b]={"reg":reg.group(1),"diff":float(diff.group(1))}
                break
    return out

def click_reg(page,reg):
    try:
        x=page.get_by_text(reg,exact=True)
        if not x.count(): return False
        x.first.click(timeout=3000); page.wait_for_timeout(500)
        return True
    except: return False

def parse_checker(text,b,diff):
    i=text.find("シンsumチェッカー")
    if i<0:return None
    lines=[x.strip() for x in text[i:].splitlines() if x.strip()]
    pos=next((k for k,x in enumerate(lines) if f"{b}号艇" in x),None)
    if pos is None:return None
    card=lines[pos:pos+90]
    joined="\n".join(card)
    mb=re.search(r"通算1着率\s*([0-9]+(?:\.[0-9]+)?)\s*%",joined)
    if not mb:return None
    if diff>=0.5: rg="+0.5以上"
    elif diff>=0: rg="0〜+0.5"
    elif diff>=-0.5: rg="-0.5〜0"
    else: rg="-0.5未満"
    idx=next((k for k,x in enumerate(card) if rg in x.replace(" ","")),None)
    if idx is None:return None
    row="\n".join(card[idx:idx+12])
    vals=re.findall(r"([+-]?\d+(?:\.\d+)?)\s*%",row)
    if not vals:return None
    return float(mb.group(1)), float(vals[0])

def inspect(page):
    text=page.locator("body").inner_text(timeout=10000)
    v=actual_venue(text); r=actual_race(text)
    if not v or not r or not within20(text) or not ready(text): return []
    boats=parse_boats(text)
    picks=[]
    for b,info in boats.items():
        if not click_reg(page,info["reg"]): continue
        c=parse_checker(page.locator("body").inner_text(timeout=10000),b,info["diff"])
        if not c: continue
        base,shift=c
        direction="DOWN" if b==1 and shift<0 else ("UP" if b>=2 and shift>0 else None)
        if direction:
            picks.append((v,r,b,info["reg"],base,info["diff"],shift,direction))
    return picks

def cycle(page):
    links=candidate_links(page)
    print(f"詳細候補リンク数: {len(links)}",flush=True)
    for u in links[:100]:
        try:
            page.goto(u,wait_until="domcontentloaded",timeout=20000); page.wait_for_timeout(350)
            for v,r,b,reg,base,diff,shift,direction in inspect(page):
                key=f"{now():%Y-%m-%d}|{v}|{r}|{b}|{direction}"
                if key in sent: continue
                notify(v,r,b,reg,base,diff,shift,direction)
                sent.add(key)
        except Exception as e:
            print("詳細ページ確認失敗:",u,repr(e),flush=True)

def main():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        c=b.new_context(http_credentials={"username":USER,"password":PASSWORD})
        page=c.new_page()
        if not active():
            print("監視時間外",flush=True); return
        print(f"[{now():%Y-%m-%d %H:%M:%S}] 1着率変動監視開始",flush=True)
        while active():
            cycle(page)
            print(f"{CHECK_INTERVAL}秒後に再チェック",flush=True)
            time.sleep(CHECK_INTERVAL)

if __name__=="__main__": main()
