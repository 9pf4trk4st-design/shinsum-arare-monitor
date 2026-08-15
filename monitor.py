import os,re,time,csv
from datetime import datetime,timedelta
from pathlib import Path
from urllib.parse import urljoin,urlparse
from zoneinfo import ZoneInfo
import requests
from playwright.sync_api import sync_playwright

BASE_URL="https://boatrace-shinsum.com/"
USER=os.environ["SHINSUM_USER"]; PASSWORD=os.environ["SHINSUM_PASSWORD"]; NTFY_TOPIC=os.environ["NTFY_TOPIC"]
CHECK_INTERVAL=int(os.getenv("CHECK_INTERVAL","120")); JST=ZoneInfo("Asia/Tokyo")
VENUES=["平和島","児島","戸田","多摩川","蒲郡","びわこ","三国","鳴門","宮島","徳山","下関","若松","芦屋","唐津","大村","住之江"]
ALERTS=("やや本命","荒れ注意")
FOCUS=(1,2,3,4)
MIN={1:8.0,2:9.0,3:10.0,4:10.0}
seen=set(); LOG=Path("logs/results.csv")

def now(): return datetime.now(JST)
def active(): return 8<=now().hour<23

def deadline(t):
    m=re.search(r"締切\s*[：:]?\s*([01]?\d|2[0-3]):([0-5]\d)",t)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""

def near15(t):
    d=deadline(t)
    if not d:return False
    h,m=map(int,d.split(":")); x=now().replace(hour=h,minute=m,second=0,microsecond=0)-now()
    return timedelta(minutes=-1)<=x<=timedelta(minutes=15)

def venue(t):
    ms=[v for v in VENUES if v in t[:1800]]
    return ms[0] if len(ms)==1 else ""

def race(t):
    m=re.search(r"(?<!\d)([1-9]|1[0-2])\s*R\b",t[:1800],re.I)
    return m.group(1)+"R" if m else ""

def alert(t,d):
    pos=-1
    for p in (f"締切 {d}",f"締切{d}",f"締切：{d}",f"締切: {d}"):
        pos=t.find(p)
        if pos>=0:break
    if pos<0:return None
    local=t[max(0,pos-300):min(len(t),pos+450)]
    return next((a for a in ALERTS if a in local),None)

def links(page):
    page.goto(BASE_URL,wait_until="domcontentloaded",timeout=30000); page.wait_for_timeout(900)
    host=urlparse(BASE_URL).netloc; out=[]; aa=page.locator("a")
    for i in range(aa.count()):
        a=aa.nth(i)
        try:
            href=a.get_attribute("href")
            if not href or href.startswith("#") or href.startswith("javascript:"):continue
            u=urljoin(BASE_URL,href)
            if urlparse(u).netloc!=host:continue
            txt=""
            try: txt=a.inner_text(timeout=200) or ""
            except: pass
            try: txt+="\n"+a.locator("xpath=ancestor::*[self::div or self::td or self::li or self::section][1]").inner_text(timeout=200)
            except: pass
            if any(v in txt for v in VENUES) or re.search(r"([1-9]|1[0-2])\s*R",txt) or any(x in u.lower() for x in ("race","detail","sum")): out.append(u)
        except: pass
    return list(dict.fromkeys(out))

def theory(t):
    i=t.find("シンsum理論")
    if i<0:return {}
    j=t.find("シンsumチェッカー",i); sec=t[i:(j if j>i else i+6500)]
    ls=[x.strip() for x in sec.splitlines() if x.strip()]; out={}
    for b in range(1,7):
        for k,x in enumerate(ls):
            if x!=str(b):continue
            w="\n".join(ls[k:k+18])
            diff=re.search(r"(?<!\d)([+-]?\d+\.\d+)(?!\d)",w)
            p=re.findall(r"([+-]?\d+(?:\.\d+)?)\s*%",w)
            if diff and p:
                out[b]={"diff":float(diff.group(1)),"first":float(p[0])}; break
    return out

def classify(th):
    if len(th)<6:return None
    vals={b:th[b]["first"] for b in range(1,7)}
    order=sorted(vals,key=vals.get,reverse=True); best,second=order[0],order[1]
    if best not in FOCUS:return None
    bv,sv=vals[best],vals[second]; gap=bv-sv
    if bv<MIN[best] or gap<5:return None
    if best==1 and bv-max(vals[3],vals[4])<7:return None
    if best in (2,3,4) and bv-vals[1]<7:return None
    return {"boat":best,"type":f"{best}号艇有利","value":bv,"second":second,"second_value":sv,"gap":gap}

def rank(c):
    if c["gap"]>=12 and c["value"]>=18:return "S"
    if c["gap"]>=8 and c["value"]>=12:return "A"
    return "B"

def logrow(a,v,r,d,th,c,rank_):
    LOG.parent.mkdir(parents=True,exist_ok=True); new=not LOG.exists()
    with LOG.open("a",newline="",encoding="utf-8-sig") as f:
        w=csv.writer(f)
        if new:w.writerow(["通知日時","場","R","元判定","選別判定","ランク","1号艇","2号艇","3号艇","4号艇","5号艇","6号艇","締切"])
        w.writerow([now().strftime("%Y-%m-%d %H:%M:%S"),v,r,a,c["type"],rank_,th[1]["first"],th[2]["first"],th[3]["first"],th[4]["first"],th[5]["first"],th[6]["first"],d])

def notify(a,v,r,d,th,c,rank_):
    syms={1:"🟦",2:"🟨",3:"🟧",4:"🟥"}; b=c["boat"]
    lines=[f"{x}号艇 {th[x]['first']:+g}% / 差 {th[x]['diff']:+.2f}"+(" ←注目" if x==b else "") for x in range(1,7)]
    body=f"{syms[b]} {c['type']}【{rank_}】\n{v} {r}【{a}】\n\nシンsum理論\n"+"\n".join(lines)+f"\n\n{b}号艇が6艇中トップ\n2番手の{c['second']}号艇より {c['gap']:+.1f}pt\n締切 {d}"
    x=requests.post(f"https://ntfy.sh/{NTFY_TOPIC}",data=body.encode("utf-8"),headers={"Priority":"high","Tags":"ship"},timeout=15)
    x.raise_for_status(); logrow(a,v,r,d,th,c,rank_)
    print(f"通知送信: {c['type']} / {rank_} / {a} / {v} {r}",flush=True)

def inspect(page):
    t=page.locator("body").inner_text(timeout=10000); v,r=venue(t),race(t)
    if not v or not r or not near15(t):return None
    d=deadline(t); a=alert(t,d)
    if not a:return None
    th=theory(t)
    if len(th)<6:return None
    c=classify(th)
    if not c:return None
    rk=rank(c)
    return {"v":v,"r":r,"d":d,"a":a,"th":th,"c":c,"rk":rk,"key":f"{now():%Y-%m-%d}|{v}|{r}|{a}|{c['type']}|{rk}"}

def cycle(page,initial=False):
    us=links(page); print(f"詳細候補リンク数: {len(us)}",flush=True); cur={}
    for u in us[:100]:
        try:
            page.goto(u,wait_until="domcontentloaded",timeout=20000); page.wait_for_timeout(300)
            item=inspect(page)
            if item:cur[item["key"]]=item
        except Exception as e:print("詳細ページ確認失敗:",repr(e),flush=True)
    if initial:
        seen.update(cur.keys()); print(f"初期既読登録: {len(cur)}件",flush=True); return
    new=[x for k,x in cur.items() if k not in seen]
    if not new:print("新規選別判定なし",flush=True)
    for x in new:notify(x["a"],x["v"],x["r"],x["d"],x["th"],x["c"],x["rk"])
    seen.update(cur.keys())

def main():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True); c=b.new_context(http_credentials={"username":USER,"password":PASSWORD}); page=c.new_page()
        if not active(): print("監視時間外（23:00〜08:00 JST）。終了します。",flush=True); return
        print(f"[{now():%Y-%m-%d %H:%M:%S}] やや本命/荒れ注意 → 1〜4号艇選別監視開始",flush=True)
        cycle(page,True)
        while active():
            print(f"{CHECK_INTERVAL}秒後に再チェック",flush=True); time.sleep(CHECK_INTERVAL)
            cycle(page,False)

if __name__=="__main__":main()
