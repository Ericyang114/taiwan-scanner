"""
台股動能回測 - Python 版 (v1.1)
直連 Yahoo Finance，不走 CORS Proxy，與 TG 推播使用相同資料來源
生成靜態 HTML 報告推到 GitHub Pages

回測規則：
- 股票清單：TWSE 全部上市普通股（動態抓取）
- 進場條件：A方案 — 突破必須(brk) + score >= 3
- 停損：突破點 × 97%（方案A）
- 目標：進場價 + 2R
- 強制出場：15 個交易日
- 回測窗口：最近 365 天
"""

import os, sys, io, re, json, time, random, requests, numpy as np
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

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

MAX_WORKERS = 3   # GitHub Actions 限速嚴，不能太多並行
BACKTEST_DAYS = 365
MAX_HOLD = 15
print_lock = threading.Lock()

def safe_print(*a, **k):
    with print_lock: print(*a, **k)

# ── 股票清單 ─────────────────────────────────────────
def fetch_stock_list():
    url = 'https://openapi.twse.com.tw/v1/opendata/t187ap03_L'
    try:
        r = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
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
        safe_print(f'股票清單：{len(stocks)} 支')
        return stocks
    except Exception as e:
        safe_print(f'[警告] 股票清單失敗：{e}')
        return []

# ── 數學工具 ─────────────────────────────────────────
def ema(arr, period):
    res = [None]*len(arr); k = 2/(period+1)
    clean = [(i,v) for i,v in enumerate(arr) if v is not None and not np.isnan(v)]
    if len(clean) < period: return res
    si = clean[period-1][0]
    prev = sum(v for _,v in clean[:period])/period; res[si]=prev
    for i in range(si+1, len(arr)):
        v = arr[i]
        if v is None or np.isnan(v): res[i]=prev
        else: prev=v*k+prev*(1-k); res[i]=prev
    return res

def sma(arr, period):
    res=[None]*len(arr)
    for i in range(period-1, len(arr)):
        sl=[v for v in arr[i-period+1:i+1] if v is not None and not np.isnan(v)]
        if len(sl)>=int(period*0.8): res[i]=sum(sl)/len(sl)
    return res

def wmax(arr,n,end):
    sl=[v for v in arr[max(0,end-n+1):end+1] if v is not None]; return max(sl) if sl else None
def wmin(arr,n,end):
    sl=[v for v in arr[max(0,end-n+1):end+1] if v is not None]; return min(sl) if sl else None
def wmean(arr,n,end):
    sl=[v for v in arr[max(0,end-n+1):end+1] if v is not None and not np.isnan(v)]
    return sum(sl)/len(sl) if sl else None
def get(arr,i): i=max(0,i); return arr[i] if i<len(arr) and arr[i] is not None else None

def true_range(highs,lows,closes):
    tr=[]
    for i in range(len(closes)):
        h,l=highs[i],lows[i]
        if h is None or l is None: tr.append(None); continue
        if i==0 or closes[i-1] is None: tr.append(h-l)
        else: pc=closes[i-1]; tr.append(max(h-l,abs(h-pc),abs(l-pc)))
    return tr

# ── 條件計算 ─────────────────────────────────────────
def calc_conditions(close, high, low, volume):
    n=len(close)
    if n<252: return None
    last=n-1
    while last>0 and (close[last] is None or np.isnan(close[last])): last-=1
    if last<251: return None
    c,v=close[last],volume[last]
    if not c or not v: return None

    ma50=ema(close,50); ma150=ema(close,150); ma200=ema(close,200)
    ma30w=sma(close,150); vol50=sma(volume,50)
    if not all([get(ma50,last),get(ma150,last),get(ma200,last),get(ma30w,last)]): return None

    h52=wmax(high,252,last); l52=wmin(low,252,last)
    stage2 = c>get(ma30w,last) and get(ma30w,last)>get(ma30w,last-4)
    m1=c>get(ma150,last) and c>get(ma200,last)
    m2=get(ma150,last)>get(ma200,last)
    m3=get(ma200,last)>get(ma200,last-20) if last>=20 else False
    m4=get(ma50,last)>get(ma150,last) and get(ma50,last)>get(ma200,last)
    m5=c>get(ma50,last)
    m6=c>=h52*0.75 if h52 else False
    m7=c>=l52*1.25 if l52 else False
    tt_score=sum([m1,m2,m3,m4,m5,m6,m7]); tt=tt_score==7

    atr_arr=sma(true_range(high,low,close),14)
    atr_now=wmean(atr_arr,10,last); atr_prev=wmean(atr_arr,10,last-10)
    vol_now=wmean(volume,10,last); vol_prev=wmean(volume,10,last-10)
    vcp=bool(atr_now and atr_prev and vol_now and vol_prev and h52
             and atr_now<atr_prev*0.75 and vol_now<vol_prev*0.80 and c>=h52*0.70)

    pivot=wmax(high,20,last-1); avg_vol=get(vol50,last)
    vol_ratio = v/avg_vol if avg_vol else None
    brk=bool(pivot and avg_vol and c>pivot and vol_ratio and vol_ratio>=1.2 and c<=pivot*1.05)

    # 回測停損：突破點 × 97%（固定，避免回測因單日波動失真）
    sl = pivot * 0.97 if pivot else None

    risk_pct=(c-sl)/c*100 if sl and sl<c else None
    vol_tier = 2 if (vol_ratio and vol_ratio>=1.5) else (1 if (vol_ratio and vol_ratio>=1.2) else 0)
    score=sum([stage2,tt,vcp,brk])
    return {'price':c,'stage2':stage2,'tt':tt,'tt_score':tt_score,
            'vcp':vcp,'brk':brk,'pivot':pivot,'score':score,
            'risk_pct':risk_pct,'stop_loss':sl,'vol_tier':vol_tier}

# ── 資料抓取 ─────────────────────────────────────────
_UA_LIST = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36',
]

def fetch_ohlcv(stock, retries=3):
    suffix='.TWO' if stock['ex']=='TWO' else '.TW'
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{stock['code']}{suffix}?interval=1d&range=2y"
    for attempt in range(retries):
        try:
            ua = random.choice(_UA_LIST)
            r=requests.get(url, headers={'User-Agent': ua}, timeout=10)
            if r.status_code == 429:          # 被限速
                wait = 5 + attempt * 5
                time.sleep(wait); continue
            if r.status_code != 200:
                time.sleep(2); continue
            result=r.json()['chart']['result'][0]
            q=result['indicators']['quote'][0]
            ts=result['timestamp']
            fix=lambda a:[v if v is not None and not np.isnan(v) else None for v in (a or [])]
            # 隨機延遲避免連續請求被擋
            time.sleep(random.uniform(0.3, 0.8))
            return {'timestamps':ts,'close':fix(q.get('close',[])),'high':fix(q.get('high',[])),'low':fix(q.get('low',[])),'volume':fix(q.get('volume',[]))}
        except Exception:
            if attempt < retries-1: time.sleep(3 + attempt * 2)
    return None

# ── 回測單支股票 ─────────────────────────────────────
def backtest_stock(stock, data):
    ts=data['timestamps']; close=data['close']; high=data['high']
    low=data['low']; volume=data['volume']; n=len(close)
    if n<260: return []

    cutoff=datetime.now(timezone.utc).timestamp()-BACKTEST_DAYS*86400
    signals=[]

    for i in range(252, n-1):
        if ts[i]<cutoff: continue

        slice_data={'close':close[:i+1],'high':high[:i+1],'low':low[:i+1],'volume':volume[:i+1]}
        cond=calc_conditions(slice_data['close'],slice_data['high'],slice_data['low'],slice_data['volume'])
        if not cond or not cond['brk'] or cond['score']<3: continue

        entry=close[i]; sl=cond['stop_loss']
        if not sl or sl>=entry: continue
        risk=entry-sl

        # 移動停損（棘輪式）：不設固定 2R 目標，讓獲利繼續跑
        exit_price=close[n-1] if close[n-1] else entry
        exit_ts=ts[n-1]; exit_reason='持有中'
        cur_stop=sl; max_r=0

        for j in range(i+1, n):
            if j-i>MAX_HOLD:
                idx=j-1
                exit_price=close[idx] if close[idx] else entry
                exit_ts=ts[idx]; exit_reason=f'時間出場({MAX_HOLD}日)'; break
            # 棘輪：+3R 才開始鎖停損（+1R/+2R 不移動，給漲勢空間）
            if high[j] is not None:
                reached_r=int((high[j]-entry)/risk)
                if reached_r>max_r:
                    max_r=reached_r
                    if reached_r>=3:
                        new_stop=entry+(reached_r-1)*risk
                        if new_stop>cur_stop: cur_stop=new_stop
            # 動態停損觸發
            if low[j] is not None and low[j]<=cur_stop:
                exit_price=cur_stop; exit_ts=ts[j]
                exit_reason='移動停損出場' if cur_stop>sl else '停損出場'
                break

        pnl=(exit_price-entry); pnl_pct=pnl/entry*100; r_mult=pnl/risk
        days=round((exit_ts-ts[i])/86400)

        signals.append({
            'code':stock['code'],'name':stock['name'],'sector':stock['sector'],
            'entry_ts':ts[i],'exit_ts':exit_ts,
            'entry':round(entry,1),'stop':round(cur_stop,1),'target':None,
            'exit':round(exit_price,1),'reason':exit_reason,
            'pnl_pct':round(pnl_pct,1),'r_mult':round(r_mult,2),'days':days,
            'score':cond['score'],'stage2':cond['stage2'],'tt':cond['tt'],'vcp':cond['vcp'],
            'pivot':round(cond['pivot'],1) if cond['pivot'] else None,
            'risk_pct':round(cond['risk_pct'],1) if cond['risk_pct'] else None,
            'vol_tier':cond.get('vol_tier',0),
        })
    return signals

# ── 抓取並回測（供執行緒使用）────────────────────────
def fetch_and_backtest(stock):
    data=fetch_ohlcv(stock)
    if not data: return []
    return backtest_stock(stock, data)

# ── 生成靜態 HTML 報告 ─────────────────────────────
HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>一年回測報告 v1.1（Python版）| 台股動能策略</title>
<style>
:root{--bg:#0d1117;--bg2:#161b22;--bg3:#21262d;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--green:#3fb950;--yellow:#d29922;--red:#f85149;--blue:#58a6ff;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;font-size:14px;}
.container{max-width:1300px;margin:0 auto;padding:24px 16px;}
.nav{margin-bottom:20px;display:flex;gap:12px;font-size:12px;}
.nav a{color:var(--muted);text-decoration:none;}.nav a:hover{color:var(--text);}
.nav span{color:var(--border);}
h1{font-size:22px;font-weight:700;margin-bottom:4px;}
.subtitle{color:var(--muted);font-size:13px;margin-bottom:6px;}
.rules-line{color:var(--muted);font-size:12px;margin-bottom:12px;}
.rules-line span{color:var(--blue);margin:0 4px;}
.badge-py{display:inline-block;background:rgba(88,166,255,.15);color:var(--blue);border-radius:4px;padding:2px 8px;font-size:11px;font-weight:700;margin-left:8px;}
.meta-bar{color:var(--muted);font-size:12px;margin-bottom:20px;}
.summary-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(165px,1fr));gap:12px;margin-bottom:20px;}
.sum-card{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:14px 16px;}
.sum-label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.4px;margin-bottom:6px;}
.sum-value{font-size:22px;font-weight:700;font-family:monospace;}
.sum-sub{color:var(--muted);font-size:11px;margin-top:3px;}
.divider{grid-column:1/-1;border-top:1px solid var(--border);margin:2px 0;}
.col-green{color:var(--green);}.col-red{color:var(--red);}.col-yellow{color:var(--yellow);}.col-blue{color:var(--blue);}
.section-title{font-size:14px;font-weight:600;color:var(--muted);margin-bottom:10px;}
.sector-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px;margin-bottom:24px;}
.sector-card{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:12px 14px;display:flex;justify-content:space-between;align-items:center;}
.sector-name{font-size:13px;font-weight:600;}
.sector-meta{font-size:11px;color:var(--muted);margin-top:2px;}
.sector-wr{font-size:16px;font-weight:700;font-family:monospace;}
.filter-bar{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:16px;}
.filter-tag{padding:4px 12px;border-radius:20px;font-size:12px;border:1px solid var(--border);background:var(--bg3);color:var(--muted);cursor:pointer;}
.filter-tag.active{border-color:var(--blue);background:rgba(88,166,255,.1);color:var(--text);}
.table-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:8px;margin-bottom:16px;}
table{width:100%;border-collapse:collapse;font-size:12px;}
thead{background:var(--bg2);}
th{padding:9px 10px;text-align:left;color:var(--muted);font-weight:500;white-space:nowrap;border-bottom:1px solid var(--border);}
td{padding:8px 10px;border-bottom:1px solid var(--border);white-space:nowrap;}
tr:last-child td{border-bottom:none;}
tr.win{background:rgba(63,185,80,.06);}tr.loss{background:rgba(248,81,73,.06);}tr.hold{background:rgba(88,166,255,.04);}
.tag{display:inline-block;border-radius:4px;padding:2px 8px;font-size:11px;font-weight:700;}
.tag-win{background:rgba(63,185,80,.2);color:var(--green);}
.tag-loss{background:rgba(248,81,73,.15);color:var(--red);}
.tag-hold{background:rgba(88,166,255,.15);color:var(--blue);}
.tag-time{background:rgba(210,153,34,.2);color:var(--yellow);}
.mono{font-family:monospace;}
.code-link{color:var(--blue);text-decoration:none;font-weight:700;}.code-link:hover{text-decoration:underline;}
@media(max-width:768px){.summary-grid{grid-template-columns:repeat(3,1fr);}td,th{padding:6px 8px;font-size:11px;}}
</style>
</head>
<body><div class="container">

<div class="nav">
  <a href="index.html">← 舊版掃描器</a><span>|</span>
  <a href="v2.html">v2 掃描器</a><span>|</span>
  <a href="backtest.html">回測（瀏覽器版）</a>
</div>

<h1>一年回測報告 <span class="badge-py">Python v1.1 直連版</span></h1>
<p class="subtitle">TWSE 全部上市普通股 · 直連 Yahoo Finance（無 CORS Proxy）</p>
<p class="rules-line">
  進場：<span>突破✓（量≥1.2x）+ score≥3</span>
  停損：<span>突破點×97%</span>
  目標：<span>進場+2R</span>
  強制出場：<span>15個交易日</span>
  窗口：<span>一年</span>
</p>
<p class="meta-bar" id="metaBar"></p>

<div class="summary-grid" id="summaryCards"></div>
<p class="section-title">各類股勝率</p>
<div class="sector-grid" id="sectorGrid"></div>
<div class="filter-bar" id="filterBar"></div>
<p class="section-title">交易明細（新→舊）</p>
<div id="tradeTable"></div>

</div>
<script>
const SIGNALS = %SIGNALS%;
const TOTAL   = %TOTAL%;
const ERRORS  = %ERRORS%;
const GEN_TIME = "%GEN_TIME%";
const SHARES = 1000;

function fmtDate(ts){ const d=new Date(ts*1000); return `${d.getMonth()+1}/${d.getDate()}`; }
function fmtPrice(p){ return p!=null?p.toFixed(1):'—'; }
function fmtMoney(n){ if(Math.abs(n)>=1e8)return(n/1e8).toFixed(2)+'億'; if(Math.abs(n)>=1e4)return(n/1e4).toFixed(1)+'萬'; return Math.round(n).toLocaleString(); }
function tvUrl(s){ return `https://www.tradingview.com/chart/?symbol=TWSE:${s.code}`; }

let activeFilter='all';

document.getElementById('metaBar').textContent=
  `掃描 ${TOTAL} 支 · 失敗/無資料 ${ERRORS} 支 · 共 ${SIGNALS.length} 個回測訊號 · 生成時間：${GEN_TIME}`;

function renderSummary(sigs){
  const completed=sigs.filter(s=>s.reason!=='持有中');
  const wins2R=sigs.filter(s=>s.reason==='達標(2R)');
  const losses=sigs.filter(s=>s.reason==='停損出場');
  const timeEx=sigs.filter(s=>s.reason.startsWith('時間出場'));
  const holding=sigs.filter(s=>s.reason==='持有中');
  const profit=completed.filter(s=>s.pnl_pct>0);
  const loss=completed.filter(s=>s.pnl_pct<=0);
  const wr=completed.length?(profit.length/completed.length*100).toFixed(0):'—';
  const avgW=profit.length?(profit.reduce((a,s)=>a+s.pnl_pct,0)/profit.length).toFixed(1):'—';
  const avgL=loss.length?(loss.reduce((a,s)=>a+s.pnl_pct,0)/loss.length).toFixed(1):'—';
  let ev='—';
  if(completed.length&&avgW!=='—'&&avgL!=='—'){
    const w=profit.length/completed.length;
    ev=(w*parseFloat(avgW)+(1-w)*parseFloat(avgL)).toFixed(1)+'%';
  }
  const totalTxn=sigs.reduce((a,s)=>a+s.entry*SHARES,0);
  const hCost=holding.reduce((a,s)=>a+s.entry*SHARES,0);
  const hVal=holding.reduce((a,s)=>a+s.exit*SHARES,0);
  const realPnl=completed.reduce((a,s)=>a+(s.exit-s.entry)*SHARES,0);
  const unreal=hVal-hCost;
  const total=realPnl+unreal;
  const pc=n=>n>=0?'col-green':'col-red';
  const ps=n=>(n>=0?'+':'')+fmtMoney(n);

  document.getElementById('summaryCards').innerHTML=`
    <div class="sum-card"><div class="sum-label">觸發訊號</div><div class="sum-value col-blue">${sigs.length} 筆</div>
      <div class="sum-sub">達標${wins2R.length}·停損${losses.length}·時間${timeEx.length}·持有${holding.length}</div></div>
    <div class="sum-card"><div class="sum-label">勝率</div><div class="sum-value ${parseFloat(wr)>=50?'col-green':'col-red'}">${wr}%</div>
      <div class="sum-sub">獲利${profit.length}/虧損${loss.length}</div></div>
    <div class="sum-card"><div class="sum-label">平均獲利</div><div class="sum-value col-green">${avgW!=='—'?'+'+avgW+'%':'—'}</div>
      <div class="sum-sub">獲利出場均值</div></div>
    <div class="sum-card"><div class="sum-label">平均虧損</div><div class="sum-value col-red">${avgL!=='—'?avgL+'%':'—'}</div>
      <div class="sum-sub">虧損出場均值</div></div>
    <div class="sum-card"><div class="sum-label">期望值</div><div class="sum-value ${ev[0]==='+'?'col-green':'col-red'}">${ev}</div>
      <div class="sum-sub">勝率×均獲利+敗率×均虧損</div></div>
    <div class="divider"></div>
    <div class="sum-card"><div class="sum-label">總交易金額</div><div class="sum-value col-blue">${fmtMoney(totalTxn)}</div>
      <div class="sum-sub">${sigs.length}筆合計成本</div></div>
    <div class="sum-card"><div class="sum-label">現有庫存市值</div><div class="sum-value col-blue">${holding.length?fmtMoney(hVal):'—'}</div>
      <div class="sum-sub">成本${holding.length?fmtMoney(hCost):'—'}·${holding.length}支</div></div>
    <div class="sum-card"><div class="sum-label">已實現損益</div><div class="sum-value ${pc(realPnl)}">${completed.length?ps(realPnl):'—'}</div>
      <div class="sum-sub">${completed.length}筆已出場</div></div>
    <div class="sum-card"><div class="sum-label">未實現損益</div><div class="sum-value ${pc(unreal)}">${holding.length?ps(unreal):'—'}</div>
      <div class="sum-sub">庫存浮動損益</div></div>
    <div class="sum-card"><div class="sum-label">總損益（含浮盈）</div><div class="sum-value ${pc(total)}">${sigs.length?ps(total):'—'}</div>
      <div class="sum-sub">已實現+未實現</div></div>`;
}

function renderSectorGrid(sigs){
  const map={};
  for(const s of sigs){
    if(!map[s.sector])map[s.sector]={w:0,l:0,h:0,t:0};
    if(s.reason==='達標(2R)')map[s.sector].w++;
    else if(s.reason==='停損出場')map[s.sector].l++;
    else if(s.reason.startsWith('時間出場'))map[s.sector].t++;
    else map[s.sector].h++;
  }
  const sorted=Object.entries(map).sort((a,b)=>(b[1].w+b[1].l+b[1].h+b[1].t)-(a[1].w+a[1].l+a[1].h+a[1].t));
  document.getElementById('sectorGrid').innerHTML=sorted.map(([sec,d])=>{
    const tot=d.w+d.l+d.t+d.h; const comp=d.w+d.l+d.t;
    const wr=comp?Math.round(d.w/comp*100):null;
    const cls=wr!=null?(wr>=50?'col-green':wr>=33?'col-yellow':'col-red'):'col-blue';
    return `<div class="sector-card" style="cursor:pointer" onclick="setFilter('${sec}')">
      <div><div class="sector-name">${sec}</div>
      <div class="sector-meta">共${tot}·勝${d.w}/敗${d.l}/時間${d.t}/持有${d.h}</div></div>
      <div class="sector-wr ${cls}">${wr!=null?wr+'%':'—'}</div></div>`;
  }).join('');
}

function renderFilterBar(sigs){
  const secs=[...new Set(sigs.map(s=>s.sector))].sort();
  document.getElementById('filterBar').innerHTML=
    `<span class="filter-tag active" onclick="setFilter('all')" id="f-all">全部</span>`+
    secs.map(s=>`<span class="filter-tag" onclick="setFilter('${s}')" id="f-${s.replace(/\\s/g,'_')}">${s}</span>`).join('');
}

function setFilter(sec){
  activeFilter=sec;
  document.querySelectorAll('.filter-tag').forEach(e=>e.classList.remove('active'));
  const id=sec==='all'?'f-all':'f-'+sec.replace(/\\s/g,'_');
  const el=document.getElementById(id); if(el)el.classList.add('active');
  const filtered=sec==='all'?SIGNALS:SIGNALS.filter(s=>s.sector===sec);
  renderSummary(filtered); renderTradeTable(filtered);
}

function renderTradeTable(sigs){
  if(!sigs.length){document.getElementById('tradeTable').innerHTML='<div style="text-align:center;padding:40px;color:var(--muted)">無訊號</div>';return;}
  const rows=sigs.map(s=>{
    const isW=s.reason==='達標(2R)',isL=s.reason==='停損出場',isT=s.reason.startsWith('時間出場');
    const rc=isW?'win':isL?'loss':'hold';
    const tc=isW?'tag-win':isL?'tag-loss':isT?'tag-time':'tag-hold';
    const pp=s.pnl_pct>=0?'+'+s.pnl_pct.toFixed(1)+'%':s.pnl_pct.toFixed(1)+'%';
    const pc=s.pnl_pct>=0?'col-green':'col-red';
    const rr=s.r_mult>=0?'+'+s.r_mult.toFixed(2)+'R':s.r_mult.toFixed(2)+'R';
    const rc2=s.r_mult>=0?'col-green':'col-red';
    const cost=s.entry*SHARES; const pnlNTD=(s.exit-s.entry)*SHARES;
    const pnlStr=(pnlNTD>=0?'+':'')+fmtMoney(pnlNTD);
    const vols=s.vol_tier>=2?'⭐⭐':s.vol_tier===1?'⭐':'○';
    return `<tr class="${rc}">
      <td>${fmtDate(s.entry_ts)}</td>
      <td><a class="code-link" href="${tvUrl(s)}" target="_blank">${s.code}</a></td>
      <td>${s.name}</td><td style="color:var(--muted)">${s.sector}</td>
      <td>${vols}</td>
      <td class="mono">NT$${fmtPrice(s.entry)}</td>
      <td class="mono" style="color:var(--muted)">${fmtMoney(cost)}</td>
      <td class="mono col-red">NT$${fmtPrice(s.stop)}<br><small style="color:var(--muted)">突破點×97%</small></td>
      <td class="mono col-green">NT$${fmtPrice(s.target)}</td>
      <td>${s.reason!=='持有中'?fmtDate(s.exit_ts):'—'}</td>
      <td class="mono">${s.reason!=='持有中'?'NT$'+fmtPrice(s.exit):'持有中'}</td>
      <td><span class="tag ${tc}">${s.reason}</span></td>
      <td class="mono ${pc}">${pp}</td>
      <td class="mono ${pc}">${pnlStr}</td>
      <td class="mono ${rc2}">${rr}</td>
      <td style="color:var(--muted)">${s.days}天</td></tr>`;
  }).join('');
  document.getElementById('tradeTable').innerHTML=`
    <div class="table-wrap"><table>
      <thead><tr>
        <th>進場日</th><th>代號</th><th>名稱</th><th>類股</th>
        <th>量</th><th>進場價</th><th>成本(1張)</th><th>停損</th><th>目標(2R)</th>
        <th>出場日</th><th>出場價</th><th>結果</th>
        <th>損益%</th><th>損益金額</th><th>R值</th><th>持有</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
}

// 初始渲染
const sorted=[...SIGNALS].sort((a,b)=>b.entry_ts-a.entry_ts);
renderSummary(sorted); renderSectorGrid(sorted); renderFilterBar(sorted); renderTradeTable(sorted);
</script></body></html>
'''

# ── 主程式 ───────────────────────────────────────────
def main():
    tz_tw   = timezone(timedelta(hours=8))
    now_tw  = datetime.now(tz_tw)
    gen_time= now_tw.strftime('%Y/%m/%d %H:%M')

    safe_print(f'=== 台股回測 Python v1.1 | {gen_time} ===')

    stocks = fetch_stock_list()
    if not stocks:
        safe_print('無法取得股票清單，中止')
        return

    all_signals = []
    errors = 0
    done   = 0
    total  = len(stocks)

    safe_print(f'開始抓取並回測 {total} 支股票（{MAX_WORKERS} 執行緒）...')

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_and_backtest, s): s for s in stocks}
        for future in as_completed(futures):
            sigs = future.result()
            done += 1
            if not sigs and sigs is not None:
                errors += 1
            all_signals.extend(sigs or [])
            if done % 100 == 0 or done == total:
                safe_print(f'  {done}/{total}  訊號數：{len(all_signals)}')

    all_signals.sort(key=lambda x: x['entry_ts'], reverse=True)
    safe_print(f'\n完成！訊號：{len(all_signals)} 個，失敗：{errors} 支')

    # 生成 HTML
    signals_json = json.dumps(all_signals, ensure_ascii=False)
    html = HTML_TEMPLATE \
        .replace('%SIGNALS%', signals_json) \
        .replace('%TOTAL%', str(total)) \
        .replace('%ERRORS%', str(errors)) \
        .replace('%GEN_TIME%', gen_time)

    out_path = 'backtest_result.html'
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    safe_print(f'報告已生成：{out_path}')

if __name__ == '__main__':
    main()
