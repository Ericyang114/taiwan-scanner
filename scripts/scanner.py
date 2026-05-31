"""
台股動能掃描器 - Python 版
每天 13:30 收盤後由 GitHub Actions 執行
掃描 A方案：突破必須 + score >= 3（Stage2/TT/VCP 至少兩項）
結果推播到 Telegram
"""

import os
import sys
import io
import time
import requests
import numpy as np
from datetime import datetime, timezone, timedelta

# Windows 終端機 UTF-8 輸出
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── 設定 ──────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
CAPITAL          = 100   # 萬元，用於計算建議部位
SHARES           = 1000  # 一張 = 1000 股
MAX_PRICE        = 9999  # 股價上限（不設限）

# 精選 41 支熱門股
STOCKS = [
    # 半導體 / IC 設計
    {"code": "2330", "name": "台積電",     "ex": "TW",  "sector": "半導體業"},
    {"code": "2303", "name": "聯電",       "ex": "TW",  "sector": "半導體業"},
    {"code": "6488", "name": "環球晶",     "ex": "TWO", "sector": "半導體業"},
    {"code": "3105", "name": "穩懋",       "ex": "TW",  "sector": "半導體業"},
    {"code": "2454", "name": "聯發科",     "ex": "TW",  "sector": "IC設計"},
    {"code": "3034", "name": "聯詠",       "ex": "TW",  "sector": "IC設計"},
    {"code": "2379", "name": "瑞昱",       "ex": "TW",  "sector": "IC設計"},
    {"code": "3443", "name": "創意",       "ex": "TW",  "sector": "IC設計"},
    {"code": "3661", "name": "世芯-KY",    "ex": "TW",  "sector": "IC設計"},
    {"code": "5274", "name": "信驊",       "ex": "TWO", "sector": "IC設計"},
    {"code": "5269", "name": "祥碩",       "ex": "TWO", "sector": "IC設計"},
    {"code": "4966", "name": "譜瑞-KY",    "ex": "TWO", "sector": "IC設計"},
    {"code": "2344", "name": "華邦電",     "ex": "TW",  "sector": "記憶體"},
    # AI / 伺服器
    {"code": "6669", "name": "緯穎",       "ex": "TW",  "sector": "AI伺服器"},
    {"code": "2382", "name": "廣達",       "ex": "TW",  "sector": "AI伺服器"},
    {"code": "3231", "name": "緯創",       "ex": "TW",  "sector": "伺服器"},
    {"code": "2317", "name": "鴻海",       "ex": "TW",  "sector": "EMS"},
    {"code": "2308", "name": "台達電",     "ex": "TW",  "sector": "電源"},
    {"code": "3533", "name": "嘉澤",       "ex": "TW",  "sector": "連接器"},
    # 封測 / PCB / 被動元件
    {"code": "3711", "name": "日月光投控", "ex": "TW",  "sector": "封測"},
    {"code": "3037", "name": "欣興",       "ex": "TW",  "sector": "PCB"},
    {"code": "8046", "name": "南電",       "ex": "TWO", "sector": "PCB"},
    {"code": "6278", "name": "台表科",     "ex": "TW",  "sector": "PCB"},
    {"code": "2327", "name": "國巨",       "ex": "TW",  "sector": "被動元件"},
    # 科技硬體
    {"code": "3008", "name": "大立光",     "ex": "TW",  "sector": "光學"},
    {"code": "2357", "name": "華碩",       "ex": "TW",  "sector": "電腦"},
    {"code": "2376", "name": "技嘉",       "ex": "TW",  "sector": "主機板"},
    {"code": "2395", "name": "研華",       "ex": "TW",  "sector": "工控"},
    {"code": "2360", "name": "致茂",       "ex": "TW",  "sector": "量測"},
    {"code": "3714", "name": "富采",       "ex": "TWO", "sector": "光電"},
    # 航運
    {"code": "2603", "name": "長榮",       "ex": "TW",  "sector": "航運"},
    {"code": "2618", "name": "長榮航",     "ex": "TW",  "sector": "航空"},
    # 金融
    {"code": "2881", "name": "富邦金",     "ex": "TW",  "sector": "金控"},
    {"code": "2882", "name": "國泰金",     "ex": "TW",  "sector": "金控"},
    {"code": "2886", "name": "兆豐金",     "ex": "TW",  "sector": "金控"},
    # 傳產 / 電信
    {"code": "1301", "name": "台塑",       "ex": "TW",  "sector": "石化"},
    {"code": "2002", "name": "中鋼",       "ex": "TW",  "sector": "鋼鐵"},
    {"code": "2412", "name": "中華電",     "ex": "TW",  "sector": "電信"},
    {"code": "6505", "name": "台塑化",     "ex": "TW",  "sector": "石化"},
    {"code": "6409", "name": "旭隼",       "ex": "TWO", "sector": "網通"},
    {"code": "2337", "name": "旺宏",       "ex": "TW",  "sector": "記憶體"},
]

# ── 數學工具 ──────────────────────────────────────────
def ema(arr, period):
    result = [None] * len(arr)
    k = 2 / (period + 1)
    clean = [(i, v) for i, v in enumerate(arr) if v is not None and not np.isnan(v)]
    if len(clean) < period:
        return result
    seed_idx = clean[period - 1][0]
    prev = sum(v for _, v in clean[:period]) / period
    result[seed_idx] = prev
    for i in range(seed_idx + 1, len(arr)):
        v = arr[i]
        if v is None or np.isnan(v):
            result[i] = prev
        else:
            prev = v * k + prev * (1 - k)
            result[i] = prev
    return result

def sma(arr, period):
    result = [None] * len(arr)
    for i in range(period - 1, len(arr)):
        sl = [v for v in arr[i - period + 1:i + 1] if v is not None and not np.isnan(v)]
        if len(sl) >= int(period * 0.8):
            result[i] = sum(sl) / len(sl)
    return result

def wmax(arr, n, end):
    sl = [v for v in arr[max(0, end - n + 1):end + 1] if v is not None]
    return max(sl) if sl else None

def wmin(arr, n, end):
    sl = [v for v in arr[max(0, end - n + 1):end + 1] if v is not None]
    return min(sl) if sl else None

def wmean(arr, n, end):
    sl = [v for v in arr[max(0, end - n + 1):end + 1] if v is not None and not np.isnan(v)]
    return sum(sl) / len(sl) if sl else None

def get(arr, i):
    i = max(0, i)
    return arr[i] if i < len(arr) and arr[i] is not None else None

def true_range(highs, lows, closes):
    tr = []
    for i in range(len(closes)):
        h, l, c = highs[i], lows[i], closes[i]
        if h is None or l is None:
            tr.append(None)
            continue
        if i == 0 or closes[i - 1] is None:
            tr.append(h - l)
        else:
            pc = closes[i - 1]
            tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    return tr

# ── 條件計算 ──────────────────────────────────────────
def calc_conditions(close, high, low, volume):
    n = len(close)
    if n < 252:
        return None

    last = n - 1
    while last > 0 and (close[last] is None or np.isnan(close[last])):
        last -= 1
    if last < 251:
        return None

    c = close[last]
    v = volume[last]
    if not c or not v:
        return None

    ma50  = ema(close, 50)
    ma150 = ema(close, 150)
    ma200 = ema(close, 200)
    ma30w = sma(close, 150)
    vol50 = sma(volume, 50)

    if not all([get(ma50, last), get(ma150, last), get(ma200, last), get(ma30w, last)]):
        return None

    h52 = wmax(high,  252, last)
    l52 = wmin(low,   252, last)

    # Stage 2
    stage2 = (c > get(ma30w, last)) and (get(ma30w, last) > get(ma30w, last - 4))

    # Minervini TT
    m1 = c > get(ma150, last) and c > get(ma200, last)
    m2 = get(ma150, last) > get(ma200, last)
    m3 = get(ma200, last) > get(ma200, last - 20) if last >= 20 else False
    m4 = get(ma50, last) > get(ma150, last) and get(ma50, last) > get(ma200, last)
    m5 = c > get(ma50, last)
    m6 = c >= h52 * 0.75 if h52 else False
    m7 = c >= l52 * 1.25 if l52 else False
    tt_score = sum([m1, m2, m3, m4, m5, m6, m7])
    tt = tt_score == 7

    # VCP
    atr_arr  = sma(true_range(high, low, close), 14)
    atr_now  = wmean(atr_arr, 10, last)
    atr_prev = wmean(atr_arr, 10, last - 10)
    vol_now  = wmean(volume, 10, last)
    vol_prev = wmean(volume, 10, last - 10)
    vcp = bool(
        atr_now and atr_prev and vol_now and vol_prev and h52 and
        atr_now  < atr_prev * 0.75 and
        vol_now  < vol_prev * 0.80 and
        c >= h52 * 0.70
    )

    # 突破（A方案：量 >= 1.2x）
    pivot   = wmax(high, 20, last - 1)
    avg_vol = get(vol50, last)
    brk = bool(pivot and avg_vol and c > pivot and v > avg_vol * 1.2 and c <= pivot * 1.05)

    score = sum([stage2, tt, vcp, brk])
    sl    = wmin(low, 20, last)
    risk_pct = (c - sl) / c * 100 if sl else None

    return {
        "price": c, "stage2": stage2, "tt": tt, "tt_score": tt_score,
        "vcp": vcp, "brk": brk, "pivot": pivot, "score": score,
        "risk_pct": risk_pct, "stop_loss": sl,
    }

# ── 資料抓取 ──────────────────────────────────────────
def fetch_ohlcv(stock, retries=3):
    suffix = ".TWO" if stock["ex"] == "TWO" else ".TW"
    sym    = stock["code"] + suffix
    url    = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=2y"
    headers = {"User-Agent": "Mozilla/5.0"}

    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code != 200:
                time.sleep(2)
                continue
            data = r.json()
            result = data["chart"]["result"][0]
            q = result["indicators"]["quote"][0]

            def fix(arr):
                return [v if v is not None and not np.isnan(v) else None for v in (arr or [])]

            return {
                "close":  fix(q.get("close", [])),
                "high":   fix(q.get("high", [])),
                "low":    fix(q.get("low", [])),
                "volume": fix(q.get("volume", [])),
            }
        except Exception as e:
            print(f"  [{stock['code']}] attempt {attempt+1} failed: {e}")
            time.sleep(3)
    return None

# ── Telegram 發送 ─────────────────────────────────────
def send_telegram(text):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
    }
    r = requests.post(url, data=data, timeout=10)
    if not r.ok:
        print(f"Telegram error: {r.text}")

# ── 訊息格式化 ────────────────────────────────────────
def fmt_price(p):
    if p is None:
        return "—"
    return f"{p:,.1f}"

def fmt_money(n):
    if abs(n) >= 1e8:
        return f"{n/1e8:.2f}億"
    if abs(n) >= 1e4:
        return f"{n/1e4:.1f}萬"
    return f"{int(n):,}"

def build_message(entries, watches, scan_time):
    tz_tw = timezone(timedelta(hours=8))
    dt    = datetime.now(tz_tw)
    date_str = dt.strftime("%Y/%m/%d")
    time_str = scan_time

    lines = [f"📊 <b>台股動能掃描 {date_str}</b>"]
    lines.append(f"⏰ {time_str} 更新\n")

    # 今日進場
    lines.append(f"🟢 <b>今日進場 {len(entries)} 支</b>")
    if entries:
        lines.append("━━━━━━━━━━━━━━━━━━━")
        for s in entries:
            risk    = s["cond"]["risk_pct"]
            sl      = s["cond"]["stop_loss"]
            pivot   = s["cond"]["pivot"]
            price   = s["cond"]["price"]
            target  = price + 2 * (price - sl) if sl else None
            pos_wan = (CAPITAL * 10000 * 0.01 / (risk / 100)) / 10000 if risk else None

            lines.append(f"▶ <b>{s['code']} {s['name']}</b>（{s['sector']}）")
            lines.append(f"   進場 NT${fmt_price(price)} | 停損 NT${fmt_price(sl)} | 目標 NT${fmt_price(target)}")
            if risk:
                lines.append(f"   風險 {risk:.1f}% | 成本/張 {fmt_money(price * SHARES)}")
            if pos_wan:
                lines.append(f"   建議部位 {pos_wan:.1f}萬（{CAPITAL}萬資金×1%規則）")
            conds = []
            if s["cond"]["stage2"]: conds.append("Stage2✓")
            if s["cond"]["tt"]:     conds.append("TT7/7✓")
            if s["cond"]["vcp"]:    conds.append("VCP✓")
            lines.append(f"   條件：{'  '.join(conds)}")
            lines.append("")
    else:
        lines.append("   今日無進場訊號\n")

    # 觀察名單
    lines.append(f"🟡 <b>觀察名單 {len(watches)} 支</b>（等突破）")
    if watches:
        lines.append("━━━━━━━━━━━━━━━━━━━")
        for s in watches:
            pivot = s["cond"]["pivot"]
            miss  = []
            if not s["cond"]["stage2"]: miss.append("Stage2")
            if not s["cond"]["tt"]:     miss.append("TT")
            if not s["cond"]["vcp"]:    miss.append("VCP")
            miss_str = "、".join(miss) if miss else "突破"
            lines.append(
                f"▷ <b>{s['code']} {s['name']}</b>（{s['sector']}）"
                f"  缺{miss_str}  突破點 NT${fmt_price(pivot)}"
            )
    else:
        lines.append("   無觀察標的")

    return "\n".join(lines)

# ── 主程式 ────────────────────────────────────────────
def main():
    tz_tw    = timezone(timedelta(hours=8))
    now_tw   = datetime.now(tz_tw)
    scan_time = now_tw.strftime("%H:%M")

    print(f"=== 台股動能掃描 {now_tw.strftime('%Y/%m/%d %H:%M')} ===")
    print(f"掃描 {len(STOCKS)} 支股票...")

    entries = []
    watches = []
    errors  = 0

    for i, stock in enumerate(STOCKS, 1):
        print(f"[{i:02d}/{len(STOCKS)}] {stock['code']} {stock['name']}...")
        data = fetch_ohlcv(stock)
        if not data:
            print(f"  → 無法取得資料")
            errors += 1
            time.sleep(1)
            continue

        cond = calc_conditions(
            data["close"], data["high"], data["low"], data["volume"]
        )
        if not cond:
            print(f"  → 資料不足")
            errors += 1
            time.sleep(0.5)
            continue

        price = cond["price"]
        if price > MAX_PRICE:
            print(f"  → 股價 {price:.0f} 超過上限，略過")
            time.sleep(0.5)
            continue

        entry = {**stock, "cond": cond}

        # A方案：突破必須 + score >= 3
        if cond["brk"] and cond["score"] >= 3:
            entries.append(entry)
            print(f"  → ★ 今日進場！score={cond['score']} price={price:.1f}")
        elif not cond["brk"] and cond["score"] >= 3:
            watches.append(entry)
            print(f"  → 觀察名單 score={cond['score']} pivot={cond['pivot']:.1f}")
        else:
            print(f"  → score={cond['score']} brk={cond['brk']}")

        time.sleep(0.8)  # 避免 Yahoo Finance rate limit

    print(f"\n結果：進場 {len(entries)} 支，觀察 {len(watches)} 支，失敗 {errors} 支")

    # 發送 Telegram
    msg = build_message(entries, watches, scan_time)
    send_telegram(msg)
    print("\n=== Telegram 訊息 ===")
    print(msg)
    print("=== 完成！===")

if __name__ == "__main__":
    main()
