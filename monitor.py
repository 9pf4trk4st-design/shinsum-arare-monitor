import os
import re
import time
import csv
import json
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

BASE_URL = "https://boatrace-shinsum.com/"

USER = os.environ["SHINSUM_USER"]
PASSWORD = os.environ["SHINSUM_PASSWORD"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))
JST = ZoneInfo("Asia/Tokyo")

# -----------------------------
# 通知後の結果自動追跡
# -----------------------------
RESULT_CHECK_DELAY_MIN = int(os.getenv("RESULT_CHECK_DELAY_MIN", "3"))
RESULT_DATA_DIR = Path(os.getenv("SHINSUM_DATA_DIR", "data"))
RESULTS_CSV = RESULT_DATA_DIR / "shinsum_results.csv"
PENDING_JSON = RESULT_DATA_DIR / "shinsum_pending.json"
AUTO_GIT_SAVE = os.getenv("AUTO_GIT_SAVE", "1") == "1"

# BOAT RACE オフィシャルの場コード
VENUE_JCD = {
    "戸田": "02",
    "平和島": "04",
    "多摩川": "05",
    "蒲郡": "07",
    "三国": "10",
    "びわこ": "11",
    "住之江": "12",
    "鳴門": "14",
    "児島": "16",
    "宮島": "17",
    "徳山": "18",
    "下関": "19",
    "若松": "20",
    "芦屋": "21",
    "唐津": "23",
    "大村": "24",
}

TARGET_VENUES = [
    "平和島", "児島", "戸田", "多摩川",
    "蒲郡", "びわこ", "三国", "鳴門",
    "宮島", "徳山", "下関", "若松",
    "芦屋", "唐津", "大村", "住之江"
]

ALERT_TYPES = ("やや本命", "荒れ注意")

# -----------------------------
# 選別基準
# -----------------------------
# 最終補正1着率で比較する。
# 最終補正1着率 =
#   元の1着率
# + シンsum理論欄の1着補正
# + シンsumチェッカー該当ゾーンの1着補正

# 1号艇有利
ONE_MIN = float(os.getenv("ONE_MIN", "8.0"))
ONE_GAP_VS_34 = float(os.getenv("ONE_GAP_VS_34", "8.0"))
ONE_GAP_VS_SECOND = float(os.getenv("ONE_GAP_VS_SECOND", "5.0"))

# 3・4号艇有利
OUT_MIN = float(os.getenv("OUT_MIN", "10.0"))
OUT_GAP_VS_1 = float(os.getenv("OUT_GAP_VS_1", "8.0"))
OUT_GAP_VS_OTHER = float(os.getenv("OUT_GAP_VS_OTHER", "5.0"))

# 独立バフ検知
BUFF_TARGET_BOATS = (2, 3, 4)

# 最終比較のため1〜6号艇すべてチェッカー解析
CHECKER_PARSE_BOATS = (1, 2, 3, 4, 5, 6)

# 「理論補正 + チェッカー補正」がこの値を超えた2〜4号艇をバフ候補にする
BUFF_MIN_TOTAL = float(os.getenv("BUFF_MIN_TOTAL", "0.0"))

# 1号艇の「理論補正 + チェッカー補正」が0未満なら弱化扱い
ONE_WEAK_TOTAL_THRESHOLD = float(os.getenv("ONE_WEAK_TOTAL_THRESHOLD", "0.0"))

# 1号艇の逃げ強化 / 逃げ崩れ判定
# 逃げ強化:
#   元より上昇し、最終1着率が75%以上
#   かつ、最終1着率が判明している2〜6号艇に15%以上がいない
ESCAPE_FINAL_MIN = float(os.getenv("ESCAPE_FINAL_MIN", "75.0"))

# 他艇の「強い対抗」判定
RIVAL_FINAL_MIN = float(os.getenv("RIVAL_FINAL_MIN", "15.0"))

# 逃げ崩れ:
#   1号艇が元1着率から15pt以上低下し、
#   2〜6号艇のどれかが最終1着率15%以上
ONE_BIG_DROP_MIN = float(os.getenv("ONE_BIG_DROP_MIN", "15.0"))

seen = set()
pending_results = {}



def _ensure_result_dir():
    RESULT_DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_pending_results():
    """前回実行から残っている未確定通知を読み込む。"""
    global pending_results
    _ensure_result_dir()

    if not PENDING_JSON.exists():
        pending_results = {}
        return

    try:
        data = json.loads(PENDING_JSON.read_text(encoding="utf-8"))
        pending_results = data if isinstance(data, dict) else {}
        print(
            f"結果追跡pending読込: {len(pending_results)}件",
            flush=True
        )
    except Exception as e:
        pending_results = {}
        print(
            f"結果追跡pending読込失敗: {repr(e)}",
            flush=True
        )


def _save_pending_results():
    _ensure_result_dir()
    PENDING_JSON.write_text(
        json.dumps(
            pending_results,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


def _git_persist_result_files():
    """
    GitHub Actions上なら結果CSV/pendingをリポジトリへ自動保存する。
    workflowに contents: write がない場合はpush失敗ログだけ出し、監視は継続。
    """
    if not AUTO_GIT_SAVE:
        return

    if os.getenv("GITHUB_ACTIONS", "").lower() != "true":
        return

    try:
        subprocess.run(
            ["git", "config", "user.name", "github-actions[bot]"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        paths = [str(RESULTS_CSV), str(PENDING_JSON)]
        subprocess.run(["git", "add", *paths], check=True)

        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            check=False
        )
        if diff.returncode == 0:
            return

        subprocess.run(
            ["git", "commit", "-m", "chore: save shinsum race results"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        pushed = subprocess.run(
            ["git", "push"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        if pushed.returncode == 0:
            print("結果ログをGitHubへ保存", flush=True)
        else:
            print(
                "結果ログGitHub保存失敗: "
                + (pushed.stderr or pushed.stdout).strip(),
                flush=True
            )

    except Exception as e:
        print(
            f"結果ログGitHub保存処理失敗: {repr(e)}",
            flush=True
        )


def _race_datetime(date_str, deadline_value):
    h, m = map(int, deadline_value.split(":"))
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0,
        tzinfo=JST,
    )
    return dt


def register_result_tracking(
    venue,
    race,
    deadline_value,
    alert,
    final_rates,
    classification,
):
    """通知したレースを結果確認待ちとして保存する。"""
    if venue not in VENUE_JCD:
        return

    race_no_match = re.search(r"([1-9]|1[0-2])", race)
    if not race_no_match:
        return

    date_str = f"{now():%Y-%m-%d}"
    key = f"{date_str}|{venue}|{race}"

    snapshot_rates = {
        str(boat): {
            "base": x.get("base"),
            "theory": x.get("theory"),
            "checker": x.get("checker"),
            "final": x.get("final"),
        }
        for boat, x in final_rates.items()
    }

    pending_results[key] = {
        "date": date_str,
        "venue": venue,
        "race": race,
        "race_no": int(race_no_match.group(1)),
        "deadline": deadline_value,
        "alert": alert or "通常レース",
        "classification": classification.get("type", ""),
        "focus": classification.get("focus", []),
        "final_rates": snapshot_rates,
        "notified_at": now().isoformat(),
    }

    _save_pending_results()

    print(
        f"結果追跡登録: {venue} / {race} / "
        f"締切 {deadline_value} / "
        f"注目 {classification.get('focus', [])}",
        flush=True
    )


def fetch_official_winner(item):
    """
    BOAT RACE オフィシャル結果ページから1着艇を取得。
    未確定ならNone。
    """
    jcd = VENUE_JCD.get(item["venue"])
    if not jcd:
        return None

    hd = item["date"].replace("-", "")
    url = (
        "https://www.boatrace.jp/owpc/pc/race/raceresult"
        f"?hd={hd}&jcd={jcd}&rno={item['race_no']}"
    )

    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 Chrome/131 Safari/537.36"
                )
            },
            timeout=15,
        )
        r.raise_for_status()

        html = r.text
        # HTMLタグを空白化して1行にまとめる。
        plain = re.sub(r"<[^>]+>", " ", html)
        plain = re.sub(r"&nbsp;|&#160;", " ", plain)
        plain = re.sub(r"\s+", " ", plain)

        # 結果表: 1着(１) → 枠番 → 4桁登録番号 の並びを利用。
        m = re.search(
            r"(?:１|1)\s*([1-6])\s*\d{4}",
            plain
        )

        if not m:
            return None

        return int(m.group(1))

    except Exception as e:
        print(
            f"公式結果取得失敗: {item['venue']} / "
            f"{item['race']} / {repr(e)}",
            flush=True
        )
        return None


def _append_result_csv(item, winner):
    _ensure_result_dir()

    rates = {
        int(k): v
        for k, v in item.get("final_rates", {}).items()
    }
    focus = [int(x) for x in item.get("focus", [])]

    ranked = sorted(
        rates.items(),
        key=lambda kv: (
            kv[1].get("final")
            if kv[1].get("final") is not None
            else -999
        ),
        reverse=True,
    )

    predicted_top = ranked[0][0] if ranked else None
    predicted_top_rate = (
        ranked[0][1].get("final") if ranked else None
    )
    winner_final = (
        rates.get(winner, {}).get("final")
        if winner in rates
        else None
    )

    row = {
        "date": item["date"],
        "venue": item["venue"],
        "race": item["race"],
        "deadline": item["deadline"],
        "alert": item.get("alert", ""),
        "classification": item.get("classification", ""),
        "focus_boats": "-".join(map(str, focus)),
        "winner": winner,
        "focus_hit": 1 if winner in focus else 0,
        "predicted_top": predicted_top or "",
        "top_hit": 1 if predicted_top == winner else 0,
        "predicted_top_rate": (
            f"{predicted_top_rate:.1f}"
            if predicted_top_rate is not None else ""
        ),
        "winner_final_rate": (
            f"{winner_final:.1f}"
            if winner_final is not None else ""
        ),
        "resolved_at": now().isoformat(),
    }

    fields = list(row.keys())
    exists = RESULTS_CSV.exists()

    with RESULTS_CSV.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerow(row)

    print(
        f"結果ログ保存: {item['venue']} / {item['race']} / "
        f"1着{winner}号艇 / 注目的中={row['focus_hit']} / "
        f"最終値トップ的中={row['top_hit']}",
        flush=True
    )


def check_pending_results():
    """締切後の通知済みレースを自動で結果確認しCSVへ保存する。"""
    if not pending_results:
        return

    resolved = []

    for key, item in list(pending_results.items()):
        try:
            race_dt = _race_datetime(
                item["date"],
                item["deadline"]
            )
        except Exception:
            continue

        if now() < race_dt + timedelta(minutes=RESULT_CHECK_DELAY_MIN):
            continue

        winner = fetch_official_winner(item)
        if winner is None:
            continue

        _append_result_csv(item, winner)
        resolved.append(key)

    if not resolved:
        return

    for key in resolved:
        pending_results.pop(key, None)

    _save_pending_results()
    _git_persist_result_files()


def print_result_stats():
    """保存済み結果の簡単な自動集計をログへ表示。"""
    if not RESULTS_CSV.exists():
        return

    try:
        rows = list(csv.DictReader(
            RESULTS_CSV.open("r", encoding="utf-8")
        ))
        if not rows:
            return

        n = len(rows)
        focus_hits = sum(
            1 for r in rows if r.get("focus_hit") == "1"
        )
        top_hits = sum(
            1 for r in rows if r.get("top_hit") == "1"
        )

        print(
            f"結果統計: {n}件 / "
            f"注目艇1着 {focus_hits}件({focus_hits / n * 100:.1f}%) / "
            f"最終値トップ1着 {top_hits}件({top_hits / n * 100:.1f}%)",
            flush=True
        )
    except Exception as e:
        print(f"結果統計読込失敗: {repr(e)}", flush=True)

def now():
    return datetime.now(JST)


def active():
    return 8 <= now().hour < 23


def deadline(text):
    m = re.search(
        r"締切\s*[：:]?\s*([01]?\d|2[0-3]):([0-5]\d)",
        text
    )
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


def within15(text):
    d = deadline(text)
    if not d:
        return False

    h, m = map(int, d.split(":"))
    t = now().replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0
    )

    return timedelta(minutes=-1) <= t - now() <= timedelta(minutes=15)


def candidate_links(page):
    page.goto(
        BASE_URL,
        wait_until="domcontentloaded",
        timeout=30000
    )
    page.wait_for_timeout(1000)

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


def actual_venue(text):
    head = text[:1800]
    matches = [v for v in TARGET_VENUES if v in head]
    return matches[0] if len(matches) == 1 else ""


def actual_race(text):
    m = re.search(
        r"(?<!\d)([1-9]|1[0-2])\s*R\b",
        text[:1800],
        re.I
    )
    return m.group(1) + "R" if m else ""


def alert_near_deadline(text, d):
    pos = -1

    for p in (
        f"締切 {d}",
        f"締切{d}",
        f"締切：{d}",
        f"締切: {d}"
    ):
        pos = text.find(p)

        if pos >= 0:
            break

    if pos < 0:
        return None

    local = text[
        max(0, pos - 250):
        min(len(text), pos + 350)
    ]

    return next(
        (a for a in ALERT_TYPES if a in local),
        None
    )



def parse_base_1st_rates_dom(page):
    """
    画面上の位置(Y座標)を使って「選手名・1着率」欄だけから元1着率を取る。

    inner_text の並びや「←シンsum理論に戻る」等には依存しない。
    「選手名・1着率」見出しより下、
    「危険艇 / 戦法別上昇率 / スリット隊形 / シンsum理論」より上に
    実際に表示されている符号なし%だけを取得する。
    """
    try:
        data = page.evaluate(
            r"""
            () => {
              const norm = (s) =>
                (s || "")
                  .replace(/\u00a0/g, " ")
                  .replace(/\s+/g, " ")
                  .trim();

              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return (
                  r.width > 0 &&
                  r.height > 0 &&
                  st.display !== "none" &&
                  st.visibility !== "hidden"
                );
              };

              const els = Array.from(document.querySelectorAll("body *"))
                .filter(visible);

              // できるだけ小さい要素で「選手名・1着率」見出しを探す。
              let headers = els.filter((el) => {
                const t = norm(el.innerText);
                return (
                  t.length <= 30 &&
                  /選手名\s*[・･]\s*1着率/.test(t)
                );
              });

              if (!headers.length) {
                // 見出しが「選手名」「1着率」に分かれている場合の保険。
                headers = els.filter((el) => {
                  const t = norm(el.innerText);
                  return t === "選手名";
                });
              }

              if (!headers.length) {
                return {
                  ok: false,
                  reason: "header_not_found",
                  values: []
                };
              }

              // ページ上部側の最初の候補を使う。
              headers.sort(
                (a, b) =>
                  a.getBoundingClientRect().top -
                  b.getBoundingClientRect().top
              );

              const header = headers[0];
              const hr = header.getBoundingClientRect();
              const startY = hr.bottom;

              // 次セクションの開始Yを探す。
              const endWords = [
                "危険艇",
                "戦法別上昇率",
                "スリット隊形",
                "シンsum理論"
              ];

              const endYs = [];

              for (const el of els) {
                const r = el.getBoundingClientRect();
                if (r.top <= startY + 2) continue;

                const t = norm(el.innerText);

                // 親要素全体を誤検出しないよう短いラベルだけを見る。
                if (t.length > 40) continue;

                if (endWords.some((w) => t === w || t.startsWith(w))) {
                  endYs.push(r.top);
                }
              }

              const endY = endYs.length
                ? Math.min(...endYs)
                : startY + 1400;

              const raw = [];

              for (const el of els) {
                const r = el.getBoundingClientRect();
                if (r.top < startY - 2 || r.bottom > endY + 2) {
                  continue;
                }

                const t = norm(el.innerText);

                // 元1着率は符号なし xx% だけ。
                if (!/^\d+(?:\.\d+)?%$/.test(t)) continue;

                raw.push({
                  text: t,
                  value: parseFloat(t),
                  x: r.left,
                  y: r.top,
                  h: r.height,
                  tag: el.tagName
                });
              }

              // 入れ子要素で同じ%が重複することがあるため、
              // 同一Y付近・同一値は1件にする。
              raw.sort((a, b) => a.y - b.y || a.x - b.x);

              const dedup = [];
              for (const item of raw) {
                const dup = dedup.some(
                  (x) =>
                    Math.abs(x.y - item.y) < 3 &&
                    x.value === item.value
                );
                if (!dup) dedup.push(item);
              }

              // 選手一覧は上から1〜6号艇。
              const values = dedup.slice(0, 6).map((x) => x.value);

              return {
                ok: values.length > 0,
                reason: values.length ? "ok" : "no_percent_in_section",
                startY,
                endY,
                values,
                raw: dedup.slice(0, 12)
              };
            }
            """
        )

        values = data.get("values", []) if isinstance(data, dict) else []

        result = {
            boat: float(values[boat - 1])
            for boat in range(1, len(values) + 1)
        }

        print(
            f"元1着率候補(DOM/V19): {values} -> {result}",
            flush=True
        )

        if not result:
            print(
                f"元1着率DOM取得失敗: {data}",
                flush=True
            )

        return result

    except Exception as e:
        print(
            f"元1着率DOM解析例外: {repr(e)}",
            flush=True
        )
        return {}

def parse_base_1st_rates(text):
    """
    ページ上部の「選手名・1着率」欄から、明示された元1着率だけを取得する。

    V18の重要修正:
    ページ上部には「←シンsum理論に戻る」があるため、
    ページ先頭から最初の「シンsum理論」を終了位置にすると
    選手一覧より前で切れてしまう。

    そこで、
      1. 締切表示より後にある最初の「選手名」を開始点
      2. その開始点より後にある
         「危険艇 / 戦法別上昇率 / スリット隊形 / シンsum理論」
         の最初を終了点
    として、選手一覧だけを切り出す。

    切り出した範囲の符号なし%を上から順に1〜6号艇へ割り当てる。
    「危険艇」以降の12/13/14/15/16の組み合わせ確率は含めない。
    """

    # 締切表示より後から選手一覧を探す。
    deadline_pos = -1
    m_deadline = re.search(
        r"締切\s*[：:]?\s*(?:[01]?\d|2[0-3]):[0-5]\d",
        text
    )
    if m_deadline:
        deadline_pos = m_deadline.end()

    # 「選手名・1着率」でも「選手名」単独でも対応。
    search_from = max(0, deadline_pos)
    m_header = re.search(
        r"選手名(?:\s*[・･]\s*1着率)?",
        text[search_from:]
    )

    if not m_header:
        print(
            "元1着率: 選手一覧開始位置を検出できず",
            flush=True
        )
        return {}

    section_start = search_from + m_header.start()

    # 必ず section_start より後だけを検索する。
    tail = text[section_start:]
    end_candidates = []

    for pattern in (
        r"危険艇",
        r"戦法別上昇率",
        r"スリット隊形",
        r"シン\s*sum理論",
    ):
        m = re.search(pattern, tail[1:])
        if m:
            end_candidates.append(
                section_start + 1 + m.start()
            )

    section_end = (
        min(end_candidates)
        if end_candidates
        else min(len(text), section_start + 7000)
    )

    section = text[section_start:section_end]

    # 元1着率は符号なしの%。
    # +11% / -8% のような上昇率・補正値は除外。
    values = [
        float(x)
        for x in re.findall(
            r"(?<![+\-\d.])(\d+(?:\.\d+)?)\s*%",
            section
        )
    ][:6]

    result = {
        boat: values[boat - 1]
        for boat in range(1, len(values) + 1)
    }

    print(
        f"元1着率候補(V18): {values} -> {result}",
        flush=True
    )

    # 取得失敗時は切り出した本文も一部出して原因確認できるようにする。
    if not result:
        print(
            "元1着率解析section:",
            repr(section[:1200]),
            flush=True
        )

    return result


def extract_theory_section(text):
    """
    実際のシンsum理論表だけを切り出す。

    ページ上部の「←シンsum理論に戻る」を誤認しないため、
    text内のすべての「シンsum理論」候補を調べ、
    その後に4桁登録番号が最も多く並ぶ候補を本表として採用する。
    """
    starts = [m.start() for m in re.finditer(r"シン\s*sum理論", text)]

    best_section = ""
    best_score = -1

    for start in starts:
        end = text.find("シンsumチェッカー", start)
        section = text[start:(end if end > start else min(len(text), start + 10000))]

        regs = re.findall(r"(?<!\d)([3-5]\d{3})(?!\d)", section)
        regs_unique = list(dict.fromkeys(regs))

        diff_like = re.findall(
            r"(?<![\d.])([+-]\d+(?:\.\d+)?)(?!\s*%)",
            section
        )

        score = len(regs_unique) * 100 + len(diff_like)

        if score > best_score:
            best_score = score
            best_section = section

    return best_section

def parse_theory_adjustments(text):
    """
    シンsum理論表の「1着」補正を6艇全部取得。

    各4桁登録番号を起点に、その艇ブロック内で最初に出る
    %付き数値を1着補正として読む。
    """
    section = extract_theory_section(text)
    if not section:
        return {}

    regs = re.findall(r"(?<!\d)([3-5]\d{3})(?!\d)", section)
    regs = list(dict.fromkeys(regs))

    if len(regs) < 6:
        return {}

    regs = regs[:6]
    result = {}

    for boat, reg in enumerate(regs, start=1):
        mreg = re.search(
            rf"(?<!\d){re.escape(reg)}(?!\d)",
            section
        )
        if not mreg:
            continue

        next_pos = len(section)
        if boat < 6:
            next_reg = regs[boat]
            mn = re.search(
                rf"(?<!\d){re.escape(next_reg)}(?!\d)",
                section[mreg.end():]
            )
            if mn:
                next_pos = mreg.end() + mn.start()

        block = section[mreg.end():next_pos]

        pcts = re.findall(
            r"([+-]?\d+(?:\.\d+)?)\s*%",
            block
        )

        if pcts:
            result[boat] = float(pcts[0])

    return result


def parse_current_diffs(text):
    """
    シンsum理論表の「平均との差」を6艇分取得する。

    重要:
    艇番の数字(1,2,3...)やヘッダーの「1着/2着/3着」を起点にしない。
    各艇の4桁登録番号を起点に、その直後に出る
    「符号付き・%なし」の数値を平均との差として読む。

    例:
      4836 -> +0.87
      3807 -> +0.03
      4419 -> +0.18
    """
    section = extract_theory_section(text)
    if not section:
        return {}

    # 登録番号を出現順に拾う。シンsum理論では上から1〜6号艇。
    regs = re.findall(r"(?<!\d)([3-5]\d{3})(?!\d)", section)

    regs = list(dict.fromkeys(regs))

    if len(regs) < 6:
        return {}

    regs = regs[:6]
    result = {}

    for boat, reg in enumerate(regs, start=1):
        mreg = re.search(
            rf"(?<!\d){re.escape(reg)}(?!\d)",
            section
        )
        if not mreg:
            continue

        # 次の登録番号までをその艇の範囲にする。
        next_pos = len(section)
        if boat < 6:
            next_reg = regs[boat]
            mn = re.search(
                rf"(?<!\d){re.escape(next_reg)}(?!\d)",
                section[mreg.end():]
            )
            if mn:
                next_pos = mreg.end() + mn.start()

        block = section[mreg.end():next_pos]

        # +0.87 / -0.02 のような符号付き数値。
        # %付きの理論補正は除外する。
        candidates = re.findall(
            r"(?<![\d.])([+-]\d+(?:\.\d+)?)(?!\s*%)",
            block
        )

        if not candidates:
            continue

        # 最初に出る値が平均との差。
        value = float(candidates[0])

        # 誤取得防止
        if -5.0 <= value <= 5.0:
            result[boat] = value

    return result


def parse_registration_numbers(text):
    """
    シンsum理論欄の4桁登録番号を上から6個取得し、
    1〜6号艇に割り当てる。

    艇番の単独行を探さないため、
    ヘッダー数字を誤認しない。
    """
    section = extract_theory_section(text)
    if not section:
        return {}

    regs = re.findall(
        r"(?<!\d)([3-5]\d{3})(?!\d)",
        section
    )

    # 出現順を維持したまま重複除外
    regs = list(dict.fromkeys(regs))

    if len(regs) < 6:
        print(
            f"登録番号候補不足: {regs}",
            flush=True
        )
        return {}

    regs = regs[:6]

    return {
        boat: regs[boat - 1]
        for boat in range(1, 7)
    }


def parse_single_checker_1st(text, boat, current_diff):
    """
    現在表示中のシンsumチェッカーから、指定艇の該当ゾーン1着率を取得する。
    """
    start = text.find("シンsumチェッカー")
    if start < 0:
        return None

    section = text[start:]
    compact = re.sub(r"\s+", "", section)

    token = f"{boat}号艇"
    pos = compact.find(token)
    if pos < 0:
        return None

    # 選択中のカードだけ取れればよいので、次の艇カードか十分な長さまで
    next_positions = []
    for other in range(1, 7):
        if other == boat:
            continue
        p = compact.find(f"{other}号艇", pos + len(token))
        if p >= 0:
            next_positions.append(p)

    end = min(next_positions) if next_positions else min(len(compact), pos + 5000)
    card = compact[pos:end]

    zone = checker_zone(current_diff)
    zpos = -1
    matched = None

    for variant in _zone_variants(zone):
        zpos = card.find(variant)
        if zpos >= 0:
            matched = variant
            break

    if zpos < 0:
        return None

    row = card[zpos:zpos + 420]
    pcts = re.findall(r"([+-]?\d+(?:\.\d+)?)%", row)

    if not pcts:
        return None

    return {
        "zone": zone,
        "matched_zone_text": matched,
        "checker_1st": float(pcts[0]),
    }


def collect_checker_1st(page, current_diffs, registrations):
    """
    1〜6号艇の登録番号を順番にクリックしてチェッカーを取得する。

    V9:
    - クリック後450ms固定待ちを廃止
    - 最大4秒、250ms間隔で表示完了を待つ
    - 1回で出なければ最大3回まで再クリック
    - 「データが見つかりません」は正式なデータなしとして区別
    """
    result = {}

    for boat in CHECKER_PARSE_BOATS:
        diff = current_diffs.get(boat)
        reg = registrations.get(boat)

        if diff is None or not reg:
            print(
                f"チェッカー前提データ不足: {boat}号艇 / "
                f"差={diff} / 登録番号={reg}",
                flush=True
            )
            continue

        got = None
        no_data = False

        for attempt in range(1, 4):
            try:
                loc = page.locator("a").filter(
                    has_text=re.compile(
                        rf"^\s*{re.escape(reg)}\s*$"
                    )
                )

                if loc.count() == 0:
                    loc = page.get_by_text(reg, exact=True)

                if loc.count() == 0:
                    print(
                        f"登録番号リンク未検出: "
                        f"{boat}号艇 / {reg}",
                        flush=True
                    )
                    break

                try:
                    loc.first.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass

                loc.first.click(
                    timeout=4000,
                    force=(attempt >= 2)
                )

                # 固定450msではなく、チェッカー表示を最大4秒待つ
                for _ in range(16):
                    page.wait_for_timeout(250)

                    body = page.locator("body").inner_text(
                        timeout=10000
                    )

                    # サイト側が明示的に「データなし」と返した場合
                    if (
                        f"{boat}号艇のデータが見つかりません" in body
                        or "データが見つかりません" in body[
                            max(0, body.find("シンsumチェッカー")):
                        ]
                    ):
                        no_data = True
                        break

                    info = parse_single_checker_1st(
                        body,
                        boat,
                        diff
                    )

                    if info:
                        got = info
                        break

                if got or no_data:
                    break

                print(
                    f"チェッカー表示待ち再試行: "
                    f"{boat}号艇 / {reg} / "
                    f"{attempt}回目",
                    flush=True
                )

            except Exception as e:
                print(
                    f"チェッカークリック再試行: "
                    f"{boat}号艇 / {reg} / "
                    f"{attempt}回目 / {repr(e)}",
                    flush=True
                )

        if got:
            result[boat] = got
            print(
                f"チェッカー取得成功: {boat}号艇 / "
                f"{reg} / 差{diff:+.2f} / "
                f"{got['zone']} / "
                f"1着{got['checker_1st']:+.1f}%",
                flush=True
            )
        elif no_data:
            print(
                f"チェッカーデータなし: "
                f"{boat}号艇 / {reg} / 差{diff:+.2f}",
                flush=True
            )
        else:
            print(
                f"チェッカー取得失敗: "
                f"{boat}号艇 / {reg} / 差{diff:+.2f} / "
                f"3回再試行しても表示確認できず",
                flush=True
            )

    return result


def checker_zone(current_diff):
    if current_diff >= 0.5:
        return "+0.5以上"
    if current_diff >= 0:
        return "0〜+0.5"
    if current_diff >= -0.5:
        return "-0.5〜0"
    return "-0.5未満"


def _zone_variants(zone):
    variants = {
        "+0.5以上": (
            "+0.5以上",
            "0.5以上",
        ),
        "0〜+0.5": (
            "0〜+0.5",
            "0～+0.5",
            "0~+0.5",
            "0～0.5",
            "0〜0.5",
            "0~0.5",
        ),
        "-0.5〜0": (
            "-0.5〜0",
            "-0.5～0",
            "-0.5~0",
        ),
        "-0.5未満": (
            "-0.5未満",
        ),
    }
    return variants.get(zone, (zone,))


def parse_checker_1st(text, current_diffs):
    """
    各艇のシンsumチェッカーから、
    現在の「平均との差」が属するゾーンの1着率補正を取得。

    例:
    1号艇 差+0.52 → +0.5以上 → +24.1%
    """
    start = text.find("シンsumチェッカー")
    if start < 0:
        return {}

    section = text[start:]
    compact = re.sub(r"\s+", "", section)
    result = {}

    for boat in CHECKER_PARSE_BOATS:
        if boat not in current_diffs:
            continue

        token = f"{boat}号艇"
        pos = compact.find(token)

        if pos < 0:
            continue

        # 次の艇カードまで
        next_positions = []

        for other in range(1, 7):
            if other == boat:
                continue

            p = compact.find(
                f"{other}号艇",
                pos + len(token)
            )

            if p >= 0:
                next_positions.append(p)

        end = (
            min(next_positions)
            if next_positions
            else min(len(compact), pos + 4500)
        )

        card = compact[pos:end]
        zone = checker_zone(current_diffs[boat])

        zpos = -1
        matched_zone_text = None

        for variant in _zone_variants(zone):
            zpos = card.find(variant)
            if zpos >= 0:
                matched_zone_text = variant
                break

        if zpos < 0:
            continue

        # 該当行の後ろ側を確認
        row = card[zpos:zpos + 320]

        # 「件数」の数字を%と誤認しないよう、%付きだけを拾う
        pcts = re.findall(
            r"([+-]?\d+(?:\.\d+)?)%",
            row
        )

        if not pcts:
            continue

        result[boat] = {
            "zone": zone,
            "matched_zone_text": matched_zone_text,
            "checker_1st": float(pcts[0]),
        }

    return result



def build_final_rates(base_rates, theory_adj, checker):
    """
    元1着率が明示されている艇:
      最終補正1着率 = 元1着率 + 理論補正 + チェッカー補正

    元1着率が表示されていない艇:
      最終補正1着率は作らず、
      理論補正 + チェッカー補正 = 総合バフ/デバフ として保持する。
    """
    result = {}

    for boat in range(1, 7):
        theory = theory_adj.get(boat)
        checker_info = checker.get(boat)

        if theory is None or not checker_info:
            continue

        base = base_rates.get(boat)
        checker_1st = checker_info["checker_1st"]
        total_adjustment = theory + checker_1st

        result[boat] = {
            "base": base,
            "base_known": base is not None,
            "theory": theory,
            "checker": checker_1st,
            "zone": checker_info["zone"],
            "total_adjustment": total_adjustment,
            "final": (
                base + total_adjustment
                if base is not None
                else None
            ),
        }

    return result


def classify_buff(final_rates, current_diffs):
    """
    2・3・4号艇の独立バフ判定。

    元1着率が分かる艇:
      最終1着率まで計算。

    元1着率が分からない艇:
      「理論補正 + チェッカー補正」の総合バフだけで評価。
      元1着率を勝手に補完しない。
    """
    buffs = []

    one = final_rates.get(1)

    if one:
        one_total_adjustment = one["total_adjustment"]
        one_final = one["final"]
        one_weak = (
            one_total_adjustment < ONE_WEAK_TOTAL_THRESHOLD
        )
    else:
        one_total_adjustment = None
        one_final = None
        one_weak = False

    for boat in BUFF_TARGET_BOATS:
        info = final_rates.get(boat)
        diff = current_diffs.get(boat)

        if not info or diff is None:
            continue

        total_boost = info["total_adjustment"]

        if total_boost <= BUFF_MIN_TOTAL:
            continue

        edge_vs_one_final = (
            info["final"] - one_final
            if info["final"] is not None and one_final is not None
            else None
        )

        buffs.append({
            "boat": boat,
            "current_diff": diff,
            "zone": info["zone"],
            "base_1st": info["base"],
            "base_known": info["base_known"],
            "theory_1st": info["theory"],
            "checker_1st": info["checker"],
            "total_boost": total_boost,
            "final_1st": info["final"],
            "one_final_1st": one_final,
            "one_total_adjustment": one_total_adjustment,
            "one_weak": one_weak,
            "edge_vs_one_final": edge_vs_one_final,
        })

    if not buffs:
        return None

    buffs.sort(
        key=lambda x: (
            1 if x["one_weak"] else 0,
            x["total_boost"],
            x["final_1st"] if x["final_1st"] is not None else -999.0,
        ),
        reverse=True
    )

    best = buffs[0]
    focus = [x["boat"] for x in buffs]

    if best["base_known"]:
        reason = (
            f"{best['boat']}号艇: 元 {best['base_1st']:.1f}% "
            f"+ 理論 {best['theory_1st']:+.1f}% "
            f"+ チェッカー {best['checker_1st']:+.1f}% "
            f"= 最終 {best['final_1st']:.1f}%"
        )
    else:
        reason = (
            f"{best['boat']}号艇: 元1着率は表示なし。"
            f"理論 {best['theory_1st']:+.1f}% "
            f"+ チェッカー {best['checker_1st']:+.1f}% "
            f"= 総合バフ {best['total_boost']:+.1f}pt"
        )

    if one:
        if one["final"] is not None:
            reason += (
                f"。1号艇は 元 {one['base']:.1f}% "
                f"+ 理論 {one['theory']:+.1f}% "
                f"+ チェッカー {one['checker']:+.1f}% "
                f"= 最終 {one['final']:.1f}%"
            )
        else:
            reason += (
                f"。1号艇は元1着率表示なし、"
                f"補正合計 {one['total_adjustment']:+.1f}pt"
            )

    if best["edge_vs_one_final"] is not None:
        reason += f"。最終1着率の差 {best['edge_vs_one_final']:+.1f}pt"

    if one_weak:
        reason += (
            f"。1号艇の補正合計 "
            f"{one_total_adjustment:+.1f}ptで弱化"
        )

    return {
        "type": "独立バフ",
        "focus": focus,
        "buffs": buffs,
        "one": one,
        "one_weak": one_weak,
        "reason": reason,
    }

def classify_escape_boost(final_rates):
    """
    1号艇逃げ強化:
      - 1号艇の元1着率と最終1着率が取得できる
      - 最終1着率が75%以上
      - 元より最終が上昇している（元80→最終76のような弱化は除外）
      - 2〜6号艇で、最終1着率が判明している艇に15%以上がいない
    """
    one = final_rates.get(1)
    if not one or one.get("base") is None or one.get("final") is None:
        return None

    base = one["base"]
    final = one["final"]
    rise = final - base

    if final < ESCAPE_FINAL_MIN or rise <= 0:
        return None

    strong_rivals = []
    for boat in range(2, 7):
        info = final_rates.get(boat)
        if not info or info.get("final") is None:
            continue
        if info["final"] >= RIVAL_FINAL_MIN:
            strong_rivals.append({
                "boat": boat,
                "final": info["final"],
            })

    if strong_rivals:
        return None

    return {
        "type": "1号艇逃げ強化",
        "focus": [1],
        "one_base": base,
        "one_final": final,
        "one_change": rise,
        "strong_rivals": [],
        "reason": (
            f"1号艇が元 {base:.1f}% → 最終 {final:.1f}% "
            f"（{rise:+.1f}pt）。最終75%以上かつ上昇。"
            f"確認できる2〜6号艇に最終15%以上なし"
        ),
    }


def classify_escape_collapse(final_rates):
    """
    1号艇逃げ崩れ:
      - 1号艇の元/最終が取得できる
      - 最終が元から大幅低下（初期値15pt以上）
      - 2〜6号艇のどれかが最終1着率15%以上
    """
    one = final_rates.get(1)
    if not one or one.get("base") is None or one.get("final") is None:
        return None

    base = one["base"]
    final = one["final"]
    drop = base - final

    if drop < ONE_BIG_DROP_MIN:
        return None

    strong_rivals = []
    for boat in range(2, 7):
        info = final_rates.get(boat)
        if not info or info.get("final") is None:
            continue
        if info["final"] >= RIVAL_FINAL_MIN:
            strong_rivals.append({
                "boat": boat,
                "final": info["final"],
                "base": info.get("base"),
                "change": (
                    info["final"] - info["base"]
                    if info.get("base") is not None
                    else None
                ),
            })

    if not strong_rivals:
        return None

    strong_rivals.sort(key=lambda x: x["final"], reverse=True)

    return {
        "type": "1号艇逃げ崩れ",
        "focus": [x["boat"] for x in strong_rivals],
        "one_base": base,
        "one_final": final,
        "one_change": final - base,
        "strong_rivals": strong_rivals,
        "reason": (
            f"1号艇が元 {base:.1f}% → 最終 {final:.1f}% "
            f"（{final-base:+.1f}pt）と大幅弱化。"
            + " / ".join(
                f"{x['boat']}号艇 最終{x['final']:.1f}%"
                for x in strong_rivals
            )
            + " が15%以上"
        ),
    }


def classify_final_rates(final_rates):
    """
    取得できた艇だけで「最終補正1着率」を比較する。

    ・6艇全部あれば通常どおり6艇比較
    ・チェッカー履歴がまだ無い若い選手などが1艇だけいる場合は、
      その艇を除外して残り5艇で判定する
    ・2艇以上欠けて4艇以下になった場合は、誤判定防止のため判定しない
    """
    available = sorted(
        boat
        for boat, info in final_rates.items()
        if info.get("final") is not None
    )

    if len(available) < 5:
        return None

    values = {
        boat: final_rates[boat]["final"]
        for boat in available
    }

    vals = list(values.values())
    best_all = max(vals)
    second_best = sorted(vals, reverse=True)[1]

    # -----------------------------
    # 1号艇有利
    # 1号艇のチェッカーデータが無い場合は判定不能なのでスキップ
    # -----------------------------
    if 1 in values:
        b1 = values[1]

        # 3・4号艇のうち取得できているもの
        vals34 = [
            values[b]
            for b in (3, 4)
            if b in values
        ]

        if vals34:
            best34 = max(vals34)

            if (
                b1 == best_all
                and b1 >= ONE_MIN
                and (b1 - best34) >= ONE_GAP_VS_34
                and (b1 - second_best) >= ONE_GAP_VS_SECOND
            ):
                missing = [
                    str(b)
                    for b in range(1, 7)
                    if b not in values
                ]
                missing_text = (
                    f"（{','.join(missing)}号艇はチェッカーデータなしで除外）"
                    if missing else ""
                )

                return {
                    "type": "1号艇有利",
                    "focus": [1],
                    "reason": (
                        f"最終補正1着率で1号艇が取得艇中トップ。"
                        f"3・4号艇の最大値より {b1 - best34:+.1f}pt、"
                        f"2番手より {b1 - second_best:+.1f}pt 優勢"
                        f"{missing_text}"
                    )
                }

    # -----------------------------
    # 3・4号艇有利
    # 取得できている3・4号艇だけを候補にする
    # -----------------------------
    candidates34 = [
        b for b in (3, 4)
        if b in values
    ]

    if not candidates34:
        return None

    best_boat = max(
        candidates34,
        key=lambda b: values[b]
    )
    best_value = values[best_boat]

    # 3・4以外の取得済み艇
    other_values = [
        values[b]
        for b in available
        if b not in (3, 4)
    ]

    if not other_values:
        return None

    best_other = max(other_values)

    # 1号艇が取得できている時だけ「1号艇との差」を条件に使う。
    # 1号艇がデータ不足なら、残り5艇比較なのでこの条件は課さない。
    gap_vs_1_ok = True
    gap_vs_1_text = ""

    if 1 in values:
        gap_vs_1 = best_value - values[1]
        gap_vs_1_ok = gap_vs_1 >= OUT_GAP_VS_1
        gap_vs_1_text = f"1号艇より {gap_vs_1:+.1f}pt、"
    else:
        gap_vs_1_text = "1号艇はチェッカーデータなしで比較除外、"

    if (
        best_value == best_all
        and best_value >= OUT_MIN
        and gap_vs_1_ok
        and (best_value - best_other) >= OUT_GAP_VS_OTHER
    ):
        focus = [best_boat]

        other_boat = 4 if best_boat == 3 else 3

        if (
            other_boat in values
            and values[other_boat] >= OUT_MIN
            and abs(
                values[best_boat] - values[other_boat]
            ) <= 8
        ):
            focus = [3, 4]

        missing = [
            str(b)
            for b in range(1, 7)
            if b not in values
        ]
        missing_text = (
            f"（{','.join(missing)}号艇を除外した{len(values)}艇比較）"
            if missing else ""
        )

        return {
            "type": "3・4号艇有利",
            "focus": focus,
            "reason": (
                f"最終補正1着率で{best_boat}号艇が取得艇中トップ。"
                f"{gap_vs_1_text}"
                f"3・4以外の最上位より "
                f"{best_value - best_other:+.1f}pt 優勢"
                f"{missing_text}"
            )
        }

    return None



def notify_selected(
    alert,
    venue,
    race,
    deadline_value,
    final_rates,
    classification
):
    kind = classification["type"]
    focus = set(classification["focus"])

    secondary_buffs = classification.get("secondary_buffs", [])
    secondary_focus = {
        x["boat"] for x in secondary_buffs
        if x["boat"] not in focus
    }

    if kind in ("1号艇有利", "1号艇逃げ強化"):
        symbol = "🟢"
    elif kind == "1号艇逃げ崩れ":
        symbol = "🔴"
    elif kind == "独立バフ":
        symbol = "🚀"
    else:
        symbol = "🔥"

    rate_lines = []

    for boat in range(1, 7):
        x = final_rates.get(boat)
        if not x:
            continue

        if boat in focus:
            mark = " ←主注目"
        elif boat in secondary_focus:
            mark = " ←バフ注目"
        else:
            mark = ""

        if x["base"] is not None:
            line = (
                f"{boat}号艇 "
                f"{x['base']:.1f}% "
                f"+ 理論{x['theory']:+.1f}% "
                f"+ チェッカー{x['checker']:+.1f}% "
                f"= {x['final']:.1f}%"
            )
        else:
            line = (
                f"{boat}号艇 元1着率表示なし / "
                f"理論{x['theory']:+.1f}% "
                f"+ チェッカー{x['checker']:+.1f}% "
                f"= 補正合計{x['total_adjustment']:+.1f}pt"
            )

        rate_lines.append(line + mark)

    if kind == "独立バフ":
        buff_lines = []

        for b in classification["buffs"]:
            extra = ""

            if b["edge_vs_one_final"] is not None:
                extra = (
                    f" / 1号艇最終 {b['one_final_1st']:.1f}%"
                    f" / 差 {b['edge_vs_one_final']:+.1f}pt"
                )

                if b["one_weak"]:
                    extra += " ←1号艇弱化"

            if b["base_known"]:
                value_text = (
                    f"元 {b['base_1st']:.1f}% "
                    f"+ 理論 {b['theory_1st']:+.1f}% "
                    f"+ チェッカー {b['checker_1st']:+.1f}% "
                    f"= 最終 {b['final_1st']:.1f}%"
                )
            else:
                value_text = (
                    f"元1着率表示なし / "
                    f"理論 {b['theory_1st']:+.1f}% "
                    f"+ チェッカー {b['checker_1st']:+.1f}% "
                    f"= 総合バフ {b['total_boost']:+.1f}pt"
                )

            buff_lines.append(
                f"{b['boat']}号艇 "
                f"平均との差 {b['current_diff']:+.2f} ({b['zone']})\n"
                f"{value_text}"
                f"{extra}"
            )

        body = (
            f"{symbol} シンsum独立バフ検知\n"
            f"{venue} {race}\n\n"
            + "\n\n".join(buff_lines)
            + "\n\n全艇・評価\n"
            + "\n".join(rate_lines)
            + "\n\n"
            + f"{classification['reason']}\n"
            + f"締切 {deadline_value}"
        )
    else:
        secondary_text = ""

        if secondary_buffs:
            secondary_lines = []

            for b in secondary_buffs:
                if b["final_1st"] is not None:
                    ending = f" / 最終 {b['final_1st']:.1f}%"
                else:
                    ending = " / 元1着率表示なし"

                secondary_lines.append(
                    f"{b['boat']}号艇 "
                    f"平均との差 {b['current_diff']:+.2f} ({b['zone']}) / "
                    f"理論 {b['theory_1st']:+.1f}% + "
                    f"チェッカー {b['checker_1st']:+.1f}% "
                    f"= 補正 {b['total_boost']:+.1f}pt"
                    f"{ending}"
                )

            secondary_text = (
                "\n\n🚀 2・3・4号艇のバフも確認\n"
                + "\n".join(secondary_lines)
            )

        body = (
            f"{symbol} {kind}\n"
            f"{venue} {race}【{alert}】\n\n"
            f"全艇・評価\n"
            + "\n".join(rate_lines)
            + secondary_text
            + "\n\n"
            + f"{classification['reason']}\n"
            + f"締切 {deadline_value}"
        )

    x = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={
            "Priority": "high",
            "Tags": "ship"
        },
        timeout=15
    )

    x.raise_for_status()

    register_result_tracking(
        venue,
        race,
        deadline_value,
        alert,
        final_rates,
        classification,
    )

    print(
        f"選別通知送信: "
        f"{kind} / "
        f"{alert or '通常レース'} / "
        f"{venue} / "
        f"{race} / "
        f"締切 {deadline_value}",
        flush=True
    )

def inspect(page):
    text = page.locator("body").inner_text(
        timeout=10000
    )

    v = actual_venue(text)
    r = actual_race(text)

    if not v or not r:
        return None

    if not within15(text):
        return None

    d = deadline(text)
    a = alert_near_deadline(text, d)

    # -----------------------------
    # 3種類のデータを取得
    # -----------------------------
    # DOM方式とテキスト方式を両方実行し、
    # より多く「明示された元1着率」を取れた方を採用する。
    #
    # 理由:
    # - DOM方式は画面構造に強いが、レイアウトによって1艇分だけ拾うことがある
    # - テキスト方式は別レースで6艇分を正しく取れることが確認できている
    # - 1〜2艇しか本当に表示されないレースでは、両方式ともその範囲に留まる
    base_dom = parse_base_1st_rates_dom(page)
    base_text = parse_base_1st_rates(text)

    # V21:
    # TEXT方式を優先する。
    # V20では DOM=6艇 / TEXT=6艇 の同数時にDOMを採用してしまい、
    # DOM側の入れ子要素重複（例: 34,34,25,25,22,22）を誤採用した。
    #
    # TEXT方式は
    #   34,25,22,11,2,4
    # のように正しい6艇を取得できているため、
    # TEXTが1艇以上取れている場合はTEXTを採用。
    # TEXTが0艇の時だけDOMへフォールバックする。
    if len(base_text) > 0:
        base_rates = base_text
        print(
            f"元1着率採用(V21): TEXT {len(base_text)}艇 "
            f"(DOM {len(base_dom)}艇は参考のみ)",
            flush=True
        )
    else:
        base_rates = base_dom
        print(
            f"元1着率採用(V21): TEXT 0艇のためDOM {len(base_dom)}艇を採用",
            flush=True
        )

    theory_adj = parse_theory_adjustments(text)
    current_diffs = parse_current_diffs(text)
    registrations = parse_registration_numbers(text)

    # シンsumチェッカーは登録番号をタップして初めて表示されるため、
    # 1〜6号艇を順番にクリックして取得する。
    checker = collect_checker_1st(
        page,
        current_diffs,
        registrations
    )

    if len(base_rates) < 6:
        missing_base = [
            b for b in range(1, 7)
            if b not in base_rates
        ]
        print(
            f"元1着率は明示分のみ使用: "
            f"{v} / {r} / "
            f"取得 {len(base_rates)}艇 {base_rates} / "
            f"元1着率表示なし {missing_base}号艇",
            flush=True
        )

    if len(theory_adj) < 6:
        print(
            f"シンsum理論1着補正6艇取得失敗: "
            f"{v} / {r} / "
            f"取得 {len(theory_adj)}艇 / {theory_adj}",
            flush=True
        )
        return None

    if len(current_diffs) < 6:
        print(
            f"平均との差6艇取得失敗: "
            f"{v} / {r} / "
            f"取得 {len(current_diffs)}艇 / {current_diffs}",
            flush=True
        )
        return None

    if len(registrations) < 6:
        print(
            f"登録番号6艇取得失敗: "
            f"{v} / {r} / "
            f"取得 {len(registrations)}艇 / {registrations}",
            flush=True
        )
        return None

    if len(checker) < 6:
        missing_checker = [
            b for b in range(1, 7)
            if b not in checker
        ]
        print(
            f"シンsumチェッカー取得済み艇で判定継続: "
            f"{v} / {r} / "
            f"取得 {len(checker)}艇 / "
            f"未取得・データなし {missing_checker}号艇",
            flush=True
        )

    final_rates = build_final_rates(
        base_rates,
        theory_adj,
        checker
    )

    if not final_rates:
        print(
            f"評価データ取得なし: {v} / {r}",
            flush=True
        )
        return None

    print(
        f"艇別評価: {v} / {r} / "
        + " | ".join(
            (
                f"{boat}号艇 "
                f"{final_rates[boat]['base']:.1f}"
                f"{final_rates[boat]['theory']:+.1f}"
                f"{final_rates[boat]['checker']:+.1f}"
                f"={final_rates[boat]['final']:.1f}%"
                if final_rates[boat]["base"] is not None
                else
                f"{boat}号艇 元不明 "
                f"補正{final_rates[boat]['theory']:+.1f}"
                f"{final_rates[boat]['checker']:+.1f}"
                f"={final_rates[boat]['total_adjustment']:+.1f}pt"
            )
            for boat in sorted(final_rates.keys())
        ),
        flush=True
    )

    # 全レース共通で、2〜4号艇の独立バフを先に計算する。
    # 元1着率が不明な艇でも「理論 + チェッカー」の総合バフで評価する。
    buff_classification = classify_buff(
        final_rates,
        current_diffs
    )

    # -----------------------------
    # NEW: 1号艇の逃げ強化 / 逃げ崩れ
    # -----------------------------
    escape = classify_escape_boost(final_rates)
    collapse = classify_escape_collapse(final_rates)

    # 逃げ崩れを優先。1号艇大幅低下＋15%以上の他艇があるケース。
    if collapse:
        return {
            "venue": v,
            "race": r,
            "deadline": d,
            "alert": a or "",
            "final_rates": final_rates,
            "classification": collapse,
            "key": (
                f"{now():%Y-%m-%d}|{v}|{r}|ESCAPE_COLLAPSE"
            )
        }

    # 1号艇が元より上昇し、最終75%以上。
    # かつ確認できる他艇に最終15%以上がいないケース。
    if escape:
        return {
            "venue": v,
            "race": r,
            "deadline": d,
            "alert": a or "",
            "final_rates": final_rates,
            "classification": escape,
            "key": (
                f"{now():%Y-%m-%d}|{v}|{r}|ESCAPE_BOOST"
            )
        }

    # -----------------------------
    # A. やや本命 / 荒れ注意
    #    → 最終補正1着率で主判定
    #    ＋ 2〜4号艇のバフを副注目として同時表示
    # -----------------------------
    if a:
        classification = classify_final_rates(
            final_rates
        )

        if classification:
            if buff_classification:
                classification["secondary_buffs"] = buff_classification["buffs"]

            buff_key = ""
            if buff_classification:
                buff_key = "|B" + "-".join(
                    str(x["boat"])
                    for x in buff_classification["buffs"]
                )

            return {
                "venue": v,
                "race": r,
                "deadline": d,
                "alert": a,
                "final_rates": final_rates,
                "classification": classification,
                "key": (
                    f"{now():%Y-%m-%d}|"
                    f"{v}|{r}|{a}|{classification['type']}"
                    f"{buff_key}"
                )
            }

        print(
            f"最終補正で主選別は対象外、独立バフを続けて確認: "
            f"{a} / {v} / {r}",
            flush=True
        )

    # -----------------------------
    # B. 全レース共通
    #    2〜4号艇の独立バフ
    # -----------------------------
    classification = buff_classification

    if not classification:
        return None

    print(
        f"独立バフ候補: {v} / {r} / "
        + ", ".join(
            (
                f"{x['boat']}号艇 "
                f"差{x['current_diff']:+.2f} "
                f"元{x['base_1st']:.1f}% "
                f"理論{x['theory_1st']:+.1f}% "
                f"チェッカー{x['checker_1st']:+.1f}% "
                f"最終{x['final_1st']:.1f}%"
                if x["base_known"]
                else
                f"{x['boat']}号艇 "
                f"差{x['current_diff']:+.2f} "
                f"元1着率表示なし "
                f"理論{x['theory_1st']:+.1f}% "
                f"チェッカー{x['checker_1st']:+.1f}% "
                f"総合バフ{x['total_boost']:+.1f}pt"
            )
            for x in classification["buffs"]
        ),
        flush=True
    )

    return {
        "venue": v,
        "race": r,
        "deadline": d,
        "alert": "",
        "final_rates": final_rates,
        "classification": classification,
        "key": (
            f"{now():%Y-%m-%d}|"
            f"{v}|{r}|BUFF|"
            + "-".join(
                str(x["boat"])
                for x in classification["buffs"]
            )
        )
    }


def cycle(page, initial=False):
    links = candidate_links(page)

    print(
        f"詳細候補リンク数: {len(links)}",
        flush=True
    )

    current = {}

    for u in links[:100]:
        try:
            page.goto(
                u,
                wait_until="domcontentloaded",
                timeout=20000
            )

            page.wait_for_timeout(350)

            item = inspect(page)

            if item:
                current[item["key"]] = item

        except Exception as e:
            print(
                "詳細ページ確認失敗:",
                u,
                repr(e),
                flush=True
            )

    # 起動直後でも、締切15分以内で現在条件を満たす候補は通知する。
    # その後 seen に登録するため、同一実行中の重複通知は防止される。
    if initial:
        print(
            f"初回候補通知対象: {len(current)}件",
            flush=True
        )

        for x in current.values():
            c = x["classification"]

            print(
                f"初回通知: "
                f"{c['type']} / "
                f"{x['alert'] or '通常レース'} / "
                f"{x['venue']} / "
                f"{x['race']} / "
                f"締切 {x['deadline']}",
                flush=True
            )

            notify_selected(
                x["alert"],
                x["venue"],
                x["race"],
                x["deadline"],
                x["final_rates"],
                x["classification"]
            )

        seen.update(current.keys())
        return

    new_items = [
        x
        for k, x in current.items()
        if k not in seen
    ]

    if not new_items:
        print(
            "新規選別判定なし",
            flush=True
        )

    for x in new_items:
        notify_selected(
            x["alert"],
            x["venue"],
            x["race"],
            x["deadline"],
            x["final_rates"],
            x["classification"]
        )

    seen.update(
        current.keys()
    )


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(
            headless=True
        )

        c = b.new_context(
            http_credentials={
                "username": USER,
                "password": PASSWORD
            }
        )

        page = c.new_page()

        _load_pending_results()
        print_result_stats()
        check_pending_results()

        if not active():
            print(
                "監視時間外（23:00〜08:00 JST）。終了します。",
                flush=True
            )
            return

        print(
            f"逃げ判定設定: "
            f"逃げ強化最終>={ESCAPE_FINAL_MIN:.1f}% / "
            f"対抗艇>={RIVAL_FINAL_MIN:.1f}% / "
            f"逃げ崩れ低下>={ONE_BIG_DROP_MIN:.1f}pt",
            flush=True
        )

        print(
            f"[{now():%Y-%m-%d %H:%M:%S}] "
            f"最終補正1着率 "
            f"(元1着率 + 理論補正 + チェッカー補正) "
            f"監視開始 [V25 constants-fix]",
            flush=True
        )

        cycle(
            page,
            initial=True
        )
        check_pending_results()

        while active():
            print(
                f"{CHECK_INTERVAL}秒後に再チェック",
                flush=True
            )

            time.sleep(
                CHECK_INTERVAL
            )

            print(
                f"[{now():%Y-%m-%d %H:%M:%S}] "
                f"再チェック",
                flush=True
            )

            cycle(
                page,
                initial=False
            )
            check_pending_results()


if __name__ == "__main__":
    main()
