import os, time, threading
from cloud_app import db, PROVIDERS, MMA_SPORT

_started = False
_lock = threading.Lock()

def _insert_football(provider_name, sport, rows, ts):
    with db() as c:
        for r in rows:
            c.execute("""INSERT INTO odds_snapshots(ts,provider,sport,event_id,home,away,commence,bookmaker,market,selection,point,price)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (ts, provider_name, sport, r.get('event_id'), r.get('home',''), r.get('away',''), r.get('commence',''), r.get('bookmaker','unknown'), r.get('market',''), r.get('selection',''), r.get('point'), float(r.get('price'))))

def _insert_mma(provider_name, rows, ts):
    with db() as c:
        for r in rows:
            c.execute("""INSERT INTO mma_cloud_quotes(ts,event_id,fighter_a,fighter_b,bookmaker,provider,market,fighter,selection,line,decimal_odds)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (ts, r.get('event_id'), r.get('home',''), r.get('away',''), r.get('bookmaker','unknown'), provider_name, r.get('market','h2h'), r.get('selection'), r.get('selection',''), r.get('point'), float(r.get('price'))))

def sync_once():
    ts=time.time(); result={'ts':ts,'mma':{},'football':{}}
    for provider_name in ('theoddsapi','bbs'):
        p=PROVIDERS.get(provider_name)
        if not p: continue
        try:
            rows=p.fetch(MMA_SPORT,'h2h')
            _insert_mma(provider_name, rows, ts)
            result['mma'][provider_name]=len(rows)
        except Exception as e:
            result['mma'][provider_name]=f'error:{type(e).__name__}'
    sports=[s.strip() for s in os.getenv('CLOUD_FOOTBALL_SPORTS','soccer_epl,soccer_efl_champ,soccer_uefa_champs_league').split(',') if s.strip()]
    p=PROVIDERS.get('theoddsapi')
    if p:
        for sport in sports:
            try:
                rows=p.fetch(sport,'h2h,totals')
                _insert_football('theoddsapi',sport,rows,ts)
                result['football'][sport]=len(rows)
            except Exception as e:
                result['football'][sport]=f'error:{type(e).__name__}'
    return result

def _loop():
    interval=max(300,int(os.getenv('CLOUD_SYNC_INTERVAL_SECONDS','600')))
    while True:
        try:
            if os.getenv('CLOUD_AUTO_SYNC','1').lower() in ('1','true','yes','on'):
                sync_once()
        except Exception:
            pass
        time.sleep(interval)

def start_background_sync():
    global _started
    with _lock:
        if _started: return False
        _started=True
        threading.Thread(target=_loop,name='edge-cloud-sync',daemon=True).start()
        return True
