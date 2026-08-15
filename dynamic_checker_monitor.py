import os,re,time,statistics
from datetime import datetime,timedelta
from urllib.parse import urljoin,urlparse
from zoneinfo import ZoneInfo
import requests
from playwright.sync_api import sync_playwright

# ------------------------------
# 基本設定
# ------------------------------
SHINSUM_URL="https://boatrace-shinsum.com/"
BIYORI_URL="https://kyoteibiyori.com/race_shusso.php"

USER=os.environ["SHINSUM_USER"]
PASSWORD=os.environ["SHINSUM_PASSWORD"]
NTFY_TOPIC=os.environ["NTFY_TOPIC"]
CHECK_INTERVAL=int(os.getenv("CHECK_INTERVAL","120"))

JST=ZoneInfo("Asia/Tokyo")

VENUE_CODES={
    "戸田":2,"平和島":4,"多摩川":5,"蒲郡":7,"三国":10,"びわこ":11,
    "住之江":12,"鳴門":14,"児島":16,"宮島":17,"徳山":18,"下関":19,
    "若松":20,"芦屋":21,"唐津":23,"大村":24
}
TARGET_BOATS=(2,3,4,5)
BUCKETS=("+0.5以上","0〜+0.5","-0.5〜0","-0.5未満")
sent=set()

def now():
    return datetime.now(JST)

def active():
    return 8 <= now().hour < 23

def race_date():
    return now().strftime("%Y%m%d")

def deadline(text):
    m=re.search(r"締切\s*[：:]?\s*([01]?\d|2[0-3]):([0-5]\d)",text)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""

def within10(text):
    d=deadline(text)
    if not d:return False
    h,m=map(int,d.split(":"))
    t=now().replace(hour=h,minute=m,second=0,microsecond=0)
    return timedelta(minutes=-1) <= t-now() <= timedelta(minutes=10)

def actual_venue(text):
    ms=[v for v in VENUE_CODES if v in text[:1800]]
    return ms[0] if len(ms)==1 else ""

def actual_race(text):
    m=re.search(r"(?<!\d)([1-9]|1[0-2])\s*R\b",text[:1800],re.I)
    return int(m.group(1)) if m else None

# ------------------------------
# シンsum側
# ------------------------------
def shinsum_links(page):
    page.goto(SHINSUM_URL,wait_until="domcontentloaded",timeout=30000)
    page.wait_for_timeout(900)
    host=urlparse(SHINSUM_URL).netloc
    out=[]
    aa=page.locator("a")
    for i in range(aa.count()):
        a=aa.nth(i)
        try:
            href=a.get_attribute("href")
            if not href or href.startswith("#") or href.startswith("javascript:"):continue
            u=urljoin(SHINSUM_URL,href)
            if urlparse(u).netloc!=host:continue
            txt=""
            try:txt=a.inner_text(timeout=200) or ""
            except:pass
            try:
                txt+="\n"+a.locator("xpath=ancestor::*[self::div or self::td or self::li or self::section][1]").inner_text(timeout=200)
            except:pass
            if any(v in txt for v in VENUE_CODES) or re.search(r"([1-9]|1[0-2])\s*R",txt) or any(x in u.lower() for x in ("race","detail","sum")):
                out.append(u)
        except:pass
    return list(dict.fromkeys(out))

def theory_rows(text):
    i=text.find("シンsum理論")
    if i<0:return {}
    j=text.find("シンsumチェッカー",i)
    sec=text[i:(j if j>i else i+5000)]
    ls=[x.strip() for x in sec.splitlines() if x.strip()]
    out={}
    for b in TARGET_BOATS:
        for k,x in enumerate(ls):
            if x!=str(b):continue
            w="\n".join(ls[k:k+14])
            reg=re.search(r"\b(\d{4})\b",w)
            diff=re.search(r"(?<!\d)([+-]?\d+\.\d+)(?!\d)",w)
            if reg and diff:
                out[b]={"reg":reg.group(1),"diff":float(diff.group(1))}
                break
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
        x.first.click(timeout=3000)
        page.wait_for_timeout(450)
        return True
    except:return False

def checker_data(text,b):
    i=text.find("シンsumチェッカー")
    if i<0:return None
    ls=[x.strip() for x in text[i:].splitlines() if x.strip()]
    p=next((k for k,x in enumerate(ls) if f"{b}号艇" in x),None)
    if p is None:return None
    card=ls[p:p+120]
    norm=[x.replace(" ","") for x in card]
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
        pcts=re.findall(r"([+-]?\d+(?:\.\d+)?)\s*%",row)
        nums=re.findall(r"(?<![\d.])(\d{1,4})(?![\d.%])",row)
        count=int(nums[0]) if nums else 0
        if pcts:
            rows[name]={"rise":float(pcts[0]),"count":count}
    return {"base":float(base.group(1)),"rows":rows}

def strong_dynamic(chk,bk):
    cur=chk["rows"].get(bk)
    if not cur or cur["rise"]<=0:return False,"",0
    rise=cur["rise"]; n=cur["count"]
    others=[x["rise"] for k,x in chk["rows"].items() if k!=bk]
    if not others:return False,"",0
    med=statistics.median(others)
    gap=rise-med
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

# ------------------------------
# 競艇日和 ST順位
# ------------------------------
def biyori_url(venue,race_no):
    return f"{BIYORI_URL}?place_no={VENUE_CODES[venue]}&race_no={race_no}&hiduke={race_date()}&slider=0"

def parse_float_cell(s):
    s=(s or "").strip()
    m=re.search(r"\d+(?:\.\d+)?",s)
    return float(m.group()) if m else None

def day_type(body_text):
    # 開催日の判定。ページ内に「最終日」があれば最終日を優先。
    head=body_text[:2500]
    if "最終日" in head:
        return "final"
    if "初日" in head:
        return "first"
    return "middle"

def f_holder_for_boat(body_text,boat):
    # 各艇ブロック近辺に F1/F2 がある場合のみF持ちとみなす
    lines=[x.strip() for x in body_text.splitlines() if x.strip()]
    for i,x in enumerate(lines):
        if x==str(boat) or x.startswith(f"{boat}号艇"):
            local=" ".join(lines[max(0,i-4):i+18])
            return bool(re.search(r"\bF[1-9]\b",local))
    return False

def parse_st_table(page):
    """
    ST順位を含むtableを探し、ヘッダ名と艇別の数値を取得。
    戻り値: {boat: {"初日":3.0,"当地":2.57,"F持":...,"最終日":...}}
    """
    result={}
    tables=page.locator("table")
    for ti in range(tables.count()):
        table=tables.nth(ti)
        try:
            txt=table.inner_text(timeout=500)
        except:
            continue
        if "ST順位" not in txt:
            continue
        rows=table.locator("tr")
        matrix=[]
        for ri in range(rows.count()):
            cells=rows.nth(ri).locator("th,td")
            vals=[]
            for ci in range(cells.count()):
                try: vals.append((cells.nth(ci).inner_text(timeout=200) or "").strip())
                except: vals.append("")
            if vals: matrix.append(vals)

        # ヘッダを初日/当地/F持/最終日が多く含まれる行から作る
        header_idx=None
        for i,row in enumerate(matrix):
            joined="|".join(row)
            if "当地" in joined and ("初日" in joined or "最終日" in joined or "F持" in joined):
                header_idx=i
                break
        if header_idx is None:
            continue
        headers=matrix[header_idx]

        # 列名の位置
        col={}
        for name in ("初日","当地","F持","最終日"):
            for i,h in enumerate(headers):
                if name in h:
                    col[name]=i
                    break

        # 艇番を含む各行を読む
        for row in matrix[header_idx+1:]:
            joined=" ".join(row)
            bm=re.search(r"(^|\s)([1-6])(?:号艇)?($|\s)",joined)
            if not bm:
                # 行頭セルが1〜6なら艇番扱い
                if row and row[0].strip() in list("123456"):
                    boat=int(row[0].strip())
                else:
                    continue
            else:
                boat=int(bm.group(2))
            vals={}
            for name,idx in col.items():
                if idx<len(row):
                    vals[name]=parse_float_cell(row[idx])
            if vals:
                result[boat]=vals

        if result:
            break

    # tableで取れない場合の保険：本文からラベル近傍を拾う
    if not result:
        body=page.locator("body").inner_text(timeout=10000)
        lines=[x.strip() for x in body.splitlines() if x.strip()]
        labels=[x for x in ("初日","当地","F持","最終日") if x in body]
        # このフォールバックは誤取得防止のため、取れなければ空のままにする
    return result

def composite_st(st,day,f_holder):
    vals=[]
    if day=="first":
        for k in ("初日","当地"):
            if st.get(k) is not None: vals.append(st[k])
    elif day=="final":
        for k in ("最終日","当地"):
            if st.get(k) is not None: vals.append(st[k])
    else:
        if st.get("当地") is not None: vals.append(st["当地"])

    if f_holder and st.get("F持") is not None:
        vals.append(st["F持"])

    return statistics.mean(vals) if vals else None

def st_assessment(page,boat):
    body=page.locator("body").inner_text(timeout=10000)
    day=day_type(body)
    table=parse_st_table(page)
    if boat not in table:
        return None

    holder=f_holder_for_boat(body,boat)
    score=composite_st(table[boat],day,holder)
    if score is None:return None

    # 6艇内順位
    scores={}
    for b,st in table.items():
        h=f_holder_for_boat(body,b)
        sc=composite_st(st,day,h)
        if sc is not None:scores[b]=sc
    rank=1+sum(1 for sc in scores.values() if sc<score)

    inner_score=scores.get(boat-1) if boat>1 else None
    inner_adv=(inner_score-score) if inner_score is not None else None

    return {
        "score":score,"rank":rank,"f_holder":holder,"day":day,
        "inner_score":inner_score,"inner_adv":inner_adv,
        "raw":table[boat]
    }

def chance_level(st):
    """
    ST順位は小さいほど良い。
    高チャンス: 総合2.8以下かつ、内艇より0.15以上優勢（2号艇は順位2位以内で代用）
    チャンス: 総合3.5以下かつ6艇中3位以内
    """
    if st is None:return "CHECK","ST取得できず"
    if st["score"]<=2.8:
        if st["inner_adv"] is None:
            if st["rank"]<=2:return "HIGH","ST総合が上位"
        elif st["inner_adv"]>=0.15:
            return "HIGH",f"内艇より {st['inner_adv']:.2f} 優勢"
    if st["score"]<=3.5 and st["rank"]<=3:
        return "CHANCE","ST総合が上位"
    return "WEAK","ST優位性は弱め"

# ------------------------------
# 通知
# ------------------------------
def notify(v,r,b,reg,diff,bk,base,rise,n,reason,deadline,st,level,st_reason):
    if level=="HIGH":
        head="🔥 高チャンス"
    elif level=="CHANCE":
        head="🟡 チャンス"
    else:
        head="📌 シンsum強上昇（ST要確認）"

    ftxt="F持ち" if st and st["f_holder"] else "Fなし"
    sttxt="取得不可"
    if st:
        sttxt=f"{st['score']:.2f} / 6艇中{st['rank']}位"
        if st["inner_adv"] is not None:
            sttxt+=f" / 内艇差 {st['inner_adv']:+.2f}"

    body=(
        f"{head}\n{v} {r} / {b}号艇\n"
        f"シンsum: {bk} → 1着率 {rise:+.1f}%（{n}件）\n"
        f"平均との差 {diff:+.2f} / 通算1着率 {base:.1f}%\n"
        f"ST評価: {sttxt}\n{ftxt} / {st_reason}\n"
        f"{reason}\n締切 {deadline}"
    )

    x=requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={"Priority":"high","Tags":"fire"},
        timeout=15
    )
    x.raise_for_status()
    print(f"通知: {head} {v} {r} {b}号艇",flush=True)

# ------------------------------
# 1レース判定
# ------------------------------
def inspect(page):
    text=page.locator("body").inner_text(timeout=10000)
    v=actual_venue(text)
    race_no=actual_race(text)
    d=deadline(text)
    if not v or not race_no or not d or not within10(text):
        return []

    rows=theory_rows(text)
    out=[]

    for b,info in rows.items():
        reg=info["reg"]; diff=info["diff"]; bk=bucket(diff)

        if not click_reg(page,reg):
            continue

        chk=checker_data(page.locator("body").inner_text(timeout=10000),b)
        if not chk:
            continue

        strong,reason,n=strong_dynamic(chk,bk)
        cur=chk["rows"].get(bk)
        if not strong or not cur:
            continue

        # 競艇日和を別タブで確認
        st=None
        try:
            with page.context.expect_page(timeout=1000) as _:
                pass
        except:
            pass

        st_page=page.context.new_page()
        try:
            st_page.goto(biyori_url(v,race_no),wait_until="domcontentloaded",timeout=20000)
            st_page.wait_for_timeout(600)
            st=st_assessment(st_page,b)
        except Exception as e:
            print("ST取得失敗:",v,race_no,b,repr(e),flush=True)
        finally:
            st_page.close()

        level,st_reason=chance_level(st)

        # シンsum強上昇なら通知。STが強ければ高チャンスに格上げ。
        out.append({
            "v":v,"r":f"{race_no}R","b":b,"reg":reg,"diff":diff,"bk":bk,
            "base":chk["base"],"rise":cur["rise"],"n":n,"reason":reason,
            "deadline":d,"st":st,"level":level,"st_reason":st_reason
        })

    return out

def cycle(page):
    us=shinsum_links(page)
    print(f"詳細候補リンク数: {len(us)}",flush=True)
    hits=0
    for u in us[:100]:
        try:
            page.goto(u,wait_until="domcontentloaded",timeout=20000)
            page.wait_for_timeout(300)
            for x in inspect(page):
                hits+=1
                key=f"{now():%Y-%m-%d}|{x['v']}|{x['r']}|{x['b']}|{x['bk']}|{x['level']}"
                if key in sent:continue
                notify(
                    x["v"],x["r"],x["b"],x["reg"],x["diff"],x["bk"],x["base"],
                    x["rise"],x["n"],x["reason"],x["deadline"],x["st"],x["level"],x["st_reason"]
                )
                sent.add(key)
        except Exception as e:
            print("確認失敗:",repr(e),flush=True)
    print(f"今回のシンsum強上昇候補: {hits}件",flush=True)

def main():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        ctx=browser.new_context(
            http_credentials={"username":USER,"password":PASSWORD}
        )
        page=ctx.new_page()
        if not active():
            print("監視時間外（23:00〜08:00 JST）",flush=True)
            return
        print(f"[{now():%Y-%m-%d %H:%M:%S}] シンsum×ST総合監視開始",flush=True)
        while active():
            cycle(page)
            print(f"{CHECK_INTERVAL}秒後に再チェック",flush=True)
            time.sleep(CHECK_INTERVAL)

if __name__=="__main__":
    main()
