"""
台股動能掃描器 v3 - Python 版（TG 推播）
每天 13:35 收盤後執行，與 v3.html 使用相同邏輯：
- 大盤狀態（TWII Stage 分析）
- 相對強弱 RS vs 大盤
- 近期突破追溯（1-5天前）
- 突破日低點停損
- 量能分級 ⭐⭐/⭐
- TT 6+ 觀察名單
"""

import os, sys, io, time, re, random, json, requests, numpy as np
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'buffer'):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
CAPITAL          = 100
SHARES           = 1000
MAX_WORKERS      = 8
WATCH_LIMIT      = 20
TG_MAX_LEN       = 4000

HOT_SECTORS = {
    '半導體業','IC設計','記憶體','化合物半導體',
    'AI伺服器','伺服器','EMS','電源','連接器',
    '封測','PCB','光電業','通信網路','電子零組件',
    '電子通路','資訊服務','其他電子','電機機械','電腦週邊',
    '生技醫療','生技','光學','工控','量測','被動元件','主機板','電腦',
}

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

_UA = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15',
]
print_lock = threading.Lock()
def safe_print(*a): print_lock.acquire(); print(*a); print_lock.release()

# ── 股票清單 ─────────────────────────────────────────────
def fetch_stock_list():
    url = 'https://openapi.twse.com.tw/v1/opendata/t187ap03_L'
    try:
        r = requests.get(url, timeout=15, headers={'User-Agent': random.choice(_UA)})
        r.raise_for_status()
        data = json.loads(r.content.decode('utf-8'))
        stocks = []
        for item in data:
            code   = item.get('公司代號', '').strip()
            name   = item.get('公司簡稱', code).strip()
            sec_id = item.get('產業別', '').strip()
            if re.match(r'^\d{4}$', code) and not code.startswith('00'):
                stocks.append({'code': code, 'name': name, 'ex': 'TW',
                               'sector': SECTOR_MAP.get(sec_id, sec_id or '—')})
        return stocks
    except Exception as e:
        safe_print(f'[警告] 股票清單失敗：{e}')
        return []

# ── 數學工具 ─────────────────────────────────────────────
def ema(arr, period):
    res = [None]*len(arr); k = 2/(period+1)
    clean = [(i,v) for i,v in enumerate(arr) if v is not None and not np.isnan(v)]
    if len(clean) < period: return res
    si = clean[period-1][0]; prev = sum(v for _,v in clean[:period])/period; res[si] = prev
    for i in range(si+1, len(arr)):
        v = arr[i]
        if v is None or np.isnan(v): res[i] = prev
        else: prev = v*k + prev*(1-k); res[i] = prev
    return res

def sma(arr, period):
    res = [None]*len(arr)
    for i in range(period-1, len(arr)):
        sl = [v for v in arr[i-period+1:i+1] if v is not None and not np.isnan(v)]
        if len(sl) >= int(period*0.8): res[i] = sum(sl)/len(sl)
    return res

def wmax(arr, n, end):
    sl = [v for v in arr[max(0,end-n+1):end+1] if v is not None]; return max(sl) if sl else None
def wmin(arr, n, end):
    sl = [v for v in arr[max(0,end-n+1):end+1] if v is not None]; return min(sl) if sl else None
def wmean(arr, n, end):
    sl = [v for v in arr[max(0,end-n+1):end+1] if v is not None and not np.isnan(v)]
    return sum(sl)/len(sl) if sl else None
def get(arr, i): i = max(0,i); return arr[i] if i < len(arr) and arr[i] is not None else None

def true_range(highs, lows, closes):
    tr = []
    for i in range(len(closes)):
        h, l = highs[i], lows[i]
        if h is None or l is None: tr.append(None); continue
        if i == 0 or closes[i-1] is None: tr.append(h-l)
        else: pc = closes[i-1]; tr.append(max(h-l, abs(h-pc), abs(l-pc)))
    return tr

# ── TWII 大盤狀態 ─────────────────────────────────────────
def fetch_twii():
    url = 'https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII?interval=1d&range=2y'
    for _ in range(2):
        try:
            r = requests.get(url, headers={'User-Agent': random.choice(_UA)}, timeout=12)
            if r.status_code != 200: time.sleep(1); continue
            res = r.json()['chart']['result'][0]
            q   = res['indicators']['quote'][0]
            fix = lambda a: [v if v is not None and not np.isnan(v) else None for v in (a or [])]
            return fix(q.get('close', []))
        except: time.sleep(2)
    return None

def calc_market_status(twii_close):
    if not twii_close or len(twii_close) < 200:
        return {'stage': 0, 'label': '大盤資料不足', 'emoji': '⚪'}
    ma = sma(twii_close, 150)
    n  = len(twii_close); c = twii_close[n-1]
    ma_v = get(ma, n-1); ma_prev = get(ma, n-5)
    if not ma_v or not ma_prev or c is None:
        return {'stage': 0, 'label': '計算中', 'emoji': '⚪'}
    if c > ma_v and ma_v > ma_prev:
        return {'stage': 2, 'label': 'Stage 2 多頭趨勢', 'emoji': '🟢'}
    if c < ma_v and ma_v < ma_prev:
        return {'stage': 4, 'label': 'Stage 4 空頭趨勢', 'emoji': '🔴'}
    return {'stage': 3, 'label': 'Stage 1/3 盤整', 'emoji': '🟡'}

# ── v3 條件計算（含 RS / 近期突破 / 突破日低點）──────────
def calc_conditions(close, high, low, volume, twii_close=None):
    n = len(close)
    if n < 252: return None
    last = n - 1
    while last > 0 and (close[last] is None or np.isnan(close[last])): last -= 1
    if last < 251: return None
    c, v = close[last], volume[last]
    if not c or not v: return None

    ma50  = ema(close, 50); ma150 = ema(close, 150); ma200 = ema(close, 200)
    ma30w = sma(close, 150); vol50 = sma(volume, 50)
    if not all([get(ma50,last), get(ma150,last), get(ma200,last), get(ma30w,last)]): return None

    h52 = wmax(high, 252, last); l52 = wmin(low, 252, last)

    stage2 = c > get(ma30w, last) and get(ma30w, last) > get(ma30w, last-4)
    m = [
        c > get(ma150,last) and c > get(ma200,last),
        get(ma150,last) > get(ma200,last),
        get(ma200,last) > get(ma200,last-20) if last >= 20 else False,
        get(ma50,last) > get(ma150,last) and get(ma50,last) > get(ma200,last),
        c > get(ma50,last),
        c >= h52*0.75 if h52 else False,
        c >= l52*1.25 if l52 else False,
    ]
    tt_score = sum(m); tt = tt_score == 7

    atr_arr  = sma(true_range(high, low, close), 14)
    atr_now  = wmean(atr_arr, 10, last); atr_prev = wmean(atr_arr, 10, last-10)
    vol_now  = wmean(volume, 10, last);  vol_prev = wmean(volume, 10, last-10)
    vcp = bool(atr_now and atr_prev and vol_now and vol_prev and h52
               and atr_now < atr_prev*0.75 and vol_now < vol_prev*0.80 and c >= h52*0.70)

    pivot   = wmax(high, 20, last-1)
    avg_vol = get(vol50, last)
    vol_ratio = v / avg_vol if avg_vol else None
    brk = bool(pivot and avg_vol and c > pivot and vol_ratio and vol_ratio >= 1.2 and c <= pivot*1.05)
    vol_tier = 2 if (vol_ratio and vol_ratio >= 1.5) else (1 if (vol_ratio and vol_ratio >= 1.2) else 0)

    # 近期突破追溯（往前 1-5 根）
    recent_breakout = None
    if not brk and stage2 and tt_score >= 6:
        for d in range(1, 6):
            i = last - d
            if i < 252: break
            past_pivot   = wmax(high, 20, i-1)
            past_avg_vol = get(vol50, i)
            past_v       = volume[i]; past_c = close[i]
            if not past_pivot or not past_avg_vol or not past_c: continue
            past_ratio   = past_v / past_avg_vol if past_avg_vol else 0
            was_brk = past_c > past_pivot and past_ratio >= 1.2 and past_c <= past_pivot*1.05
            still_ok = c >= past_pivot * 0.95
            if was_brk and still_ok:
                recent_breakout = {'days_ago': d, 'pivot': past_pivot,
                                   'low': low[i], 'vol_ratio': past_ratio}
                vol_tier = 2 if past_ratio >= 1.5 else 1
                break

    has_breakout = brk or bool(recent_breakout)

    # 停損：突破日低點
    sl = pivot * 0.97 if pivot else None
    breakout_days = 0
    if brk:
        sl = max(low[last] if low[last] else pivot*0.97, pivot*0.97)
        breakout_days = 0
    elif recent_breakout:
        bp = recent_breakout['pivot']
        sl = max(recent_breakout['low'] if recent_breakout['low'] else bp*0.97, bp*0.97)
        breakout_days = recent_breakout['days_ago']
        pivot = recent_breakout['pivot']  # 用突破時的 pivot

    risk_pct = (c - sl) / c * 100 if sl and sl < c else None
    score    = sum([stage2, tt, vcp, has_breakout])

    # 相對強弱 RS（3個月 ≈ 63 交易日）
    rs = None
    if twii_close and len(twii_close) >= 64 and last >= 63:
        try:
            tn = len(twii_close)
            tc_now  = next((v for v in reversed(twii_close) if v is not None), None)
            tc_prev = next((v for v in reversed(twii_close[:tn-63]) if v is not None), None)
            sc_prev = close[last-63] if close[last-63] is not None else None
            if tc_now and tc_prev and sc_prev:
                s_ret = (c - sc_prev) / sc_prev * 100
                t_ret = (tc_now - tc_prev) / tc_prev * 100
                rs = round(s_ret - t_ret, 1)
        except Exception:
            pass

    return {
        'price': c, 'stage2': stage2, 'tt': tt, 'tt_score': tt_score,
        'vcp': vcp, 'brk': brk, 'has_breakout': has_breakout,
        'recent_breakout': recent_breakout, 'pivot': pivot,
        'score': score, 'risk_pct': risk_pct, 'stop_loss': sl,
        'rs': rs, 'vol_ratio': vol_ratio, 'vol_tier': vol_tier,
        'breakout_days': breakout_days,
    }

# ── 資料抓取 ─────────────────────────────────────────────
def fetch_ohlcv(stock, retries=2):
    suffix = '.TWO' if stock['ex'] == 'TWO' else '.TW'
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock['code']}{suffix}?interval=1d&range=2y"
    for attempt in range(retries):
        try:
            r = requests.get(url, headers={'User-Agent': random.choice(_UA)}, timeout=12)
            if r.status_code == 429: time.sleep(5); continue
            if r.status_code != 200: time.sleep(1); continue
            res = r.json()['chart']['result'][0]
            q   = res['indicators']['quote'][0]
            fix = lambda a: [v if v is not None and not np.isnan(v) else None for v in (a or [])]
            time.sleep(random.uniform(0.2, 0.5))
            return {
                'close':  fix(q.get('close',  [])),
                'high':   fix(q.get('high',   [])),
                'low':    fix(q.get('low',    [])),
                'volume': fix(q.get('volume', [])),
            }
        except:
            if attempt < retries-1: time.sleep(2)
    return None

def scan_one(stock, twii_close):
    data = fetch_ohlcv(stock)
    if not data: return stock, None, '無資料'
    cond = calc_conditions(data['close'], data['high'], data['low'], data['volume'], twii_close)
    if not cond: return stock, None, '資料不足'
    return stock, cond, 'ok'

# ── Telegram 發送 ─────────────────────────────────────────
def send_telegram(text):
    url  = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    data = {'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'}
    try:
        r = requests.post(url, data=data, timeout=10)
        if not r.ok: safe_print(f'[TG] {r.text}')
    except Exception as e:
        safe_print(f'[TG 錯誤] {e}')

def send_long(text):
    if len(text) <= TG_MAX_LEN:
        send_telegram(text); return
    lines = text.split('\n'); chunk = ''
    for line in lines:
        if len(chunk) + len(line) + 1 > TG_MAX_LEN:
            send_telegram(chunk); time.sleep(0.5); chunk = line + '\n'
        else:
            chunk += line + '\n'
    if chunk.strip(): send_telegram(chunk)

# ── 訊息格式化 ────────────────────────────────────────────
def tv_link(stock):
    market = 'TPEX' if stock.get('ex') == 'TWO' else 'TWSE'
    return f'<a href="https://www.tradingview.com/chart/?symbol={market}:{stock["code"]}">{stock["code"]} {stock["name"]}</a>'

def fmt_price(p): return f'{p:,.1f}' if p else '—'

def fmt_money(n):
    if abs(n) >= 1e8: return f'{n/1e8:.2f}億'
    if abs(n) >= 1e4: return f'{n/1e4:.1f}萬'
    return f'{int(n):,}'

def vol_stars(tier): return '⭐⭐' if tier >= 2 else ('⭐' if tier == 1 else '○')

def build_message(entries, watches, total, errors, scan_time, date_str, market_status):
    lines = [
        f'📊 <b>台股動能掃描 {date_str}</b>  v3',
        f'⏰ {scan_time}  掃描 {total} 支 · 失敗 {errors} 支',
        f'{market_status["emoji"]} 大盤：{market_status["label"]}',
    ]
    if market_status['stage'] == 4:
        lines.append('⚠️ 大盤空頭，突破成功率低，建議觀望或減碼')
    elif market_status['stage'] == 3:
        lines.append('⚠️ 大盤盤整，只做最強突破')
    lines.append('')

    # 今日進場
    lines.append(f'🟢 <b>今日進場 {len(entries)} 支</b>')
    if entries:
        lines.append('━━━━━━━━━━━━━━━━━━━')
        for s in entries:
            c    = s['cond']
            p    = c['price']
            sl   = c['stop_loss']
            piv  = c['pivot']
            tgt  = p + 2*(p-sl) if sl else None
            max_chase = piv * 1.03 if piv else None
            risk = c['risk_pct']
            pos  = (CAPITAL * 10000 * 0.01 / (risk/100)) / 10000 if risk else None
            rs   = c['rs']
            rs_str = (f'+{rs}%' if rs >= 0 else f'{rs}%') if rs is not None else None
            above_chase = max_chase and p > max_chase
            days = c['breakout_days']
            days_str = '今日突破 🔥' if days == 0 else f'突破 {days}天前'
            is_hot = s.get('sector') in HOT_SECTORS

            lines.append(
                f'\n▶ <b>{tv_link(s)}</b>（{s.get("sector","—")}）'
                + (' 🔥' if is_hot else '')
            )
            lines.append(f'   {vol_stars(c["vol_tier"])} {days_str}'
                         + (f'  RS {rs_str}' if rs_str else ''))
            lines.append(f'   現價 NT${fmt_price(p)}')
            if above_chase:
                over = ((p/piv)-1)*100
                lines.append(f'   ⚠️ 已超出進場區間 +{over:.1f}%，不宜追')
            else:
                lines.append(f'   進場區間 NT${fmt_price(piv)} ~ {fmt_price(max_chase)}（突破點 ~ 最高追+3%）')
            lines.append(f'   停損 NT${fmt_price(sl)} ｜ 目標 NT${fmt_price(tgt)}')
            if risk:
                lines.append(f'   風險 {risk:.1f}% ｜ 成本/張 {fmt_money(p*SHARES)}')
            if pos:
                lines.append(f'   建議部位 {pos:.1f}萬（{CAPITAL}萬×1%規則）')
            conds = ' '.join(t for t, ok in [('Stage2',c['stage2']),('TT',c['tt']),('VCP',c['vcp'])] if ok)
            lines.append(f'   TT {c["tt_score"]}/7 · {conds}')
    else:
        lines.append('   今日無進場訊號\n')

    # 觀察名單（TT 6+/7）
    watch_show = watches[:WATCH_LIMIT]
    extra = len(watches) - len(watch_show)
    lines.append(f'\n🟡 <b>觀察名單 {len(watches)} 支</b>（TT 6+/7，等突破）')
    if watch_show:
        lines.append('━━━━━━━━━━━━━━━━━━━')
        for s in watch_show:
            c = s['cond']
            miss = [t for t, ok in [('Stage2',c['stage2']),('TT',c['tt']),('VCP',c['vcp'])] if not ok]
            miss_str = '、'.join(miss) if miss else '突破'
            rs = c['rs']
            rs_str = (f' RS +{rs}%' if rs >= 0 else f' RS {rs}%') if rs is not None else ''
            lines.append(
                f'▷ <b>{tv_link(s)}</b>（{s.get("sector","—")}）'
                f'  TT {c["tt_score"]}/7  缺{miss_str}  突破點 NT${fmt_price(c["pivot"])}{rs_str}'
            )
        if extra:
            lines.append(f'   ⋯ 另有 {extra} 支，詳見網頁版')
    else:
        lines.append('   無觀察標的')

    return '\n'.join(lines)

# ── 主程式 ────────────────────────────────────────────────
def main():
    tz_tw     = timezone(timedelta(hours=8))
    now_tw    = datetime.now(tz_tw)
    scan_time = now_tw.strftime('%H:%M')
    date_str  = now_tw.strftime('%Y/%m/%d')

    safe_print(f'=== 台股動能掃描 v3 | {date_str} {scan_time} ===')

    # 1. 大盤資料
    safe_print('取得大盤（TWII）資料...')
    twii_close = fetch_twii()
    market_status = calc_market_status(twii_close)
    safe_print(f'大盤：{market_status["label"]}')

    # 2. 股票清單
    safe_print('取得 TWSE 股票清單...')
    stocks = fetch_stock_list()
    if not stocks:
        safe_print('無法取得股票清單，中止'); return
    safe_print(f'共 {len(stocks)} 支')

    entries, watches = [], []
    errors = 0; done = 0; total = len(stocks)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(scan_one, s, twii_close): s for s in stocks}
        for future in as_completed(futures):
            stock, cond, status = future.result()
            done += 1
            if done % 100 == 0 or done == total:
                safe_print(f'  {done}/{total}  進場 {len(entries)}  觀察 {len(watches)}')
            if status != 'ok' or not cond:
                errors += 1; continue

            entry = {**stock, 'cond': cond}

            # A方案：有突破（今日或近期）+ score>=3 + RS>=0
            if cond['has_breakout'] and cond['score'] >= 3 and (cond['rs'] is None or cond['rs'] >= 0):
                entries.append(entry)
            # 觀察名單：TT 6+/7 + score>=2 + 尚未突破
            elif not cond['has_breakout'] and cond['score'] >= 2 and cond['tt_score'] >= 6:
                watches.append(entry)

    # 排序：今日突破在前，RS 高的優先
    entries.sort(key=lambda x: (x['cond']['breakout_days'], -(x['cond']['rs'] or 0)))
    watches.sort(key=lambda x: (-(x['cond']['tt_score']), (x['cond']['rs'] or 0) * -1))

    safe_print(f'\n結果：進場 {len(entries)}，觀察 {len(watches)}，失敗 {errors}')

    msg = build_message(entries, watches, total, errors, scan_time, date_str, market_status)
    safe_print('\n=== 發送 Telegram ===')
    safe_print(msg)
    send_long(msg)
    safe_print('=== 完成 ===')

if __name__ == '__main__':
    main()
