import os
import re
import time
import statistics
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo
import requests
from playwright.sync_api import sync_playwright

SHINSUM_URL = "https://boatrace-shinsum.com/"
BIYORI_URL = "https://kyoteibiyori.com/race_shusso.php"
USER = os.environ["SHINSUM_USER"]
PASSWORD = os.environ["SHINSUM_PASSWORD"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))
JST = ZoneInfo("Asia/Tokyo")

VENUE_CODES = {
    "戸田":2,"平和島":4,"多摩川":5,"蒲郡":7,"三国":10,"びわこ":11,
    "住之江":12,"鳴門":14,"児島":16,"宮島":17,"徳山":18,"下関":19,
    "若松":20,"芦屋":21,"唐津":23,"大村":24,
}
TARGET_BOATS=(2,3,4,5)
BUCKETS=("+0.5以上","0〜+0.5","-0.5〜0","-0.5未満")
sent=set()

def now_jst(): return datetime.now(JST)
def active_hours(): return 8 <= now_jst().hour < 23
def race_date(): return now_jst().strftime("%Y%m%d")

def extract_deadline(text):
    m=re.search(r"締切\s*[：:]?\s*([01]?\d|2[0-3]):([0-5]\d)", text)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""

def within_10_minutes(text):
    d=extract_deadline(text)
    if not d: return False
    hh,mm=map(int,d.split(':'))
    t=now_jst().replace(hour=hh,minute=mm,second=0,microsecond=0)
    return timedelta(minutes=-1) <= t-now_jst() <= timedelta(minutes=10)

def actual_venue(text):
    ms=[v for v in VENUE_CODES if v in text[:1800]]
    return ms[0] if len(ms)==1 else ""

def actual_race(text):
    m=re.search(r"(?<!\d)([1-9]|1[0-2])\s*R\b", text[:1800], re.I)
    return int(m.group(1)) if m else None

def shinsum_links(page):
    page.goto(SHINSUM_URL,wait_until='domcontentloaded',timeout=30000)
    page.wait_for_timeout(900)
    host=urlparse(SHINSUM_URL).netloc
    out=[]; aa=page.locator('a')
    for i in range(aa.count()):
        a=aa.nth(i)
        try:
            href=a.get_attribute('href')
            if not href or href.startswith('#') or href.startswith('javascript:'): continue
            full=urljoin(SHINSUM_URL,href)
            if urlparse(full).netloc!=host: continue
            nearby=''
            try: nearby=a.inner_text(timeout=200) or ''
            except: pass
            try: nearby+='\n'+a.locator("xpath=ancestor::*[self::div or self::td or self::li or self::section][1]").inner_text(timeout=200)
            except: pass
            if any(v in nearby for v in VENUE_CODES) or re.search(r"([1-9]|1[0-2])\s*R",nearby) or any(x in full.lower() for x in ('race','detail','sum')):
                out.append(full)
        except: pass
    return list(dict.fromkeys(out))

def parse_theory_rows(text):
    start=text.find('シンsum理論')
    if start<0: return {}
    end=text.find('シンsumチェッカー',start)
    sec=text[start:(end if end>start else start+5000)]
    lines=[x.strip() for x in sec.splitlines() if x.strip()]
    out={}
    for boat in TARGET_BOATS:
        for i,line in enumerate(lines):
            if line!=str(boat): continue
            w='\n'.join(lines[i:i+16])
            reg=re.search(r"\b(\d{4})\b",w)
            diff=re.search(r"(?<!\d)([+-]?\d+\.\d+)(?!\d)",w)
            pcts=re.findall(r"([+-]?\d+(?:\.\d+)?)\s*%",w)
            if reg and diff:
                out[boat]={'reg_no':reg.group(1),'diff':float(diff.group(1)),'theory_1st':float(pcts[0]) if pcts else None}
                break
    return out

def bucket_for_diff(diff):
    if diff>=.5: return '+0.5以上'
    if diff>=0: return '0〜+0.5'
    if diff>=-.5: return '-0.5〜0'
    return '-0.5未満'

def click_registration(page,reg_no):
    try:
        loc=page.get_by_text(reg_no,exact=True)
        if loc.count()==0: return False
        loc.first.click(timeout=3000); page.wait_for_timeout(450); return True
    except: return False

def parse_checker(text,boat):
    start=text.find('シンsumチェッカー')
    if start<0: return None
    lines=[x.strip() for x in text[start:].splitlines() if x.strip()]
    pos=next((i for i,x in enumerate(lines) if f'{boat}号艇' in x),None)
    if pos is None: return None
    card=lines[pos:pos+120]; norm=[x.replace(' ','') for x in card]
    base=re.search(r"通算1着率\s*([0-9]+(?:\.[0-9]+)?)\s*%",'\n'.join(card))
    if not base: return None
    rows={}
    for name in BUCKETS:
        idx=next((i for i,x in enumerate(norm) if name in x),None)
        if idx is None: continue
        end=len(card)
        for j in range(idx+1,len(card)):
            if any(n in norm[j] for n in BUCKETS): end=j; break
        row='\n'.join(card[idx:end])
        pcts=re.findall(r"([+-]?\d+(?:\.\d+)?)\s*%",row)
        nums=re.findall(r"(?<![\d.])(\d{1,4})(?![\d.%])",row)
        if pcts: rows[name]={'rise_1st':float(pcts[0]),'count':int(nums[0]) if nums else 0}
    return {'base_rate':float(base.group(1)),'rows':rows}

def strong_dynamic(checker,current_bucket):
    cur=checker['rows'].get(current_bucket)
    if not cur: return False,'',0
    rise=cur['rise_1st']; count=cur['count']
    if rise<=0: return False,'',count
    others=[r['rise_1st'] for k,r in checker['rows'].items() if k!=current_bucket]
    if not others: return False,'',count
    med=statistics.median(others); gap=rise-med
    if count>=25: strong=rise>=8 and gap>=6
    elif count>=10: strong=rise>=10 and gap>=8
    elif count>=5: strong=rise>=10 and gap>=10
    else: strong=rise>=20 and gap>=15
    return strong,f"他ゾーン中央値 {med:+.1f}% / 差 {gap:+.1f}pt / {count}件",count

def biyori_url(venue,race_no):
    return f"{BIYORI_URL}?place_no={VENUE_CODES[venue]}&race_no={race_no}&hiduke={race_date()}&slider=0"

def parse_st_from_text(page):
    body=page.locator('body').inner_text(timeout=10000)
    lines=[x.strip() for x in body.splitlines() if x.strip()]
    st_idx=next((i for i,x in enumerate(lines) if x.strip()=='ST順位'),None)
    if st_idx is None: return {},body
    end=next((i for i in range(st_idx+1,len(lines)) if '展示順位' in lines[i]),min(len(lines),st_idx+120))
    section=lines[st_idx:end]
    labels=('直近3ヶ月','直近1ヶ月','当地','初日','最終日','ナイター','F持')
    result={}
    for idx,line in enumerate(section):
        label=next((lb for lb in labels if line==lb or line.startswith(lb)),None)
        if not label: continue
        vals=[]
        for raw in section[idx+1:idx+15]:
            if any(raw==lb or raw.startswith(lb) for lb in labels): break
            for n in re.findall(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)",raw):
                f=float(n)
                if 0 <= f <= 9: vals.append(f)
            if len(vals)>=6: break
        if len(vals)>=6: result[label]={b:vals[b-1] for b in range(1,7)}
    return result,body

def detect_day_type(body):
    if '最終日' in body: return 'final'
    if '初日' in body: return 'first'
    return 'middle'

def is_f_holder(body,boat):
    # 出走表本文にF1/F2があれば艇ごとの近辺から判定。取れない時はFalse。
    lines=[x.strip() for x in body.splitlines() if x.strip()]
    # 選手番号/艇番ブロックの正確なDOM差に備え広めの近傍を見る
    for i,line in enumerate(lines):
        if line==f'{boat}号艇' or line==str(boat):
            local=' '.join(lines[max(0,i-6):i+30])
            if re.search(r"\bF[1-9]\b",local): return True
    return False

def composite_st(st_rows,body,boat):
    day=detect_day_type(body); holder=is_f_holder(body,boat); vals=[]
    def add(label):
        v=st_rows.get(label,{}).get(boat)
        if v is not None: vals.append(v)
    if day=='first': add('初日'); add('当地')
    elif day=='final': add('最終日'); add('当地')
    else: add('当地')
    if holder: add('F持')
    return (statistics.mean(vals) if vals else None),holder,day

def st_assessment(page,boat):
    rows,body=parse_st_from_text(page)
    if not rows: return None
    scores={}; holders={}; day='middle'
    for b in range(1,7):
        sc,h,day=composite_st(rows,body,b); holders[b]=h
        if sc is not None: scores[b]=sc
    if boat not in scores: return None
    score=scores[boat]; rank=1+sum(1 for s in scores.values() if s<score)
    inner=scores.get(boat-1) if boat>1 else None
    return {'score':score,'rank':rank,'f_holder':holders.get(boat,False),'day':day,'inner_advantage':(inner-score if inner is not None else None)}

def chance_level(st):
    if st is None: return 'CHECK','ST取得できず'
    if st['score']<=2.8:
        if st['inner_advantage'] is None and st['rank']<=2: return 'HIGH','ST総合が上位'
        if st['inner_advantage'] is not None and st['inner_advantage']>=0.15: return 'HIGH',f"内艇より {st['inner_advantage']:.2f} 優勢"
    if st['score']<=3.5 and st['rank']<=3: return 'CHANCE','ST総合が上位'
    return 'WEAK','ST優位性は弱め'

def combine_level(theory_1st,checker_strong,checker_rise,st_level):
    theory_strong=theory_1st is not None and theory_1st>=15
    theory_very=theory_1st is not None and theory_1st>=20
    checker_very=checker_strong and checker_rise>=15
    if st_level=='HIGH' and (theory_strong or checker_strong): return 'SUPER'
    if theory_very and checker_very: return 'SUPER'
    if theory_strong and checker_strong: return 'HIGH'
    if checker_strong or theory_strong: return 'CHANCE'
    return 'NONE'

def send_notification(p):
    title={'SUPER':'🔥🔥 超高チャンス','HIGH':'🔥 高チャンス','CHANCE':'🟡 チャンス'}[p['final_level']]
    st=p['st']; st_text='取得不可'; f_text='F不明'
    if st:
        st_text=f"{st['score']:.2f} / 6艇中{st['rank']}位"
        if st['inner_advantage'] is not None: st_text+=f" / 内艇差 {st['inner_advantage']:+.2f}"
        f_text='F持ち' if st['f_holder'] else 'Fなし'
    theory_text=f"{p['theory_1st']:+.1f}%" if p['theory_1st'] is not None else '取得不可'
    body=(f"{title}\n{p['venue']} {p['race_label']} / {p['boat']}号艇\n登録番号 {p['reg_no']}\n"
          f"平均との差 {p['diff']:+.2f}\nシンsum理論 1着 {theory_text}\n該当ゾーン {p['bucket_name']}\n"
          f"通算1着率 {p['base_rate']:.1f}%\nチェッカー1着変化 {p['checker_rise']:+.1f}%（{p['count']}件）\n"
          f"ST評価 {st_text}\n{f_text} / {p['st_reason']}\n{p['dynamic_reason']}\n締切 {p['deadline_value']}")
    r=requests.post(f"https://ntfy.sh/{NTFY_TOPIC}",data=body.encode('utf-8'),headers={'Priority':'high','Tags':'fire'},timeout=15)
    r.raise_for_status(); print(f"通知送信: {title} / {p['venue']} {p['race_label']} / {p['boat']}号艇",flush=True)

def inspect_race(page):
    text=page.locator('body').inner_text(timeout=10000)
    venue=actual_venue(text); race_no=actual_race(text); deadline=extract_deadline(text)
    if not venue or not race_no or not deadline or not within_10_minutes(text): return []
    rows=parse_theory_rows(text); picks=[]
    for boat,info in rows.items():
        reg=info['reg_no']; diff=info['diff']; theory_1st=info.get('theory_1st'); bk=bucket_for_diff(diff)
        if not click_registration(page,reg): continue
        chk=parse_checker(page.locator('body').inner_text(timeout=10000),boat)
        if not chk: continue
        checker_strong,reason,count=strong_dynamic(chk,bk)
        cur=chk['rows'].get(bk)
        if not cur: continue
        checker_rise=cur['rise_1st']
        theory_candidate=theory_1st is not None and theory_1st>=15
        if not checker_strong and not theory_candidate: continue
        st=None; st_page=page.context.new_page()
        try:
            st_page.goto(biyori_url(venue,race_no),wait_until='domcontentloaded',timeout=20000); st_page.wait_for_timeout(700)
            st=st_assessment(st_page,boat)
        except Exception as e:
            print('ST取得失敗:',venue,race_no,boat,repr(e),flush=True)
        finally: st_page.close()
        st_level,st_reason=chance_level(st)
        final_level=combine_level(theory_1st,checker_strong,checker_rise,st_level)
        if final_level=='NONE': continue
        picks.append({'venue':venue,'race_label':f'{race_no}R','boat':boat,'reg_no':reg,'diff':diff,'bucket_name':bk,
                      'base_rate':chk['base_rate'],'checker_rise':checker_rise,'count':count,'dynamic_reason':reason,
                      'theory_1st':theory_1st,'deadline_value':deadline,'st':st,'final_level':final_level,'st_reason':st_reason})
    return picks

def one_cycle(page):
    links=shinsum_links(page); print(f"詳細候補リンク数: {len(links)}",flush=True); hits=0
    for url in links[:100]:
        try:
            page.goto(url,wait_until='domcontentloaded',timeout=20000); page.wait_for_timeout(300)
            for p in inspect_race(page):
                hits+=1; key=f"{now_jst():%Y-%m-%d}|{p['venue']}|{p['race_label']}|{p['boat']}|{p['bucket_name']}|{p['final_level']}"
                if key in sent: continue
                send_notification(p); sent.add(key)
        except Exception as e: print('レース確認失敗:',url,repr(e),flush=True)
    print(f"今回の総合候補: {hits}件",flush=True)

def main():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(http_credentials={'username':USER,'password':PASSWORD})
        page=context.new_page()
        if not active_hours(): print('監視時間外（23:00〜08:00 JST）',flush=True); return
        print(f"[{now_jst():%Y-%m-%d %H:%M:%S}] シンsum理論×チェッカー×ST 総合監視開始",flush=True)
        while active_hours():
            one_cycle(page); print(f"{CHECK_INTERVAL}秒後に再チェック",flush=True); time.sleep(CHECK_INTERVAL)

if __name__=='__main__': main()
