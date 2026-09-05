#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Musigny FX Paper Bot v2 - PAPER ONLY
# Exit strengthened / max leverage 5x

from __future__ import annotations
import csv, hashlib, json, math, os, time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np
import pandas as pd
import requests

PUBLIC='https://forex-api.coin.z.com/public'
SYMBOLS=['USD_JPY','EUR_JPY','GBP_JPY','EUR_USD']
PRICE_TYPE=os.getenv('FX_PRICE_TYPE','BID').upper()
STATE_DIR=Path(os.getenv('FX_STATE_DIR','fx_state')); STATE_DIR.mkdir(parents=True,exist_ok=True)
STATE_FILE=STATE_DIR/'bot_state.json'; TRADES_FILE=STATE_DIR/'paper_trades.csv'; SIGNALS_FILE=STATE_DIR/'signal_log.csv'; WEEKLY_REVIEW=STATE_DIR/'weekly_review.json'
EVENT_FILE=Path(os.getenv('FX_EVENT_FILE','fx_events.json')); NEWS_FILE=Path(os.getenv('FX_NEWS_FILE','fx_news_flags.json'))
NTFY_TOPIC=os.getenv('NTFY_TOPIC','')

ENTRY_THRESHOLD=int(os.getenv('FX_ENTRY_THRESHOLD','62')); STRONG_THRESHOLD=int(os.getenv('FX_STRONG_THRESHOLD','72')); OPPOSITE_GAP=int(os.getenv('FX_OPPOSITE_GAP','10'))
PAPER_BALANCE_DEFAULT=float(os.getenv('FX_PAPER_BALANCE','100000')); RISK_PCT=float(os.getenv('FX_RISK_PCT','0.0075'))
MAX_DAILY_LOSS_PCT=float(os.getenv('FX_MAX_DAILY_LOSS_PCT','0.03')); MAX_CONSECUTIVE_LOSSES=int(os.getenv('FX_MAX_CONSECUTIVE_LOSSES','3'))
MAX_LEVERAGE=float(os.getenv('FX_MAX_LEVERAGE','5'))

MIN_RR=float(os.getenv('FX_MIN_RR','1.0')); PENDING_EXPIRE_HOURS=int(os.getenv('FX_PENDING_EXPIRE_HOURS','8')); EVENT_BLACKOUT_MINUTES=int(os.getenv('FX_EVENT_BLACKOUT_MINUTES','30'))
RCI_PERIODS=(8,25,47); TP1_PCT=.30; TP2_PCT=.40

TP1_R=float(os.getenv('FX_TP1_R','1.0'))
TP2_R=float(os.getenv('FX_TP2_R','1.5'))
TP3_R=float(os.getenv('FX_TP3_R','2.2'))
REVIEW_HOURS=float(os.getenv('FX_REVIEW_HOURS','8'))
MAX_HOLD_HOURS=float(os.getenv('FX_MAX_HOLD_HOURS','12'))
TRAIL_START_R=float(os.getenv('FX_TRAIL_START_R','1.5'))
TRAIL_GIVEBACK_R=float(os.getenv('FX_TRAIL_GIVEBACK_R','0.6'))
REVIEW_CLOSE_R=float(os.getenv('FX_REVIEW_CLOSE_R','0.3'))

@dataclass
class Analysis:
    symbol:str; side:str; long:int; short:int; confidence:int; price:float
    entry_low:float|None; entry_high:float|None; stop:float|None; tp1:float|None; tp2:float|None; tp3:float|None
    rr1:float|None; rr2:float|None; reasons:list[str]; counter:list[str]; candle_id:str; high:float; low:float
    weekly_zone:str; daily_sr:str; corr_note:str

def now_utc(): return datetime.now(timezone.utc)
def now_jst(): return now_utc()+timedelta(hours=9)

def api_get(path,params=None,retries=3):
    last=None
    for i in range(retries):
        try:
            r=requests.get(PUBLIC+path,params=params or {},timeout=25)
            if r.status_code in (429,500,502,503,504):
                last=requests.HTTPError(f'HTTP {r.status_code}',response=r); time.sleep(2*(i+1)); continue
            r.raise_for_status(); j=r.json()
            if int(j.get('status',1))!=0: raise RuntimeError(f'GMO FX API error: {j}')
            return j.get('data',[])
        except Exception as e:
            last=e
            if i<retries-1: time.sleep(2*(i+1))
    raise last or RuntimeError('API failed')

def frame(rows):
    x=pd.DataFrame([{'time':pd.to_datetime(int(r['openTime']),unit='ms',utc=True),'open':float(r['open']),'high':float(r['high']),'low':float(r['low']),'close':float(r['close'])} for r in rows])
    return x.sort_values('time').drop_duplicates('time').reset_index(drop=True) if not x.empty else x

def fetch_intraday(sym,interval,days,extra=8,min_rows=60):
    frames=[]; base=now_jst()-timedelta(hours=6)
    for i in range(days+extra+1):
        d=(base-timedelta(days=i)).strftime('%Y%m%d')
        try:
            f=frame(api_get('/v1/klines',{'symbol':sym,'priceType':PRICE_TYPE,'interval':interval,'date':d}))
            if not f.empty: frames.append(f)
        except requests.HTTPError as e:
            if getattr(e.response,'status_code',None)==404: continue
            print(f'[WARN] {sym} {interval} {d}: {e}',flush=True)
        except Exception as e: print(f'[WARN] {sym} {interval} {d}: {e}',flush=True)
        if frames:
            m=pd.concat(frames).drop_duplicates('time')
            if i>=days and len(m)>=min_rows: break
    if not frames: raise RuntimeError(f'No data {sym} {interval}')
    return pd.concat(frames).sort_values('time').drop_duplicates('time').reset_index(drop=True)

def fetch_yearly(sym,interval,years=3):
    frames=[]; y=now_utc().year
    for yr in range(y-years,y+1):
        try:
            f=frame(api_get('/v1/klines',{'symbol':sym,'priceType':PRICE_TYPE,'interval':interval,'date':str(yr)}))
            if not f.empty: frames.append(f)
        except requests.HTTPError as e:
            if getattr(e.response,'status_code',None)==404: continue
        except Exception as e: print(f'[WARN] {sym} {interval} {yr}: {e}',flush=True)
    if not frames: raise RuntimeError(f'No data {sym} {interval}')
    return pd.concat(frames).sort_values('time').drop_duplicates('time').reset_index(drop=True)

def drop_open(df): return df.iloc[:-1].copy().reset_index(drop=True) if len(df)>=3 else df.copy()

def rci(s,p):
    out=np.full(len(s),np.nan); v=s.to_numpy(float)
    for i in range(p-1,len(v)):
        w=v[i-p+1:i+1]; tr=np.arange(1,p+1,dtype=float); pr=pd.Series(w).rank(method='average').to_numpy(float); d2=np.sum((tr-pr)**2)
        out[i]=(1-6*d2/(p*(p**2-1)))*100
    return pd.Series(out,index=s.index)

def indicators(df):
    x=df.copy(); x['ema12']=x['close'].ewm(span=12,adjust=False).mean(); x['ema_slope5']=x['ema12'].pct_change(5)
    for p in RCI_PERIODS: x[f'rci{p}']=rci(x['close'],p)
    pc=x['close'].shift(1); tr=pd.concat([(x['high']-x['low']),(x['high']-pc).abs(),(x['low']-pc).abs()],axis=1).max(axis=1); x['atr']=tr.rolling(14).mean(); return x

def line_dir(x,p):
    a=x.iloc[-1][f'rci{p}']; b=x.iloc[-2][f'rci{p}']
    if pd.isna(a) or pd.isna(b): return 0
    return 1 if a>b+1 else -1 if a<b-1 else 0

def higher_bias(W,D):
    ds=[line_dir(W,25),line_dir(W,47),line_dir(D,25),line_dir(D,47)]; up=sum(v>0 for v in ds); dn=sum(v<0 for v in ds)
    if up==4:return 'STRONG_UP',15
    if up>=3:return 'UP',11
    if dn==4:return 'STRONG_DOWN',15
    if dn>=3:return 'DOWN',11
    return 'MIXED',0

def red_reversal(x):
    vals=x['rci8'].iloc[-4:].dropna()
    if len(vals)<3:return None,0
    prev=float(vals.iloc[-2]); cur=float(vals.iloc[-1]); mn=float(vals.min()); mx=float(vals.max())
    if mn<=-85 and cur>prev+2:return 'LONG',16
    if mn<=-75 and cur>prev+2:return 'LONG',12
    if mx>=85 and cur<prev-2:return 'SHORT',16
    if mx>=75 and cur<prev-2:return 'SHORT',12
    return None,0

def trend(x):
    r=x.iloc[-1]; bull=bear=0
    if r['close']>r['ema12'] and r['ema_slope5']>0: bull+=2
    if r['close']<r['ema12'] and r['ema_slope5']<0: bear+=2
    if r['rci25']>0 and r['rci47']>-50: bull+=1
    if r['rci25']<0 and r['rci47']<50: bear+=1
    return 'BULL' if bull>=bear+2 else 'BEAR' if bear>=bull+2 else 'NEUTRAL'

def pivots(df,l=2,r=2):
    H=[];L=[]; h=df['high'].to_numpy(); lo=df['low'].to_numpy()
    for i in range(l,len(df)-r):
        if h[i]>=np.max(h[i-l:i+r+1]):H.append(h[i])
        if lo[i]<=np.min(lo[i-l:i+r+1]):L.append(lo[i])
    return H,L

def weekly_zone(W,p):
    w=W.iloc[-104:]; lo=float(w['low'].min()); hi=float(w['high'].max()); q1=lo+(hi-lo)*.25; q3=lo+(hi-lo)*.75; mid=(lo+hi)/2
    zone='LOWER_25' if p<=q1 else 'LOWER_MID' if p<=mid else 'UPPER_MID' if p<=q3 else 'UPPER_25'; return zone,lo,hi

def daily_sr(D,p):
    x=D.iloc[-95:]; H,L=pivots(x); tol=max(p*.0025,1e-6)
    def cluster(vals):
        gs=[]
        for v in sorted(vals):
            for g in gs:
                if abs(v-g['m'])<=tol: g['v'].append(v); g['m']=float(np.mean(g['v'])); break
            else: gs.append({'m':v,'v':[v]})
        return gs
    sg=cluster([v for v in L if v<p]); rg=cluster([v for v in H if v>p]); s=max(sg,key=lambda g:g['m']) if sg else None; r=min(rg,key=lambda g:g['m']) if rg else None
    sp=s['m'] if s else None; rp=r['m'] if r else None; sh=len(s['v']) if s else 0; rh=len(r['v']) if r else 0
    return f'S={sp if sp else "NA"}({sh}) R={rp if rp else "NA"}({rh})',sp,rp,sh,rh

def fib_score(p,c,lo,hi):
    d=hi-lo; levels={'382':hi-d*.382,'500':hi-d*.5,'618':hi-d*.618,'786':hi-d*.786}; L=S=0; rl=[];rs=[]
    for n,lv in levels.items():
        if abs(p-lv)/max(p,1e-9)<=.005:
            w=10 if n in ('382','618') else 7 if n=='500' else 3
            if c['low']<=lv<=c['close'] and c['close']>c['open']: L+=w; rl.append(f'W_FIB_{n}_BOUNCE')
            if c['close']<=lv<=c['high'] and c['close']<c['open']: S+=w; rs.append(f'W_FIB_{n}_REJECT')
    return L,S,rl,rs

def correlations(allf):
    ss=[]
    for sym,fs in allf.items():
        if len(fs['1hour'])>=50: ss.append(fs['1hour'].set_index('time')['close'].pct_change().rename(sym))
    if len(ss)<2:return {}
    df=pd.concat(ss,axis=1,join='inner').dropna().tail(120); c=df.corr(); out={}
    for s in c.columns: out[s]=sorted([(o,float(c.loc[s,o])) for o in c.columns if o!=s],key=lambda z:abs(z[1]),reverse=True)
    return out

def corr_note(sym,c):
    if sym not in c or not c[sym]:return 'CORR_NA'
    o,v=c[sym][0]; return f'CORR {o}={v:+.2f}'

def load_json(path,default):
    try:return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    except:return default

def event_block(sym):
    now=now_utc(); pair=set(sym.split('_'))
    for e in load_json(EVENT_FILE,[]):
        try:
            if str(e.get('impact','')).upper()!='HIGH':continue
            t=datetime.fromisoformat(str(e['time_utc']).replace('Z','+00:00'))
            if pair.intersection(set(e.get('currencies',[]))) and abs((now-t).total_seconds())<=EVENT_BLACKOUT_MINUTES*60:return True,e.get('name','HIGH_EVENT')
        except:pass
    return False,''

def news_block(sym):
    d=load_json(NEWS_FILE,{}); f=d.get(sym) or d.get('GLOBAL')
    return (bool(f.get('block_new_entries')),str(f.get('reason','NEWS_RISK'))) if isinstance(f,dict) else (False,'')

def analyze(sym,fs,cn):
    W,D,H4,H1,M15,M5=[indicators(fs[k]) for k in ('1week','1day','4hour','1hour','15min','5min')]; c=M15.iloc[-1]; prev=M15.iloc[-2]; p=float(c['close']); L=S=0; rl=[];rs=[]; cl=[];cs=[]
    zone,wlo,whi=weekly_zone(W,p); sr,ds,dr,sh,rh=daily_sr(D,p)
    for lab,x,w in [('4H',H4,18),('1H',H1,13)]:
        st=trend(x)
        if st=='BULL':L+=w;rl.append(lab+'_BULL');cs.append(lab+' bullish')
        elif st=='BEAR':S+=w;rs.append(lab+'_BEAR');cl.append(lab+' bearish')
    hb,hp=higher_bias(W,D)
    if hb in ('UP','STRONG_UP'):L+=hp;rl.append('W_D_RCI25_47_UP');cs.append('W/D RCI25/47 rising')
    elif hb in ('DOWN','STRONG_DOWN'):S+=hp;rs.append('W_D_RCI25_47_DOWN');cl.append('W/D RCI25/47 falling')
    if c['close']>c['ema12'] and c['ema_slope5']>0:L+=11;rl.append('15M_EMA_UP')
    elif c['close']<c['ema12'] and c['ema_slope5']<0:S+=11;rs.append('15M_EMA_DOWN')
    if c['rci8']>prev['rci8'] and c['rci25']>=prev['rci25']:L+=7;rl.append('15M_RCI_UP')
    if c['rci8']<prev['rci8'] and c['rci25']<=prev['rci25']:S+=7;rs.append('15M_RCI_DOWN')
    for lab,x,sc in [('4H',H4,1),('1H',H1,.9),('15M',M15,.85)]:
        rev,pts=red_reversal(x); pts=int(round(pts*sc))
        if hb in ('UP','STRONG_UP') and rev=='LONG':L+=pts;rl.append(lab+'_RCI8_BOTTOM_REV')
        elif hb in ('DOWN','STRONG_DOWN') and rev=='SHORT':S+=pts;rs.append(lab+'_RCI8_TOP_REV')
    rev,_=red_reversal(M5)
    if hb in ('UP','STRONG_UP') and rev=='LONG':L+=7;rl.append('5M_LONG_TIMING')
    if hb in ('DOWN','STRONG_DOWN') and rev=='SHORT':S+=7;rs.append('5M_SHORT_TIMING')
    if ds and abs(p-ds)/p<=.004:L+=min(10,3+sh*2);rl.append(f'DAILY_SUPPORT_{sh}');cs.append('near repeated daily support')
    if dr and abs(p-dr)/p<=.004:S+=min(10,3+rh*2);rs.append(f'DAILY_RESIST_{rh}');cl.append('near repeated daily resistance')
    a,b,ar,br=fib_score(p,c,wlo,whi);L+=a;S+=b;rl+=ar;rs+=br
    if zone=='UPPER_25':cl.append('upper 25% of 2Y range')
    if zone=='LOWER_25':cs.append('lower 25% of 2Y range')
    side='WAIT'; conf=max(L,S); reasons=[f'L={L}',f'S={S}']; counter=[]
    if L>=ENTRY_THRESHOLD and L>=S+OPPOSITE_GAP:side='LONG';conf=min(100,L);reasons=rl;counter=cl
    elif S>=ENTRY_THRESHOLD and S>=L+OPPOSITE_GAP:side='SHORT';conf=min(100,S);reasons=rs;counter=cs
    cid=str(c['time'])
    if side=='WAIT':return Analysis(sym,side,L,S,conf,p,None,None,None,None,None,None,None,None,reasons,[],cid,float(c['high']),float(c['low']),zone,sr,cn)
    atr=float(H1['atr'].iloc[-1]); atr=atr if math.isfinite(atr) and atr>0 else p*.002
    if side=='LONG':
        eh=p-.10*atr; el=p-.35*atr; stop=min(el-.80*atr,(ds-.25*atr if ds else el-.80*atr)); mid=(el+eh)/2
    else:
        el=p+.10*atr; eh=p+.35*atr; stop=max(eh+.80*atr,(dr+.25*atr if dr else eh+.80*atr)); mid=(el+eh)/2
    risk=max(abs(mid-stop),p*.0008)
    if side=='LONG':
        t1=mid+TP1_R*risk;t2=mid+TP2_R*risk;t3=mid+TP3_R*risk
    else:
        t1=mid-TP1_R*risk;t2=mid-TP2_R*risk;t3=mid-TP3_R*risk
    rr1=abs(t1-mid)/abs(mid-stop);rr2=abs(t2-mid)/abs(mid-stop)
    if rr1<MIN_RR:return Analysis(sym,'WAIT',L,S,conf,p,None,None,None,None,None,None,rr1,rr2,reasons,counter+[f'RR_LOW={rr1:.2f}'],cid,float(c['high']),float(c['low']),zone,sr,cn)
    return Analysis(sym,side,L,S,conf,p,el,eh,stop,t1,t2,t3,rr1,rr2,reasons,counter,cid,float(c['high']),float(c['low']),zone,sr,cn)

def load_state():
    if STATE_FILE.exists():
        try:return json.loads(STATE_FILE.read_text(encoding='utf-8'))
        except:pass
    return {'paper_balance':PAPER_BALANCE_DEFAULT,'position':None,'pending':None,'daily_date':None,'daily_start_balance':PAPER_BALANCE_DEFAULT,'consecutive_losses':0,'last_key':None}

def save_state(s):STATE_FILE.write_text(json.dumps(s,ensure_ascii=False,indent=2),encoding='utf-8')
def notify(t):
    print(t,flush=True)
    if NTFY_TOPIC:requests.post(f'https://ntfy.sh/{NTFY_TOPIC}',data=t.encode('utf-8'),headers={'Title':'Musigny FX Paper Bot v2'},timeout=15).raise_for_status()

def can_open(s):
    d=now_jst().date().isoformat()
    if s.get('daily_date')!=d:s['daily_date']=d;s['daily_start_balance']=s['paper_balance']
    dd=(s['paper_balance']-s['daily_start_balance'])/max(s['daily_start_balance'],1)
    if dd<=-MAX_DAILY_LOSS_PCT:return False,'DAILY_LOSS_LIMIT'
    if s.get('consecutive_losses',0)>=MAX_CONSECUTIVE_LOSSES:return False,'CONSECUTIVE_LOSS_LIMIT'
    if s.get('position'):return False,'POSITION_OPEN'
    return True,'OK'

def create_pending(s,a):s['pending']={'symbol':a.symbol,'side':a.side,'confidence':a.confidence,'entry_low':a.entry_low,'entry_high':a.entry_high,'stop':a.stop,'tp1':a.tp1,'tp2':a.tp2,'tp3':a.tp3,'rr1':a.rr1,'rr2':a.rr2,'created_at':now_utc().isoformat(),'reasons':a.reasons[:10],'counter':a.counter[:10]}
def touch(p,h,l):return h>=min(p['entry_low'],p['entry_high']) and l<=max(p['entry_low'],p['entry_high'])

def quote_to_jpy(sym,tickers):
    quote=sym.split('_')[1]
    if quote=='JPY': return 1.0
    if quote=='USD':
        t=tickers.get('USD_JPY')
        if t:return float(t['bid'])
    return 1.0

def max_qty_by_leverage(sym,entry,balance,tickers):
    q2j=quote_to_jpy(sym,tickers)
    max_notional_jpy=balance*MAX_LEVERAGE
    return max_notional_jpy/max(entry*q2j,1e-12)

def make_pos(s,p,tickers):
    e=(p['entry_low']+p['entry_high'])/2
    risk_qty=s['paper_balance']*RISK_PCT/max(abs(e-p['stop']),1e-12)
    lev_qty=max_qty_by_leverage(p['symbol'],e,s['paper_balance'],tickers)
    qty=min(risk_qty,lev_qty)
    s['position']={'symbol':p['symbol'],'side':p['side'],'entry':e,'qty_initial':qty,'qty_remaining':qty,'stop':p['stop'],'original_stop':p['stop'],'initial_risk':abs(e-p['stop']),'tp1':p['tp1'],'tp2':p['tp2'],'tp3':p['tp3'],'tp1_done':False,'tp2_done':False,'realized_pnl':0.0,'opened_at':now_utc().isoformat(),'max_r':0.0}
    s['pending']=None
    return f'FX PAPER OPEN {p["symbol"]} {p["side"]}\nEntry {e:.5f}\nSTOP {p["stop"]:.5f}\nTP1 {p["tp1"]:.5f} TP2 {p["tp2"]:.5f} TP3 {p["tp3"]:.5f}\nMax leverage {MAX_LEVERAGE:.1f}x'

def manage_pending(s,analyses,tickers):
    p=s.get('pending')
    if not p:return None
    if now_utc()-datetime.fromisoformat(p['created_at'])>timedelta(hours=PENDING_EXPIRE_HOURS):s['pending']=None;return None
    a=next((x for x in analyses if x.symbol==p['symbol']),None)
    if a and touch(p,a.high,a.low):return make_pos(s,p,tickers)
    return None

def live_exit_price(sym,side,tickers):
    t=tickers.get(sym); return float(t['bid'] if side=='LONG' else t['ask']) if t else None

def record(row):
    ex=TRADES_FILE.exists()
    with TRADES_FILE.open('a',newline='',encoding='utf-8') as f:
        w=csv.writer(f)
        if not ex:w.writerow(['time','symbol','side','entry','exit','qty','pnl','balance','event','rule_followed'])
        w.writerow(row)

def close_remaining(s,p,px,event,label):
    q=max(p['qty_remaining'],0); side=p['side']
    pnl=(px-p['entry'])*q if side=='LONG' else (p['entry']-px)*q
    p['realized_pnl']+=pnl;s['paper_balance']+=p['realized_pnl']
    s['consecutive_losses']=s.get('consecutive_losses',0)+1 if p['realized_pnl']<0 else 0
    record([now_utc().isoformat(),p['symbol'],side,p['entry'],px,q,pnl,s['paper_balance'],event,True])
    msg=f'{label} {p["symbol"]}\nFinal PnL {p["realized_pnl"]:+,.0f}\nBalance {s["paper_balance"]:,.0f}'
    s['position']=None
    return msg

def position_r(p,px):
    risk=max(float(p.get('initial_risk',abs(p['entry']-p.get('original_stop',p['stop'])))),1e-12)
    return (px-p['entry'])/risk if p['side']=='LONG' else (p['entry']-px)/risk

def strong_reversal(p,analyses):
    a=next((x for x in analyses if x.symbol==p['symbol']),None)
    if not a:return False
    if p['side']=='LONG':return a.short>=ENTRY_THRESHOLD and a.short>=a.long+OPPOSITE_GAP
    return a.long>=ENTRY_THRESHOLD and a.long>=a.short+OPPOSITE_GAP

def manage_position(s,tickers,analyses):
    p=s.get('position');msgs=[]
    if not p:return msgs
    px=live_exit_price(p['symbol'],p['side'],tickers)
    if px is None:return msgs
    side=p['side'];tp=lambda lv:px>=lv if side=='LONG' else px<=lv;st=lambda lv:px<=lv if side=='LONG' else px>=lv
    cur_r=position_r(p,px);p['max_r']=max(float(p.get('max_r',0)),cur_r)
    age_h=(now_utc()-datetime.fromisoformat(p['opened_at'])).total_seconds()/3600
    print(f'[POSITION] {p["symbol"]} {side} ENTRY={p["entry"]:.5f} NOW={px:.5f} R={cur_r:+.2f} MAX_R={p["max_r"]:+.2f} AGE={age_h:.1f}h STOP={p["stop"]:.5f} TP1={p["tp1"]:.5f} TP2={p["tp2"]:.5f} TP3={p["tp3"]:.5f}',flush=True)

    if st(p['stop']):
        msgs.append(close_remaining(s,p,px,'STOP_END','FX PAPER STOP'));return msgs
    if strong_reversal(p,analyses):
        msgs.append(close_remaining(s,p,px,'REVERSAL_END','FX PAPER REVERSAL EXIT'));return msgs
    if age_h>=MAX_HOLD_HOURS:
        msgs.append(close_remaining(s,p,px,'TIME_END','FX PAPER TIME EXIT'));return msgs
    if age_h>=REVIEW_HOURS and not p['tp1_done'] and cur_r<=REVIEW_CLOSE_R:
        msgs.append(close_remaining(s,p,px,'REVIEW_END','FX PAPER REVIEW EXIT'));return msgs
    if p['max_r']>=TRAIL_START_R and cur_r<=p['max_r']-TRAIL_GIVEBACK_R:
        msgs.append(close_remaining(s,p,px,'TRAIL_END','FX PAPER TRAIL EXIT'));return msgs

    if not p['tp1_done'] and tp(p['tp1']):
        q=p['qty_initial']*TP1_PCT;pnl=(px-p['entry'])*q if side=='LONG' else (p['entry']-px)*q
        p['qty_remaining']-=q;p['realized_pnl']+=pnl;p['tp1_done']=True;p['stop']=p['entry']
        record([now_utc().isoformat(),p['symbol'],side,p['entry'],px,q,pnl,s['paper_balance'],'TP1',True])
        msgs.append(f'FX PAPER TP1 {p["symbol"]} 30% PnL {pnl:+,.0f}')

    if not p['tp2_done'] and tp(p['tp2']):
        q=p['qty_initial']*TP2_PCT;pnl=(px-p['entry'])*q if side=='LONG' else (p['entry']-px)*q
        p['qty_remaining']-=q;p['realized_pnl']+=pnl;p['tp2_done']=True
        risk=float(p['initial_risk']);p['stop']=p['entry']+risk if side=='LONG' else p['entry']-risk
        record([now_utc().isoformat(),p['symbol'],side,p['entry'],px,q,pnl,s['paper_balance'],'TP2',True])
        msgs.append(f'FX PAPER TP2 {p["symbol"]} 40% PnL {pnl:+,.0f}')

    if tp(p['tp3']):
        msgs.append(close_remaining(s,p,px,'TP3_END','FX PAPER TP3'));return msgs
    return msgs

def log_signals(a):
    ex=SIGNALS_FILE.exists()
    with SIGNALS_FILE.open('a',newline='',encoding='utf-8') as f:
        w=csv.writer(f)
        if not ex:w.writerow(['time','symbol','side','long','short','confidence','price','weekly_zone','daily_sr','corr','rr1','rr2','counter'])
        for x in a:w.writerow([now_utc().isoformat(),x.symbol,x.side,x.long,x.short,x.confidence,x.price,x.weekly_zone,x.daily_sr,x.corr_note,x.rr1,x.rr2,' | '.join(x.counter)])

def print_summary(a,failed):
    by={x.symbol:x for x in a};print('========== MUSIGNY FX ANALYSIS ==========',flush=True)
    for s in SYMBOLS:
        x=by.get(s)
        if not x:print(f'{s:>7} | DATA_SKIP',flush=True);continue
        strong='STRONG' if x.side!='WAIT' and x.confidence>=STRONG_THRESHOLD else ''
        print(f'{s:>7} | {x.side:<5} | L={x.long:>3} S={x.short:>3} CONF={x.confidence:>3} {strong} | {x.weekly_zone} | {x.daily_sr} | {x.corr_note}',flush=True)
    print('=========================================',flush=True)

def weekly_review():
    if not TRADES_FILE.exists():return
    try:
        d=pd.read_csv(TRADES_FILE);d['time']=pd.to_datetime(d['time'],utc=True,errors='coerce');w=d[d['time']>=pd.Timestamp(now_utc()-timedelta(days=7))]
        if w.empty:return
        losses=w[w['pnl']<0];out={'generated_at':now_utc().isoformat(),'events':len(w),'loss_events':len(losses),'pnl_events':float(w['pnl'].sum()),'loss_symbols':losses['symbol'].value_counts().to_dict(),'loss_sides':losses['side'].value_counts().to_dict()};WEEKLY_REVIEW.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
    except Exception as e:print('[WEEKLY_REVIEW_WARN]',e,flush=True)

def main():
    tick={x['symbol']:x for x in api_get('/v1/ticker')};market=any(tick.get(s,{}).get('status')=='OPEN' for s in SYMBOLS)
    allf={};failed=[]
    for s in SYMBOLS:
        try:allf[s]={'5min':drop_open(fetch_intraday(s,'5min',3,8,80)),'15min':drop_open(fetch_intraday(s,'15min',5,10,80)),'1hour':drop_open(fetch_intraday(s,'1hour',20,12,120)),'4hour':drop_open(fetch_yearly(s,'4hour',3)),'1day':drop_open(fetch_yearly(s,'1day',3)),'1week':drop_open(fetch_yearly(s,'1week',3))}
        except Exception as e:failed.append(s);print('[DATA_SKIP]',s,e,flush=True)
    corr=correlations(allf);a=[]
    for s,fs in allf.items():
        try:a.append(analyze(s,fs,corr_note(s,corr)))
        except Exception as e:failed.append(s);print('[ANALYSIS_SKIP]',s,e,flush=True)

    st=load_state()
    for m in manage_position(st,tick,a):notify(m)
    if not a:save_state(st);return

    print_summary(a,failed);log_signals(a)

    if not st.get('position'):
        m=manage_pending(st,a,tick)
        if m:notify(m)

    if market and not st.get('position') and not st.get('pending'):
        valid=sorted([x for x in a if x.side!='WAIT' and x.confidence>=ENTRY_THRESHOLD],key=lambda x:x.confidence,reverse=True)
        if valid:
            c=valid[0];eb,er=event_block(c.symbol);nb,nr=news_block(c.symbol);ok,why=can_open(st)
            print(f'[CANDIDATE] {c.symbol} {c.side} CONF={c.confidence} RR1={c.rr1:.2f} COUNTER={" | ".join(c.counter[:5]) or "NONE"}',flush=True)
            if eb:print('[BLOCK]',er,flush=True)
            elif nb:print('[BLOCK]',nr,flush=True)
            elif not ok:print('[BLOCK]',why,flush=True)
            else:
                key=hashlib.sha1(f'{c.symbol}:{c.side}:{c.candle_id}'.encode()).hexdigest()[:16]
                if st.get('last_key')!=key:create_pending(st,c);st['last_key']=key;print(f'[PENDING_CREATED] {c.symbol} {c.side}',flush=True)

    if st.get('position'):print('[STATE] OPEN_POSITION:',st['position']['symbol'],flush=True)
    elif st.get('pending'):print('[STATE] PENDING:',st['pending']['symbol'],flush=True)

    weekly_review();save_state(st)

if __name__=='__main__':main()
