import os
import re
import time
import statistics
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

# =========================================================
# 基本設定
# =========================================================
SHINSUM_URL = "https://boatrace-shinsum.com/"
BIYORI_URL = "https://kyoteibiyori.com/race_shusso.php"

USER = os.environ["SHINSUM_USER"]
PASSWORD = os.environ["SHINSUM_PASSWORD"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))
JST = ZoneInfo("Asia/Tokyo")

# 対象16場
VENUE_CODES = {
    "戸田": 2,
    "平和島": 4,
    "多摩川": 5,
    "蒲郡": 7,
    "三国": 10,
    "びわこ": 11,
    "住之江": 12,
    "鳴門": 14,
    "児島": 16,
    "宮島": 17,
    "徳山": 18,
    "下関": 19,
    "若松": 20,
    "芦屋": 21,
    "唐津": 23,
    "大村": 24,
}

# 今回チェックする艇
TARGET_BOATS = (2, 3, 4, 5)

# シンsumチェッカーの4ゾーン
BUCKETS = (
    "+0.5以上",
    "0〜+0.5",
    "-0.5〜0",
    "-0.5未満",
)

sent = set()


# =========================================================
# 時刻
# =========================================================
def now_jst():
    return datetime.now(JST)


def active_hours():
    return 8 <= now_jst().hour < 23


def race_date():
    return now_jst().strftime("%Y%m%d")


# =========================================================
# 締切
# =========================================================
def extract_deadline(text):
    m = re.search(r"締切\s*[：:]?\s*([01]?\d|2[0-3]):([0-5]\d)", text)
    if not m:
        return ""
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def within_10_minutes(text):
    d = extract_deadline(text)
    if not d:
        return False

    hh, mm = map(int, d.split(":"))
    target = now_jst().replace(
        hour=hh,
        minute=mm,
        second=0,
        microsecond=0,
    )
    delta = target - now_jst()

    # 締切1分後までは許容。10分より前は判定しない
    return timedelta(minutes=-1) <= delta <= timedelta(minutes=10)


# =========================================================
# シンsum レース識別
# =========================================================
def actual_venue(text):
    head = text[:1800]
    matches = [v for v in VENUE_CODES if v in head]
    return matches[0] if len(matches) == 1 else ""


def actual_race(text):
    m = re.search(r"(?<!\d)([1-9]|1[0-2])\s*R\b", text[:1800], re.I)
    return int(m.group(1)) if m else None


# =========================================================
# シンsum候補リンク
# =========================================================
def shinsum_links(page):
    page.goto(
        SHINSUM_URL,
        wait_until="domcontentloaded",
        timeout=30000,
    )
    page.wait_for_timeout(900)

    host = urlparse(SHINSUM_URL).netloc
    out = []
    anchors = page.locator("a")

    for i in range(anchors.count()):
        a = anchors.nth(i)

        try:
            href = a.get_attribute("href")

            if (
                not href
                or href.startswith("#")
                or href.startswith("javascript:")
            ):
                continue

            full = urljoin(SHINSUM_URL, href)

            if urlparse(full).netloc != host:
                continue

            nearby = ""

            try:
                nearby = a.inner_text(timeout=200) or ""
            except:
                pass

            try:
                nearby += "\n" + a.locator(
                    "xpath=ancestor::*[self::div or self::td or self::li or self::section][1]"
                ).inner_text(timeout=200)
            except:
                pass

            relevant = (
                any(v in nearby for v in VENUE_CODES)
                or re.search(r"([1-9]|1[0-2])\s*R", nearby)
                or any(
                    x in full.lower()
                    for x in ("race", "detail", "sum")
                )
            )

            if relevant:
                out.append(full)

        except:
            pass

    return list(dict.fromkeys(out))


# =========================================================
# シンsum理論
# 艇番 / 登録番号 / 平均との差 を取得
# =========================================================
def parse_theory_rows(text):
    start = text.find("シンsum理論")

    if start < 0:
        return {}

    end = text.find("シンsumチェッカー", start)
    section = text[
        start:(end if end > start else start + 5000)
    ]

    lines = [
        x.strip()
        for x in section.splitlines()
        if x.strip()
    ]

    out = {}

    for boat in TARGET_BOATS:
        for i, line in enumerate(lines):
            if line != str(boat):
                continue

            window = "\n".join(lines[i:i + 14])

            reg = re.search(r"\b(\d{4})\b", window)
            diff = re.search(
                r"(?<!\d)([+-]?\d+\.\d+)(?!\d)",
                window,
            )

            if reg and diff:
                out[boat] = {
                    "reg_no": reg.group(1),
                    "diff": float(diff.group(1)),
                }
                break

    return out


def bucket_for_diff(diff):
    if diff >= 0.5:
        return "+0.5以上"

    if diff >= 0:
        return "0〜+0.5"

    if diff >= -0.5:
        return "-0.5〜0"

    return "-0.5未満"


# =========================================================
# 登録番号を押してシンsumチェッカー表示
# =========================================================
def click_registration(page, reg_no):
    try:
        loc = page.get_by_text(reg_no, exact=True)

        if loc.count() == 0:
            return False

        loc.first.click(timeout=3000)
        page.wait_for_timeout(450)
        return True

    except:
        return False


# =========================================================
# シンsumチェッカー解析
# =========================================================
def parse_checker(text, boat):
    start = text.find("シンsumチェッカー")

    if start < 0:
        return None

    lines = [
        x.strip()
        for x in text[start:].splitlines()
        if x.strip()
    ]

    pos = next(
        (
            i
            for i, x in enumerate(lines)
            if f"{boat}号艇" in x
        ),
        None,
    )

    if pos is None:
        return None

    card = lines[pos:pos + 120]
    normalized = [
        x.replace(" ", "")
        for x in card
    ]

    joined = "\n".join(card)

    base = re.search(
        r"通算1着率\s*([0-9]+(?:\.[0-9]+)?)\s*%",
        joined,
    )

    if not base:
        return None

    rows = {}

    for bucket_name in BUCKETS:
        idx = next(
            (
                i
                for i, x in enumerate(normalized)
                if bucket_name in x
            ),
            None,
        )

        if idx is None:
            continue

        end_idx = len(card)

        for j in range(idx + 1, len(card)):
            if any(
                name in normalized[j]
                for name in BUCKETS
            ):
                end_idx = j
                break

        row_text = "\n".join(card[idx:end_idx])

        # 表の並び：
        # 1着率 / 2着率 / 3着率 / 3連対率
        pcts = re.findall(
            r"([+-]?\d+(?:\.\d+)?)\s*%",
            row_text,
        )

        # 件数
        nums = re.findall(
            r"(?<![\d.])(\d{1,4})(?![\d.%])",
            row_text,
        )

        count = int(nums[0]) if nums else 0

        if pcts:
            rows[bucket_name] = {
                "rise_1st": float(pcts[0]),
                "count": count,
            }

    return {
        "base_rate": float(base.group(1)),
        "rows": rows,
    }


# =========================================================
# 動的な「強上昇」判定
#
# 重要:
# 固定で全員+5%などではなく、
# その選手自身の他ゾーンと比較する。
#
# 2026-08-15 修正:
# 5〜9件は +10%以上なら拾えるよう緩和。
# 多摩川5R 2号艇
# +0.5以上 / +13.1% / 9件 のようなケースを拾う。
# =========================================================
def strong_dynamic(checker, current_bucket):
    current = checker["rows"].get(current_bucket)

    if not current:
        return False, "", 0

    rise = current["rise_1st"]
    count = current["count"]

    if rise <= 0:
        return False, "", count

    others = [
        row["rise_1st"]
        for bucket_name, row in checker["rows"].items()
        if bucket_name != current_bucket
    ]

    if not others:
        return False, "", count

    median_other = statistics.median(others)
    gap = rise - median_other

    # 件数別に信頼度を変える
    if count >= 25:
        strong = (
            rise >= 8
            and gap >= 6
        )

    elif count >= 10:
        strong = (
            rise >= 10
            and gap >= 8
        )

    elif count >= 5:
        # 5〜9件
        strong = (
            rise >= 10
            and gap >= 10
        )

    else:
        # 1〜4件はかなり厳しめ
        strong = (
            rise >= 20
            and gap >= 15
        )

    reason = (
        f"他ゾーン中央値 {median_other:+.1f}% / "
        f"差 {gap:+.1f}pt / "
        f"{count}件"
    )

    return strong, reason, count


# =========================================================
# 競艇日和 URL
# 日付は毎日自動更新
# =========================================================
def biyori_url(venue, race_no):
    return (
        f"{BIYORI_URL}"
        f"?place_no={VENUE_CODES[venue]}"
        f"&race_no={race_no}"
        f"&hiduke={race_date()}"
        f"&slider=0"
    )


# =========================================================
# 競艇日和 ST順位解析
# =========================================================
def parse_float_cell(value):
    value = (value or "").strip()
    m = re.search(r"\d+(?:\.\d+)?", value)
    return float(m.group()) if m else None


def detect_day_type(body_text):
    head = body_text[:2500]

    if "最終日" in head:
        return "final"

    if "初日" in head:
        return "first"

    return "middle"


def is_f_holder(body_text, boat):
    lines = [
        x.strip()
        for x in body_text.splitlines()
        if x.strip()
    ]

    for i, line in enumerate(lines):
        if (
            line == str(boat)
            or line.startswith(f"{boat}号艇")
        ):
            nearby = " ".join(
                lines[max(0, i - 4):i + 18]
            )

            return bool(
                re.search(r"\bF[1-9]\b", nearby)
            )

    return False


def parse_st_table(page):
    result = {}
    tables = page.locator("table")

    for table_index in range(tables.count()):
        table = tables.nth(table_index)

        try:
            table_text = table.inner_text(timeout=500)
        except:
            continue

        if "ST順位" not in table_text:
            continue

        rows = table.locator("tr")
        matrix = []

        for row_index in range(rows.count()):
            cells = rows.nth(row_index).locator("th,td")
            values = []

            for cell_index in range(cells.count()):
                try:
                    values.append(
                        (
                            cells.nth(cell_index)
                            .inner_text(timeout=200)
                            or ""
                        ).strip()
                    )
                except:
                    values.append("")

            if values:
                matrix.append(values)

        header_index = None

        for i, row in enumerate(matrix):
            joined = "|".join(row)

            if (
                "当地" in joined
                and (
                    "初日" in joined
                    or "最終日" in joined
                    or "F持" in joined
                )
            ):
                header_index = i
                break

        if header_index is None:
            continue

        headers = matrix[header_index]
        columns = {}

        for name in (
            "初日",
            "当地",
            "F持",
            "最終日",
        ):
            for i, header in enumerate(headers):
                if name in header:
                    columns[name] = i
                    break

        for row in matrix[header_index + 1:]:
            joined = " ".join(row)

            boat_match = re.search(
                r"(^|\s)([1-6])(?:号艇)?($|\s)",
                joined,
            )

            if boat_match:
                boat = int(boat_match.group(2))
            elif row and row[0].strip() in list("123456"):
                boat = int(row[0].strip())
            else:
                continue

            values = {}

            for name, idx in columns.items():
                if idx < len(row):
                    values[name] = parse_float_cell(
                        row[idx]
                    )

            if values:
                result[boat] = values

        if result:
            break

    return result


# =========================================================
# ST評価
#
# 初日:
#   初日 + 当地
#   F持ちなら F持も加味
#
# 途中日:
#   当地
#   F持ちなら F持も加味
#
# 最終日:
#   最終日 + 当地
#   F持ちなら F持も加味
# =========================================================
def composite_st(st_row, day_type, f_holder):
    values = []

    if day_type == "first":
        for key in ("初日", "当地"):
            if st_row.get(key) is not None:
                values.append(st_row[key])

    elif day_type == "final":
        for key in ("最終日", "当地"):
            if st_row.get(key) is not None:
                values.append(st_row[key])

    else:
        if st_row.get("当地") is not None:
            values.append(st_row["当地"])

    if (
        f_holder
        and st_row.get("F持") is not None
    ):
        values.append(st_row["F持"])

    if not values:
        return None

    return statistics.mean(values)


def st_assessment(page, boat):
    body = page.locator("body").inner_text(
        timeout=10000
    )

    day = detect_day_type(body)
    table = parse_st_table(page)

    if boat not in table:
        return None

    holder = is_f_holder(body, boat)

    score = composite_st(
        table[boat],
        day,
        holder,
    )

    if score is None:
        return None

    all_scores = {}

    for b, row in table.items():
        b_holder = is_f_holder(body, b)

        s = composite_st(
            row,
            day,
            b_holder,
        )

        if s is not None:
            all_scores[b] = s

    # ST順位は数字が小さいほど良い
    rank = 1 + sum(
        1
        for s in all_scores.values()
        if s < score
    )

    inner_score = (
        all_scores.get(boat - 1)
        if boat > 1
        else None
    )

    inner_advantage = (
        inner_score - score
        if inner_score is not None
        else None
    )

    return {
        "score": score,
        "rank": rank,
        "f_holder": holder,
        "day": day,
        "inner_score": inner_score,
        "inner_advantage": inner_advantage,
        "raw": table[boat],
    }


def chance_level(st):
    if st is None:
        return "CHECK", "ST取得できず"

    # ST総合がかなり良い
    if st["score"] <= 2.8:
        if st["inner_advantage"] is None:
            if st["rank"] <= 2:
                return (
                    "HIGH",
                    "ST総合が上位",
                )

        elif st["inner_advantage"] >= 0.15:
            return (
                "HIGH",
                f"内艇より "
                f"{st['inner_advantage']:.2f} 優勢",
            )

    # そこそこ良い
    if (
        st["score"] <= 3.5
        and st["rank"] <= 3
    ):
        return (
            "CHANCE",
            "ST総合が上位",
        )

    return (
        "WEAK",
        "ST優位性は弱め",
    )


# =========================================================
# ntfy 通知
# =========================================================
def send_notification(
    venue,
    race_label,
    boat,
    reg_no,
    diff,
    bucket_name,
    base_rate,
    rise,
    count,
    dynamic_reason,
    deadline_value,
    st,
    level,
    st_reason,
):
    if level == "HIGH":
        title = "🔥 高チャンス"

    elif level == "CHANCE":
        title = "🟡 チャンス"

    else:
        title = "📌 シンsum強上昇（ST要確認）"

    f_text = (
        "F持ち"
        if st and st["f_holder"]
        else "Fなし"
    )

    st_text = "取得不可"

    if st:
        st_text = (
            f"{st['score']:.2f}"
            f" / 6艇中{st['rank']}位"
        )

        if st["inner_advantage"] is not None:
            st_text += (
                f" / 内艇差 "
                f"{st['inner_advantage']:+.2f}"
            )

    body = (
        f"{title}\n"
        f"{venue} {race_label} / {boat}号艇\n"
        f"登録番号 {reg_no}\n"
        f"平均との差 {diff:+.2f}\n"
        f"該当ゾーン {bucket_name}\n"
        f"通算1着率 {base_rate:.1f}%\n"
        f"ゾーン1着率変化 "
        f"{rise:+.1f}%（{count}件）\n"
        f"ST評価 {st_text}\n"
        f"{f_text} / {st_reason}\n"
        f"{dynamic_reason}\n"
        f"締切 {deadline_value}"
    )

    response = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={
            "Priority": "high",
            "Tags": "fire",
        },
        timeout=15,
    )

    response.raise_for_status()

    print(
        f"通知送信: "
        f"{title} / "
        f"{venue} {race_label} / "
        f"{boat}号艇 / "
        f"{rise:+.1f}%",
        flush=True,
    )


# =========================================================
# 1レース判定
# =========================================================
def inspect_race(page):
    text = page.locator("body").inner_text(
        timeout=10000
    )

    venue = actual_venue(text)
    race_no = actual_race(text)
    deadline_value = extract_deadline(text)

    if (
        not venue
        or not race_no
        or not deadline_value
        or not within_10_minutes(text)
    ):
        return []

    rows = parse_theory_rows(text)

    if not rows:
        return []

    picks = []

    for boat, info in rows.items():
        reg_no = info["reg_no"]
        diff = info["diff"]
        current_bucket = bucket_for_diff(diff)

        if not click_registration(page, reg_no):
            continue

        checker = parse_checker(
            page.locator("body").inner_text(
                timeout=10000
            ),
            boat,
        )

        if not checker:
            continue

        strong, reason, count = strong_dynamic(
            checker,
            current_bucket,
        )

        current = checker["rows"].get(
            current_bucket
        )

        if not strong or not current:
            continue

        # 競艇日和 ST順位
        st = None
        st_page = page.context.new_page()

        try:
            st_page.goto(
                biyori_url(
                    venue,
                    race_no,
                ),
                wait_until="domcontentloaded",
                timeout=20000,
            )

            st_page.wait_for_timeout(600)

            st = st_assessment(
                st_page,
                boat,
            )

        except Exception as e:
            print(
                "ST取得失敗:",
                venue,
                race_no,
                boat,
                repr(e),
                flush=True,
            )

        finally:
            st_page.close()

        level, st_reason = chance_level(st)

        picks.append({
            "venue": venue,
            "race_label": f"{race_no}R",
            "boat": boat,
            "reg_no": reg_no,
            "diff": diff,
            "bucket_name": current_bucket,
            "base_rate": checker["base_rate"],
            "rise": current["rise_1st"],
            "count": count,
            "dynamic_reason": reason,
            "deadline_value": deadline_value,
            "st": st,
            "level": level,
            "st_reason": st_reason,
        })

    return picks


# =========================================================
# 1巡回
# =========================================================
def one_cycle(page):
    links = shinsum_links(page)

    print(
        f"詳細候補リンク数: {len(links)}",
        flush=True,
    )

    hits = 0

    for url in links[:100]:
        try:
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=20000,
            )

            page.wait_for_timeout(300)

            picks = inspect_race(page)

            for pick in picks:
                hits += 1

                key = (
                    f"{now_jst():%Y-%m-%d}|"
                    f"{pick['venue']}|"
                    f"{pick['race_label']}|"
                    f"{pick['boat']}|"
                    f"{pick['bucket_name']}|"
                    f"{pick['level']}"
                )

                if key in sent:
                    continue

                send_notification(**pick)
                sent.add(key)

        except Exception as e:
            print(
                "レース確認失敗:",
                url,
                repr(e),
                flush=True,
            )

    print(
        f"今回のシンsum強上昇候補: {hits}件",
        flush=True,
    )


# =========================================================
# main
# =========================================================
def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True
        )

        context = browser.new_context(
            http_credentials={
                "username": USER,
                "password": PASSWORD,
            }
        )

        page = context.new_page()

        if not active_hours():
            print(
                "監視時間外（23:00〜08:00 JST）",
                flush=True,
            )
            return

        print(
            f"[{now_jst():%Y-%m-%d %H:%M:%S}] "
            f"シンsum×ST総合監視開始",
            flush=True,
        )

        while active_hours():
            one_cycle(page)

            print(
                f"{CHECK_INTERVAL}秒後に再チェック",
                flush=True,
            )

            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
