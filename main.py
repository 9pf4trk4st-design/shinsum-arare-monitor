#!/usr/bin/env python3
# Musigny 4-Crypto BOT v9 - FX-style exits + net profit notifications
from __future__ import annotations
import os, json, math, csv, hashlib, time, sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# GitHub Actions / ntfy の日本語文字化け対策
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"

PUBLIC = "https://api.coin.z.com/public"
SYMBOLS = ["BTC", "ETH", "XRP", "SOL"]
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
STATE_DIR = Path(os.getenv("STATE_DIR", "state")); STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "bot_state.json"
TRADES_FILE = STATE_DIR / "paper_trades_net.csv"
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

# FX版と同じ短期出口
TP1_R = float(os.getenv("TP1_R", "1.0"))
TP2_R = float(os.getenv("TP2_R", "1.5"))
TP3_R = float(os.getenv("TP3_R", "2.2"))
REVIEW_HOURS = float(os.getenv("REVIEW_HOURS", "8"))
MAX_HOLD_HOURS = float(os.getenv("MAX_HOLD_HOURS", "12"))
TRAIL_START_R = float(os.getenv("TRAIL_START_R", "1.5"))
TRAIL_GIVEBACK_R = float(os.getenv("TRAIL_GIVEBACK_R", "0.6"))
REVIEW_CLOSE_R = float(os.getenv("REVIEW_CLOSE_R", "0.3"))

# ===== 実運用に近づけるためのコスト設定 =====
# GMOコインの暗号資産FXを想定すると通常の取引手数料は0。
# 別サービス/注文方式を使う場合は環境変数で変更可能。
TRADE_FEE_RATE = float(os.getenv("TRADE_FEE_RATE", "0.0"))

# 日本時間6:00をまたいだ場合のレバレッジ手数料（0.04%/日）
LEVERAGE_DAILY_RATE = float(os.getenv("LEVERAGE_DAILY_RATE", "0.0004"))
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
                    print(f"[RETRY] GMO API HTTP {r.status_code}; retry in {wait}s")
                    time.sleep(wait); continue
                raise last_error
            r.raise_for_status(); j=r.json()
            if int(j.get("status",1))!=0: raise RuntimeError(f"GMO API error: {j}")
            return j.get("data",[]) or []
        except (requests.Timeout,requests.ConnectionError) as e:
            last_error=e
            if attempt<retries:
                wait=2*attempt
                print(f"[RETRY] GMO API connection error; retry in {wait}s ({e})")
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
                print(f"[INFO] {symbol} {interval} {d}: no KLine; fallback older date"); continue
            print(f"[WARN] {symbol} {interval} {d}: {e}")
        except Exception as e:
            print(f"[WARN] {symbol} {interval} {d}: {e}")
    if not frames:
        raise RuntimeError(f"No data for {symbol} {interval} (JST6時基準で{max_lookback+1}日探索)")
    out=pd.concat(frames).sort_values("time").drop_duplicates("time").reset_index(drop=True)
    if len(out)<min_rows: print(f"[WARN] {symbol} {interval}: only {len(out)} candles fetched")
    return out

def fetch_yearly(symbol, interval, years_back=3, extra_years=1):
    frames=[]; y=now_utc().year
    for year in range(y-years_back-extra_years,y+1):
        try:
            f=frame_from_rows(api_get("/v1/klines",{"symbol":symbol,"interval":interval,"date":str(year)},retries=3))
            if not f.empty: frames.append(f)
        except requests.HTTPError as e:
            if getattr(e.response,"status_code",None)==404:
                print(f"[INFO] {symbol} {interval} {year}: no KLine; skip year"); continue
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
    if lo and hi and float(hi)>float(lo): return float(lo),float(hi),"固定"
    w=weekly.iloc[-160:]; return float(w.low.min()),float(w.high.max()),"自動"

def score_fib(price,candle,levels):
    L=S=0; rl=[]; rs=[]
    for name,lv in levels.items():
        if abs(price-lv)/price<=.008:
            w=12 if name in ("0.382","0.618") else 8 if name=="0.5" else 4
            if candle.low<=lv<=candle.close and candle.close>candle.open: L+=w; rl.append(f"週足Fib{name}反発")
            if candle.close<=lv<=candle.high and candle.close<candle.open: S+=w; rs.append(f"週足Fib{name}拒否")
    return L,S,rl,rs

def analyze(symbol,frames):
    W,D,H4,H1,M15,M5=[indicators(frames[k]) for k in ("1week","1day","4hour","1hour","15min","5min")]
    c15,p15,c5=M15.iloc[-1],M15.iloc[-2],M5.iloc[-1]; p=float(c15.close); L=S=0; rl=[]; rs=[]

    # 4H・1Hの方向
    for label,st,w in [("4H",trend_state_ind(H4),20),("1H",trend_state_ind(H1),15)]:
        if st=="BULL": L+=w; rl.append(f"{label}上向き")
        elif st=="BEAR": S+=w; rs.append(f"{label}下向き")

    # 週足・日足の青/緑RCI
    bias,bpts=higher_rci_bias(W,D)
    if bias in ("UP","STRONG_UP"): L+=bpts; rl.append("週足・日足 青/緑RCI上向き")
    elif bias in ("DOWN","STRONG_DOWN"): S+=bpts; rs.append("週足・日足 青/緑RCI下向き")

    # 15分をメイン
    if c15.close>c15.ema12 and c15.ema_slope5>0: L+=12; rl.append("15分12EMA上")
    elif c15.close<c15.ema12 and c15.ema_slope5<0: S+=12; rs.append("15分12EMA下")
    if pd.notna(c15.rci8) and pd.notna(c15.rci25):
        if c15.rci8>p15.rci8 and c15.rci25>=p15.rci25: L+=8; rl.append("15分RCI短中期上向き")
        if c15.rci8<p15.rci8 and c15.rci25<=p15.rci25: S+=8; rs.append("15分RCI短中期下向き")

    # Musigny式: 上位青緑方向 + 下位赤RCI端から反転
    for label,ind,scale in [("4H",H4,1.0),("1H",H1,.9),("15分",M15,.85)]:
        rev,pts=red_rci_reversal(ind); pts=int(round(pts*scale))
        if bias in ("UP","STRONG_UP") and rev=="LONG": L+=pts; rl.append(f"{label}赤RCI下端→上向き反転")
        elif bias in ("DOWN","STRONG_DOWN") and rev=="SHORT": S+=pts; rs.append(f"{label}赤RCI上端→下向き反転")
        elif rev=="LONG": L+=3; rl.append(f"{label}赤RCI上向き反転(上位足一致なし)")
        elif rev=="SHORT": S+=3; rs.append(f"{label}赤RCI下向き反転(上位足一致なし)")

    # 5分は最終タイミングのみ
    rev5,_=red_rci_reversal(M5)
    if bias in ("UP","STRONG_UP") and rev5=="LONG": L+=8; rl.append("5分赤RCI下端→上向き(最終タイミング)")
    if bias in ("DOWN","STRONG_DOWN") and rev5=="SHORT": S+=8; rs.append("5分赤RCI上端→下向き(最終タイミング)")
    if c5.close>c5.ema12 and c5.ema_slope5>0: L+=3
    elif c5.close<c5.ema12 and c5.ema_slope5<0: S+=3

    # 1H構造
    rh,rlow=structure(H1)
    if p>rh: L+=10; rl.append("1H戻り高値突破")
    elif p>rlow and abs(p-rlow)/p<.012 and c15.close>c15.open: L+=10; rl.append("1H押し安値反発")
    if p<rlow: S+=10; rs.append("1H押し安値割れ")
    elif p<rh and abs(p-rh)/p<.012 and c15.close<c15.open: S+=10; rs.append("1H戻り高値拒否")

    flo,fhi,fsource=major_fib(symbol,W); fL,fS,frL,frS=score_fib(p,c15,fib_levels(flo,fhi)); L+=fL; S+=fS; rl+=frL; rs+=frS
    if pd.notna(c15.span_a) and pd.notna(c15.span_b):
        hi=max(c15.span_a,c15.span_b); lo=min(c15.span_a,c15.span_b)
        if p>hi: L+=5; rl.append("15分雲上")
        elif p<lo: S+=5; rs.append("15分雲下")
    if pd.notna(c15.vol_ma20) and c15.volume>c15.vol_ma20*1.25:
        if c15.close>c15.open: L+=4; rl.append("出来高増陽線")
        elif c15.close<c15.open: S+=4; rs.append("出来高増陰線")

    side="WAIT"; confidence=max(L,S); reasons=[f"LONG {L}/SHORT {S}",f"Fib={fsource}"]
    if L>=ENTRY_THRESHOLD and L>=S+OPPOSITE_GAP: side="LONG"; confidence=min(100,L); reasons=rl
    elif S>=ENTRY_THRESHOLD and S>=L+OPPOSITE_GAP: side="SHORT"; confidence=min(100,S); reasons=rs
    candle_id=str(c15.time)
    if side=="WAIT": return Analysis(symbol,side,L,S,confidence,p,None,None,None,None,None,None,reasons,"条件不足",candle_id,float(c15.high),float(c15.low))

    atr=float(H1.atr.iloc[-1]); atr=atr if math.isfinite(atr) and atr>0 else p*.012
    if side=="LONG":
        entry_high=p-.08*atr; entry_low=p-.32*atr; stop=min(entry_low-.75*atr,rlow-.15*atr); mid=(entry_low+entry_high)/2; risk=max(mid-stop,p*.003)
        tp1,tp2,tp3=mid+TP1_R*risk,mid+TP2_R*risk,mid+TP3_R*risk; invalid=f"1H押し安値 {rlow:,.4f} 割れ"
    else:
        entry_low=p+.08*atr; entry_high=p+.32*atr; stop=max(entry_high+.75*atr,rh+.15*atr); mid=(entry_low+entry_high)/2; risk=max(stop-mid,p*.003)
        tp1,tp2,tp3=mid-TP1_R*risk,mid-TP2_R*risk,mid-TP3_R*risk; invalid=f"1H戻り高値 {rh:,.4f} 上抜け"
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
    if dd<=-MAX_DAILY_LOSS_PCT: return False,"1日最大損失到達"
    if state.get("consecutive_losses",0)>=MAX_CONSECUTIVE_LOSSES: return False,"3連敗停止"
    if state.get("position"): return False,"ポジション保有中"
    return True,"OK"

def record_trade(row):
    exists=TRADES_FILE.exists()
    with TRADES_FILE.open("a",newline="",encoding="utf-8") as f:
        w=csv.writer(f)
        if not exists:
            w.writerow([
                "time","symbol","side","entry","exit","qty_closed",
                "gross_pnl","trading_cost","leverage_fee","total_cost",
                "net_pnl","balance","event"
            ])
        w.writerow(row)

def touch_zone(pending,high,low):
    lo=min(pending["entry_low"],pending["entry_high"]); hi=max(pending["entry_low"],pending["entry_high"]); return high>=lo and low<=hi

def make_position(state,p,tickers):
    mid=(p["entry_low"]+p["entry_high"])/2
    risk_per_unit=abs(mid-p["stop"])
    risk_yen=state["paper_balance"]*RISK_PCT
    qty=min(
        risk_yen/max(risk_per_unit,1e-12),
        (state["paper_balance"]*2)/mid
    )

    # エントリー側のスプレッドを想定コストとして保存。
    # EXITはLONG=BID / SHORT=ASKを使うため、出口側スプレッドは価格に含まれる。
    t=tickers.get(p["symbol"],{})
    try:
        bid=float(t.get("bid"))
        ask=float(t.get("ask"))
        entry_half_spread=max(ask-bid,0.0)/2
    except Exception:
        entry_half_spread=0.0

    state["position"]={
        "symbol":p["symbol"],
        "side":p["side"],
        "entry":mid,
        "qty_initial":qty,
        "qty_remaining":qty,
        "stop":p["stop"],
        "original_stop":p["stop"],
        "initial_risk":risk_per_unit,
        "tp1":p["tp1"],
        "tp2":p["tp2"],
        "tp3":p["tp3"],
        "tp1_done":False,
        "tp2_done":False,
        "tp3_done":False,

        # realized_pnlは「純利益」
        "realized_pnl":0.0,
        "realized_gross_pnl":0.0,
        "realized_cost":0.0,

        "entry_half_spread":entry_half_spread,
        "opened_at":now_utc().isoformat(),
        "max_r":0.0,
        "exit_logic":"FX_STYLE_V2_NET"
    }

    return (
        f"🎯 仮想約定 {p['symbol']} {p['side']}\n"
        f"信頼度: {p['confidence']}/100\n"
        f"約定: {mid:,.4f}\n"
        f"数量: {qty:.8f}\n"
        f"TP1=1.0R / TP2=1.5R / TP3=2.2R\n"
        f"※以後の損益通知は想定コスト差引後の純利益"
    )

def manage_pending(state,analyses,tickers):
    p=state.get("pending")
    if not p: return None
    if now_utc()-datetime.fromisoformat(p["created_at"])>timedelta(hours=PENDING_EXPIRE_HOURS): state["pending"]=None; return f"⌛ {p['symbol']} {p['side']}候補失効"
    a=next((x for x in analyses if x.symbol==p["symbol"]),None)
    if not a: return None
    if p["side"]=="LONG" and a.candle_low<=p["stop"]: state["pending"]=None; return f"❌ {p['symbol']} LONG候補取消"
    if p["side"]=="SHORT" and a.candle_high>=p["stop"]: state["pending"]=None; return f"❌ {p['symbol']} SHORT候補取消"
    if touch_zone(p,a.candle_high,a.candle_low): msg=make_position(state,p,tickers); state["pending"]=None; return msg
    return None

def fetch_live_tickers():
    out={}
    for symbol in SYMBOLS:
        try:
            data=api_get("/v1/ticker",{"symbol":symbol},retries=3)
            if data:
                out[symbol]=data[0]
        except Exception as e:
            print(f"[TICKER-WARN] {symbol}: {e}",flush=True)
    return out

def live_exit_price(symbol,side,tickers):
    t=tickers.get(symbol)
    if not t:
        return None
    # LONG決済はBID、SHORT決済はASKを優先
    key="bid" if side=="LONG" else "ask"
    if t.get(key) is not None:
        return float(t[key])
    if t.get("last") is not None:
        return float(t["last"])
    return None

def migrate_position_exit(pos):
    """旧ポジションも新しいRベース＋純利益管理へ自動移行。"""
    entry=float(pos["entry"])
    original_stop=float(pos.get("original_stop",pos["stop"]))
    risk=max(abs(entry-original_stop),entry*.003,1e-12)

    pos["original_stop"]=original_stop
    pos["initial_risk"]=risk
    pos["opened_at"]=pos.get("opened_at") or now_utc().isoformat()
    pos["max_r"]=float(pos.get("max_r",0.0))

    if not str(pos.get("exit_logic","")).startswith("FX_STYLE_V2"):
        if pos["side"]=="LONG":
            pos["tp1"]=entry+TP1_R*risk
            pos["tp2"]=entry+TP2_R*risk
            pos["tp3"]=entry+TP3_R*risk
        else:
            pos["tp1"]=entry-TP1_R*risk
            pos["tp2"]=entry-TP2_R*risk
            pos["tp3"]=entry-TP3_R*risk

    # 旧stateとの互換
    legacy=float(pos.get("realized_pnl",0.0))
    pos.setdefault("realized_gross_pnl",legacy)
    pos.setdefault("realized_cost",0.0)
    pos.setdefault("entry_half_spread",0.0)
    pos["exit_logic"]="FX_STYLE_V2_NET"

def position_r(pos,price):
    risk=max(float(pos.get("initial_risk",abs(pos["entry"]-pos.get("original_stop",pos["stop"])))),1e-12)
    if pos["side"]=="LONG":
        return (price-pos["entry"])/risk
    return (pos["entry"]-price)/risk

def strong_reversal_against_position(pos,analyses):
    a=next((x for x in analyses if x.symbol==pos["symbol"]),None)
    if not a:
        return False

    if pos["side"]=="LONG":
        return (
            a.score_short>=ENTRY_THRESHOLD and
            a.score_short>=a.score_long+OPPOSITE_GAP
        )

    return (
        a.score_long>=ENTRY_THRESHOLD and
        a.score_long>=a.score_short+OPPOSITE_GAP
    )

def leverage_fee_crossings(opened_at,closed_at):
    """保有中に日本時間06:00を何回またいだか。"""
    try:
        opened=datetime.fromisoformat(opened_at)
        if opened.tzinfo is None:
            opened=opened.replace(tzinfo=timezone.utc)
    except Exception:
        return 0

    closed=closed_at
    if closed.tzinfo is None:
        closed=closed.replace(tzinfo=timezone.utc)

    oj=opened.astimezone(timezone(timedelta(hours=9)))
    cj=closed.astimezone(timezone(timedelta(hours=9)))

    # 開始日から終了日までの06:00 JSTを数える
    d=oj.date()
    count=0
    while d<=cj.date():
        boundary=datetime(d.year,d.month,d.day,6,0,0,tzinfo=timezone(timedelta(hours=9)))
        if oj < boundary <= cj:
            count+=1
        d+=timedelta(days=1)
    return count


def calc_close_cost(pos,price,qty,closed_at=None):
    """
    想定コスト:
      1) エントリー側の半スプレッド
      2) 売買手数料（環境変数。暗号資産FX想定の初期値0）
      3) 06:00 JSTまたぎのレバレッジ手数料
    EXIT側のスプレッドはBID/ASK決済価格に既に反映済み。
    """
    closed_at=closed_at or now_utc()
    entry=float(pos["entry"])

    entry_spread_cost=float(pos.get("entry_half_spread",0.0))*qty

    entry_fee=entry*qty*TRADE_FEE_RATE
    exit_fee=price*qty*TRADE_FEE_RATE
    trading_cost=entry_spread_cost+entry_fee+exit_fee

    crossings=leverage_fee_crossings(pos.get("opened_at",closed_at.isoformat()),closed_at)
    leverage_fee=entry*qty*LEVERAGE_DAILY_RATE*crossings

    total_cost=trading_cost+leverage_fee
    return trading_cost,leverage_fee,total_cost


def settle_piece(state,pos,price,qty,event):
    side=pos["side"]
    gross=(
        (price-pos["entry"])*qty
        if side=="LONG"
        else (pos["entry"]-price)*qty
    )

    trading_cost,leverage_fee,total_cost=calc_close_cost(
        pos,price,qty,now_utc()
    )
    net=gross-total_cost

    pos["realized_gross_pnl"]=float(pos.get("realized_gross_pnl",0.0))+gross
    pos["realized_cost"]=float(pos.get("realized_cost",0.0))+total_cost
    pos["realized_pnl"]=float(pos.get("realized_pnl",0.0))+net

    record_trade([
        now_utc().isoformat(),
        pos["symbol"],
        side,
        pos["entry"],
        price,
        qty,
        gross,
        trading_cost,
        leverage_fee,
        total_cost,
        net,
        state["paper_balance"],
        event
    ])

    return gross,trading_cost,leverage_fee,total_cost,net


def close_remaining(state,pos,price,event,label):
    q=max(pos["qty_remaining"],0)

    gross,trading_cost,leverage_fee,total_cost,net=settle_piece(
        state,pos,price,q,event
    )

    # トレード全体の純利益だけを残高へ反映
    state["paper_balance"]+=pos["realized_pnl"]

    state["consecutive_losses"]=(
        state.get("consecutive_losses",0)+1
        if pos["realized_pnl"]<0
        else 0
    )

    msg=(
        f"{label} {pos['symbol']}\n"
        f"今回売買損益: {gross:+,.0f}円\n"
        f"今回想定コスト: -{total_cost:,.0f}円\n"
        f"トレード総売買損益: {pos['realized_gross_pnl']:+,.0f}円\n"
        f"トレード総コスト: -{pos['realized_cost']:,.0f}円\n"
        f"最終純利益: {pos['realized_pnl']:+,.0f}円\n"
        f"残高: {state['paper_balance']:,.0f}円"
    )

    state["position"]=None
    return msg

def manage_position(state,analyses,tickers):
    pos=state.get("position")
    if not pos:
        return []

    migrate_position_exit(pos)

    price=live_exit_price(pos["symbol"],pos["side"],tickers)
    if price is None:
        print(f"[POSITION-WARN] {pos['symbol']}: live price unavailable",flush=True)
        return []

    side=pos["side"]
    msgs=[]

    cur_r=position_r(pos,price)
    pos["max_r"]=max(float(pos.get("max_r",0.0)),cur_r)

    opened=datetime.fromisoformat(pos["opened_at"])
    age_h=(now_utc()-opened).total_seconds()/3600

    tp_hit=lambda level: price>=level if side=="LONG" else price<=level
    stop_hit=lambda level: price<=level if side=="LONG" else price>=level

    print(
        f"[POSITION] {pos['symbol']} {side} "
        f"ENTRY={pos['entry']:.4f} NOW={price:.4f} "
        f"R={cur_r:+.2f} MAX_R={pos['max_r']:+.2f} AGE={age_h:.1f}h "
        f"STOP={pos['stop']:.4f} "
        f"TP1={pos['tp1']:.4f} TP2={pos['tp2']:.4f} TP3={pos['tp3']:.4f}",
        flush=True
    )

    # 1. STOP最優先
    if stop_hit(pos["stop"]):
        msgs.append(
            close_remaining(
                state,pos,price,"STOP_END","🛑 仮想STOP"
            )
        )
        return msgs

    # 2. 反対方向の強シグナルで撤退
    if strong_reversal_against_position(pos,analyses):
        msgs.append(
            close_remaining(
                state,pos,price,"REVERSAL_END","🔄 仮想反転決済"
            )
        )
        return msgs

    # 3. 最大12時間で時間切れ
    if age_h>=MAX_HOLD_HOURS:
        msgs.append(
            close_remaining(
                state,pos,price,"TIME_END","⏰ 仮想時間切れ決済"
            )
        )
        return msgs

    # 4. 8時間後、TP1未達かつ+0.3R以下なら撤退
    if (
        age_h>=REVIEW_HOURS and
        not pos.get("tp1_done",False) and
        cur_r<=REVIEW_CLOSE_R
    ):
        msgs.append(
            close_remaining(
                state,pos,price,"REVIEW_END","🕗 仮想見直し決済"
            )
        )
        return msgs

    # 5. +1.5R以上まで伸びた後、最高値から0.6R戻したら全決済
    if (
        pos["max_r"]>=TRAIL_START_R and
        cur_r<=pos["max_r"]-TRAIL_GIVEBACK_R
    ):
        msgs.append(
            close_remaining(
                state,pos,price,"TRAIL_END","📉 仮想トレーリング決済"
            )
        )
        return msgs

    # 6. TP1 = +1.0R / 30%
    if not pos.get("tp1_done",False) and tp_hit(pos["tp1"]):
        q=pos["qty_initial"]*TP1_PCT
        gross,trading_cost,leverage_fee,total_cost,net=settle_piece(
            state,pos,price,q,"TP1"
        )
        pos["qty_remaining"]-=q
        pos["tp1_done"]=True
        pos["stop"]=pos["entry"]

        msgs.append(
            f"✅ {pos['symbol']} TP1 30%利確\n"
            f"売買損益: {gross:+,.0f}円\n"
            f"想定コスト: -{total_cost:,.0f}円\n"
            f"純利益: {net:+,.0f}円\n"
            f"累計純利益: {pos['realized_pnl']:+,.0f}円\n"
            f"STOPを建値へ"
        )

    # 7. TP2 = +1.5R / 40%
    if not pos.get("tp2_done",False) and tp_hit(pos["tp2"]):
        q=pos["qty_initial"]*TP2_PCT
        gross,trading_cost,leverage_fee,total_cost,net=settle_piece(
            state,pos,price,q,"TP2"
        )
        pos["qty_remaining"]-=q
        pos["tp2_done"]=True

        risk=float(pos["initial_risk"])
        pos["stop"]=(
            pos["entry"]+risk
            if side=="LONG"
            else pos["entry"]-risk
        )

        msgs.append(
            f"✅ {pos['symbol']} TP2 40%利確\n"
            f"売買損益: {gross:+,.0f}円\n"
            f"想定コスト: -{total_cost:,.0f}円\n"
            f"純利益: {net:+,.0f}円\n"
            f"累計純利益: {pos['realized_pnl']:+,.0f}円\n"
            f"STOPを+1Rへ"
        )

    # 8. TP3 = +2.2R / 残り全決済
    if tp_hit(pos["tp3"]):
        msgs.append(
            close_remaining(
                state,pos,price,"TP3_END","🏁 仮想TP3決済"
            )
        )
        return msgs

    return msgs

def log_signals(analyses):
    exists=SIGNALS_FILE.exists()
    with SIGNALS_FILE.open("a",newline="",encoding="utf-8") as f:
        w=csv.writer(f)
        if not exists: w.writerow(["time","symbol","side","long","short","confidence","price"])
        for a in analyses: w.writerow([now_utc().isoformat(),a.symbol,a.side,a.score_long,a.score_short,a.confidence,a.price])

def rank_text(analyses):
    ranking=sorted(analyses,key=lambda a:a.confidence,reverse=True)
    return "\n".join(
        f"#{i+1} {a.symbol}: {a.side if a.side!='WAIT' else 'WAIT'} {a.confidence}"
        for i,a in enumerate(ranking)
    )

def print_analysis_summary(analyses, failed_symbols=None):
    """GitHub Actionsで文字化けしにくいASCII中心の分析一覧。"""
    failed_symbols = failed_symbols or []
    by_symbol = {a.symbol: a for a in analyses}

    print("", flush=True)
    print("========== MUSIGNY ANALYSIS ==========", flush=True)

    for symbol in SYMBOLS:
        a = by_symbol.get(symbol)

        if a is None:
            status = "DATA_SKIP" if symbol in failed_symbols else "NO_DATA"
            print(f"{symbol:>3} | {status}", flush=True)
            continue

        strength = "STRONG" if a.confidence >= STRONG_THRESHOLD and a.side != "WAIT" else ""
        print(
            f"{symbol:>3} | {a.side:<5} | "
            f"LONG={a.score_long:>3} SHORT={a.score_short:>3} "
            f"CONF={a.confidence:>3} {strength}",
            flush=True
        )

    print("======================================", flush=True)
    print("", flush=True)

def choose_winner(analyses):
    valid=[a for a in analyses if a.side!="WAIT" and a.confidence>=ENTRY_THRESHOLD]
    return sorted(valid,key=lambda a:a.confidence,reverse=True)[0] if valid else None

def create_pending(state,a):
    state["pending"]={"symbol":a.symbol,"side":a.side,"confidence":a.confidence,"entry_low":a.entry_low,"entry_high":a.entry_high,"stop":a.stop,"tp1":a.tp1,"tp2":a.tp2,"tp3":a.tp3,"created_at":now_utc().isoformat(),"candle_id":a.candle_id}

def notify(t):
    print(t, flush=True)
    if NTFY_TOPIC:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=t.encode("utf-8"),
            headers={
                "Title": "Musigny 4-Crypto BOT v9",
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=15,
        ).raise_for_status()

def fmt_candidate(a,ranking,state):
    strength = "強シグナル" if a.confidence >= STRONG_THRESHOLD else "候補"
    return (
        f"===== ENTRY {strength} =====\n"
        f"銘柄: {a.symbol}\n"
        f"方向: {a.side}\n"
        f"信頼度: {a.confidence}/100\n"
        f"{ranking}\n"
        f"現在値: {a.price:,.4f}\n"
        f"待機エントリー帯: "
        f"{min(a.entry_low,a.entry_high):,.4f} - "
        f"{max(a.entry_low,a.entry_high):,.4f}\n"
        f"STOP: {a.stop:,.4f}\n"
        f"TP1: {a.tp1:,.4f}\n"
        f"TP2: {a.tp2:,.4f}\n"
        f"TP3: {a.tp3:,.4f}\n"
        f"根拠: {' / '.join(a.reasons[:10])}\n"
        f"候補はログのみ。約定時にntfy通知。\n"
        f"=========================="
    )

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
            short=[f"{tf}:{len(frames[tf])}本" for tf,n in required.items() if len(frames[tf])<n]
            if short: raise RuntimeError(f"{symbol} データ不足 "+", ".join(short))
            analyses.append(analyze(symbol,frames))
        except Exception as e:
            failed_symbols.append(symbol)
            print(f"[DATA-SKIP] {symbol}: skip this cycle ({e})", flush=True)
    state=load_state()
    tickers=fetch_live_tickers()

    # 保有中ポジションは最新価格で先に決済監視
    for m in manage_position(state,analyses,tickers):
        notify("📊 "+m)

    if not analyses:
        print("[DATA-WAIT] No symbols available. Position monitoring only.", flush=True)
        save_state(state); return

    if failed_symbols:
        print("[DATA-INFO] skipped symbols: "+", ".join(failed_symbols), flush=True)

    print_analysis_summary(analyses, failed_symbols)
    log_signals(analyses)
    if not state.get("position"):
        msg=manage_pending(state,analyses,tickers)
        if msg:
            if msg.startswith("🎯 仮想約定"): notify(msg)
            else: print(msg,flush=True)
    if not state.get("position") and not state.get("pending"):
        winner=choose_winner(analyses); ranking=rank_text(analyses)
        if winner:
            key=hashlib.sha1(f"{winner.symbol}:{winner.candle_id}:{winner.side}".encode()).hexdigest()[:16]
            if state.get("last_notified")!=key:
                ok,why=can_open(state)
                if ok: create_pending(state,winner); print(fmt_candidate(winner,ranking,state),flush=True); state["last_notified"]=key
                else: print("新規停止:",why)
            else: print(ranking)
        else: print("[TRADE] No entry candidate this cycle", flush=True); print(ranking, flush=True)
    else:
        if state.get("position"): print("[STATE] OPEN_POSITION:", state["position"]["symbol"], flush=True)
        elif state.get("pending"): print("[STATE] PENDING:", state["pending"]["symbol"], flush=True)
    save_state(state)

if __name__=="__main__": main()
