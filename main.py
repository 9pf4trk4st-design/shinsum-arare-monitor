#!/usr/bin/env python3
# Musigny 4-Crypto DayTrade BOT v4
# BTC / ETH / XRP / SOL
# GMO Public API / シグナル通知 + 待機注文型の仮想売買（実売買なし）

from __future__ import annotations
import os, json, math, csv, hashlib
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

PUBLIC = "https://api.coin.z.com/public"
SYMBOLS = ["BTC", "ETH", "XRP", "SOL"]
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")

STATE_DIR = Path(os.getenv("STATE_DIR", "state"))
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "bot_state.json"
TRADES_FILE = STATE_DIR / "paper_trades.csv"
SIGNALS_FILE = STATE_DIR / "signal_log.csv"

EMA_PERIOD = 12
RCI_PERIODS = (8, 25, 47)

ENTRY_THRESHOLD = int(os.getenv("ENTRY_THRESHOLD", "72"))
OPPOSITE_GAP = int(os.getenv("OPPOSITE_GAP", "15"))
WINNER_GAP = int(os.getenv("WINNER_GAP", "5"))

PAPER_BALANCE_DEFAULT = float(os.getenv("PAPER_BALANCE", "100000"))
RISK_PCT = float(os.getenv("RISK_PCT", "0.0075"))
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.03"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))

TP1_PCT = 0.30
TP2_PCT = 0.40
TP3_PCT = 0.30

PENDING_EXPIRE_HOURS = int(os.getenv("PENDING_EXPIRE_HOURS", "8"))

@dataclass
class Analysis:
    symbol: str
    side: str
    score_long: int
    score_short: int
    confidence: int
    price: float
    entry_low: float | None
    entry_high: float | None
    stop: float | None
    tp1: float | None
    tp2: float | None
    tp3: float | None
    reasons: list[str]
    invalidation: str
    candle_id: str
    candle_high: float
    candle_low: float

def now_utc():
    return datetime.now(timezone.utc)

def now_jst():
    return now_utc() + timedelta(hours=9)

def api_get(path, params):
    r = requests.get(PUBLIC + path, params=params, timeout=25)
    r.raise_for_status()
    j = r.json()
    if int(j.get("status", 1)) != 0:
        raise RuntimeError(f"GMO API error: {j}")
    return j["data"]

def frame_from_rows(rows):
    out = pd.DataFrame([{
        "time": pd.to_datetime(int(x["openTime"]), unit="ms", utc=True),
        "open": float(x["open"]),
        "high": float(x["high"]),
        "low": float(x["low"]),
        "close": float(x["close"]),
        "volume": float(x["volume"]),
    } for x in rows])
    if out.empty:
        return out
    return out.sort_values("time").drop_duplicates("time").reset_index(drop=True)

def fetch_intraday(symbol, interval, days):
    frames = []
    jst = now_jst()
    for i in range(days + 1):
        d = (jst - timedelta(days=i)).strftime("%Y%m%d")
        try:
            rows = api_get("/v1/klines", {"symbol": symbol, "interval": interval, "date": d})
            f = frame_from_rows(rows)
            if not f.empty:
                frames.append(f)
        except Exception as e:
            print(f"[WARN] {symbol} {interval} {d}: {e}")
    if not frames:
        raise RuntimeError(f"No data for {symbol} {interval}")
    return pd.concat(frames).sort_values("time").drop_duplicates("time").reset_index(drop=True)

def fetch_yearly(symbol, interval, years_back=3):
    y = now_utc().year
    frames = []
    for year in range(y-years_back, y+1):
        try:
            rows = api_get("/v1/klines", {"symbol": symbol, "interval": interval, "date": str(year)})
            f = frame_from_rows(rows)
            if not f.empty:
                frames.append(f)
        except Exception as e:
            print(f"[WARN] {symbol} {interval} {year}: {e}")
    if not frames:
        raise RuntimeError(f"No data for {symbol} {interval}")
    return pd.concat(frames).sort_values("time").drop_duplicates("time").reset_index(drop=True)

def drop_open_candle(df):
    return df.iloc[:-1].copy().reset_index(drop=True) if len(df) >= 3 else df

def rci(series, period):
    out = np.full(len(series), np.nan)
    vals = series.to_numpy(float)
    for i in range(period-1, len(vals)):
        w = vals[i-period+1:i+1]
        tr = np.arange(1, period+1, dtype=float)
        pr = pd.Series(w).rank(method="average").to_numpy(float)
        d2 = np.sum((tr-pr)**2)
        out[i] = (1 - 6*d2/(period*(period**2-1))) * 100
    return pd.Series(out, index=series.index)

def ichimoku(df):
    x=df.copy()
    h9=x["high"].rolling(9).max(); l9=x["low"].rolling(9).min()
    h26=x["high"].rolling(26).max(); l26=x["low"].rolling(26).min()
    h52=x["high"].rolling(52).max(); l52=x["low"].rolling(52).min()
    x["tenkan"]=(h9+l9)/2
    x["kijun"]=(h26+l26)/2
    x["span_a"]=(x["tenkan"]+x["kijun"])/2
    x["span_b"]=(h52+l52)/2
    return x

def indicators(df):
    x=df.copy()
    x["ema12"]=x["close"].ewm(span=12, adjust=False).mean()
    x["ema_slope5"]=x["ema12"].pct_change(5)
    for p in RCI_PERIODS:
        x[f"rci{p}"]=rci(x["close"], p)
    x["atr"]=(x["high"]-x["low"]).rolling(14).mean()
    x["vol_ma20"]=x["volume"].rolling(20).mean()
    return ichimoku(x)

def trend_state(df):
    x=indicators(df); r=x.iloc[-1]
    bull=bear=0
    if r["close"]>r["ema12"] and r["ema_slope5"]>0: bull+=2
    if r["close"]<r["ema12"] and r["ema_slope5"]<0: bear+=2
    if pd.notna(r["span_a"]) and pd.notna(r["span_b"]):
        hi=max(r["span_a"],r["span_b"]); lo=min(r["span_a"],r["span_b"])
        if r["close"]>hi: bull+=2
        elif r["close"]<lo: bear+=2
    if pd.notna(r["rci25"]) and pd.notna(r["rci47"]):
        if r["rci25"]>0 and r["rci47"]>-50: bull+=1
        if r["rci25"]<0 and r["rci47"]<50: bear+=1
    if bull>=bear+2: return "BULL"
    if bear>=bull+2: return "BEAR"
    return "NEUTRAL"

def pivot_levels(df,left=3,right=3):
    highs=[]; lows=[]
    h=df["high"].to_numpy(); l=df["low"].to_numpy()
    for i in range(left,len(df)-right):
        if h[i]>=np.max(h[i-left:i+right+1]): highs.append((i,h[i]))
        if l[i]<=np.min(l[i-left:i+right+1]): lows.append((i,l[i]))
    return highs,lows

def structure(df):
    x=df.iloc[-180:].reset_index(drop=True)
    highs,lows=pivot_levels(x,3,3)
    rh=highs[-1][1] if highs else x["high"].iloc[-30:].max()
    rl=lows[-1][1] if lows else x["low"].iloc[-30:].min()
    return rh,rl

def fib_levels(low, high):
    d=high-low
    return {"0.236":high-d*.236,"0.382":high-d*.382,"0.5":high-d*.5,"0.618":high-d*.618,"0.786":high-d*.786}

def major_fib(symbol, weekly):
    lo=os.getenv(f"FIB_{symbol}_LOW")
    hi=os.getenv(f"FIB_{symbol}_HIGH")
    if lo and hi:
        lo=float(lo); hi=float(hi)
        if hi>lo:
            return lo,hi,"固定"
    w=weekly.iloc[-160:]
    return float(w["low"].min()),float(w["high"].max()),"自動"

def score_fib(price,candle,levels):
    L=S=0; rl=[]; rs=[]
    for name,lv in levels.items():
        if abs(price-lv)/price<=.008:
            w=12 if name in ("0.382","0.618") else 8 if name=="0.5" else 4
            if candle["low"]<=lv<=candle["close"] and candle["close"]>candle["open"]:
                L+=w; rl.append(f"週足Fib{name}反発")
            if candle["close"]<=lv<=candle["high"] and candle["close"]<candle["open"]:
                S+=w; rs.append(f"週足Fib{name}拒否")
    return L,S,rl,rs

def analyze(symbol,frames):
    W=indicators(frames["1week"])
    H1=indicators(frames["1hour"])
    M15=indicators(frames["15min"])
    c=M15.iloc[-1]; prev=M15.iloc[-2]
    p=float(c["close"]); L=S=0; rl=[]; rs=[]

    states={
        "週足":trend_state(frames["1week"]),
        "日足":trend_state(frames["1day"]),
        "4H":trend_state(frames["4hour"]),
        "1H":trend_state(frames["1hour"]),
    }
    weights={"週足":8,"日足":14,"4H":18,"1H":10}
    for label,state in states.items():
        if state=="BULL":
            L+=weights[label]; rl.append(f"{label}上向き")
        elif state=="BEAR":
            S+=weights[label]; rs.append(f"{label}下向き")

    if c["close"]>c["ema12"] and c["ema_slope5"]>0:
        L+=10; rl.append("15分12EMA上")
    if c["close"]<c["ema12"] and c["ema_slope5"]<0:
        S+=10; rs.append("15分12EMA下")

    if pd.notna(c["rci8"]) and pd.notna(c["rci25"]) and pd.notna(c["rci47"]):
        if c["rci8"]>prev["rci8"] and c["rci25"]>=prev["rci25"] and c["rci47"]>-80:
            L+=14; rl.append("15分RCI上向き")
        if c["rci8"]<prev["rci8"] and c["rci25"]<=prev["rci25"] and c["rci47"]<80:
            S+=14; rs.append("15分RCI下向き")
        if c["rci8"]>90:
            L-=8; rl.append("RCI過熱で追撃減点")
        if c["rci8"]<-90:
            S-=8; rs.append("RCI売られすぎで追撃減点")

    rh,rlow=structure(H1)
    if p>rh:
        L+=12; rl.append("1H戻り高値突破")
    elif p>rlow and abs(p-rlow)/p<.012 and c["close"]>c["open"]:
        L+=12; rl.append("1H押し安値反発")

    if p<rlow:
        S+=12; rs.append("1H押し安値割れ")
    elif p<rh and abs(p-rh)/p<.012 and c["close"]<c["open"]:
        S+=12; rs.append("1H戻り高値拒否")

    flo,fhi,fsource=major_fib(symbol,W)
    fL,fS,frL,frS=score_fib(p,c,fib_levels(flo,fhi))
    L+=fL; S+=fS; rl+=frL; rs+=frS

    if pd.notna(c["span_a"]) and pd.notna(c["span_b"]):
        hi=max(c["span_a"],c["span_b"]); lo=min(c["span_a"],c["span_b"])
        if p>hi:
            L+=6; rl.append("15分雲上")
        elif p<lo:
            S+=6; rs.append("15分雲下")

    if pd.notna(c["vol_ma20"]) and c["volume"]>c["vol_ma20"]*1.25:
        if c["close"]>c["open"]:
            L+=5; rl.append("出来高増陽線")
        elif c["close"]<c["open"]:
            S+=5; rs.append("出来高増陰線")

    side="WAIT"; confidence=max(L,S); reasons=[f"LONG {L}/SHORT {S}",f"Fib={fsource}"]
    if L>=ENTRY_THRESHOLD and L>=S+OPPOSITE_GAP:
        side="LONG"; confidence=min(100,L); reasons=rl
    elif S>=ENTRY_THRESHOLD and S>=L+OPPOSITE_GAP:
        side="SHORT"; confidence=min(100,S); reasons=rs

    candle_id=str(c["time"])
    if side=="WAIT":
        return Analysis(symbol,side,L,S,confidence,p,None,None,None,None,None,None,reasons,"条件不足",candle_id,float(c["high"]),float(c["low"]))

    atr=float(H1["atr"].iloc[-1])
    if not math.isfinite(atr) or atr<=0:
        atr=p*.012

    if side=="LONG":
        entry_high=p-.08*atr
        entry_low=p-.32*atr
        stop=min(entry_low-.75*atr,rlow-.15*atr)
        mid=(entry_low+entry_high)/2
        risk=max(mid-stop,p*.003)
        tp1=mid+1.4*risk; tp2=mid+2.0*risk; tp3=mid+2.8*risk
        invalid=f"1H押し安値 {rlow:,.4f} 割れ"
    else:
        entry_low=p+.08*atr
        entry_high=p+.32*atr
        stop=max(entry_high+.75*atr,rh+.15*atr)
        mid=(entry_low+entry_high)/2
        risk=max(stop-mid,p*.003)
        tp1=mid-1.4*risk; tp2=mid-2.0*risk; tp3=mid-2.8*risk
        invalid=f"1H戻り高値 {rh:,.4f} 上抜け"

    return Analysis(symbol,side,L,S,confidence,p,entry_low,entry_high,stop,tp1,tp2,tp3,reasons,invalid,candle_id,float(c["high"]),float(c["low"]))

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except:
            pass
    return {
        "paper_balance":PAPER_BALANCE_DEFAULT,
        "position":None,
        "pending":None,
        "last_notified":None,
        "daily_date":None,
        "daily_start_balance":PAPER_BALANCE_DEFAULT,
        "consecutive_losses":0
    }

def save_state(s):
    STATE_FILE.write_text(json.dumps(s,ensure_ascii=False,indent=2),encoding="utf-8")

def can_open(state):
    jst=now_jst().date().isoformat()
    if state.get("daily_date")!=jst:
        state["daily_date"]=jst
        state["daily_start_balance"]=state["paper_balance"]
        state["consecutive_losses"]=0
    dd=(state["paper_balance"]-state["daily_start_balance"])/max(state["daily_start_balance"],1)
    if dd<=-MAX_DAILY_LOSS_PCT:
        return False,"1日最大損失到達"
    if state.get("consecutive_losses",0)>=MAX_CONSECUTIVE_LOSSES:
        return False,"3連敗停止"
    if state.get("position"):
        return False,"ポジション保有中"
    return True,"OK"

def record_trade(row):
    exists=TRADES_FILE.exists()
    with TRADES_FILE.open("a",newline="",encoding="utf-8") as f:
        w=csv.writer(f)
        if not exists:
            w.writerow(["time","symbol","side","entry","exit","qty_closed","pnl","balance","event"])
        w.writerow(row)

def touch_zone(pending, high, low):
    lo=min(pending["entry_low"],pending["entry_high"])
    hi=max(pending["entry_low"],pending["entry_high"])
    return high>=lo and low<=hi

def make_position(state,p):
    mid=(p["entry_low"]+p["entry_high"])/2
    risk_per_unit=abs(mid-p["stop"])
    risk_yen=state["paper_balance"]*RISK_PCT
    qty=risk_yen/max(risk_per_unit,1e-12)
    qty=min(qty,(state["paper_balance"]*2)/mid)
    state["position"]={
        "symbol":p["symbol"],"side":p["side"],"entry":mid,
        "qty_initial":qty,"qty_remaining":qty,
        "stop":p["stop"],"original_stop":p["stop"],
        "tp1":p["tp1"],"tp2":p["tp2"],"tp3":p["tp3"],
        "tp1_done":False,"tp2_done":False,"tp3_done":False,
        "realized_pnl":0.0,
        "opened_at":now_utc().isoformat()
    }
    return f"🎯 仮想約定 {p['symbol']} {p['side']}\n約定: {mid:,.4f}\n数量: {qty:.8f}"

def manage_pending(state, analyses):
    p=state.get("pending")
    if not p:
        return None
    created=datetime.fromisoformat(p["created_at"])
    if now_utc()-created>timedelta(hours=PENDING_EXPIRE_HOURS):
        msg=f"⌛ {p['symbol']} {p['side']}候補失効（{PENDING_EXPIRE_HOURS}時間未約定）"
        state["pending"]=None
        return msg
    a=next((x for x in analyses if x.symbol==p["symbol"]),None)
    if not a:
        return None
    # シナリオ崩れ
    if p["side"]=="LONG" and a.candle_low<=p["stop"]:
        state["pending"]=None
        return f"❌ {p['symbol']} LONG候補取消：エントリー前に無効ライン到達"
    if p["side"]=="SHORT" and a.candle_high>=p["stop"]:
        state["pending"]=None
        return f"❌ {p['symbol']} SHORT候補取消：エントリー前に無効ライン到達"
    if touch_zone(p,a.candle_high,a.candle_low):
        msg=make_position(state,p)
        state["pending"]=None
        return msg
    return None

def manage_position(state, analyses):
    pos=state.get("position")
    if not pos:
        return []
    a=next((x for x in analyses if x.symbol==pos["symbol"]),None)
    if not a:
        return []
    high,low=a.candle_high,a.candle_low
    side=pos["side"]; msgs=[]

    def tp_hit(level):
        return high>=level if side=="LONG" else low<=level

    def stop_hit(level):
        return low<=level if side=="LONG" else high>=level

    # 同一足でSTOPとTPが両方触れた場合は保守的にSTOP優先
    if stop_hit(pos["stop"]):
        q=max(pos["qty_remaining"],0)
        pnl=(pos["stop"]-pos["entry"])*q if side=="LONG" else (pos["entry"]-pos["stop"])*q
        pos["realized_pnl"]+=pnl
        state["paper_balance"]+=pos["realized_pnl"]
        state["consecutive_losses"]=state.get("consecutive_losses",0)+1 if pos["realized_pnl"]<0 else 0
        record_trade([now_utc().isoformat(),pos["symbol"],side,pos["entry"],pos["stop"],q,pnl,state["paper_balance"],"STOP_END"])
        msgs.append(
            f"🛑 {pos['symbol']} STOP\n"
            f"TP1: {'済' if pos['tp1_done'] else '未'} / TP2: {'済' if pos['tp2_done'] else '未'}\n"
            f"最終損益: {pos['realized_pnl']:+,.0f}円\n残高: {state['paper_balance']:,.0f}円"
        )
        state["position"]=None
        return msgs

    if not pos["tp1_done"] and tp_hit(pos["tp1"]):
        q=pos["qty_initial"]*TP1_PCT
        pnl=(pos["tp1"]-pos["entry"])*q if side=="LONG" else (pos["entry"]-pos["tp1"])*q
        pos["qty_remaining"]-=q; pos["realized_pnl"]+=pnl; pos["tp1_done"]=True
        pos["stop"]=pos["entry"]  # 建値へ
        record_trade([now_utc().isoformat(),pos["symbol"],side,pos["entry"],pos["tp1"],q,pnl,state["paper_balance"],"TP1"])
        msgs.append(f"✅ {pos['symbol']} TP1 30%利確: {pnl:+,.0f}円\nSTOPを建値へ移動")

    if not pos["tp2_done"] and tp_hit(pos["tp2"]):
        q=pos["qty_initial"]*TP2_PCT
        pnl=(pos["tp2"]-pos["entry"])*q if side=="LONG" else (pos["entry"]-pos["tp2"])*q
        pos["qty_remaining"]-=q; pos["realized_pnl"]+=pnl; pos["tp2_done"]=True
        pos["stop"]=pos["tp1"]  # TP1まで利益保護
        record_trade([now_utc().isoformat(),pos["symbol"],side,pos["entry"],pos["tp2"],q,pnl,state["paper_balance"],"TP2"])
        msgs.append(f"✅ {pos['symbol']} TP2 40%利確: {pnl:+,.0f}円\nSTOPをTP1へ移動")

    if not pos["tp3_done"] and tp_hit(pos["tp3"]):
        q=max(pos["qty_remaining"],0)
        pnl=(pos["tp3"]-pos["entry"])*q if side=="LONG" else (pos["entry"]-pos["tp3"])*q
        pos["realized_pnl"]+=pnl
        state["paper_balance"]+=pos["realized_pnl"]
        state["consecutive_losses"]=0
        record_trade([now_utc().isoformat(),pos["symbol"],side,pos["entry"],pos["tp3"],q,pnl,state["paper_balance"],"TP3_END"])
        msgs.append(f"🏁 {pos['symbol']} TP3到達\n最終損益: {pos['realized_pnl']:+,.0f}円\n残高: {state['paper_balance']:,.0f}円")
        state["position"]=None
    return msgs

def log_signals(analyses):
    exists=SIGNALS_FILE.exists()
    with SIGNALS_FILE.open("a",newline="",encoding="utf-8") as f:
        w=csv.writer(f)
        if not exists:
            w.writerow(["time","symbol","side","long","short","confidence","price"])
        for a in analyses:
            w.writerow([now_utc().isoformat(),a.symbol,a.side,a.score_long,a.score_short,a.confidence,a.price])

def rank_text(analyses):
    ranking=sorted(analyses,key=lambda a:a.confidence,reverse=True)
    icons=["🥇","🥈","🥉","4️⃣"]
    return "\n".join(
        f"{icons[i]} {a.symbol}: {a.side if a.side!='WAIT' else '見送り'} {a.confidence}点"
        for i,a in enumerate(ranking)
    )

def choose_winner(analyses):
    valid=[a for a in analyses if a.side!="WAIT" and a.confidence>=ENTRY_THRESHOLD]
    if not valid:
        return None
    valid=sorted(valid,key=lambda a:a.confidence,reverse=True)
    if len(valid)>=2 and valid[0].confidence-valid[1].confidence<WINNER_GAP:
        return None
    return valid[0]

def create_pending(state,a):
    state["pending"]={
        "symbol":a.symbol,"side":a.side,"confidence":a.confidence,
        "entry_low":a.entry_low,"entry_high":a.entry_high,
        "stop":a.stop,"tp1":a.tp1,"tp2":a.tp2,"tp3":a.tp3,
        "created_at":now_utc().isoformat(),"candle_id":a.candle_id
    }

def notify(text):
    print(text,flush=True)
    if NTFY_TOPIC:
        r=requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=text.encode("utf-8"),
            headers={"Title":"Musigny 4-Crypto BOT v4"},
            timeout=15
        )
        r.raise_for_status()

def fmt_candidate(a,ranking,state):
    icon="🟢" if a.side=="LONG" else "🔴"
    return (
        f"{icon} 本命候補 {a.symbol} {a.side} {a.confidence}/100\n"
        f"{ranking}\n\n"
        f"現在値: {a.price:,.4f}\n"
        f"待機エントリー帯: {min(a.entry_low,a.entry_high):,.4f}〜{max(a.entry_low,a.entry_high):,.4f}\n"
        f"STOP: {a.stop:,.4f}\n"
        f"TP1(30%): {a.tp1:,.4f}\nTP2(40%): {a.tp2:,.4f}\nTP3(30%): {a.tp3:,.4f}\n"
        f"無効条件: {a.invalidation}\n"
        f"根拠: {' / '.join(a.reasons[:7])}\n"
        f"※まだ未約定。エントリー帯到達で仮想約定\n"
        f"仮想残高: {state['paper_balance']:,.0f}円"
    )

def main():
    analyses=[]
    for symbol in SYMBOLS:
        frames={
            "15min":drop_open_candle(fetch_intraday(symbol,"15min",5)),
            "1hour":drop_open_candle(fetch_intraday(symbol,"1hour",20)),
            "4hour":drop_open_candle(fetch_yearly(symbol,"4hour",2)),
            "1day":drop_open_candle(fetch_yearly(symbol,"1day",3)),
            "1week":drop_open_candle(fetch_yearly(symbol,"1week",4)),
        }
        analyses.append(analyze(symbol,frames))

    log_signals(analyses)
    state=load_state()

    # 1) 保有中ポジション管理
    for m in manage_position(state,analyses):
        notify("📊 "+m)

    # 2) 待機中候補の約定 / 失効
    # ntfy通知は「実際の仮想売買が発生した時だけ」。
    # 候補失効・候補取消はGitHub Actionsログだけに残す。
    if not state.get("position"):
        msg=manage_pending(state,analyses)
        if msg:
            if msg.startswith("🎯 仮想約定"):
                notify(msg)
            else:
                print(msg, flush=True)

    # 3) ポジションも待機候補も無ければ新候補選定
    if not state.get("position") and not state.get("pending"):
        winner=choose_winner(analyses)
        ranking=rank_text(analyses)
        if winner:
            key=hashlib.sha1(f"{winner.symbol}:{winner.candle_id}:{winner.side}".encode()).hexdigest()[:16]
            if state.get("last_notified")!=key:
                ok,why=can_open(state)
                if ok:
                    create_pending(state,winner)
                    # 候補発生は通知しない。GitHub Actionsログだけに表示。
                    print(fmt_candidate(winner,ranking,state), flush=True)
                    state["last_notified"]=key
                else:
                    print("新規停止:",why)
            else:
                print(ranking)
        else:
            print("全銘柄見送り、または1位と2位の差が小さいため見送り")
            print(ranking)
    else:
        if state.get("position"):
            print("仮想ポジション保有中:",state["position"]["symbol"])
        elif state.get("pending"):
            print("待機候補あり:",state["pending"]["symbol"])

    save_state(state)

if __name__=="__main__":
    main()
