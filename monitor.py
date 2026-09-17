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
    V41:
    V39で通知自体は成功したので、認証・リンク取得はそのまま使用。
    艇番だけ、文字列の並び順ではなくDOM上の「同じ艇の行」で判定する。

    手順:
      1) シンsum理論内の登録番号6艇を上から 1〜6号艇に対応
      2) +0.1 / +0.2 ... の表示要素を探す
      3) その要素から親要素へ上がり、「登録番号が1つだけある最小の行」を探す
      4) その登録番号から正しい艇番を確定
      5) 同じ行から 1着 +○% / SUPER を取得
    """
    alerts = []

    try:
        body = page.locator("body").inner_text(timeout=10000)
    except Exception:
        body = ""

    # --------------------------------------------------------
    # 本物の「シンsum理論」表を特定
    # --------------------------------------------------------
    theory_root = None

    # 「スリットアラート」見出しを含み、かつ4桁登録番号が複数ある
    # 最小寄りの祖先を探す。
    try:
        headers = page.get_by_text("スリットアラート", exact=False)

        for hi in range(headers.count() - 1, -1, -1):
            node = headers.nth(hi)

            for _ in range(8):
                try:
                    txt = " ".join(node.inner_text(timeout=300).split())
                except Exception:
                    txt = ""

                regs_here = re.findall(r"(?<!\d)\d{4}(?!\d)", txt)

                if (
                    "スリットアラート" in txt
                    and "シンsum理論" in txt
                    and len(set(regs_here)) >= 6
                ):
                    theory_root = node
                    break

                try:
                    node = node.locator("xpath=..")
                except Exception:
                    break

            if theory_root is not None:
                break
    except Exception:
        theory_root = None

    # 見つからない場合はbody全体を使う
    if theory_root is None:
        theory_root = page.locator("body")

    # --------------------------------------------------------
    # 登録番号 → 艇番 の対応を作る
    # --------------------------------------------------------
    reg_order = []

    try:
        links = theory_root.locator("a")

        for i in range(links.count()):
            a = links.nth(i)

            try:
                t = " ".join((a.inner_text(timeout=250) or "").split())
            except Exception:
                continue

            if re.fullmatch(r"\d{4}", t):
                if t not in reg_order:
                    reg_order.append(t)

            if len(reg_order) >= 6:
                break
    except Exception:
        pass

    # aタグで取れないサイト構造用
    if len(reg_order) < 6:
        try:
            t = theory_root.inner_text(timeout=1000)
        except Exception:
            t = ""

        for reg in re.findall(r"(?m)^\s*(\d{4})\s*$", t):
            if reg not in reg_order:
                reg_order.append(reg)

            if len(reg_order) >= 6:
                break

    reg_order = reg_order[:6]
    reg_to_boat = {
        reg: idx + 1
        for idx, reg in enumerate(reg_order)
    }

    print(
        f"理論表 登録番号対応: {reg_to_boat}",
        flush=True,
    )

    # --------------------------------------------------------
    # +0.1 / +0.2 ... の表示要素を探す
    # XPathなのでPlaywrightのtext regex依存を避ける
    # --------------------------------------------------------
    try:
        candidates = theory_root.locator(
            "xpath=.//*[contains(normalize-space(.), '+0.') and not(*)]"
        )
    except Exception:
        candidates = page.locator("xpath=//*[contains(normalize-space(.), '+0.') and not(*)]")

    for i in range(candidates.count()):
        el = candidates.nth(i)

        try:
            val = " ".join((el.inner_text(timeout=250) or "").split())
        except Exception:
            continue

        # スリット差の形式だけ
        if not re.fullmatch(r"\+0\.\d+", val):
            continue

        row_node = el
        row_text = ""
        matched_reg = None

        # ----------------------------------------------------
        # アラート要素から上へ。
        # 「登録番号が1つだけ含まれる最小祖先」を同じ艇の行とする。
        # ----------------------------------------------------
        for _ in range(10):
            try:
                txt = " ".join(row_node.inner_text(timeout=300).split())
            except Exception:
                txt = ""

            regs = []

            # まずリンクから登録番号
            try:
                aa = row_node.locator("a")
                for j in range(aa.count()):
                    try:
                        at = " ".join((aa.nth(j).inner_text(timeout=150) or "").split())
                    except Exception:
                        continue

                    if re.fullmatch(r"\d{4}", at):
                        regs.append(at)
            except Exception:
                pass

            # リンクで無ければ文字列から
            if not regs:
                regs = re.findall(
                    r"(?<!\d)(\d{4})(?!\d)",
                    txt
                )

            regs = [
                r for r in dict.fromkeys(regs)
                if r in reg_to_boat
            ]

            if len(regs) == 1:
                matched_reg = regs[0]
                row_text = txt
                break

            # 6艇全部を含むところまで来たら行を越えているので打切り
            if len(regs) >= 6:
                break

            try:
                row_node = row_node.locator("xpath=..")
            except Exception:
                break

        if not matched_reg:
            continue

        # ----------------------------------------------------
        # 同じ行から1着上昇幅を取得
        # ----------------------------------------------------
        boost_match = re.search(
            r"1着\s*([+-]\d+(?:\.\d+)?)\s*%",
            row_text
        )

        # DOM上で「+0.1」と「1着+8%」が兄弟セルの場合、
        # さらに1〜2階層だけ広げて探す。ただし登録番号は同じ1艇のみ。
        if not boost_match:
            probe = row_node

            for _ in range(2):
                try:
                    probe = probe.locator("xpath=..")
                    txt = " ".join(probe.inner_text(timeout=300).split())
                except Exception:
                    break

                regs = [
                    r for r in re.findall(r"(?<!\d)(\d{4})(?!\d)", txt)
                    if r in reg_to_boat
                ]
                regs = list(dict.fromkeys(regs))

                if len(regs) != 1 or regs[0] != matched_reg:
                    break

                boost_match = re.search(
                    r"1着\s*([+-]\d+(?:\.\d+)?)\s*%",
                    txt
                )

                if boost_match:
                    row_text = txt
                    break

        if not boost_match:
            continue

        boost = float(boost_match.group(1))
        boat = reg_to_boat[matched_reg]
        is_super = "SUPER" in row_text.upper()

        alerts.append({
            "boat": boat,
            "super": is_super,
            "slit": val,
            "boost": boost,
        })

    # --------------------------------------------------------
    # 最終フォールバック:
    # V39方式で検出はするが、艇番は登録番号対応から補正できる時だけ採用
    # --------------------------------------------------------
    if not alerts:
        marker = body.rfind("シンsum理論")
        section = body[marker:] if marker >= 0 else body

        note = section.find("※スリットアラート")
        if note >= 0:
            section = section[:note]

        # 1つのアラートだけでも検出可能な緩い抽出。
        # 艇番はここでは決め打ちしない。
        m = re.search(
            r"(?:SUPER\s*)?(\+0\.\d+)\s*[\r\n ]+1着\s*(\+\d+(?:\.\d+)?)\s*%",
            section,
            re.I,
        )

        if m:
            print(
                "警告: アラート表示は見つかったが、DOMから艇番を確定できませんでした。"
                "誤通知防止のため通知しません。",
                flush=True,
            )

    # 重複除去
    unique = {}

    for a in alerts:
        key = (
            a["boat"],
            a["slit"],
            a["boost"],
            a["super"],
        )
        unique[key] = a

    result = list(unique.values())
    result.sort(key=lambda x: x["boat"])

    if result:
        print(
            f"スリット欄解析結果: {result}",
            flush=True,
        )

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
        f"スリットアラート専用監視開始 [V41 row-dom-fix]",
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
