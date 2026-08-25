import os
import re
import time
import html
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

# ============================================================
# Shinsum Monitor - 初日/最終日専用 本番ST予測
# ============================================================
# 通知対象:
#   ・指定16場のうち「ボートレース日和で当日が初日または最終日と確認できた場」だけ
#   ・1R〜12Rのうち締切前のレースだけ
#   ・締切済みレースは展示/ST/日和取得をせず完全スキップ
#   ・展示STが6艇分そろった後に各R 1回だけ通知
#
# 本番ST予測に使う材料:
#   1. 当地ST順位
#   2. 初日ST順位
#   3. 最終日ST順位（最終日のみ）
#   4. F有無 / F持ちST順位
#   5. 今節平均ST（最終日のみ。初日は使わない）
#   6. トップST分析（直近1年）
#      - トップST確率
#      - トップST時1着率
#   7. 展示ST
#
# 「直近1か月」「直近3か月」は予測に使わない。
# 「コース補正」も使わない。
# 2着予想・最終1着率も出さない。
# ============================================================

JST = ZoneInfo("Asia/Tokyo")

NTFY_TOPIC = os.environ["NTFY_TOPIC"]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))

TARGET_VENUES = [
    "平和島", "児島", "戸田", "多摩川",
    "蒲郡", "びわこ", "三国", "鳴門",
    "宮島", "徳山", "下関", "若松",
    "芦屋", "唐津", "大村", "住之江",
]

VENUE_CODES = {
    "戸田": 2, "平和島": 4, "多摩川": 5, "蒲郡": 7,
    "三国": 10, "びわこ": 11, "住之江": 12, "鳴門": 14,
    "児島": 16, "宮島": 17, "徳山": 18, "下関": 19,
    "若松": 20, "芦屋": 21, "唐津": 23, "大村": 24,
}

# ------------------------------------------------------------
# 予測配分
# ------------------------------------------------------------
# 展示前の「基礎ST傾向」部分を100として作り、
# 最後に展示STを20%だけ混ぜる。
#
# 基礎ST傾向の内訳:
#   当地ST順位        20%
#   初日ST順位        15%
#   最終日ST順位      15%
#   今節平均ST        25%
#   トップST分析      15%
#   F補正             10%
#
# 最終出力:
#   基礎傾向 80% + 展示ST 20%
#
# ※コース補正は入れない。
BASE_W_LOCAL = 0.20
BASE_W_FIRSTDAY = 0.15
BASE_W_FINALDAY = 0.15
BASE_W_SETSU = 0.25
BASE_W_TOPSTART = 0.15
BASE_W_F = 0.10
FINAL_W_BASE = 0.80
FINAL_W_EXHIBITION = 0.20

_http = requests.Session()
_http.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; shinsum-finalday-st/2.0)"
})

seen = set()
_final_day_cache = {}


def now():
    return datetime.now(JST)


def hd():
    return now().strftime("%Y%m%d")


def active():
    return 8 <= now().hour < 23


def hiyori_url(venue, race_no):
    return (
        "https://kyoteibiyori.com/race_shusso.php"
        f"?place_no={VENUE_CODES[venue]}&race_no={race_no}&hiduke={hd()}"
    )


def official_before_url(venue, race_no):
    return (
        "https://www.boatrace.jp/owpc/pc/race/beforeinfo"
        f"?hd={hd()}&jcd={VENUE_CODES[venue]:02d}&rno={race_no}"
    )


def _float(s):
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def _pct(s):
    v = _float(s)
    return v


def _rank(s):
    v = _float(s)
    if v is None:
        return None
    if 1.0 <= v <= 6.0:
        return float(v)
    return None


def _st(s):
    """
    .12 / 0.12 / F.03 / F03 を秒へ。
    Fは負値で返す。
    """
    if s is None:
        return None
    s = str(s).upper().replace(" ", "")
    m = re.search(r"F\.?(\d{1,2})", s)
    if m:
        return -int(m.group(1)) / 100.0

    m = re.search(r"0\.(\d{2})", s)
    if m:
        return int(m.group(1)) / 100.0

    m = re.search(r"(?<!\d)\.(\d{2})(?!\d)", s)
    if m:
        return int(m.group(1)) / 100.0

    return None


def _selected_texts(page):
    try:
        return page.locator("select").evaluate_all(
            """sels => sels.map(s => {
                const o = s.selectedOptions && s.selectedOptions[0];
                return o ? (o.textContent || '').trim() : '';
            })"""
        )
    except Exception:
        return []


def get_hiyori_event_day(page, venue):
    """
    ボートレース日和の「当日選択欄」だけを見て、
    今日が 初日 / 最終日 / その他 のどれかを返す。

    ST順位表の中にある「初日」「最終日」という文字は、
    開催日判定には使わない。
    """
    cache_key = f"{hd()}|{venue}"
    if cache_key in _final_day_cache:
        return _final_day_cache[cache_key]

    p = page.context.new_page()
    try:
        p.goto(hiyori_url(venue, 1), wait_until="domcontentloaded", timeout=25000)
        p.wait_for_timeout(700)

        selected = _selected_texts(p)

        event_day = None

        # 日付selectと開催日selectが別々でも判定できるようにする。
        # 例: selected=["8月25日", "初日"] / ["8月30日", "最終日"]
        # 旧UIの "8月25日 / 初日" 形式にも対応する。
        for t in selected:
            normalized = re.sub(r"\s+", "", str(t))
            if normalized == "初日" or re.search(r"(?:/|／)初日$", normalized):
                event_day = "初日"
                break
            if normalized == "最終日" or re.search(r"(?:/|／)最終日$", normalized):
                event_day = "最終日"
                break

        # selectが独自UIで拾えない場合だけ、ページ上部を補助確認
        if event_day is None and not selected:
            body = p.locator("body").inner_text(timeout=7000)
            head = body[:1800]

            if re.search(r"\d{1,2}月\d{1,2}日\s*(?:/|／)\s*初日", head):
                event_day = "初日"
            elif re.search(r"\d{1,2}月\d{1,2}日\s*(?:/|／)\s*最終日", head):
                event_day = "最終日"

        _final_day_cache[cache_key] = event_day

        print(
            f"[EVENT-DAY] {venue} -> {event_day or '対象外'}"
            f" / selected={selected[:3]}",
            flush=True
        )

        return event_day

    except Exception as e:
        print(f"[EVENT-DAY-ERR] {venue}: {e!r} -> 対象外", flush=True)
        _final_day_cache[cache_key] = None
        return None
    finally:
        p.close()

def detect_target_venues(page):
    """
    16場を場単位で1回だけ判定。
    今日が「初日」または「最終日」の場だけ返す。
    戻り値: {venue: "初日" or "最終日"}
    """
    out = {}

    for venue in TARGET_VENUES:
        event_day = get_hiyori_event_day(page, venue)
        if event_day in ("初日", "最終日"):
            out[venue] = event_day

    print(
        "[TARGET-DAY] "
        + (
            ", ".join(f"{v}({d})" for v, d in out.items())
            if out else "なし"
        ),
        flush=True
    )

    return out

def _row_cells(page):
    """
    全trを二次元配列にして返す。
    """
    return page.locator("tr").evaluate_all(
        """els => els.map(tr =>
            Array.from(tr.querySelectorAll('th,td'))
              .map(x => (x.innerText || x.textContent || '').trim())
        )"""
    )


def fetch_hiyori_data(page, venue, race_no):
    """
    ボートレース日和から必要データだけ取得。
    直近1か月・直近3か月は使用しない。
    """
    p = page.context.new_page()
    out = {i: {} for i in range(1, 7)}

    try:
        p.goto(hiyori_url(venue, race_no), wait_until="domcontentloaded", timeout=25000)

        try:
            p.wait_for_function(
                """() => {
                    const t = document.body?.innerText || '';
                    return t.includes('ST順位');
                }""",
                timeout=9000
            )
        except Exception:
            p.wait_for_timeout(1000)

        rows = _row_cells(p)

        # ---------- ST順位・今節データ ----------
        for cells in rows:
            if len(cells) < 2:
                continue

            label = re.sub(r"\s+", "", cells[0])
            vals = cells[1:7]

            if len(vals) < 6:
                continue

            key = None
            parser = _rank

            if label == "当地" or label.startswith("当地ST"):
                key = "local_rank"
            elif label == "初日" or label.startswith("初日ST"):
                key = "firstday_rank"
            elif label == "最終日" or label.startswith("最終日ST"):
                key = "finalday_rank"
            elif label in ("F持", "F持ち") or label.startswith("F持"):
                key = "f_rank"
            elif label == "平均ST":
                # 今節データ内の平均ST。0.08〜0.30程度。
                key = "setsu_avg_st"
                parser = _float

            if key:
                for b, raw in enumerate(vals[:6], 1):
                    v = parser(raw)
                    if v is not None:
                        if key == "setsu_avg_st" and not (0.05 <= v <= 0.35):
                            continue
                        out[b][key] = float(v)

        # ---------- トップスタート分析（直近1年） ----------
        # テーブル行は艇番順に6選手。
        top_table = None
        tables = p.locator("table")
        for i in range(tables.count()):
            tb = tables.nth(i)
            try:
                text = tb.inner_text(timeout=300)
            except Exception:
                continue
            if "トップスタート" in text and "確率" in text and "1着率" in text:
                top_table = tb
                break

        if top_table is None:
            # 見出しがtable外のサイト構造向け:
            # 「トップスタート分析」の後にある最初のtableを取る。
            heads = p.get_by_text(re.compile("トップスタート分析"))
            if heads.count():
                try:
                    top_table = heads.first.locator(
                        "xpath=following::table[1]"
                    )
                except Exception:
                    top_table = None

        if top_table is not None:
            try:
                trows = top_table.locator("tr").evaluate_all(
                    """els => els.map(tr =>
                        Array.from(tr.querySelectorAll('th,td'))
                          .map(x => (x.innerText || x.textContent || '').trim())
                    )"""
                )
            except Exception:
                trows = []

            boat = 0
            for cells in trows:
                # データ行の例:
                # 選手名 / 出走数 / トップST回数 / 確率 / 1着数 / 1着率
                if len(cells) < 5:
                    continue

                joined = " ".join(cells)
                pcts = re.findall(r"(\d+(?:\.\d+)?)\s*%", joined)
                if len(pcts) < 2:
                    continue

                boat += 1
                if boat > 6:
                    break

                out[boat]["top_start_prob"] = float(pcts[0])
                out[boat]["top_start_win"] = float(pcts[1])

        # Fの有無は、日和のF持順位が存在すればF持ちと扱う。
        for b in range(1, 7):
            out[b]["has_f"] = out[b].get("f_rank") is not None

        print(
            f"[HIYORI] {venue}{race_no}R "
            + " / ".join(
                f"{b}:当地={out[b].get('local_rank')},"
                f"初日={out[b].get('firstday_rank')},"
                f"最終={out[b].get('finalday_rank')},"
                f"節ST={out[b].get('setsu_avg_st')},"
                f"TOP={out[b].get('top_start_prob')},"
                f"F={'Y' if out[b].get('has_f') else 'N'}"
                for b in range(1, 7)
            ),
            flush=True
        )

        return out

    except Exception as e:
        print(f"[HIYORI-ERR] {venue}{race_no}R {e!r}", flush=True)
        return out
    finally:
        p.close()


def _extract_exhibition_from_text(body):
    """
    BOATRACE公式の表示済み本文から展示ST・展示タイムを拾う。
    HTMLテーブル構造変更に依存しない補助パーサ。
    """
    out = {i: {} for i in range(1, 7)}

    # 展示タイムは 6.5x〜7.xx
    # body全体から「艇番の近く」にあるものを探すのは誤爆しやすいため、
    # Playwrightの行抽出を本命とし、ここはST抽出の補助だけにする。
    start_pos = body.find("スタート展示")
    if start_pos >= 0:
        section = body[start_pos:start_pos + 3000]
        lines = [re.sub(r"\s+", " ", x).strip() for x in section.splitlines() if x.strip()]

        # .xx / F.xx を登場順に6つ拾う。
        sts = []
        for line in lines:
            for m in re.finditer(r"(F\.?\d{1,2}|(?<!\d)\.\d{2}(?!\d)|0\.\d{2})", line, re.I):
                v = _st(m.group(1))
                if v is not None:
                    sts.append(v)
                    if len(sts) == 6:
                        break
            if len(sts) == 6:
                break

        if len(sts) == 6:
            for b, v in enumerate(sts, 1):
                out[b]["ex_st"] = v

    return out


def fetch_official_exhibition(page, venue, race_no):
    """
    公式直前情報をPlaywrightで確認。
    展示開始前・6艇未完了なら通知しない。
    """
    p = page.context.new_page()
    out = {i: {} for i in range(1, 7)}

    try:
        p.goto(
            official_before_url(venue, race_no),
            wait_until="domcontentloaded",
            timeout=25000
        )
        p.wait_for_timeout(600)

        body = p.locator("body").inner_text(timeout=7000)

        # 展示情報そのものが無ければ未開始。
        if "スタート展示" not in body or "展示タイム" not in body:
            return out

        # まずテーブル/行ベースで取得
        try:
            rows = _row_cells(p)
        except Exception:
            rows = []

        ex_times = []
        ex_sts = []

        for cells in rows:
            s = " ".join(cells)

            # 展示タイム候補
            for m in re.finditer(r"(?<!\d)(6\.\d{2}|7\.\d{2})(?!\d)", s):
                v = float(m.group(1))
                if 6.20 <= v <= 7.80:
                    ex_times.append(v)

            # スタート展示候補
            for tok in re.findall(
                r"F\.?\d{1,2}|(?<!\d)\.\d{2}(?!\d)|0\.\d{2}",
                s,
                flags=re.I
            ):
                v = _st(tok)
                if v is not None:
                    ex_sts.append(v)

        # 重複が多いサイト構造では本文の「スタート展示」部分を優先して上書き
        text_parsed = _extract_exhibition_from_text(body)
        text_sts = [text_parsed[b].get("ex_st") for b in range(1, 7)]

        if all(v is not None for v in text_sts):
            ex_sts = text_sts
        else:
            # 0.00チルト等の誤拾いを避けるため、6艇ちょうど取れていないなら破棄
            # 公式DOMが変わった時に誤通知するより「待つ」を優先。
            if len(ex_sts) != 6:
                ex_sts = []

        # 展示タイムも6艇ちょうど/本文順が取れた時だけ採用
        # 6艇分以上ある場合は、6.xxの連続ブロックを候補にする。
        if len(ex_times) >= 6:
            # 同一値重複を消さず、最初の6艇分を使用
            ex_times = ex_times[:6]
        else:
            ex_times = []

        if len(ex_sts) == 6:
            for b, v in enumerate(ex_sts, 1):
                out[b]["ex_st"] = float(v)

        if len(ex_times) == 6:
            for b, v in enumerate(ex_times, 1):
                out[b]["ex_time"] = float(v)

        return out

    except Exception as e:
        print(f"[OFFICIAL-EX-ERR] {venue}{race_no}R {e!r}", flush=True)
        return out
    finally:
        p.close()


# ============================================================
# 予測ロジック
# ============================================================

def rank_to_st(rank):
    """
    ST順位 1〜6 を相対STへ変換。
    1位≈0.105 / 6位≈0.195 のスケール。
    """
    if rank is None:
        return None
    return 0.105 + (float(rank) - 1.0) * 0.018


def topstart_to_st(prob, win_rate):
    """
    トップST分析を相対STへ。
    主役はトップST確率。トップST時1着率は補助（20%）。
    """
    if prob is None:
        return None

    prob = max(0.0, min(50.0, float(prob)))
    # 0% -> .195 / 50% -> .105
    st_prob = 0.195 - (prob / 50.0) * 0.090

    if win_rate is None:
        return st_prob

    win_rate = max(0.0, min(100.0, float(win_rate)))
    # 0% -> .185 / 100% -> .115
    st_win = 0.185 - (win_rate / 100.0) * 0.070

    return st_prob * 0.80 + st_win * 0.20


def f_adjusted_st(d):
    """
    F補正。
    Fなしはニュートラル(.150)。
    F持ちはF持ちST順位に応じて慎重/攻めを表す。
    """
    if not d.get("has_f"):
        return 0.150

    fr = d.get("f_rank")
    if fr is None:
        return 0.165

    # F持ちでも順位が良い選手は過度に遅くしない。
    base = rank_to_st(fr)
    return min(0.185, max(0.125, base + 0.008))


def weighted_available(parts):
    """
    欠損データがあっても、ある項目だけで重みを再正規化。
    """
    valid = [(v, w) for v, w in parts if v is not None and w > 0]
    if not valid:
        return 0.150

    sw = sum(w for _, w in valid)
    return sum(v * w for v, w in valid) / sw


def predict_st(d, event_day):
    """
    初日/最終日の本番ST予測。

    初日:
      - 当地ST順位 30%
      - 初日ST順位 30%
      - トップST分析(1年) 25%
      - F関連 15%
      - 今節平均STは使わない
      - 最終日ST順位も使わない
      → 基礎80% + 展示ST20%

    最終日:
      - 当地ST順位 25%
      - 最終日ST順位 25%
      - 今節平均ST 25%
      - トップST分析(1年) 15%
      - F関連 10%
      - 初日ST順位は使わない
      → 基礎80% + 展示ST20%

    直近1か月/3か月、コース補正は使わない。
    """
    local = rank_to_st(d.get("local_rank"))
    firstday = rank_to_st(d.get("firstday_rank"))
    finalday = rank_to_st(d.get("finalday_rank"))

    top = topstart_to_st(
        d.get("top_start_prob"),
        d.get("top_start_win")
    )
    f_st = f_adjusted_st(d)

    if event_day == "初日":
        base = weighted_available([
            (local, 0.30),
            (firstday, 0.30),
            (top, 0.25),
            (f_st, 0.15),
        ])
    else:
        setsu = d.get("setsu_avg_st")
        if setsu is not None:
            setsu = max(0.07, min(0.28, float(setsu)))

        base = weighted_available([
            (local, 0.25),
            (finalday, 0.25),
            (setsu, 0.25),
            (top, 0.15),
            (f_st, 0.10),
        ])

    ex = d.get("ex_st")
    if ex is None:
        pred = base
    else:
        # 展示Fをそのまま本番Fとは予測しない
        ex_for_calc = (
            max(0.04, float(ex))
            if ex >= 0
            else max(0.04, 0.06 - abs(float(ex)))
        )
        pred = base * FINAL_W_BASE + ex_for_calc * FINAL_W_EXHIBITION

    return max(0.04, min(0.25, pred))


# ============================================================
# 通知画像
# ============================================================

def build_image(page, venue, race_no, deadline_text, data, event_day):
    pred = {b: predict_st(data[b], event_day) for b in range(1, 7)}

    best = min(pred, key=pred.get)

    # 右に出ているほど速い。
    vals = list(pred.values())
    lo, hi = min(vals), max(vals)
    spread = max(hi - lo, 0.04)

    lanes = []
    for b in range(1, 7):
        rel = (hi - pred[b]) / spread
        x = 28 + max(0.0, min(1.0, rel)) * 52

        ex = data[b].get("ex_st")
        ext = data[b].get("ex_time")

        if ex is None:
            ex_txt = "-"
        elif ex < 0:
            ex_txt = f"F{abs(ex):.02f}".replace("0.", ".")
        else:
            ex_txt = f"{ex:.02f}"

        ext_txt = f"{ext:.2f}" if ext is not None else "-"
        fire = "🔥" if b == best else ""

        lanes.append(f"""
        <div class="lane">
          <div class="num">{b}</div>
          <div class="track">
            <div class="start"></div>
            <div class="boat" style="left:{x:.1f}%">⛵▶ {fire}</div>
          </div>
          <div class="stat">
            <b>予測 {pred[b]:.02f}</b>
            <span>展示ST {html.escape(ex_txt)}</span>
            <span>展示 {html.escape(ext_txt)}</span>
          </div>
        </div>
        """)

    doc = f"""
    <html><head><meta charset="utf-8">
    <style>
      *{{box-sizing:border-box}}
      body{{
        margin:0;padding:26px;width:920px;background:#08111f;color:#eef4ff;
        font-family:-apple-system,BlinkMacSystemFont,"Noto Sans JP","Yu Gothic",sans-serif;
      }}
      .top{{padding-bottom:18px;border-bottom:1px solid #283850}}
      .race{{font-size:31px;font-weight:900}}
      .sub{{font-size:15px;color:#90a4c2;margin-top:6px}}
      .card{{margin-top:22px;background:#0d192a;border:1px solid #263957;
        border-radius:18px;padding:20px}}
      .title{{font-size:22px;font-weight:900;margin-bottom:14px}}
      .lane{{display:grid;grid-template-columns:42px 1fr 175px;gap:10px;
        align-items:center;min-height:70px;border-top:1px solid #1d2c42}}
      .lane:first-of-type{{border-top:none}}
      .num{{width:34px;height:34px;border-radius:50%;border:1px solid #607798;
        display:flex;align-items:center;justify-content:center;font-weight:900}}
      .track{{position:relative;height:48px}}
      .track:before{{content:"";position:absolute;left:0;right:0;top:24px;height:2px;background:#263956}}
      .start{{position:absolute;left:84%;top:2px;bottom:2px;width:3px;background:#ff5252}}
      .boat{{position:absolute;top:8px;transform:translateX(-50%);font-size:25px;
        white-space:nowrap;font-weight:900}}
      .stat{{display:flex;flex-direction:column;font-size:13px;color:#99acc7}}
      .stat b{{font-size:17px;color:#f2f6ff}}
      .arrow{{text-align:center;margin-top:12px;color:#a6b8d1;font-weight:800}}
      .note{{margin-top:12px;color:#8fa5c4;font-size:13px}}
      .foot{{margin-top:18px;color:#7185a5;font-size:12px;text-align:right}}
    </style></head>
    <body>
      <div class="top">
        <div class="race">{html.escape(venue)} {race_no}R</div>
        <div class="sub">{html.escape(event_day)} / 締切 {html.escape(deadline_text or "-")}</div>
      </div>

      <div class="card">
        <div class="title">本番ST予測</div>
        {''.join(lanes)}
        <div class="arrow">進行方向 →　　STARTは赤線</div>
        <div class="note">右に出ているほど本番先行予測 / 🔥は最速予測艇</div>
      </div>

      <div class="foot">
        {("当地・初日ST順位 / F / トップST分析 / 展示ST"
          if event_day == "初日"
          else "当地・最終日ST順位 / F / 今節平均ST / トップST分析 / 展示ST")}
      </div>
    </body></html>
    """

    p = page.context.new_page()
    try:
        p.set_viewport_size({"width": 920, "height": 1000})
        p.set_content(doc, wait_until="load")
        p.wait_for_timeout(200)
        path = f"/tmp/finalday_st_{venue}_{race_no}_{int(time.time())}.png"
        p.screenshot(path=path, full_page=True)
        return path, pred
    finally:
        p.close()


def send_image(path, venue, race_no, event_day):
    with open(path, "rb") as f:
        r = requests.put(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            params={
                "filename": os.path.basename(path),
                "title": "Shinsum Monitor",
                "message": f"{venue} {race_no}R / {event_day} / 本番ST予測",
                "priority": "5",
                "tags": "ship",
            },
            data=f,
            headers={"Content-Type": "image/png"},
            timeout=30,
        )
    r.raise_for_status()


def fetch_deadline(page, venue, race_no):
    """
    締切時刻は BOATRACE公式ではなくボートレース日和から取得する。
    BOATRACE公式への締切取得アクセスはしない。

    1) requests で高速取得
    2) 失敗時だけ Playwright でボートレース日和を開く
    """
    url = hiyori_url(venue, race_no)

    try:
        r = _http.get(url, timeout=4)
        r.raise_for_status()

        raw = re.sub(
            r"<script\b[^>]*>.*?</script>",
            " ",
            r.text,
            flags=re.I | re.S,
        )
        raw = re.sub(
            r"<style\b[^>]*>.*?</style>",
            " ",
            raw,
            flags=re.I | re.S,
        )
        body = re.sub(r"<[^>]+>", " ", raw)
        body = html.unescape(body)
        body = re.sub(r"\s+", " ", body)

        # 例: 7R 予選 締切14:03
        pats = (
            rf"(?<!\d){race_no}\s*R.*?締切\s*([0-2]?\d:[0-5]\d)",
            r"締切\s*([0-2]?\d:[0-5]\d)",
        )

        for pat in pats:
            m = re.search(pat, body, flags=re.I)
            if m:
                return m.group(1)

    except Exception as e:
        print(
            f"[HIYORI-DEADLINE-HTTP-ERR] "
            f"{venue} {race_no}R {e!r}",
            flush=True,
        )

    p = page.context.new_page()
    try:
        p.goto(
            url,
            wait_until="domcontentloaded",
            timeout=10000,
        )
        body = p.locator("body").inner_text(timeout=4000)

        m = re.search(
            rf"(?<!\d){race_no}\s*R.*?締切\s*([0-2]?\d:[0-5]\d)",
            body,
            flags=re.I | re.S,
        )
        if not m:
            m = re.search(
                r"締切\s*([0-2]?\d:[0-5]\d)",
                body,
            )

        return m.group(1) if m else ""

    except Exception as e:
        print(
            f"[HIYORI-DEADLINE-PW-ERR] "
            f"{venue} {race_no}R {e!r}",
            flush=True,
        )
        return ""
    finally:
        p.close()


def find_current_race(page, venue, event_day):
    """
    現在の未締切レースを二分探索で特定する。

    締切時刻は1R→12Rで昇順なので、
    12ページ全部を開かず最大5回程度の締切取得で現在Rを探す。

    戻り値:
      (race_no, deadline_text)
      全レース終了済み -> (None, "FINISHED")
      締切取得不能      -> (None, "")
    """
    # まず12Rだけ確認。終了済みなら場ごと即終了。
    d12 = fetch_deadline(page, venue, 12)

    if not d12:
        print(
            f"[CURRENT-RACE-ERR] {venue} {event_day} / "
            "12R締切取得失敗",
            flush=True,
        )
        return None, ""

    if race_is_closed(d12):
        return None, "FINISHED"

    lo, hi = 1, 12
    deadline_cache = {12: d12}

    # 最初の「まだ締切前」のRを二分探索。
    while lo < hi:
        mid = (lo + hi) // 2

        d = deadline_cache.get(mid)
        if d is None:
            d = fetch_deadline(page, venue, mid)
            deadline_cache[mid] = d

        if not d:
            # 取得失敗時は無理に過去Rへ戻らず、
            # 右側へ寄せて誤って終了済みRを審査するのを防ぐ。
            lo = mid + 1
            continue

        if race_is_closed(d):
            lo = mid + 1
        else:
            hi = mid

    race_no = lo

    d = deadline_cache.get(race_no)
    if not d:
        d = fetch_deadline(page, venue, race_no)

    if not d:
        return None, ""

    # 通知済みなら次Rへ進める。
    while race_no <= 12:
        key = f"{hd()}|{venue}|{race_no}|{event_day}"

        if key not in seen:
            return race_no, d

        race_no += 1
        if race_no > 12:
            return None, "FINISHED"

        d = fetch_deadline(page, venue, race_no)
        if not d:
            return None, ""

        if race_is_closed(d):
            seen.add(
                f"{hd()}|{venue}|{race_no}|{event_day}"
            )
            continue

    return None, "FINISHED"

def deadline_datetime(deadline_text):
    """
    今日の締切時刻をJSTのdatetimeに変換。
    """
    if not deadline_text:
        return None

    m = re.fullmatch(r"([0-2]?\\d):([0-5]\\d)", str(deadline_text).strip())
    if not m:
        return None

    h, minute = map(int, m.groups())
    if h > 23:
        return None

    n = now()
    return n.replace(
        hour=h,
        minute=minute,
        second=0,
        microsecond=0,
    )


def race_is_closed(deadline_text):
    """
    締切時刻を過ぎたレースは審査対象外。
    締切時刻ちょうども終了扱い。
    """
    dt = deadline_datetime(deadline_text)
    if dt is None:
        return False
    return now() >= dt


def race_complete(ex):
    """
    6艇すべての展示STが取れて初めて「展示完了」。
    """
    got = [
        b for b in range(1, 7)
        if ex.get(b, {}).get("ex_st") is not None
    ]
    return len(got) == 6, got


def cycle(page, target_venues):
    """
    現在Rだけを監視する高速版。

    - BOATRACE公式へ締切取得アクセスしない
    - ボートレース日和の締切を使用
    - 二分探索で現在Rを特定
    - 終了済み1R〜過去Rを1つずつ審査しない
    - 12R終了済みの場は即終了
    """
    for venue, event_day in target_venues.items():

        race_no, deadline_text = find_current_race(
            page,
            venue,
            event_day,
        )

        if deadline_text == "FINISHED":
            print(
                f"[VENUE-FINISHED] {venue} {event_day} / "
                "12Rまで終了済み -> 審査しない",
                flush=True,
            )
            continue

        if race_no is None:
            print(
                f"[CURRENT-RACE-RETRY] {venue} {event_day} / "
                "現在Rを特定できず -> 次周期",
                flush=True,
            )
            continue

        key = f"{hd()}|{venue}|{race_no}|{event_day}"

        print(
            f"[CURRENT-RACE] {venue} {race_no}R "
            f"{event_day} / 締切 {deadline_text}",
            flush=True,
        )

        # 展示情報は「今の1レース」だけ公式から取得。
        ex = fetch_official_exhibition(
            page,
            venue,
            race_no,
        )
        complete, got = race_complete(ex)

        if not complete:
            print(
                f"[WAIT-EXHIBITION] {venue} {race_no}R "
                f"{event_day} / 締切 {deadline_text} / "
                f"展示ST取得={got} / 6艇未完了 -> 次周期",
                flush=True,
            )
            continue

        # 日和の詳細データも今の1レースだけ取得。
        hdata = fetch_hiyori_data(
            page,
            venue,
            race_no,
        )

        data = {i: {} for i in range(1, 7)}
        for b in range(1, 7):
            data[b].update(hdata.get(b, {}))
            data[b].update(ex.get(b, {}))

        image_path, pred = build_image(
            page,
            venue,
            race_no,
            deadline_text,
            data,
            event_day,
        )

        try:
            send_image(
                image_path,
                venue,
                race_no,
                event_day,
            )
            seen.add(key)

            print(
                f"[ST-PREDICT] {venue} {race_no}R / "
                f"{event_day} / "
                + " / ".join(
                    f"{b}={pred[b]:.02f}"
                    for b in range(1, 7)
                ),
                flush=True,
            )
            print(
                f"[NOTIFY] {venue} {race_no}R "
                f"{event_day} 送信完了",
                flush=True,
            )

        finally:
            try:
                os.remove(image_path)
            except Exception:
                pass


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15"
        )
        page = context.new_page()

        if not active():
            print("監視時間外（23:00〜08:00 JST）。終了します。", flush=True)
            return

        print(
            f"[{now():%Y-%m-%d %H:%M:%S}] Shinsum Monitor",
            flush=True
        )
        print(
            "初日・最終日 1〜12R 本番ST予測開始",
            flush=True
        )
        print(
            "初日使用: 当地ST順位 / 初日ST順位 / "
            "F有無 / トップST分析(1年) / 展示ST",
            flush=True
        )
        print(
            "最終日使用: 当地ST順位 / 最終日ST順位 / "
            "F有無 / 今節平均ST / トップST分析(1年) / 展示ST",
            flush=True
        )
        print(
            "不使用: 直近1か月 / 直近3か月 / コース補正 / 2着予想 / 最終1着率",
            flush=True
        )

        # 開催日判定は起動時に1回だけ。
        target_venues = detect_target_venues(page)

        if not target_venues:
            print("本日の初日・最終日対象場なし。終了します。", flush=True)
            return

        while active():
            print(
                f"[{now():%Y-%m-%d %H:%M:%S}] 再チェック / "
                f"対象場: {', '.join(f'{v}({d})' for v, d in target_venues.items())}",
                flush=True
            )

            cycle(page, target_venues)

            time.sleep(CHECK_INTERVAL)

        browser.close()


if __name__ == "__main__":
    main()
