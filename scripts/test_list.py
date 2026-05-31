import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.environ.setdefault('TELEGRAM_TOKEN', 'dummy')
os.environ.setdefault('TELEGRAM_CHAT_ID', 'dummy')
sys.path.insert(0, 'scripts')
from scanner import fetch_stock_list
stocks = fetch_stock_list()
print(f'取得股票清單：{len(stocks)} 支')
tw  = [s for s in stocks if s['ex']=='TW']
two = [s for s in stocks if s['ex']=='TWO']
print(f'上市(TW): {len(tw)} 支，上櫃(TWO): {len(two)} 支')
print('前5支：')
for s in stocks[:5]:
    print(f'  {s["code"]} {s["name"]} ({s["sector"]})')
