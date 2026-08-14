import os,re,time,statistics
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

VENUES=["平和島","児島","戸田","多摩川","蒲郡","びわこ","三国","鳴門","宮島","徳山","下関","若松","芦屋","唐津","大村","住之江"]
BOATS=(2,3,4,5)
BUCKETS=("+0.5以上","0〜+0.5","-0.5〜0","-0.5未満")
sent={}

def now(): return datetime.now(JST)
def active(): return 8<=now().hour<23

def dl(text):
    m=re.search(r"締切\s*[：:]?\s*([01]?\d|2[0-3]):([0-5]\d)",text)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""

def near(text):
    d=dl(text)
    if not d:return False
    h,m=map(int,d.split(":"))
    t=now().replace(hour=h,minute=m,second=0,microsecond=0)
    return timedelta(minutes=-1)<=t-now()<=timedelta(minutes=10)

def venue(text):
    ms=[v for v in VENUES if v in text[:1800]]
    return ms[0] if len(ms)==1 else ""

def race(text):
    m=re.search(r"(?<!\d)([1-9]|1[0-2])\s*R\b",text[:1800],re.I)
    return m.group(1)+"R" if m else ""

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
            try:txt=a.inner_text(timeout=200) or ""
            except:pass
            try:txt+="\n"+a.locator("xpath=ancestor::*[self::div or self::td or self::li or self::section][1]").inner_text(timeout=200)
            except:pass
            if any(v in txt for v in VENUES) or re.search(r"([1-9]|1[0-2])\s*R",txt) or any(x in u.lower() for x in ("race","detail","sum")):
                out.append(u)
        except:pass
    return list(dict.fromkeys(out))

def theory(text):
    i=text.find("シンsum理論")
    if i<0:return {}
    j=text.find("シンsumチェッカー",i)
    sec=text[i:(j if j>i else i+5000)]
    ls=[x.strip() for x in sec.splitlines() if x.strip()]
    out={}
    for b in BOATS:
        for k,x in enumerate(ls):
            if x!=str(b):continue
            w="\n".join(ls[k:k+14])
            reg=re.search(r"\b(\d{4})\b",w)
            diff=re.search(r"(?<!\d)([+-]?\d+\.\d+)(?!\d)",w)
            if reg and diff:
                out[b]=(reg.group(1),float(diff.group(1)));break
    return out

def bucket(diff):
    if diff>=.5:return "+0.5以上"
    if diff>=0:return "0〜+0.5"
    if diff>=-.5:return "-0.5〜0"
    return "-0.5未満"

def click_reg(page,reg):
    try:
        x=page.get_by_text(reg,exact=True)
        if not x.count():return False
        x.first.click(timeout=3000);page.wait_for_timeout(450);return True
    except:return False

def checker(text,b):
    i=text.find("シンsumチェッカー")
    if i<0:return None
    ls=[x.strip() for x in text[i:].splitlines() if x.strip()]
    p=next((k for k,x in enumerate(ls) if f"{b}号艇" in x),None)
    if p is None:return None
    card=ls[p:p+120]; norm=[x.replace(" ","") for x in card]
    base=re.search(r"通算1着率\s*([0-9]+(?:\.[0-9]+)?)\s*%","\n".join(card))
    if not base:return None
    rows={}
    for name in BUCKETS:
        idx=next((k for k,x in enumerate(norm) if name in x),None)
        if idx is None:continue
        end=len(card)
        for q in range(idx+1,len(card)):
            if any(n in norm[q] for n in BUCKETS):
                end=q;break
        row="\n".join(card[idx:end])
        ps=re.findall(r"([+-]?\d+(?:\.\d+)?)\s*%",row)
        # 件数: bucket名の後に現れる整数を候補にする
        nums=re.findall(r"(?<![\d.])(\d{1,4})(?![\d.%])",row)
        count=int(nums[0]) if nums else 0
        if ps:rows[name]={"rise":float(ps[0]),"count":count}
    return {"base":float(base.group(1)),"rows":rows}

def strong_dynamic(chk,current_bucket):
    """固定+5/+10ではなく、その選手自身の4ゾーンと標本数で相対判定。"""
    rows=chk["rows"]
    cur=rows.get(current_bucket)
    if not cur or cur["rise"]<=0:return False,"",0
    rise=cur["rise"]; n=cur["count"]
    others=[x["rise"] for k,x in rows.items() if k!=current_bucket]
    if not others:return False,"",0
    med=statistics.median(others)
    gap=rise-med

    # 標本が多いほど小さめの差でも強い。少数標本は厳しくする。
    if n>=25:
        strong=(rise>=8 and gap>=6)
    elif n>=10:
        strong=(rise>=10 and gap>=8)
    elif n>=5:
        strong=(rise>=14 and gap>=10)
    else:
        strong=(rise>=20 and gap>=15)

    reason=f"他ゾーン中央値 {med:+.1f}% / 差 {gap:+.1f}pt / {n}件"
    return strong,reason,n

def notify(v,r,b,reg,diff,bk,base,rise,n,reason,deadline):
    body=(f"🔥 シンsum 強上昇\n{v} {r}\n{b}号艇 / 登録番号 {reg}\n"
          f"平均との差 {diff:+.2f} → {bk}\n通算1着率 {base:.1f}%\n"
          f"該当ゾーン 1着率 {rise:+.1f}%（{n}件）\n{reason}\n締切 {deadline}")
    x=requests.post(f"https://ntfy.sh/{NTFY_TOPIC}",data=body.encode("utf-8"),
                    headers={"Priority":"high","Tags":"fire"},timeout=15)
    x.raise_for_status()
    print(f"通知: {v} {r} {b}号艇 {bk} {rise:+.1f}% ({n}件)",flush=True)

def inspect(page):
    text=page.locator("body").inner_text(timeout=10000)
    v=venue(text);r=race(text);deadline=dl(text)
    if not v or not r or not deadline or not near(text):return []
    rows=theory(text);out=[]
    for b,(reg,diff) in rows.items():
        bk=bucket(diff)
        if not click_reg(page,reg):continue
        chk=checker(page.locator("body").inner_text(timeout=10000),b)
        if not chk:continue
        strong,reason,n=strong_dynamic(chk,bk)
        cur=chk["rows"].get(bk)
        if strong and cur:
            out.append((v,r,b,reg,diff,bk,chk["base"],cur["rise"],n,reason,deadline))
    return out

def cycle(page):
    us=links(page);print(f"詳細候補リンク数: {len(us)}",flush=True);hits=0
    for u in us[:100]:
        try:
            page.goto(u,wait_until="domcontentloaded",timeout=20000);page.wait_for_timeout(300)
            for x in inspect(page):
                hits+=1
                v,r,b,reg,diff,bk,base,rise,n,reason,deadline=x
                # zoneが変われば再通知可能。全く同じ判定は重複しない
                key=f"{now():%Y-%m-%d}|{v}|{r}|{b}|{bk}"
                if key in sent:continue
                notify(*x);sent[key]=True
        except Exception as e:print("確認失敗:",repr(e),flush=True)
    print(f"今回の強上昇判定: {hits}件",flush=True)

def main():
    with sync_playwright() as p:
        br=p.chromium.launch(headless=True)
        ctx=br.new_context(http_credentials={"username":USER,"password":PASSWORD})
        page=ctx.new_page()
        if not active():print("監視時間外",flush=True);return
        print(f"[{now():%Y-%m-%d %H:%M:%S}] 動的シンsumチェッカー監視開始",flush=True)
        while active():
            cycle(page)
            print(f"{CHECK_INTERVAL}秒後に再チェック",flush=True)
            time.sleep(CHECK_INTERVAL)

if __name__=="__main__":main()
