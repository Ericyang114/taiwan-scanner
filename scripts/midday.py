"""
台股盤中預警 - 12:00 推播
用每日歷史資料計算指標條件，加上今日盤中即時量能，
找出「接近突破」或「已突破」的股票，讓你在收盤前還有1.5小時可以操作。

判斷邏輯：
1. 歷史條件 OK：Stage2 ✓ + TT ✓（從日線計算）
2. 今日現價：距突破點 ≤ 5%（接近或已突破）
3. 預估今日量能：盤中累積量 × (總交易時間 / 已過時間) vs 50日均量
"""

import os, sys, io, re, json, time, random, requests, numpy as np
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

TELEGRAM_TOKEN   = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']

# 台灣股市：09:00 ~ 13:30 = 270 分鐘
MARKET_OPEN_MINUTES  = 9 * 60          # 09:00
MARKET_TOTAL_MINUTES = 270             # 4.5 小時

MAX_WORKERS = 4

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
def safe_print(*a):
    with print_lock: print(*a)

# ── 股票清單 ─────────────────────────────────────────
def fetch_stock_list():
    try:
        r = requests.get('https://openapi.twse.com.tw/v1/opendata/t187ap03_L',
                         timeout=15, headers={'User-Agent': random.choice(_UA)})
        data = json.loads(r.content.decode('utf-8'))
        stocks = []
        for item in data:
            code   = item.get('公司代號','').strip()
            name   = item.get('公司簡稱', code).strip()
            sec_id = item.get('產業別','').strip()
            if re.match(r'^\d{4}$', code) and not code.startswith('00'):
                stocks.append({'code':code,'name':name,'ex':'TW',
                               'sector': SECTOR_MAP.get(sec_id, sec_id or '—')})
        return stocks
    except Exception as e:
        safe_print(f'[警告] 股票清單失敗：{e}')
        return []

# ── 數學工具（同 scanner.py）────────────────────────
def ema(arr, period):
    res=[None]*len(arr); k=2/(period+1)
    clean=[(i,v) for i,v in enumerate(arr) if v is not None and not np.isnan(v)]
    if len(clean)<period: return res
    si=clean[period-1][0]; prev=sum(v for _,v in clean[:period])/period; res[si]=prev
    for i in range(si+1,len(arr)):
        v=arr[i]
        if v is None or np.isnan(v): res[i]=prev
        else: prev=v*k+prev*(1-k); res[i]=prev
    return res

def sma(arr, period):
    res=[None]*len(arr)
    for i in range(period-1,len(arr)):
        sl=[v for v in arr[i-period+1:i+1] if v is not None and not np.isnan(v)]
        if len(sl)>=int(period*0.8): res[i]=sum(sl)/len(sl)
    return res

def wmax(arr,n,end):
    sl=[v for v in arr[max(0,end-n+1):end+1] if v is not None]; return max(sl) if sl else None
def wmean(arr,n,end):
    sl=[v for v in arr[max(0,end-n+1):end+1] if v is not None and not np.isnan(v)]
    return sum(sl)/len(sl) if sl else None
def get(arr,i): i=max(0,i); return arr[i] if i<len(arr) and arr[i] is not None else None

# ── 計算歷史每日條件 ─────────────────────────────────
def calc_daily(close, high, low, volume):
    n=len(close)
    if n<252: return None
    last=n-1
    while last>0 and (close[last] is None or np.isnan(close[last])): last-=1
    if last<251: return None
    c=close[last]; v=volume[last]
    if not c or not v: return None

    ma50=ema(close,50); ma150=ema(close,150); ma200=ema(close,200)
    ma30w=sma(close,150); vol50=sma(volume,50)
    if not all([get(ma50,last),get(ma150,last),get(ma200,last),get(ma30w,last)]): return None

    h52=wmax(high,252,last)
    l52_arr=[v2 for v2 in low[max(0,last-251):last+1] if v2 is not None]
    l52=min(l52_arr) if l52_arr else None

    stage2=c>get(ma30w,last) and get(ma30w,last)>get(ma30w,last-4)
    m1=c>get(ma150,last) and c>get(ma200,last)
    m2=get(ma150,last)>get(ma200,last)
    m3=get(ma200,last)>get(ma200,last-20) if last>=20 else False
    m4=get(ma50,last)>get(ma150,last) and get(ma50,last)>get(ma200,last)
    m5=c>get(ma50,last)
    m6=c>=h52*0.75 if h52 else False
    m7=c>=l52*1.25 if l52 else False
    tt_score=sum([m1,m2,m3,m4,m5,m6,m7]); tt=tt_score==7

    pivot=wmax(high,20,last-1)
    avg_vol=get(vol50,last)
    return {
        'close':c,'stage2':stage2,'tt':tt,'tt_score':tt_score,
        'pivot':pivot,'avg_vol':avg_vol,
    }

# ── 抓取資料 ─────────────────────────────────────────
def fetch_daily(stock):
    suffix='.TWO' if stock['ex']=='TWO' else '.TW'
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{stock['code']}{suffix}?interval=1d&range=2y"
    for _ in range(2):
        try:
            r=requests.get(url,headers={'User-Agent':random.choice(_UA)},timeout=10)
            if r.status_code==429: time.sleep(5); continue
            if r.status_code!=200: continue
            res=r.json()['chart']['result'][0]
            q=res['indicators']['quote'][0]
            fix=lambda a:[v if v is not None and not np.isnan(v) else None for v in (a or [])]
            time.sleep(random.uniform(0.2,0.6))
            return {'close':fix(q.get('close',[])),'high':fix(q.get('high',[])),'low':fix(q.get('low',[])),'volume':fix(q.get('volume',[]))}
        except: time.sleep(2)
    return None

def fetch_intraday(stock):
    """抓今日盤中資料：現價 + 累積成交量"""
    suffix='.TWO' if stock['ex']=='TWO' else '.TW'
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{stock['code']}{suffix}?interval=1m&range=1d"
    try:
        r=requests.get(url,headers={'User-Agent':random.choice(_UA)},timeout=8)
        if r.status_code!=200: return None
        res=r.json()['chart']['result'][0]
        q=res['indicators']['quote'][0]
        closes=[v for v in q.get('close',[]) if v is not None]
        vols=[v for v in q.get('volume',[]) if v is not None]
        if not closes: return None
        return {'current_price':closes[-1],'intraday_volume':sum(vols)}
    except: return None

# ── 預估今日量能 ─────────────────────────────────────
def projected_volume(intraday_vol, tz_tw):
    """根據現在時間推算今日預估總量"""
    now_tw = datetime.now(tz_tw)
    elapsed = (now_tw.hour * 60 + now_tw.minute) - MARKET_OPEN_MINUTES
    elapsed = max(1, min(elapsed, MARKET_TOTAL_MINUTES))
    return intraday_vol * MARKET_TOTAL_MINUTES / elapsed

# ── 掃描單支 ─────────────────────────────────────────
def scan_one(stock, tz_tw):
    daily = fetch_daily(stock)
    if not daily: return None
    cond = calc_daily(daily['close'],daily['high'],daily['low'],daily['volume'])
    if not cond: return None
    if not (cond['stage2'] and cond['tt']): return None   # 至少 Stage2 + TT
    if not cond['pivot'] or not cond['avg_vol']: return None

    intra = fetch_intraday(stock)
    if not intra: return None

    cur   = intra['current_price']
    proj  = projected_volume(intra['intraday_volume'], tz_tw)
    vol_ratio = proj / cond['avg_vol'] if cond['avg_vol'] else 0
    pivot = cond['pivot']
    dist  = (cur - pivot) / pivot * 100   # 正 = 已突破，負 = 距突破點

    # 篩選：現價在突破點 -5% 以內（接近或已突破），且預估量能 ≥ 60%
    if dist < -5 or vol_ratio < 0.6: return None

    return {
        **stock,
        'close':     cond['close'],
        'cur':       cur,
        'pivot':     pivot,
        'dist':      dist,          # % 距突破點（負=還沒到，正=已突破）
        'vol_ratio': vol_ratio,     # 預估量/均量
        'stage2':    cond['stage2'],
        'tt':        cond['tt'],
        'tt_score':  cond['tt_score'],
    }

# ── Telegram ─────────────────────────────────────────
def tv_link(s):
    market='TPEX' if s.get('ex')=='TWO' else 'TWSE'
    return f'<a href="https://www.tradingview.com/chart/?symbol={market}:{s["code"]}">{s["code"]} {s["name"]}</a>'

def fmt(p): return f'{p:,.1f}' if p else '—'

def send_tg(text):
    url=f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    try:
        r=requests.post(url,data={'chat_id':TELEGRAM_CHAT_ID,'text':text,'parse_mode':'HTML'},timeout=10)
        if not r.ok: safe_print(f'[TG] {r.text}')
    except Exception as e:
        safe_print(f'[TG 錯誤] {e}')

def build_message(results, scan_time):
    breakouts = [r for r in results if r['dist'] >= 0]      # 已突破
    nearings  = [r for r in results if r['dist'] < 0]       # 接近突破
    breakouts.sort(key=lambda x: x['vol_ratio'], reverse=True)
    nearings.sort(key=lambda x: x['dist'], reverse=True)    # 距離最近的先

    tz_tw = timezone(timedelta(hours=8))
    date_str = datetime.now(tz_tw).strftime('%Y/%m/%d')

    lines=[
        f'⚡ <b>盤中預警 {date_str} {scan_time}</b>',
        f'距收盤還有約 1.5 小時，留意尾盤走勢\n',
    ]

    lines.append(f'🟢 <b>今日已突破 {len(breakouts)} 支</b>（注意量能是否持續）')
    if breakouts:
        lines.append('━━━━━━━━━━━━━━━━━━━')
        for s in breakouts[:10]:
            vol_str = f'{s["vol_ratio"]*100:.0f}%'
            vol_cls = '充足✓' if s['vol_ratio']>=1.2 else ('偏弱⚠' if s['vol_ratio']>=0.8 else '不足✗')
            lines.append(
                f'\n▶ <b>{tv_link(s)}</b>（{s["sector"]}）'
                f'\n   現價 NT${fmt(s["cur"])} ｜ 突破點 NT${fmt(s["pivot"])} ｜ 超出 +{s["dist"]:.1f}%'
                f'\n   預估量能：{vol_str}（{vol_cls}）｜ TT {s["tt_score"]}/7'
            )
    else:
        lines.append('   今日尚無突破股票')

    lines.append(f'\n🔶 <b>接近突破 {len(nearings)} 支</b>（距突破點 ≤5%，注意尾盤）')
    if nearings:
        lines.append('━━━━━━━━━━━━━━━━━━━')
        for s in nearings[:15]:
            vol_str = f'{s["vol_ratio"]*100:.0f}%'
            vol_cls = '量能充足' if s['vol_ratio']>=0.8 else '量能偏弱'
            lines.append(
                f'▷ <b>{tv_link(s)}</b>（{s["sector"]}）'
                f'  現價 NT${fmt(s["cur"])}  距突破點 {abs(s["dist"]):.1f}%  預估量 {vol_str}（{vol_cls}）'
            )
        if len(nearings)>15:
            lines.append(f'   ⋯ 另有 {len(nearings)-15} 支')
    else:
        lines.append('   無接近突破股票')

    return '\n'.join(lines)

# ── 主程式 ───────────────────────────────────────────
def main():
    tz_tw     = timezone(timedelta(hours=8))
    now_tw    = datetime.now(tz_tw)
    scan_time = now_tw.strftime('%H:%M')

    safe_print(f'=== 盤中預警 {now_tw.strftime("%Y/%m/%d")} {scan_time} ===')

    stocks = fetch_stock_list()
    if not stocks:
        safe_print('無法取得股票清單'); return

    safe_print(f'開始掃描 {len(stocks)} 支（{MAX_WORKERS} 執行緒）...')

    results = []
    done = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(scan_one, s, tz_tw): s for s in stocks}
        for future in as_completed(futures):
            done += 1
            r = future.result()
            if r: results.append(r)
            if done % 100 == 0:
                safe_print(f'  {done}/{len(stocks)}  找到 {len(results)} 支')

    safe_print(f'完成！找到 {len(results)} 支（突破 {sum(1 for r in results if r["dist"]>=0)} + 接近 {sum(1 for r in results if r["dist"]<0)}）')

    msg = build_message(results, scan_time)
    safe_print('\n=== 發送 Telegram ===')
    safe_print(msg)
    send_tg(msg)
    safe_print('=== 完成 ===')

if __name__ == '__main__':
    main()
