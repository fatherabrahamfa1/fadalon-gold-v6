import os, time, math, logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pandas as pd
import psycopg2
from dotenv import load_dotenv
from fadalon import fetch_bars, analyse_from_bars, send_telegram
load_dotenv()
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
SYMBOL='XAU/USD'; SCAN_SECONDS=int(os.getenv('SCAN_INTERVAL_SECONDS','120')); LOOP_SECONDS=10
MAX_CLOSED_AGE=int(os.getenv('MAX_CLOSED_CANDLE_AGE_SECONDS','120')); GHANA=ZoneInfo('Africa/Accra')
DATABASE_URL=os.getenv('DATABASE_URL')

def db():
    if not DATABASE_URL: raise RuntimeError('DATABASE_URL is not configured.')
    c=psycopg2.connect(DATABASE_URL,connect_timeout=15,sslmode='require')
    with c.cursor() as q:
        q.execute('''CREATE TABLE IF NOT EXISTS v6_gold_signals(id BIGSERIAL PRIMARY KEY,symbol TEXT NOT NULL,direction TEXT NOT NULL,source_candle_time TEXT NOT NULL,scheduled_entry TEXT NOT NULL,entry_price DOUBLE PRECISION,agreement TEXT NOT NULL,created_at TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'SCHEDULED',UNIQUE(source_candle_time,direction))''')
        q.execute('''CREATE TABLE IF NOT EXISTS v6_gold_outcomes(signal_id BIGINT PRIMARY KEY REFERENCES v6_gold_signals(id) ON DELETE CASCADE,actual_entry_time TEXT,actual_entry_price DOUBLE PRECISION,expiry_time TEXT,exit_price DOUBLE PRECISION,result TEXT NOT NULL DEFAULT 'PENDING',evaluated_at TEXT)''')
    c.commit(); return c

def closed_fresh(b):
    if b.empty: raise RuntimeError('No XAU/USD candles returned.')
    f=b.copy(); f['datetime']=pd.to_datetime(f.datetime,utc=True,errors='coerce'); f=f.dropna(subset=['datetime']); now=pd.Timestamp.now(tz='UTC'); f=f[f.datetime+pd.Timedelta(minutes=1)<=now]
    if f.empty: raise RuntimeError('No completed one-minute gold candle available.')
    age=(now-(f.datetime.max()+pd.Timedelta(minutes=1))).total_seconds()
    if age>MAX_CLOSED_AGE: raise RuntimeError(f'Gold feed stale ({int(age)} seconds); no signal sent.')
    return f.reset_index(drop=True)

def future_entry(now):
    # Schedule at the next even-minute boundary plus 2 minutes; this yields 2-4 minutes ahead.
    epoch=now.timestamp(); next_even=(math.floor(epoch/120)+1)*120
    target=datetime.fromtimestamp(next_even+120,tz=timezone.utc)
    delta=(target-now).total_seconds()/60
    if not 2.0<=delta<=5.0: raise RuntimeError('Could not create a valid 2-5 minute future entry window.')
    return target

def schedule_signal(conn,sig):
    now=datetime.now(timezone.utc); entry=future_entry(now)
    with conn.cursor() as q:
        q.execute('''INSERT INTO v6_gold_signals(symbol,direction,source_candle_time,scheduled_entry,agreement,created_at,status) VALUES(%s,%s,%s,%s,%s,%s,'SCHEDULED') ON CONFLICT(source_candle_time,direction) DO NOTHING RETURNING id''',(SYMBOL,sig['direction'],sig['candle_time'],entry.isoformat(),sig['agreement'],now.isoformat()))
        row=q.fetchone()
    conn.commit()
    if not row: return False
    sid=row[0]; local=entry.astimezone(GHANA); expiry=(entry+timedelta(minutes=1)).astimezone(GHANA)
    icon='🟩' if sig['direction']=='BUY' else '🟥'
    msg=(f'📡 FADALON GOLD SPECIAL SIGNAL\n\n📈 XAU/USD 🥇\n⏱️ Timeframe: 1-min expiry\n🎯 Rule Agreement: {sig["agreement"]} (not a win probability)\n🕓 Entry Time: {local.strftime("%-I:%M %p")} GMT\n⏳ Expiry: {expiry.strftime("%-I:%M %p")} GMT\n\n{icon} {sig["direction"]}\n\nReference gold feed; not broker OTC pricing. Entry must be rechecked at scheduled time. Demo testing only.')
    send_telegram(msg); logging.info('Scheduled V6 gold signal %s for %s',sid,entry.isoformat()); return True

def process_due(conn):
    now=datetime.now(timezone.utc)
    with conn.cursor() as q: q.execute("SELECT id,direction,scheduled_entry FROM v6_gold_signals WHERE status='SCHEDULED' AND scheduled_entry<=%s ORDER BY scheduled_entry",(now.isoformat(),)); rows=q.fetchall()
    for sid,direction,entry_text in rows:
        try:
            entry_time=pd.to_datetime(entry_text,utc=True).to_pydatetime()
            if now-entry_time>timedelta(seconds=45):
                with conn.cursor() as q: q.execute("UPDATE v6_gold_signals SET status='MISSED' WHERE id=%s",(sid,))
                conn.commit(); logging.warning('Scheduled gold entry %s missed; no late entry sent.',sid); continue
            bars=closed_fresh(fetch_bars(SYMBOL,'1min',30)); latest=bars.iloc[-1]
            actual_price=float(latest.close)
            expiry=entry_time+timedelta(minutes=1)
            with conn.cursor() as q:
                q.execute("UPDATE v6_gold_signals SET status='ENTERED',entry_price=%s WHERE id=%s",(actual_price,sid))
                q.execute("INSERT INTO v6_gold_outcomes(signal_id,actual_entry_time,actual_entry_price,expiry_time) VALUES(%s,%s,%s,%s) ON CONFLICT(signal_id) DO NOTHING",(sid,now.isoformat(),actual_price,expiry.isoformat()))
            conn.commit()
            send_telegram(f'⏱️ FADALON GOLD ENTRY CHECK\nXAU/USD {direction}\nReference price at check: {actual_price}\nScheduled entry: {entry_time.astimezone(GHANA).strftime("%-I:%M %p")} GMT\nOne-minute expiry target: {expiry.astimezone(GHANA).strftime("%-I:%M %p")} GMT\nReference feed only; verify your broker quote. This is not an automated execution.')
        except Exception:
            conn.rollback(); logging.exception('Could not validate scheduled gold entry %s',sid)

def resolve_outcomes(conn):
    now=datetime.now(timezone.utc)
    with conn.cursor() as q: q.execute("SELECT signal_id,actual_entry_price,expiry_time FROM v6_gold_outcomes WHERE result='PENDING' AND expiry_time<=%s",(now.isoformat(),)); rows=q.fetchall()
    if not rows: return
    bars=closed_fresh(fetch_bars(SYMBOL,'1min',30)); latest=bars.iloc[-1]; latest_time=pd.Timestamp(latest.datetime)
    for sid,entry,expiry_text in rows:
        expiry=pd.to_datetime(expiry_text,utc=True)
        # Use the first completed bar at/after expiry; explicitly label as reference approximation.
        candidates=bars[pd.to_datetime(bars.datetime,utc=True)>=expiry]
        if candidates.empty: continue
        exit_price=float(candidates.iloc[0].close)
        with conn.cursor() as q:
            q.execute("SELECT direction FROM v6_gold_signals WHERE id=%s",(sid,)); direction=q.fetchone()[0]
            result='WIN' if (exit_price>entry if direction=='BUY' else exit_price<entry) else 'LOSS' if exit_price!=entry else 'DRAW'
            q.execute("UPDATE v6_gold_outcomes SET exit_price=%s,result=%s,evaluated_at=%s WHERE signal_id=%s",(exit_price,result,now.isoformat(),sid))
            q.execute("UPDATE v6_gold_signals SET status='RESOLVED' WHERE id=%s",(sid,))
        conn.commit(); send_telegram(f'📊 FADALON GOLD REFERENCE RESULT\nXAU/USD {direction}: {result}\nEntry reference: {entry}\nExit reference: {exit_price}\nApproximate Twelve Data candle comparison, not broker execution result.')

def main():
    conn=db(); logging.info('FADALON V6 GOLD started: XAU/USD only; 2-minute scan; scheduled entry 2-5 minutes ahead; 1-minute expiry; demo/reference mode.')
    last_scan=0.0
    while True:
        try:
            process_due(conn); resolve_outcomes(conn)
            if time.monotonic()-last_scan>=SCAN_SECONDS:
                logging.info('Scanning XAU/USD only.')
                bars=closed_fresh(fetch_bars(SYMBOL,'1min',250)); sig=analyse_from_bars(SYMBOL,bars)
                logging.info('XAU/USD %s | agreement %s',sig['direction'],sig['agreement'])
                if sig['direction'] in ('BUY','SELL'): schedule_signal(conn,sig)
                last_scan=time.monotonic()
        except Exception:
            conn.rollback(); logging.exception('V6 gold cycle error; will retry on next loop.')
        time.sleep(LOOP_SECONDS)
if __name__=='__main__': main()
