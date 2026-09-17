import os
import re
import time
from datetime import datetime, timedelta
from email.header import Header
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright


# ============================================================
# 基本設定
# ============================================================

BASE_URL = "https://boatrace-shinsum.com/"

SHINSUM_USER = os.environ["SHINSUM_USER"]
SHINSUM_PASSWORD = os.environ["SHINSUM_PASSWORD"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))
ALERT_WINDOW_MIN = int(os.getenv("ALERT_WINDOW_MIN", "15"))

JST = ZoneInfo("Asia/Tokyo")

# 通知対象場
TARGET_VENUES = (
    "戸田",
    "多摩川",
    "びわこ",
    "浜名湖",
    "平和島",
    "福岡",
    "蒲郡",
    "下関",
    "大村",
)

# 同一実行中の重複通知防止
SENT = set()


# ============================================================
# 時刻
# ============================================================

def now():
    return datetime.now(JST)


def active():
    # JST 08:00〜23:00
    return 8 <= now().hour < 23


def get_deadline(text):
    m = re.search(
        r"締切\s*[：:]?\s*([01]?\d|2[0-3]):([0-5]\d)",
        text
    )
    if not m:
        return ""

    return f"{int(m.group(1)):02d}:{m.group(2)}"


def within_alert_window(text):
    d = get_deadline(text)

    if not d:
        return False

    h, m = map(int, d.split(":"))

    deadline_dt = now().replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0,
    )

    diff = deadline_dt - now()

    return (
        timedelta(minutes=-1)
        <= diff
        <= timedelta(minutes=ALERT_WINDOW_MIN)
    )


# ============================================================
# レース情報
# ============================================================

def get_venue(text):
    head = text[:2500]

    for venue in TARGET_VENUES:
        if venue in head:
            return venue

    return ""


def get_race(text):
    head = text[:2500]

    m = re.search(
        r"(?<!\d)([1-9]|1[0-2])\s*R\b",
        head,
        re.IGNORECASE,
    )

    if not m:
        return ""

    return f"{m.group(1)}R"


# ============================================================
# 詳細ページ候補
# ============================================================

def candidate_links(page):
    """
    以前の監視ツールで使っていた候補リンク抽出方式をそのまま採用。
    a[href] を走査し、対象場/R/race/detail/sum を含むリンクを拾う。

    V38では、認証失敗を見逃さないよう HTTP status も確認する。
    """

    response = page.goto(
        BASE_URL,
        wait_until="domcontentloaded",
        timeout=30000
    )
    page.wait_for_timeout(1000)

    if response is None:
        print(
            "トップページ応答なし",
            flush=True
        )
        return []

    status = response.status

    print(
        f"トップページHTTP: {status}",
        flush=True
    )

    if status in (401, 403):
        print(
            "認証失敗: SHINSUM_USER / SHINSUM_PASSWORD を確認",
            flush=True
        )
        return []

    host = urlparse(BASE_URL).netloc
    out = []
    aa = page.locator("a")

    for i in range(aa.count()):
        a = aa.nth(i)

        try:
            href = a.get_attribute("href")

            if (
                not href
                or href.startswith("#")
                or href.startswith("javascript:")
            ):
                continue

            full = urljoin(BASE_URL, href)

            if urlparse(full).netloc != host:
                continue

            txt = ""

            try:
                txt = a.inner_text(timeout=250) or ""
            except Exception:
                pass

            try:
                txt += "\n" + a.locator(
                    "xpath=ancestor::*[self::div or self::td or self::li or self::section][1]"
                ).inner_text(timeout=250)
            except Exception:
                pass

            if (
                any(v in txt for v in TARGET_VENUES)
                or re.search(r"([1-9]|1[0-2])\s*R", txt)
                or "race" in full.lower()
                or "detail" in full.lower()
                or "sum" in full.lower()
            ):
                out.append(full)

        except Exception:
            pass

    return list(dict.fromkeys(out))


# ============================================================
# スリットアラート解析
# ============================================================

def parse_alert_cell(cell_text):
    """
    スリットアラート欄の1セルだけを解析。

    例:
      +0.2
      1着 +10%

      ⚡ SUPER
      +0.1
      1着 +15%
    """

    text = " ".join(cell_text.split())

    if "1着" not in text:
        return None

    slit_match = re.search(
        r"([+-]\d+(?:\.\d+)?)",
        text
    )

    boost_match = re.search(
        r"1着\s*([+-]\d+(?:\.\d+)?)\s*%",
        text
    )

    if not slit_match or not boost_match:
        return None

    slit = slit_match.group(1)
    boost = float(boost_match.group(1))

    # スリット差は +0.1 などの小数値。
    # 通常の1着補正セルだけを誤検知しないため1.0未満に限定。
    try:
        slit_num = float(slit)
    except ValueError:
        return None

    if abs(slit_num) >= 1.0:
        return None

    return {
        "super": "SUPER" in text.upper(),
        "slit": slit,
        "boost": boost,
    }


def extract_boat_number(cell_texts, row_text):
    # まず先頭付近のセルから艦番を取得
    for txt in cell_texts[:3]:
        cleaned = " ".join(txt.split())

        m = re.fullmatch(
            r"([1-6])(?:号艇)?",
            cleaned
        )

        if m:
            return int(m.group(1))

    # DOM差異用フォールバック
    m = re.search(
        r"(?:^|\s)([1-6])(?:号艇)?\s+\d{4}(?:\s|$)",
        row_text
    )

    if m:
        return int(m.group(1))

    return None


def parse_slit_alerts(page):
    """
    V43:
    V39で通知成功した認証・リンク取得・ntfyは維持。

    スリットアラートは画面上の列位置で直接読む。
    1) 「スリットアラート」見出しのX座標を取得
    2) その列直下の +0.1 / +0.2 ... だけ拾う
    3) 同じY位置の「1着 +○%」を結びつける
    4) 同じY位置の4桁登録番号を拾う
    5) その登録番号と同じ行の艇番1〜6を拾う

    平均との差（+0.09等）は別列なので拾わない。
    """
    try:
        data = page.evaluate(
            r"""
            () => {
              const norm = s => (s || '').replace(/\s+/g, ' ').trim();
              const rect = el => {
                const r = el.getBoundingClientRect();
                return {
                  x: r.left + window.scrollX,
                  y: r.top + window.scrollY,
                  w: r.width,
                  h: r.height,
                  cx: r.left + window.scrollX + r.width / 2,
                  cy: r.top + window.scrollY + r.height / 2
                };
              };
              const visible = el => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 0 && r.height > 0 &&
                  st.display !== 'none' && st.visibility !== 'hidden';
              };

              const all = Array.from(document.querySelectorAll('body *')).filter(visible);

              // シンsum理論見出しはページ内の最後のものを採用
              const theoryHeaders = all.filter(el => norm(el.innerText) === 'シンsum理論');
              if (!theoryHeaders.length) {
                return {error: 'theory-header-not-found', alerts: []};
              }
              theoryHeaders.sort((a,b) => rect(a).y - rect(b).y);
              const theory = theoryHeaders[theoryHeaders.length - 1];
              const theoryY = rect(theory).y;

              // 注釈「※スリットアラート」より上だけを表領域とする
              const notes = all.filter(el => norm(el.innerText).startsWith('※スリットアラート'))
                .map(el => ({el, ...rect(el)}))
                .filter(o => o.y > theoryY)
                .sort((a,b) => a.y - b.y);
              const bottomY = notes.length ? notes[0].y : Infinity;

              // スリットアラート見出し。最後の理論見出しより下のものを使う
              const headers = all
                .filter(el => norm(el.innerText).replace(/\s+/g,'').includes('スリットアラート'))
                .map(el => ({el, text:norm(el.innerText), ...rect(el)}))
                .filter(o => o.y > theoryY && o.y < bottomY)
                .sort((a,b) => a.y - b.y);

              if (!headers.length) {
                return {error: 'slit-header-not-found', alerts: []};
              }

              // 最も上のスリットアラート見出しの中心X
              const slitHeader = headers[0];
              const slitX = slitHeader.cx;
              const headerBottom = slitHeader.y + slitHeader.h;

              // 4桁登録番号リンクを理論表領域から取得
              const regs = Array.from(document.querySelectorAll('a'))
                .filter(visible)
                .map(el => ({text:norm(el.innerText), ...rect(el)}))
                .filter(o => /^\d{4}$/.test(o.text) && o.y > headerBottom && o.y < bottomY)
                .sort((a,b) => a.cy - b.cy);

              // 重複登録番号除去
              const uniqueRegs = [];
              const seen = new Set();
              for (const r of regs) {
                if (!seen.has(r.text)) {
                  uniqueRegs.push(r);
                  seen.add(r.text);
                }
              }

              // +0.1/+0.2... のleaf要素だけ取得。
              // X座標がスリット列見出しの近辺にあるものだけ採用。
              const slitEls = all
                .filter(el => el.children.length === 0)
                .map(el => ({text:norm(el.textContent), ...rect(el)}))
                .filter(o => /^\+0\.\d+$/.test(o.text))
                .filter(o => o.y > headerBottom && o.y < bottomY)
                .filter(o => Math.abs(o.cx - slitX) <= 110)
                .sort((a,b) => a.cy - b.cy);

              // 1着 +N% のleaf要素
              const boosts = all
                .filter(el => el.children.length === 0)
                .map(el => ({text:norm(el.textContent), ...rect(el)}))
                .filter(o => /^1着\s*\+\d+(?:\.\d+)?%$/.test(o.text))
                .filter(o => o.y > headerBottom && o.y < bottomY);

              // SUPER leaf要素
              const supers = all
                .filter(el => el.children.length === 0)
                .map(el => ({text:norm(el.textContent), ...rect(el)}))
                .filter(o => /SUPER/i.test(o.text))
                .filter(o => o.y > headerBottom && o.y < bottomY);

              const results = [];

              for (const s of slitEls) {
                // 同じセルの1着+N%を最短距離で探す
                let boost = null;
                let bestBoost = Infinity;
                for (const b of boosts) {
                  const dy = Math.abs(b.cy - s.cy);
                  const dx = Math.abs(b.cx - s.cx);
                  // 縦積みされるため、Xは近く、Yは多少ずれてよい
                  if (dx <= 120 && dy <= 75) {
                    const d = dx + dy;
                    if (d < bestBoost) {
                      bestBoost = d;
                      boost = b;
                    }
                  }
                }
                if (!boost) continue;

                // 同じ行の登録番号をY差で特定
                let reg = null;
                let bestReg = Infinity;
                for (const r of uniqueRegs) {
                  const dy = Math.abs(r.cy - s.cy);
                  if (dy <= 80 && dy < bestReg) {
                    bestReg = dy;
                    reg = r;
                  }
                }
                if (!reg) continue;

                // 登録番号の並び順から艇番を確定（表は1〜6号艇の順）
                const boatIndex = uniqueRegs.findIndex(r => r.text === reg.text);
                if (boatIndex < 0 || boatIndex > 5) continue;

                const mBoost = boost.text.match(/\+(\d+(?:\.\d+)?)%/);
                if (!mBoost) continue;

                const isSuper = supers.some(sp =>
                  Math.abs(sp.cy - s.cy) <= 75 && Math.abs(sp.cx - s.cx) <= 150
                );

                results.push({
                  boat: boatIndex + 1,
                  reg: reg.text,
                  super: isSuper,
                  slit: s.text,
                  boost: Number(mBoost[1]),
                  ydiff: bestReg,
                  xdiff: Math.abs(s.cx - slitX),
                  boostText: boost.text
                });
              }

              return {
                error: '',
                theoryY,
                bottomY,
                slitX,
                regs: uniqueRegs.map((r,i) => ({boat:i+1, reg:r.text, y:r.cy})),
                slitCandidates: slitEls.map(s => ({text:s.text, x:s.cx, y:s.cy})),
                alerts: results
              };
            }
            """
        )
    except Exception as e:
        print(f"スリットDOM解析失敗: {repr(e)}", flush=True)
        return []

    if data.get('error'):
        print(f"スリット解析: {data['error']}", flush=True)
        return []

    regs = data.get('regs', [])
    if regs:
        print(
            '理論表 艇番対応: ' + str({r['reg']: r['boat'] for r in regs[:6]}),
            flush=True,
        )

    candidates = data.get('slitCandidates', [])
    if candidates:
        print(f"スリット候補: {candidates}", flush=True)

    out = []
    for a in data.get('alerts', []):
        item = {
            'boat': int(a['boat']),
            'super': bool(a['super']),
            'slit': str(a['slit']),
            'boost': float(a['boost']),
        }
        out.append(item)
        print(
            f"スリット確定: {a['reg']}→{a['boat']}号艇 / "
            f"{a['slit']} / {a['boostText']} / "
            f"Y差={a['ydiff']:.1f}px",
            flush=True,
        )

    # 重複除去
    unique = {}
    for a in out:
        unique[(a['boat'], a['super'], a['slit'], a['boost'])] = a

    result = list(unique.values())
    result.sort(key=lambda x: x['boat'])

    if result:
        print(f"スリット欄解析結果: {result}", flush=True)

    return result


# ============================================================
# ntfy通知
# ============================================================

def send_ntfy(title, body):
    encoded_title = Header(
        title,
        "utf-8"
    ).encode()

    r = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={
            "Title": encoded_title,
            "Priority": "high",
            "Tags": "zap,ship",
        },
        timeout=15,
    )

    r.raise_for_status()


def notify_slit_alert(
    venue,
    race,
    deadline,
    alerts,
):
    new_alerts = []

    for a in alerts:
        key = (
            now().strftime("%Y-%m-%d"),
            venue,
            race,
            a["boat"],
            a["super"],
            a["slit"],
            a["boost"],
        )

        if key in SENT:
            continue

        SENT.add(key)
        new_alerts.append(a)

    if not new_alerts:
        return

    has_super = any(
        a["super"]
        for a in new_alerts
    )

    title = (
        "⚡ SUPERスリットアラート"
        if has_super
        else "🚤 スリットアラート"
    )

    lines = [
        f"{venue} {race}",
        "",
    ]

    for a in new_alerts:
        parts = [
            f"{a['boat']}号艇"
        ]

        if a["super"]:
            parts.append("⚡SUPER")

        parts.append(a["slit"])
        parts.append(
            f"1着{a['boost']:+g}%"
        )

        lines.append(
            "  ".join(parts)
        )

    if deadline:
        lines.extend([
            "",
            f"締切 {deadline}",
        ])

    body = "\n".join(lines)

    send_ntfy(
        title,
        body
    )

    print(
        f"通知送信: {venue} {race} / "
        f"{[a['boat'] for a in new_alerts]}号艇",
        flush=True,
    )


# ============================================================
# 1レース確認
# ============================================================

def inspect_race_page(page):
    try:
        text = page.locator(
            "body"
        ).inner_text(timeout=10000)
    except Exception:
        return

    venue = get_venue(text)
    race = get_race(text)

    if not venue or not race:
        return

    if venue not in TARGET_VENUES:
        return

    if not within_alert_window(text):
        return

    alerts = parse_slit_alerts(page)

    if not alerts:
        return

    d = get_deadline(text)

    print(
        f"スリットアラート検知: "
        f"{venue} {race} / {alerts}",
        flush=True,
    )

    notify_slit_alert(
        venue=venue,
        race=race,
        deadline=d,
        alerts=alerts,
    )


# ============================================================
# 監視サイクル
# ============================================================

def cycle(page):
    links = candidate_links(page)

    print(
        f"詳細候補リンク数: {len(links)}",
        flush=True,
    )

    if not links:
        print(
            "詳細候補リンクなし",
            flush=True,
        )
        return

    for url in links[:150]:
        try:
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=20000,
            )

            page.wait_for_timeout(350)

            inspect_race_page(page)

        except Exception as e:
            print(
                f"詳細ページ確認失敗: "
                f"{url} / {repr(e)}",
                flush=True,
            )


# ============================================================
# メイン
# ============================================================

def main():
    if not active():
        print(
            "監視時間外（23:00〜08:00 JST）。終了します。",
            flush=True,
        )
        return

    print(
        f"[{now():%Y-%m-%d %H:%M:%S}] "
        f"スリットアラート専用監視開始 [V43 column-position-fix]",
        flush=True,
    )

    print(
        "対象場: "
        + " / ".join(TARGET_VENUES),
        flush=True,
    )

    print(
        f"通知条件: サイトの「スリットアラート」欄に"
        f"実表示あり + 締切{ALERT_WINDOW_MIN}分以内",
        flush=True,
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True
        )

        context = browser.new_context(
            http_credentials={
                "username": SHINSUM_USER,
                "password": SHINSUM_PASSWORD,
                "origin": BASE_URL.rstrip("/"),
                "send": "always",
            }
        )

        page = context.new_page()

        while active():
            print(
                f"[{now():%Y-%m-%d %H:%M:%S}] チェック",
                flush=True,
            )

            cycle(page)

            if not active():
                break

            print(
                f"{CHECK_INTERVAL}秒後に再チェック",
                flush=True,
            )

            time.sleep(
                CHECK_INTERVAL
            )

        browser.close()


if __name__ == "__main__":
    main()
