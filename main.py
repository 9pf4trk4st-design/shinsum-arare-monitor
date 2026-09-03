#!/usr/bin/env python3
from __future__ import annotations
import os, json, math, csv, hashlib, time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

PUBLIC = "https://api.coin.z.com/public"
SYMBOLS = ["BTC", "ETH", "XRP", "SOL"]
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
STATE_DIR = Path(os.getenv("STATE_DIR", "state")); STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "bot_state.json"
TRADES_FILE = STATE_DIR / "paper_trades.csv"
SIGNALS_FILE = STATE_DIR / "signal_log.csv"

EMA_PERIOD = 12
RCI_PERIODS = (8, 25, 47)
ENTRY_THRESHOLD = int(os.getenv("ENTRY_THRESHOLD", "60"))
STRONG_THRESHOLD = int(os.getenv("STRONG_THRESHOLD", "70"))
OPPOSITE_GAP = int(os.getenv("OPPOSITE_GAP", "10"))
PAPER_BALANCE_DEFAULT = float(os.getenv("PAPER_BALANCE", "100000"))
RISK_PCT = float(os.getenv("RISK_PCT", "0.0075"))
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.03"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
TP1_PCT, TP2_PCT, TP3_PCT = 0.30, 0.40, 0.30
PENDING_EXPIRE_HOURS = int(os.getenv("PENDING_EXPIRE_HOURS", "8"))
RCI_EXTREME = 75.0
RCI_STRONG_EXTREME = 85.0

@dataclass
class Analysis:
    symbol: str; side: str; score_long: int; score_short: int; confidence: int
    price: float; entry_low: float|None; entry_high: float|None; stop: float|None
    tp1: float|None; tp2: float|None; tp3: float|None; reasons: list[str]
    invalidation: str; candle_id: str; candle_high: float; candle_low: float

def now_utc(): return datetime.now(timezone.utc)
def now_jst(): return now_utc() + timedelta(hours=9)

def api_get(path, params, retries=3):
    last_error=None
    for attempt in range(1,retries+1):
        try:
            r=requests.get(PUBLIC+path,params=params,timeout=25)
            if r.status_code==404:
                r.raise_for_status()
            if r.status_code==429 or 500<=r.status_code<600:
                last_error=requests.HTTPError(f"{r.status_code} temporary error",response=r)
                if attempt<retries:
                    wait=2*attempt
                    print(f"[RETRY] GMO API {r.status_code}: {wait}ç§å¾ã«åè©¦è¡")
                    time.sleep(wait); continue
                raise last_error
            r.raise_for_status(); j=r.json()
            if int(j.get("status",1))!=0: raise RuntimeError(f"GMO API error: {j}")
            return j.get("data",[]) or []
        except (requests.Timeout,requests.ConnectionError) as e:
            last_error=e
            if attempt<retries:
                wait=2*attempt
                print(f"[RETRY] GMO APIéä¿¡å¤±æ: {wait}ç§å¾ã«åè©¦è¡ ({e})")
                time.sleep(wait); continue
            raise
    if last_error: raise last_error
    return []

def frame_from_rows(rows):
    out = pd.DataFrame([{
        "time": pd.to_datetime(int(x["openTime"]), unit="ms", utc=True),
        "open": float(x["open"]), "high": float(x["high"]), "low": float(x["low"]),
        "close": float(x["close"]), "volume": float(x["volume"])
    } for x in rows])
    return out if out.empty else out.sort_values("time").drop_duplicates("time").reset_index(drop=True)

def fetch_intraday(symbol, interval, days, extra_days=10, min_rows=60):
    frames=[]; api_day=now_jst()-timedelta(hours=6)
    max_lookback=max(days+extra_days,days+2)
    for i in range(max_lookback+1):
        d=(api_day-timedelta(days=i)).strftime("%Y%m%d")
        try:
            f=frame_from_rows(api_get("/v1/klines",{"symbol":symbol,"interval":interval,"date":d},retries=3))
            if not f.empty:
                frames.append(f)
                merged=pd.concat(frames).sort_values("time").drop_duplicates("time").reset_index(drop=True)
                if len(merged)>=min_rows and i>=days: break
        except requests.HTTPError as e:
            if getattr(e.response,"status_code",None)==404:
                print(f"[INFO] {symbol} {interval} {d}: KLineæªæä¾ â åæ¥ã¸"); continue
            print(f"[WARN] {symbol} {interval} {d}: {e}")
        except Exception as e:
            print(f"[WARN] {symbol} {interval} {d}: {e}")
    if not frames:
        raise RuntimeError(f"No data for {symbol} {interval} (JST6æåºæºã§{max_lookback+1}æ¥æ¢ç´¢)")
    out=pd.concat(frames).sort_values("time").drop_duplicates("time").reset_index(drop=True)
    if len(out)<min_rows: print(f"[WARN] {symbol} {interval}: {len(out)}æ¬ã®ã¿åå¾")
    return out

def fetch_yearly(symbol, interval, years_back=3, extra_years=1):
    frames=[]; y=now_utc().year
    for year in range(y-years_back-extra_years,y+1):
        try:
            f=frame_from_rows(api_get("/v1/klines",{"symbol":symbol,"interval":interval,"date":str(year)},retries=3))
            if not f.empty: frames.append(f)
        except requests.HTTPError as e:
            if getattr(e.response,"status_code",None)==404:
                print(f"[INFO] {symbol} {interval} {year}: KLineæªæä¾ã®ããã¹ã­ãã"); continue
            print(f"[WARN] {symbol} {interval} {year}: {e}")
        except Exception as e:
            print(f"[WARN] {symbol} {interval} {year}: {e}")
    if not frames: raise RuntimeError(f"No data for {symbol} {interval}")
    return pd.concat(frames).sort_values("time").drop_duplicates("time").reset_index(drop=True)

def drop_open_candle(df): return df.iloc[:-1].copy().reset_index(drop=True) if len(df)>=3 else df

def rci(series, period):
    out=np.full(len(series),np.nan); vals=series.to_numpy(float)
    for i in range(period-1,len(vals)):
        w=vals[i-period+1:i+1]; tr=np.arange(1,period+1,dtype=float)
        pr=pd.Series(w).rank(method="average").to_numpy(float); d2=np.sum((tr-pr)**2)
        out[i]=(1-6*d2/(period*(period**2-1)))*100
    return pd.Series(out,index=series.index)

def ichimoku(df):
    x=df.copy(); h9=x.high.rolling(9).max(); l9=x.low.rolling(9).min(); h26=x.high.rolling(26).max(); l26=x.low.rolling(26).min(); h52=x.high.rolling(52).max(); l52=x.low.rolling(52).min()
    x["tenkan"]=(h9+l9)/2; x["kijun"]=(h26+l26)/2; x["span_a"]=(x.tenkan+x.kijun)/2; x["span_b"]=(h52+l52)/2
    return x

def indicators(df):
    x=df.copy(); x["ema12"]=x.close.ewm(span=EMA_PERIOD,adjust=False).mean(); x["ema_slope5"]=x.ema12.pct_change(5)
    for p in RCI_PERIODS: x[f"rci{p}"]=rci(x.close,p)
    x["atr"]=(x.high-x.low).rolling(14).mean(); x["vol_ma20"]=x.volume.rolling(20).mean(); return ichimoku(x)

def trend_state_ind(x):
    r=x.iloc[-1]; bull=bear=0
    if r.close>r.ema12 and r.ema_slope5>0: bull+=2
    if r.close<r.ema12 and r.ema_slope5<0: bear+=2
    if pd.notna(r.span_a) and pd.notna(r.span_b):
        hi=max(r.span_a,r.span_b); lo=min(r.span_a,r.span_b)
        if r.close>hi: bull+=2
        elif r.close<lo: bear+=2
    if pd.notna(r.rci25) and pd.notna(r.rci47):
        if r.rci25>0 and r.rci47>-50: bull+=1
        if r.rci25<0 and r.rci47<50: bear+=1
    if bull>=bear+2: return "BULL"
    if bear>=bull+2: return "BEAR"
    return "NEUTRAL"

def rci_dir(ind, period):
    if len(ind)<2: return 0
    a,b=ind.iloc[-2][f"rci{period}"],ind.iloc[-1][f"rci{period}"]
    if pd.isna(a) or pd.isna(b): return 0
    return 1 if b-a>1 else -1 if b-a<-1 else 0

def higher_rci_bias(W,D):
    dirs=[rci_dir(W,25),rci_dir(W,47),rci_dir(D,25),rci_dir(D,47)]
    up=sum(x>0 for x in dirs); down=sum(x<0 for x in dirs)
    if up==4: return "STRONG_UP",14
    if up>=3: return "UP",10
    if down==4: return "STRONG_DOWN",14
    if down>=3: return "DOWN",10
    return "MIXED",0

def red_rci_reversal(ind):
    vals=ind["rci8"].iloc[-4:].dropna()
    if len(vals)<3: return None,0
    prev,cur=float(vals.iloc[-2]),float(vals.iloc[-1]); lo,hi=float(vals.min()),float(vals.max())
    rising=cur>prev+2; falling=cur<prev-2
    if lo<=-RCI_STRONG_EXTREME and rising: return "LONG",16
    if lo<=-RCI_EXTREME and rising: return "LONG",12
    if hi>=RCI_STRONG_EXTREME and falling: return "SHORT",16
    if hi>=RCI_EXTREME and falling: return "SHORT",12
    return None,0

def pivot_levels(df,left=3,right=3):
    highs=[]; lows=[]; h=df.high.to_numpy(); l=df.low.to_numpy()
    for i in range(left,len(df)-right):
        if h[i]>=np.max(h[i-left:i+right+1]): highs.append((i,h[i]))
        if l[i]<=np.min(l[i-left:i+right+1]): lows.append((i,l[i]))
    return highs,lows

def structure(df):
    x=df.iloc[-180:].reset_index(drop=True); highs,lows=pivot_levels(x)
    return (highs[-1][1] if highs else x.high.iloc[-30:].max(), lows[-1][1] if lows else x.low.iloc[-30:].min())

def fib_levels(low,high):
    d=high-low; return {"0.236":high-d*.236,"0.382":high-d*.382,"0.5":high-d*.5,"0.618":high-d*.618,"0.786":high-d*.786}

def major_fib(symbol,weekly):
    lo=os.getenv(f"FIB_{symbol}_LOW"); hi=os.getenv(f"FIB_{symbol}_HIGH")
    if lo and hi and float(hi)>float(lo): return float(lo),float(hi),"åºå®"
    w=weekly.iloc[-160:]; return float(w.low.min()),float(w.high.max()),"èªå"

def score_fib(price,candle,levels):
    L=S=0; rl=[]; rs=[]
    for name,lv in levels.items():
        if abs(price-lv)/price<=.008:
            w=12 if name in ("0.382","0.618") else 8 if name=="0.5" else 4
            if candle.low<=lv<=candle.close and candle.close>candle.open: L+=w; rl.append(f"é±è¶³Fib{name}åçº")
            if candle.close<=lv<=candle.high and candle.close<candle.open: S+=w; rs.append(f"é±è¶³Fib{name}æå¦")
    return L,S,rl,rs

def analyze(symbol,frames):
    W,D,H4,H1,M15,M5=[indicators(frames[k]) for k in ("1week","1day","4hour","1hour","15min","5min")]
    c15,p15,c5=M15.iloc[-1],M15.iloc[-2],M5.iloc[-1]; p=float(c15.close); L=S=0; rl=[]; rs=[]

    # 4Hã»1Hã®æ¹å
    for label,st,w in [("4H",trend_state_ind(H4),20),("1H",trend_state_ind(H1),15)]:
        if st=="BULL": L+=w; rl.append(f"{label}ä¸åã")
        elif st=="BEAR": S+=w; rs.append(f"{label}ä¸åã")

    # é±è¶³ã»æ¥è¶³ã®é/ç·RCI
    bias,bpts=higher_rci_bias(W,D)
    if bias in ("UP","STRONG_UP"): L+=bpts; rl.append("é±è¶³ã»æ¥è¶³ é/ç·RCIä¸åã")
    elif bias in ("DOWN","STRONG_DOWN"): S+=bpts; rs.append("é±è¶³ã»æ¥è¶³ é/ç·RCIä¸åã")

    # 15åãã¡ã¤ã³
    if c15.close>c15.ema12 and c15.ema_slope5>0: L+=12; rl.append("15å12EMAä¸")
    elif c15.close<c15.ema12 and c15.ema_slope5<0: S+=12; rs.append("15å12EMAä¸")
    if pd.notna(c15.rci8) and pd.notna(c15.rci25):
        if c15.rci8>p15.rci8 and c15.rci25>=p15.rci25: L+=8; rl.append("15åRCIç­ä¸­æä¸åã")
        if c15.rci8<p15.rci8 and c15.rci25<=p15.rci25: S+=8; rs.append("15åRCIç­ä¸­æä¸åã")

    # Musignyå¼: ä¸ä½éç·æ¹å + ä¸ä½èµ¤RCIç«¯ããåè»¢
    for label,ind,scale in [("4H",H4,1.0),("1H",H1,.9),("15å",M15,.85)]:
        rev,pts=red_rci_reversal(ind); pts=int(round(pts*scale))
        if bias in ("UP","STRONG_UP") and rev=="LONG": L+=pts; rl.append(f"{label}èµ¤RCIä¸ç«¯âä¸åãåè»¢")
        elif bias in ("DOWN","STRONG_DOWN") and rev=="SHORT": S+=pts; rs.append(f"{label}èµ¤RCIä¸ç«¯âä¸åãåè»¢")
        elif rev=="LONG": L+=3; rl.append(f"{label}èµ¤RCIä¸åãåè»¢(ä¸ä½è¶³ä¸è´ãªã)")
        elif rev=="SHORT": S+=3; rs.append(f"{label}èµ¤RCIä¸åãåè»¢(ä¸ä½è¶³ä¸è´ãªã)")

    # 5åã¯æçµã¿ã¤ãã³ã°ã®ã¿
    rev5,_=red_rci_reversal(M5)
    if bias in ("UP","STRONG_UP") and rev5=="LONG": L+=8; rl.append("5åèµ¤RCIä¸ç«¯âä¸åã(æçµã¿ã¤ãã³ã°)")
    if bias in ("DOWN","STRONG_DOWN") and rev5=="SHORT": S+=8; rs.append("5åèµ¤RCIä¸ç«¯âä¸åã(æçµã¿ã¤ãã³ã°)")
    if c5.close>c5.ema12 and c5.ema_slope5>0: L+=3
    elif c5.close<c5.ema12 and c5.ema_slope5<0: S+=3

    # 1Hæ§é 
    rh,rlow=structure(H1)
    if p>rh: L+=10; rl.append("1Hæ»ãé«å¤çªç ´")
    elif p>rlow and abs(p-rlow)/p<.012 and c15.close>c15.open: L+=10; rl.append("1Hæ¼ãå®å¤åçº")
    if p<rlow: S+=10; rs.append("1Hæ¼ãå®å¤å²ã")
    elif p<rh and abs(p-rh)/p<.012 and c15.close<c15.open: S+=10; rs.append("1Hæ»ãé«å¤æå¦")

    flo,fhi,fsource=major_fib(symbol,W); fL,fS,frL,frS=score_fib(p,c15,fib_levels(flo,fhi)); L+=fL; S+=fS; rl+=frL; rs+=frS
    if pd.notna(c15.span_a) and pd.notna(c15.span_b):
        hi=max(c15.span_a,c15.span_b); lo=min(c15.span_a,c15.span_b)
        if p>hi: L+=5; rl.append("15åé²ä¸")
        elif p<lo: S+=5; rs.append("15åé²ä¸")
    if pd.notna(c15.vol_ma20) and c15.volume>c15.vol_ma20*1.25:
        if c15.close>c15.open: L+=4; rl.append("åºæ¥é«å¢é½ç·")
        elif c15.close<c15.open: S+=4; rs.append("åºæ¥é«å¢é°ç·")

    side="WAIT"; confidence=max(L,S); reasons=[f"LONG {L}/SHORT {S}",f"Fib={fsource}"]
    if L>=ENTRY_THRESHOLD and L>=S+OPPOSITE_GAP: side="LONG"; confidence=min(100,L); reasons=rl
    elif S>=ENTRY_THRESHOLD and S>=L+OPPOSITE_GAP: side="SHORT"; confidence=min(100,S); reasons=rs
    candle_id=str(c15.time)
    if side=="WAIT": return Analysis(symbol,side,L,S,confidence,p,None,None,None,None,None,None,reasons,"æ¡ä»¶ä¸è¶³",candle_id,float(c15.high),float(c15.low))

    atr=float(H1.atr.iloc[-1]); atr=atr if math.isfinite(atr) and atr>0 else p*.012
    if side=="LONG":
        entry_high=p-.08*atr; entry_low=p-.32*atr; stop=min(entry_low-.75*atr,rlow-.15*atr); mid=(entry_low+entry_high)/2; risk=max(mid-stop,p*.003)
        tp1,tp2,tp3=mid+1.4*risk,mid+2.0*risk,mid+2.8*risk; invalid=f"1Hæ¼ãå®å¤ {rlow:,.4f} å²ã"
    else:
        entry_low=p+.08*atr; entry_high=p+.32*atr; stop=max(entry_high+.75*atr,rh+.15*atr); mid=(entry_low+entry_high)/2; risk=max(stop-mid,p*.003)
        tp1,tp2,tp3=mid-1.4*risk,mid-2.0*risk,mid-2.8*risk; invalid=f"1Hæ»ãé«å¤ {rh:,.4f} ä¸æã"
    return Analysis(symbol,side,L,S,confidence,p,entry_low,entry_high,stop,tp1,tp2,tp3,reasons,invalid,candle_id,float(c15.high),float(c15.low))

def load_state():
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except: pass
    return {"paper_balance":PAPER_BALANCE_DEFAULT,"position":None,"pending":None,"last_notified":None,"daily_date":None,"daily_start_balance":PAPER_BALANCE_DEFAULT,"consecutive_losses":0}

def save_state(s): STATE_FILE.write_text(json.dumps(s,ensure_ascii=False,indent=2),encoding="utf-8")

def can_open(state):
    jst=now_jst().date().isoformat()
    if state.get("daily_date")!=jst: state["daily_date"]=jst; state["daily_start_balance"]=state["paper_balance"]; state["consecutive_losses"]=0
    dd=(state["paper_balance"]-state["daily_start_balance"])/max(state["daily_start_balance"],1)
    if dd<=-MAX_DAILY_LOSS_PCT: return False,"1æ¥æå¤§æå¤±å°é"
    if state.get("consecutive_losses",0)>=MAX_CONSECUTIVE_LOSSES: return False,"3é£æåæ­¢"
    if state.get("position"): return False,"ãã¸ã·ã§ã³ä¿æä¸­"
    return True,"OK"

def record_trade(row):
    exists=TRADES_FILE.exists()
    with TRADES_FILE.open("a",newline="",encoding="utf-8") as f:
        w=csv.writer(f)
        if not exists: w.writerow(["time","symbol","side","entry","exit","qty_closed","pnl","balance","event"])
        w.writerow(row)

def touch_zone(pending,high,low):
    lo=min(pending["entry_low"],pending["entry_high"]); hi=max(pending["entry_low"],pending["entry_high"]); return high>=lo and low<=hi

def make_position(state,p):
    mid=(p["entry_low"]+p["entry_high"])/2; risk_per_unit=abs(mid-p["stop"]); risk_yen=state["paper_balance"]*RISK_PCT
    qty=min(risk_yen/max(risk_per_unit,1e-12),(state["paper_balance"]*2)/mid)
    state["position"]={"symbol":p["symbol"],"side":p["side"],"entry":mid,"qty_initial":qty,"qty_remaining":qty,"stop":p["stop"],"original_stop":p["stop"],"tp1":p["tp1"],"tp2":p["tp2"],"tp3":p["tp3"],"tp1_done":False,"tp2_done":False,"tp3_done":False,"realized_pnl":0.0,"opened_at":now_utc().isoformat()}
    return f"ð¯ ä»®æ³ç´å® {p['symbol']} {p['side']}\nä¿¡é ¼åº¦: {p['confidence']}/100\nç´å®: {mid:,.4f}\næ°é: {qty:.8f}"

def manage_pending(state,analyses):
    p=state.get("pending")
    if not p: return None
    if now_utc()-datetime.fromisoformat(p["created_at"])>timedelta(hours=PENDING_EXPIRE_HOURS): state["pending"]=None; return f"â {p['symbol']} {p['side']}åè£å¤±å¹"
    a=next((x for x in analyses if x.symbol==p["symbol"]),None)
    if not a: return None
    if p["side"]=="LONG" and a.candle_low<=p["stop"]: state["pending"]=None; return f"â {p['symbol']} LONGåè£åæ¶"
    if p["side"]=="SHORT" and a.candle_high>=p["stop"]: state["pending"]=None; return f"â {p['symbol']} SHORTåè£åæ¶"
    if touch_zone(p,a.candle_high,a.candle_low): msg=make_position(state,p); state["pending"]=None; return msg
    return None

def manage_position(state,analyses):
    pos=state.get("position")
    if not pos: return []
    a=next((x for x in analyses if x.symbol==pos["symbol"]),None)
    if not a: return []
    high,low,side=a.candle_high,a.candle_low,pos["side"]; msgs=[]
    tp_hit=lambda level: high>=level if side=="LONG" else low<=level
    stop_hit=lambda level: low<=level if side=="LONG" else high>=level
    if stop_hit(pos["stop"]):
        q=max(pos["qty_remaining"],0); pnl=(pos["stop"]-pos["entry"])*q if side=="LONG" else (pos["entry"]-pos["stop"])*q
        pos["realized_pnl"]+=pnl; state["paper_balance"]+=pos["realized_pnl"]; state["consecutive_losses"]=state.get("consecutive_losses",0)+1 if pos["realized_pnl"]<0 else 0
        record_trade([now_utc().isoformat(),pos["symbol"],side,pos["entry"],pos["stop"],q,pnl,state["paper_balance"],"STOP_END"])
        msgs.append(f"ð {pos['symbol']} STOP\næçµæç: {pos['realized_pnl']:+,.0f}å\næ®é«: {state['paper_balance']:,.0f}å"); state["position"]=None; return msgs
    if not pos["tp1_done"] and tp_hit(pos["tp1"]):
        q=pos["qty_initial"]*TP1_PCT; pnl=(pos["tp1"]-pos["entry"])*q if side=="LONG" else (pos["entry"]-pos["tp1"])*q
        pos["qty_remaining"]-=q; pos["realized_pnl"]+=pnl; pos["tp1_done"]=True; pos["stop"]=pos["entry"]; record_trade([now_utc().isoformat(),pos["symbol"],side,pos["entry"],pos["tp1"],q,pnl,state["paper_balance"],"TP1"]); msgs.append(f"â {pos['symbol']} TP1 30%å©ç¢º: {pnl:+,.0f}å\nSTOPãå»ºå¤ã¸ç§»å")
    if not pos["tp2_done"] and tp_hit(pos["tp2"]):
        q=pos["qty_initial"]*TP2_PCT; pnl=(pos["tp2"]-pos["entry"])*q if side=="LONG" else (pos["entry"]-pos["tp2"])*q
        pos["qty_remaining"]-=q; pos["realized_pnl"]+=pnl; pos["tp2_done"]=True; pos["stop"]=pos["tp1"]; record_trade([now_utc().isoformat(),pos["symbol"],side,pos["entry"],pos["tp2"],q,pnl,state["paper_balance"],"TP2"]); msgs.append(f"â {pos['symbol']} TP2 40%å©ç¢º: {pnl:+,.0f}å\nSTOPãTP1ã¸ç§»å")
    if not pos["tp3_done"] and tp_hit(pos["tp3"]):
        q=max(pos["qty_remaining"],0); pnl=(pos["tp3"]-pos["entry"])*q if side=="LONG" else (pos["entry"]-pos["tp3"])*q
        pos["realized_pnl"]+=pnl; state["paper_balance"]+=pos["realized_pnl"]; state["consecutive_losses"]=0; record_trade([now_utc().isoformat(),pos["symbol"],side,pos["entry"],pos["tp3"],q,pnl,state["paper_balance"],"TP3_END"]); msgs.append(f"ð {pos['symbol']} TP3å°é\næçµæç: {pos['realized_pnl']:+,.0f}å\næ®é«: {state['paper_balance']:,.0f}å"); state["position"]=None
    return msgs

def log_signals(analyses):
    exists=SIGNALS_FILE.exists()
    with SIGNALS_FILE.open("a",newline="",encoding="utf-8") as f:
        w=csv.writer(f)
        if not exists: w.writerow(["time","symbol","side","long","short","confidence","price"])
        for a in analyses: w.writerow([now_utc().isoformat(),a.symbol,a.side,a.score_long,a.score_short,a.confidence,a.price])

def rank_text(analyses):
    ranking=sorted(analyses,key=lambda a:a.confidence,reverse=True); icons=["ð¥","ð¥","ð¥","4ï¸â£"]
    return "\n".join(f"{icons[i]} {a.symbol}: {a.side if a.side!='WAIT' else 'è¦éã'} {a.confidence}ç¹" for i,a in enumerate(ranking))

def choose_winner(analyses):
    valid=[a for a in analyses if a.side!="WAIT" and a.confidence>=ENTRY_THRESHOLD]
    return sorted(valid,key=lambda a:a.confidence,reverse=True)[0] if valid else None

def create_pending(state,a):
    state["pending"]={"symbol":a.symbol,"side":a.side,"confidence":a.confidence,"entry_low":a.entry_low,"entry_high":a.entry_high,"stop":a.stop,"tp1":a.tp1,"tp2":a.tp2,"tp3":a.tp3,"created_at":now_utc().isoformat(),"candle_id":a.candle_id}

def notify(text):
    print(text,flush=True)
    if NTFY_TOPIC:
        r=requests.post(f"https://ntfy.sh/{NTFY_TOPIC}",data=text.encode("utf-8"),headers={"Title":"Musigny 4-Crypto BOT v5"},timeout=15); r.raise_for_status()

def fmt_candidate(a,ranking,state):
    strength="ð¥å¼·ã·ã°ãã«" if a.confidence>=STRONG_THRESHOLD else "åè£"
    return f"{strength} {a.symbol} {a.side} {a.confidence}/100\n{ranking}\nç¾å¨å¤: {a.price:,.4f}\nå¾æ©ã¨ã³ããªã¼å¸¯: {min(a.entry_low,a.entry_high):,.4f}ã{max(a.entry_low,a.entry_high):,.4f}\nSTOP: {a.stop:,.4f}\nTP1: {a.tp1:,.4f} / TP2: {a.tp2:,.4f} / TP3: {a.tp3:,.4f}\næ ¹æ : {' / '.join(a.reasons[:10])}\nâ»åè£éç¥ã¯ntfyã«éããã­ã°ã®ã¿"

def main():
    analyses=[]; failed_symbols=[]
    for symbol in SYMBOLS:
        try:
            frames={
                "5min":drop_open_candle(fetch_intraday(symbol,"5min",3,extra_days=7,min_rows=80)),
                "15min":drop_open_candle(fetch_intraday(symbol,"15min",5,extra_days=10,min_rows=80)),
                "1hour":drop_open_candle(fetch_intraday(symbol,"1hour",20,extra_days=12,min_rows=120)),
                "4hour":drop_open_candle(fetch_yearly(symbol,"4hour",2,extra_years=1)),
                "1day":drop_open_candle(fetch_yearly(symbol,"1day",3,extra_years=1)),
                "1week":drop_open_candle(fetch_yearly(symbol,"1week",4,extra_years=1))
            }
            required={"5min":55,"15min":55,"1hour":55,"4hour":55,"1day":55,"1week":50}
            short=[f"{tf}:{len(frames[tf])}æ¬" for tf,n in required.items() if len(frames[tf])<n]
            if short: raise RuntimeError(f"{symbol} ãã¼ã¿ä¸è¶³ "+", ".join(short))
            analyses.append(analyze(symbol,frames))
        except Exception as e:
            failed_symbols.append(symbol)
            print(f"[DATA-SKIP] {symbol}: ä»åã®å¤å®ãã¹ã­ãã ({e})",flush=True)
    state=load_state()
    if not analyses:
        print("[DATA-WAIT] å¨éæã®åæãã¼ã¿åå¾å¤±æãå£²è²·ããæ¬¡åãã§ãã¯ã¸ã",flush=True)
        save_state(state); return
    if failed_symbols: print("[DATA-INFO] ä»åã¹ã­ãã: "+", ".join(failed_symbols),flush=True)
    log_signals(analyses)
    for m in manage_position(state,analyses): notify("ð "+m)
    if not state.get("position"):
        msg=manage_pending(state,analyses)
        if msg:
            if msg.startswith("ð¯ ä»®æ³ç´å®"): notify(msg)
            else: print(msg,flush=True)
    if not state.get("position") and not state.get("pending"):
        winner=choose_winner(analyses); ranking=rank_text(analyses)
        if winner:
            key=hashlib.sha1(f"{winner.symbol}:{winner.candle_id}:{winner.side}".encode()).hexdigest()[:16]
            if state.get("last_notified")!=key:
                ok,why=can_open(state)
                if ok: create_pending(state,winner); print(fmt_candidate(winner,ranking,state),flush=True); state["last_notified"]=key
                else: print("æ°è¦åæ­¢:",why)
            else: print(ranking)
        else: print("å¨éæè¦éã"); print(ranking)
    else:
        if state.get("position"): print("ä»®æ³ãã¸ã·ã§ã³ä¿æä¸­:",state["position"]["symbol"])
        elif state.get("pending"): print("å¾æ©åè£ãã:",state["pending"]["symbol"])
    save_state(state)

if __name__=="__main__": main()
