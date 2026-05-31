"""
台股動能掃描器 - Python 版
每天 13:35 收盤後由 GitHub Actions 執行
自動從 TWSE/TPEX 官方 API 抓取全部上市上櫃普通股（約 1083 支）
A方案：突破必須 + score >= 3
結果推播到 Telegram
"""

import os, sys, io, time, re, json, requests, numpy as np
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# ── 設定 ──────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
CAPITAL          = 100    # 萬元（建議部位計算用）
SHARES           = 1000   # 一張 = 1000 股
MAX_WORKERS      = 8      # 並行執行緒數
WATCH_LIMIT      = 20     # 觀察名單最多顯示幾支
TG_MAX_LEN       = 4000   # Telegram 單則訊息字元上限

SECTOR_MAP = {
    '01':'水泥工業','02':'食品工業','03':'塑膠工業','04':'紡織纖維',
    '05':'電機機械','06':'電器電纜','08':'化學工業','09':'生技醫療',
    '10':'玻璃陶瓷','11':'造紙工業','12':'鋼鐵工業','13':'橡膠工業',
    '14':'汽車工業','15':'電子工業','16':'航運業', '17':'觀光餐旅',
    '18':'金融保險','19':'貿易百貨','20':'其他',   '21':'油電燃氣',
    '22':'存託憑證','24':'半導體業','25':'電腦週邊','26':'光電業',
    '27':'通信網路','28':'電子零組件','29':'電子通路','30':'資訊服務',
    '31':'其他電子','32':'建材營造','33':'運動休閒','35':'居家生活',
    '36':'生技',   '37':'文創',   '38':'農業科技','91':'DR憑證',
}

print_lock = threading.Lock()

def safe_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)

# ── 從 TWSE/TPEX 取得完整股票清單 ──────────────────────
def fetch_stock_list():
    sources = [
        ("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", "TW"),
        ("https://openapi.twse.com.tw/v1/opendata/t187ap03_T", "TWO"),
    ]
    stocks = []

    def fix_mojibake(s):
        """修正 PowerShell/requests 可能的 latin-1 錯誤解碼"""
        try:
            return s.encode('latin-1').decode('utf-8')
        except Exception:
            return s

    for url, ex in sources:
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            data = json.loads(r.content.decode('utf-8'))
            for item in data:
                # 用欄位名稱取值（TWSE API 回傳標準 UTF-8 JSON）
                code   = item.get('公司代號', '').strip()
                name   = item.get('公司簡稱', code).strip()
                sec_id = item.get('產業別', '').strip()
                sector = SECTOR_MAP.get(sec_id, sec_id or '—')
                # 只保留 4 位數字普通股，排除 ETF（開頭 00）
                if re.match(r'^\d{4}$', code) and not code.startswith('00'):
                    stocks.append({"code": code, "name": name, "ex": ex, "sector": sector})
        except Exception as e:
            safe_print(f"[警告] 無法取得 {ex} 清單：{e}")

    if not stocks:
        safe_print("[警告] TWSE API 失敗，使用備用清單")
        # 最小備用清單
        stocks = [
            {"code":"2330","name":"台積電","ex":"TW","sector":"半導體業"},
            {"code":"2454","name":"聯發科","ex":"TW","sector":"IC設計"},
            {"code":"2317","name":"鴻海",  "ex":"TW","sector":"EMS"},
            {"code":"2882","name":"國泰金","ex":"TW","sector":"金控"},
            {"code":"2603","name":"長榮",  "ex":"TW","sector":"航運"},
        ]
    return stocks

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
    sl = [v for v in arr[max(0, end-n+1):end+1] if v is not None]
    return max(sl) if sl else None

def wmin(arr, n, end):
    sl = [v for v in arr[max(0, end-n+1):end+1] if v is not None]
    return min(sl) if sl else None

def wmean(arr, n, end):
    sl = [v for v in arr[max(0, end-n+1):end+1] if v is not None and not np.isnan(v)]
    return sum(sl) / len(sl) if sl else None

def get(arr, i):
    i = max(0, i)
    return arr[i] if i < len(arr) and arr[i] is not None else None

def true_range(highs, lows, closes):
    tr = []
    for i in range(len(closes)):
        h, l = highs[i], lows[i]
        if h is None or l is None:
            tr.append(None); continue
        if i == 0 or closes[i-1] is None:
            tr.append(h - l)
        else:
            pc = closes[i-1]
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
    c, v = close[last], volume[last]
    if not c or not v:
        return None

    ma50  = ema(close, 50)
    ma150 = ema(close, 150)
    ma200 = ema(close, 200)
    ma30w = sma(close, 150)
    vol50 = sma(volume, 50)

    if not all([get(ma50,last), get(ma150,last), get(ma200,last), get(ma30w,last)]):
        return None

    h52 = wmax(high, 252, last)
    l52 = wmin(low,  252, last)

    stage2 = c > get(ma30w, last) and get(ma30w, last) > get(ma30w, last-4)
    m1 = c > get(ma150,last) and c > get(ma200,last)
    m2 = get(ma150,last) > get(ma200,last)
    m3 = get(ma200,last) > get(ma200,last-20) if last >= 20 else False
    m4 = get(ma50,last) > get(ma150,last) and get(ma50,last) > get(ma200,last)
    m5 = c > get(ma50,last)
    m6 = c >= h52 * 0.75 if h52 else False
    m7 = c >= l52 * 1.25 if l52 else False
    tt_score = sum([m1,m2,m3,m4,m5,m6,m7])
    tt = tt_score == 7

    atr_arr  = sma(true_range(high, low, close), 14)
    atr_now  = wmean(atr_arr, 10, last)
    atr_prev = wmean(atr_arr, 10, last-10)
    vol_now  = wmean(volume, 10, last)
    vol_prev = wmean(volume, 10, last-10)
    vcp = bool(
        atr_now and atr_prev and vol_now and vol_prev and h52
        and atr_now < atr_prev * 0.75
        and vol_now < vol_prev * 0.80
        and c >= h52 * 0.70
    )

    pivot   = wmax(high, 20, last-1)
    avg_vol = get(vol50, last)
    brk = bool(pivot and avg_vol and c > pivot and v > avg_vol * 1.2 and c <= pivot * 1.05)

    score    = sum([stage2, tt, vcp, brk])
    sl       = wmin(low, 20, last)
    risk_pct = (c - sl) / c * 100 if sl else None

    return {
        "price": c, "stage2": stage2, "tt": tt, "tt_score": tt_score,
        "vcp": vcp, "brk": brk, "pivot": pivot, "score": score,
        "risk_pct": risk_pct, "stop_loss": sl,
    }

# ── 資料抓取 ──────────────────────────────────────────
def fetch_ohlcv(stock, retries=2):
    suffix = ".TWO" if stock["ex"] == "TWO" else ".TW"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock['code']}{suffix}?interval=1d&range=2y"
    for attempt in range(retries):
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
            if r.status_code != 200:
                time.sleep(1); continue
            result = r.json()["chart"]["result"][0]
            q = result["indicators"]["quote"][0]
            fix = lambda arr: [v if v is not None and not np.isnan(v) else None for v in (arr or [])]
            return {
                "close":  fix(q.get("close",  [])),
                "high":   fix(q.get("high",   [])),
                "low":    fix(q.get("low",    [])),
                "volume": fix(q.get("volume", [])),
            }
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
    return None

# ── 掃描單支股票（供多執行緒使用）─────────────────────
def scan_one(stock):
    data = fetch_ohlcv(stock)
    if not data:
        return stock, None, "無資料"
    cond = calc_conditions(data["close"], data["high"], data["low"], data["volume"])
    if not cond:
        return stock, None, "資料不足"
    return stock, cond, "ok"

# ── Telegram 發送 ─────────────────────────────────────
def send_telegram(text):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, data=data, timeout=10)
        if not r.ok:
            safe_print(f"[Telegram 錯誤] {r.text}")
    except Exception as e:
        safe_print(f"[Telegram 例外] {e}")

def send_long_message(text):
    """自動分割超過 TG_MAX_LEN 的訊息"""
    if len(text) <= TG_MAX_LEN:
        send_telegram(text)
        return
    lines  = text.split("\n")
    chunk  = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > TG_MAX_LEN:
            send_telegram(chunk)
            time.sleep(0.5)
            chunk = line + "\n"
        else:
            chunk += line + "\n"
    if chunk.strip():
        send_telegram(chunk)

# ── 訊息格式化 ────────────────────────────────────────
def fmt_price(p):
    return f"{p:,.1f}" if p else "—"

def fmt_money(n):
    if abs(n) >= 1e8: return f"{n/1e8:.2f}億"
    if abs(n) >= 1e4: return f"{n/1e4:.1f}萬"
    return f"{int(n):,}"

def build_message(entries, watches, total, errors, scan_time, date_str):
    lines = [
        f"📊 <b>台股動能掃描 {date_str}</b>",
        f"⏰ {scan_time}  掃描 {total} 支 · 失敗 {errors} 支\n",
    ]

    lines.append(f"🟢 <b>今日進場 {len(entries)} 支</b>（A方案：突破＋score≥3）")
    if entries:
        lines.append("━━━━━━━━━━━━━━━━━━━")
        for s in entries:
            c   = s["cond"]
            sl  = c["stop_loss"]
            p   = c["price"]
            tgt = p + 2 * (p - sl) if sl else None
            risk = c["risk_pct"]
            pos  = (CAPITAL * 10000 * 0.01 / (risk / 100)) / 10000 if risk else None
            cond_tags = " ".join(
                t for t, ok in [("Stage2",c["stage2"]),("TT",c["tt"]),("VCP",c["vcp"])] if ok
            )
            lines.append(f"\n▶ <b>{s['code']} {s['name']}</b>（{s['sector']}）")
            lines.append(f"   進場 NT${fmt_price(p)} ｜ 停損 NT${fmt_price(sl)} ｜ 目標 NT${fmt_price(tgt)}")
            if risk:
                lines.append(f"   風險 {risk:.1f}% ｜ 成本/張 {fmt_money(p * SHARES)}")
            if pos:
                lines.append(f"   建議部位 {pos:.1f}萬（{CAPITAL}萬×1%規則）")
            if cond_tags:
                lines.append(f"   達成：{cond_tags}")
    else:
        lines.append("   今日無進場訊號\n")

    watch_show = watches[:WATCH_LIMIT]
    extra = len(watches) - len(watch_show)
    lines.append(f"\n🟡 <b>觀察名單 {len(watches)} 支</b>（等突破）")
    if watch_show:
        lines.append("━━━━━━━━━━━━━━━━━━━")
        for s in watch_show:
            c = s["cond"]
            miss = [t for t, ok in [("Stage2",c["stage2"]),("TT",c["tt"]),("VCP",c["vcp"])] if not ok]
            miss_str = "、".join(miss) if miss else "突破"
            lines.append(
                f"▷ <b>{s['code']} {s['name']}</b>（{s['sector']}）"
                f"  缺{miss_str}  突破點 NT${fmt_price(c['pivot'])}"
            )
        if extra:
            lines.append(f"   ⋯ 另有 {extra} 支，詳見網頁版")
    else:
        lines.append("   無觀察標的")

    return "\n".join(lines)

# ── 主程式 ────────────────────────────────────────────
def main():
    tz_tw     = timezone(timedelta(hours=8))
    now_tw    = datetime.now(tz_tw)
    scan_time = now_tw.strftime("%H:%M")
    date_str  = now_tw.strftime("%Y/%m/%d")

    safe_print(f"=== 台股動能掃描 {date_str} {scan_time} ===")

    # 取得股票清單
    safe_print("取得 TWSE/TPEX 股票清單...")
    stocks = fetch_stock_list()
    safe_print(f"股票清單：{len(stocks)} 支")

    entries, watches = [], []
    errors = 0
    done   = 0
    total  = len(stocks)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(scan_one, s): s for s in stocks}
        for future in as_completed(futures):
            stock, cond, status = future.result()
            done += 1
            if done % 50 == 0 or done == total:
                safe_print(f"  進度 {done}/{total}  進場 {len(entries)} 觀察 {len(watches)}")

            if status != "ok" or cond is None:
                errors += 1
                continue

            entry = {**stock, "cond": cond}
            if cond["brk"] and cond["score"] >= 3:
                entries.append(entry)
                safe_print(f"  ★ 進場！{stock['code']} {stock['name']} score={cond['score']}")
            elif not cond["brk"] and cond["score"] >= 3:
                watches.append(entry)

    # 依 score 降冪排列
    entries.sort(key=lambda x: x["cond"]["score"], reverse=True)
    watches.sort(key=lambda x: x["cond"]["score"], reverse=True)

    safe_print(f"\n結果：進場 {len(entries)} 支，觀察 {len(watches)} 支，失敗 {errors} 支")

    msg = build_message(entries, watches, total, errors, scan_time, date_str)
    safe_print("\n=== 發送 Telegram ===")
    safe_print(msg)
    send_long_message(msg)
    safe_print("=== 完成 ===")

if __name__ == "__main__":
    # UTF-8 stdout（Windows 環境）
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, 'buffer'):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    main()
