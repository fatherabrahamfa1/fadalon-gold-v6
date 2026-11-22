import os, time, logging, requests
import numpy as np
import pandas as pd
from dotenv import load_dotenv
load_dotenv()
BASE = 'https://api.twelvedata.com'
_LAST_API_REQUEST = 0.0
_REQUEST_GAP_SECONDS = float(os.getenv('API_REQUEST_GAP_SECONDS', '8'))
SYMBOL = 'XAU/USD'
MIN_ADX_5M = float(os.getenv('MIN_ADX_5M', '18'))
MIN_EMA_GAP_ATR = float(os.getenv('MIN_EMA_GAP_ATR', '0.10'))
MIN_ATR_RATIO = float(os.getenv('MIN_ATR_RATIO', '0.60'))
MAX_ATR_RATIO = float(os.getenv('MAX_ATR_RATIO', '1.80'))
MIN_CANDLE_BODY_RATIO = float(os.getenv('MIN_CANDLE_BODY_RATIO', '0.35'))

def fetch_bars(symbol=SYMBOL, interval='1min', outputsize=250):
    global _LAST_API_REQUEST
    key = os.getenv('TWELVE_DATA_API_KEY')
    if not key: raise RuntimeError('TWELVE_DATA_API_KEY is not configured.')
    wait = _REQUEST_GAP_SECONDS - (time.monotonic() - _LAST_API_REQUEST)
    if wait > 0: time.sleep(wait)
    params = {'symbol': symbol, 'interval': interval, 'outputsize': outputsize, 'apikey': key, 'format': 'JSON', 'timezone':'UTC'}
    for attempt in range(2):
        _LAST_API_REQUEST = time.monotonic()
        response = requests.get(f'{BASE}/time_series', params=params, timeout=25)
        if response.status_code == 429 and attempt == 0:
            logging.warning('Twelve Data rate limit reached; waiting 65 seconds before one retry.')
            time.sleep(65); continue
        if response.status_code == 401: raise RuntimeError('Twelve Data rejected the API key (401). Check Render Environment.')
        response.raise_for_status(); payload = response.json()
        if 'values' not in payload:
            raise RuntimeError(f"Twelve Data returned no candles: {payload.get('message', payload.get('code','unknown response'))}")
        frame = pd.DataFrame(payload['values'])
        for col in ('open','high','low','close'): frame[col] = pd.to_numeric(frame[col], errors='coerce')
        frame['datetime'] = pd.to_datetime(frame['datetime'], utc=True, errors='coerce')
        return frame.dropna(subset=['datetime','open','high','low','close']).sort_values('datetime').drop_duplicates('datetime').reset_index(drop=True)
    raise RuntimeError('Twelve Data rate limit persisted; skipped this request.')

def _rsi(s, n=14):
    d=s.diff(); gain=d.clip(lower=0).ewm(alpha=1/n,adjust=False).mean(); loss=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean(); rs=gain/loss.replace(0,np.nan); return 100-100/(1+rs)
def _atr(f,n=14):
    p=f.close.shift(1); tr=pd.concat([f.high-f.low,(f.high-p).abs(),(f.low-p).abs()],axis=1).max(axis=1); return tr.ewm(alpha=1/n,adjust=False).mean()
def _adx(f,n=14):
    up=f.high.diff(); down=-f.low.diff(); plus=pd.Series(np.where((up>down)&(up>0),up,0.),index=f.index); minus=pd.Series(np.where((down>up)&(down>0),down,0.),index=f.index); atr=_atr(f,n).replace(0,np.nan); p=100*plus.ewm(alpha=1/n,adjust=False).mean()/atr; m=100*minus.ewm(alpha=1/n,adjust=False).mean()/atr; dx=100*(p-m).abs()/(p+m).replace(0,np.nan); return dx.ewm(alpha=1/n,adjust=False).mean()

def analyse_from_bars(symbol, bars):
    if symbol != SYMBOL: raise ValueError('FADALON V6 is restricted to XAU/USD.')
    if len(bars)<100: raise RuntimeError('Not enough closed XAU/USD one-minute candles.')
    m=bars.copy().sort_values('datetime').reset_index(drop=True); c=m.close
    m['ema9']=c.ewm(span=9,adjust=False).mean(); m['ema21']=c.ewm(span=21,adjust=False).mean(); m['rsi']=_rsi(c)
    macd=c.ewm(span=12,adjust=False).mean()-c.ewm(span=26,adjust=False).mean(); m['macd']=macd; m['macd_signal']=macd.ewm(span=9,adjust=False).mean(); m['atr']=_atr(m); m['atr_median']=m.atr.rolling(50,min_periods=30).median(); m['atr_ratio']=m.atr/m.atr_median.replace(0,np.nan); m['body_ratio']=(m.close-m.open).abs()/(m.high-m.low).replace(0,np.nan)
    m5=m.set_index('datetime').resample('5min').agg({'open':'first','high':'max','low':'min','close':'last'}).dropna().reset_index()
    if len(m5)<45: raise RuntimeError('Not enough 5-minute resampled gold candles.')
    m5['ema20']=m5.close.ewm(span=20,adjust=False).mean(); m5['ema50']=m5.close.ewm(span=50,adjust=False).mean(); m5['adx']=_adx(m5)
    x=m.iloc[-1]; t=m5.iloc[-1]; up=t.ema20>t.ema50; down=t.ema20<t.ema50
    buy=[x.ema9>x.ema21,x.macd>x.macd_signal,50<x.rsi<68,up,x.close>x.open]
    sell=[x.ema9<x.ema21,x.macd<x.macd_signal,32<x.rsi<50,down,x.close<x.open]
    bs,ss=sum(map(bool,buy)),sum(map(bool,sell)); candidate='BUY' if bs==5 and bs>ss else 'SELL' if ss==5 and ss>bs else 'NO TRADE'; score=max(bs,ss)
    adx=float(t.adx) if pd.notna(t.adx) else 0.; atrr=float(x.atr_ratio) if pd.notna(x.atr_ratio) else 0.; gap=abs(float(x.ema9-x.ema21))/float(x.atr) if pd.notna(x.atr) and x.atr>0 else 0.; body=float(x.body_ratio) if pd.notna(x.body_ratio) else 0.
    passed=adx>=MIN_ADX_5M and gap>=MIN_EMA_GAP_ATR and MIN_ATR_RATIO<=atrr<=MAX_ATR_RATIO and body>=MIN_CANDLE_BODY_RATIO
    direction=candidate if candidate in ('BUY','SELL') and passed else 'NO TRADE'
    return {'symbol':SYMBOL,'direction':direction,'agreement':f'{score}/5','entry_price':float(x.close),'candle_time':pd.Timestamp(x.datetime).isoformat(),'rsi':round(float(x.rsi),2) if pd.notna(x.rsi) else None,'adx_5m':round(adx,2),'atr_ratio':round(atrr,2),'ema_gap_atr':round(gap,2),'body_ratio':round(body,2),'quality_pass':passed}

def send_telegram(message):
    token=os.getenv('TELEGRAM_BOT_TOKEN'); chat=os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat: raise RuntimeError('Telegram credentials are missing from Render Environment.')
    r=requests.post(f'https://api.telegram.org/bot{token}/sendMessage',json={'chat_id':chat,'text':message},timeout=20); r.raise_for_status(); data=r.json()
    if not data.get('ok'): raise RuntimeError('Telegram did not confirm message delivery.')
    return data
