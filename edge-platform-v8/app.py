"""Edge Platform – Phase 1 (data/odds infra + EV engine + football model + paper/CLV).
Run: uvicorn app:app --reload   |  Docs: http://localhost:8000/docs
No profitability is claimed: /paper/report says 'insufficient sample' until the data proves otherwise."""
import os, sqlite3, time, math, statistics, datetime, threading, uuid, json, random
from abc import ABC, abstractmethod
from collections import defaultdict
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

DB = os.getenv("DATABASE_URL", "sqlite:///edge.db").replace("sqlite:///", "")
SHARP = {"pinnacle", "betfair_ex_uk", "betfair_ex_eu", "matchbook", "smarkets"}
CFG = dict(min_ev=float(os.getenv("MIN_EV", .03)), strong_ev=.08, min_books=int(os.getenv("MIN_BOOKS", 4)),
           kelly=float(os.getenv("KELLY_FRACTION", .25)), stake_cap=float(os.getenv("STAKE_CAP", .03)))

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

with db() as c:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS odds_snapshots(id INTEGER PRIMARY KEY, ts REAL, provider TEXT, sport TEXT,
      event_id TEXT, home TEXT, away TEXT, commence TEXT, bookmaker TEXT, market TEXT, selection TEXT, point REAL, price REAL);
    CREATE INDEX IF NOT EXISTS ix_ev ON odds_snapshots(event_id, market, ts);
    CREATE TABLE IF NOT EXISTS paper_bets(id INTEGER PRIMARY KEY, ts REAL, sport TEXT, event_id TEXT, event TEXT, market TEXT,
      selection TEXT, point REAL, bookmaker TEXT, odds REAL, fair_prob REAL, model_prob REAL, model_version TEXT,
      stake REAL, closing_fair_prob REAL, clv REAL, won INTEGER);
    CREATE TABLE IF NOT EXISTS scanner_config(
      id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
      interval_seconds INTEGER NOT NULL DEFAULT 300, provider TEXT NOT NULL DEFAULT 'theoddsapi',
      sports TEXT NOT NULL DEFAULT 'soccer_epl', min_ev REAL NOT NULL DEFAULT 0.03,
      min_books INTEGER NOT NULL DEFAULT 4, max_odds_age_seconds INTEGER NOT NULL DEFAULT 180,
      last_scan REAL, next_scan REAL, updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS scanner_runs(
      id INTEGER PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL, provider TEXT, sport TEXT,
      rows INTEGER DEFAULT 0, opportunities INTEGER DEFAULT 0, error TEXT
    );
    CREATE TABLE IF NOT EXISTS scanner_opportunities(
      id INTEGER PRIMARY KEY, opportunity_key TEXT NOT NULL, detected_at REAL NOT NULL,
      run_id INTEGER, sport TEXT, event_id TEXT, event TEXT, market TEXT, selection TEXT, point REAL,
      bookmaker TEXT, odds REAL, fair_prob REAL, ev REAL, edge REAL, confidence REAL, books INTEGER,
      odds_age_seconds REAL, status TEXT NOT NULL DEFAULT 'DETECTED',
      closing_odds REAL, closing_fair_prob REAL, clv REAL
    );
    CREATE INDEX IF NOT EXISTS ix_scanner_opp_key ON scanner_opportunities(opportunity_key, detected_at);
    CREATE TABLE IF NOT EXISTS scanner_alerts(
      id INTEGER PRIMARY KEY, opportunity_key TEXT NOT NULL, created_at REAL NOT NULL,
      event TEXT, market TEXT, selection TEXT, bookmaker TEXT, odds REAL, ev REAL, message TEXT
    );
    CREATE TABLE IF NOT EXISTS immutable_events(
      id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, action TEXT NOT NULL,
      ts REAL NOT NULL, payload TEXT NOT NULL
    );
    """)

# ---------- Provider abstraction (add Betfair/Sportradar/OpticOdds by subclassing) ----------
class OddsProvider(ABC):
    name = "base"
    @abstractmethod
    def fetch(self, sport: str, markets: str, **kw) -> list[dict]: ...

class TheOddsAPI(OddsProvider):
    """the-odds-api.com v4. Free tier ~500 credits/mo; bulk endpoint gives h2h/spreads/totals only;
    BTTS & player props need the per-event endpoint; historical odds is a paid tier; no UFC method markets."""
    name = "theoddsapi"
    def fetch(self, sport, markets="h2h,totals", **kw):
        r = httpx.get(f"https://api.the-odds-api.com/v4/sports/{sport}/odds/", timeout=30,
                      params=dict(apiKey=os.getenv("ODDS_API_KEY", ""), regions="uk,eu", markets=markets, oddsFormat="decimal"))
        r.raise_for_status()
        return [dict(event_id=e["id"], home=e["home_team"], away=e["away_team"], commence=e["commence_time"],
                     bookmaker=b["key"], market=m["key"], selection=o["name"], point=o.get("point"), price=o["price"])
                for e in r.json() for b in e["bookmakers"] for m in b["markets"] for o in m["outcomes"]]

    def fetch_historical(self, sport, snapshot_time, markets="h2h,totals", **kw):
        key=os.getenv("ODDS_API_KEY", "")
        if not key: raise RuntimeError("ODDS_API_KEY is not configured")
        r=httpx.get(f"https://api.the-odds-api.com/v4/historical/sports/{sport}/odds/", timeout=45,
                    params=dict(apiKey=key, regions="uk", markets=markets, oddsFormat="decimal", date=snapshot_time))
        r.raise_for_status(); payload=r.json(); events=payload.get("data", payload if isinstance(payload,list) else [])
        rows=[]
        for e in events:
            for b in e.get("bookmakers",[]):
                for m in b.get("markets",[]):
                    for o in m.get("outcomes",[]):
                        rows.append(dict(event_id=e["id"], home=e["home_team"], away=e["away_team"], commence=e["commence_time"],
                            bookmaker=b["key"], market=m["key"], selection=o["name"], point=o.get("point"), price=o["price"]))
        return rows, payload.get("timestamp", snapshot_time)

class APIFootball(OddsProvider):
    """API-Football (api-sports.io). Header x-apisports-key; football only; free plan = 100 req/day and may restrict seasons.
    Maps 'Match Winner', 'Goals Over/Under', 'Both Teams Score' – verify field names in your dashboard Live Tester."""
    name = "apifootball"
    LEAGUES = {"soccer_epl": 39, "soccer_spain_la_liga": 140, "soccer_italy_serie_a": 135, "soccer_germany_bundesliga": 78,
               "soccer_france_ligue_one": 61, "soccer_uefa_champs_league": 2, "soccer_efl_champ": 40}
    def fetch(self, sport, markets="", date="", season=0, **kw):
        lg = self.LEAGUES.get(sport)
        if not lg: raise HTTPException(400, "API-Football covers football only")
        t = datetime.date.today(); date = date or t.isoformat(); season = season or (t.year if t.month >= 7 else t.year - 1)
        H = {"x-apisports-key": os.getenv("API_FOOTBALL_KEY", "")}
        def get(path, **q):
            d = httpx.get("https://v3.football.api-sports.io" + path, headers=H, params=q, timeout=30).json()
            if d.get("errors"): raise HTTPException(502, str(d["errors"]))
            return d
        fx = {x["fixture"]["id"]: x for x in get("/fixtures", league=lg, season=season, date=date)["response"]}
        rows, page, pages = [], 1, 1
        while page <= min(pages, 3):
            d = get("/odds", league=lg, season=season, date=date, page=page); pages = d.get("paging", {}).get("total", 1); page += 1
            for o in d["response"]:
                f = fx.get(o["fixture"]["id"])
                if not f: continue
                h, a = f["teams"]["home"]["name"], f["teams"]["away"]["name"]
                base = dict(event_id=str(o["fixture"]["id"]), home=h, away=a, commence=f["fixture"]["date"], point=None)
                for b in o["bookmakers"]:
                    bk = "".join(ch for ch in b["name"].lower() if ch.isalnum())
                    for bt in b["bets"]:
                        for v in bt["values"]:
                            px = float(v["odd"])
                            if bt["name"] == "Match Winner":
                                rows.append({**base, "bookmaker": bk, "market": "h2h", "selection": {"Home": h, "Away": a}.get(v["value"], "Draw"), "price": px})
                            elif bt["name"] == "Goals Over/Under":
                                side, pt = str(v["value"]).split(" ")
                                rows.append({**base, "bookmaker": bk, "market": "totals", "selection": side, "point": float(pt), "price": px})
                            elif bt["name"] == "Both Teams Score":
                                rows.append({**base, "bookmaker": bk, "market": "btts", "selection": v["value"], "price": px})
        return rows

    def fetch_results(self, sport, season):
        lg=self.LEAGUES.get(sport)
        if not lg: raise RuntimeError("API-Football covers football only")
        key=os.getenv("API_FOOTBALL_KEY", "")
        if not key: raise RuntimeError("API_FOOTBALL_KEY is not configured")
        H={"x-apisports-key":key}
        r=httpx.get("https://v3.football.api-sports.io/fixtures",headers=H,params={"league":lg,"season":season,"status":"FT-AET-PEN","timezone":"UTC"},timeout=45)
        r.raise_for_status(); d=r.json()
        if d.get("errors"): raise RuntimeError(str(d["errors"]))
        out=[]
        for x in d.get("response",[]):
            fx=x["fixture"]; goals=x.get("goals",{})
            if goals.get("home") is None or goals.get("away") is None: continue
            out.append(dict(event_id=str(fx["id"]),sport=sport,kickoff=parse_ts(fx["date"]),home=x["teams"]["home"]["name"],away=x["teams"]["away"]["name"],
                            home_team_id=canonical_team_id(x["teams"]["home"]["name"]),away_team_id=canonical_team_id(x["teams"]["away"]["name"]),
                            home_goals=int(goals["home"]),away_goals=int(goals["away"])))
        return out

PROVIDERS = {"theoddsapi": TheOddsAPI(), "apifootball": APIFootball()}

# ---------- Maths ----------
def devig(prices):
    """Power-method margin removal (handles favourite-longshot bias)."""
    p = [1 / x for x in prices]
    if sum(p) <= 1: return [x / sum(p) for x in p]
    lo, hi = 1.0, 30.0
    for _ in range(60):
        k = (lo + hi) / 2
        lo, hi = (k, hi) if sum(x ** k for x in p) > 1 else (lo, k)
    return [x ** ((lo + hi) / 2) for x in p]

def kelly(p, odds):
    full = max(0.0, (p * odds - 1) / (odds - 1))
    return dict(full=full, half=full / 2, quarter=full / 4, recommended=min(CFG["stake_cap"], full * CFG["kelly"]))

def consensus(rows):
    """{(event,market,point,selection): fair prob (sharp-weighted), dispersion, books, best soft price}."""
    g = defaultdict(lambda: defaultdict(dict)); meta = {}
    for r in rows:
        k = (r["event_id"], r["market"], r["point"]); g[k][r["bookmaker"]][r["selection"]] = r["price"]
        meta[r["event_id"]] = f'{r["home"]} v {r["away"]}'
    out = {}
    for (ev, mk, pt), books in g.items():
        n = max(len(b) for b in books.values())
        if n < 2: continue  # one-sided markets cannot be de-margined; skipped rather than assigned fair=1.0
        names = list(next(b for b in books.values() if len(b) == n))
        est = defaultdict(list); best = {}
        for bk, pr in books.items():
            if not all(s in pr for s in names): continue
            f = devig([pr[s] for s in names])
            for s, fp in zip(names, f): est[s] += [(fp, 3 if bk in SHARP else 1)]
            for s in names:
                if bk not in SHARP and pr[s] > best.get(s, (0, ""))[0]: best[s] = (pr[s], bk)
        for s, v in est.items():
            w = sum(x[1] for x in v); fair = sum(a * b for a, b in v) / w
            sd = statistics.pstdev([a for a, _ in v]) if len(v) > 1 else .1
            out[(ev, mk, pt, s)] = dict(event=meta[ev], fair=fair, sd=sd, n=len(v), best=best.get(s))
    return out

def latest_rows(sport, event_id=None):
    with db() as c:
        ts = c.execute("SELECT MAX(ts) FROM odds_snapshots WHERE sport=?", (sport,)).fetchone()[0]
        q = "SELECT * FROM odds_snapshots WHERE sport=? AND ts=?" + (" AND event_id=?" if event_id else "")
        return [dict(r) for r in c.execute(q, (sport, ts) + ((event_id,) if event_id else ()))] if ts else []

# ---------- Football: Dixon-Coles scoreline matrix ----------
def pois(l, k):
    p = math.exp(-l)
    for i in range(1, k + 1): p *= l / i
    return p

class FBIn(BaseModel):
    home_xg_for: float; home_xg_against: float; away_xg_for: float; away_xg_against: float
    league_avg: float = 1.4; home_adv: float = 1.12; rho: float = -0.08

def football(m: FBIn):
    lh = m.home_xg_for / m.league_avg * m.away_xg_against / m.league_avg * m.league_avg * m.home_adv
    la = m.away_xg_for / m.league_avg * m.home_xg_against / m.league_avg * m.league_avg / math.sqrt(m.home_adv)
    M = {}
    for i in range(11):
        for j in range(11):
            t = 1 - lh * la * m.rho if (i, j) == (0, 0) else 1 + lh * m.rho if (i, j) == (0, 1) else \
                1 + la * m.rho if (i, j) == (1, 0) else 1 - m.rho if (i, j) == (1, 1) else 1
            M[i, j] = pois(lh, i) * pois(la, j) * t
    z = sum(M.values()); M = {k: v / z for k, v in M.items()}
    s = lambda f: sum(v for (i, j), v in M.items() if f(i, j))
    return dict(lambda_home=lh, lambda_away=la, home=s(lambda i, j: i > j), draw=s(lambda i, j: i == j), away=s(lambda i, j: i < j),
                over={str(x + .5): s(lambda i, j, x=x: i + j > x) for x in (0, 1, 2, 3, 4)}, btts=s(lambda i, j: i and j),
                ah_home_minus_0_5=s(lambda i, j: i > j), ah_home_plus_0_5=s(lambda i, j: i >= j),
                top_scores=[dict(score=f"{i}-{j}", p=v) for (i, j), v in sorted(M.items(), key=lambda x: -x[1])[:6]])

# ---------- API ----------
app = FastAPI(title="Edge Platform – Automated Odds Monitor")

# Browser dashboard / GitHub Pages support. Restrict this in production with ALLOWED_ORIGINS.
_origins = [x.strip() for x in os.getenv("ALLOWED_ORIGINS", "*").split(",") if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])

SCANNER = {
    "enabled": False,
    "interval_seconds": int(os.getenv("SCANNER_INTERVAL_SECONDS", "300")),
    "provider": os.getenv("SCANNER_PROVIDER", "theoddsapi"),
    "sports": [x.strip() for x in os.getenv("SCANNER_SPORTS", "soccer_epl").split(",") if x.strip()],
    "min_ev": float(os.getenv("SCANNER_MIN_EV", "0.03")),
    "min_books": int(os.getenv("SCANNER_MIN_BOOKS", "4")),
    "max_odds_age_seconds": int(os.getenv("SCANNER_MAX_ODDS_AGE", "180")),
    "last_scan": None, "next_scan": None, "last_error": None,
}
_scanner_lock = threading.Lock()
_scanner_stop = threading.Event()

def _load_scanner_config():
    with db() as c:
        row = c.execute("SELECT * FROM scanner_config WHERE id=1").fetchone()
        if not row:
            c.execute("INSERT INTO scanner_config(id,enabled,interval_seconds,provider,sports,min_ev,min_books,max_odds_age_seconds,updated_at) VALUES(1,?,?,?,?,?,?,?,?)",
                      (int(os.getenv("AUTO_SCANNER","0").lower() in ("1","true","yes","on")), SCANNER["interval_seconds"], SCANNER["provider"], ",".join(SCANNER["sports"]), SCANNER["min_ev"], SCANNER["min_books"], SCANNER["max_odds_age_seconds"], time.time()))
            row = c.execute("SELECT * FROM scanner_config WHERE id=1").fetchone()
        for k in ("enabled","interval_seconds","provider","sports","min_ev","min_books","max_odds_age_seconds","last_scan","next_scan"):
            if k in row.keys(): SCANNER[k] = row[k]
        SCANNER["enabled"] = bool(SCANNER["enabled"])
        SCANNER["sports"] = [x.strip() for x in str(SCANNER["sports"]).split(",") if x.strip()]

_load_scanner_config()

def _persist_scanner():
    with db() as c:
        c.execute("UPDATE scanner_config SET enabled=?,interval_seconds=?,provider=?,sports=?,min_ev=?,min_books=?,max_odds_age_seconds=?,last_scan=?,next_scan=?,updated_at=? WHERE id=1",
                  (int(SCANNER["enabled"]), int(SCANNER["interval_seconds"]), SCANNER["provider"], ",".join(SCANNER["sports"]), float(SCANNER["min_ev"]), int(SCANNER["min_books"]), int(SCANNER["max_odds_age_seconds"]), SCANNER.get("last_scan"), SCANNER.get("next_scan"), time.time()))

def _bookmaker_coverage(rows):
    return sorted({r["bookmaker"] for r in rows})

def run_scanner_once(sport: str | None = None):
    """Fetch one snapshot, persist it, and persist qualifying opportunities. Never places bets."""
    provider = SCANNER["provider"]; sport = sport or SCANNER["sports"][0]
    started = time.time(); run_id = None
    with db() as c:
        cur = c.execute("INSERT INTO scanner_runs(started_at,provider,sport) VALUES(?,?,?)", (started, provider, sport)); run_id = cur.lastrowid
    try:
        if provider not in PROVIDERS: raise RuntimeError(f"Unknown provider: {provider}")
        if provider == "theoddsapi" and not os.getenv("ODDS_API_KEY"): raise RuntimeError("ODDS_API_KEY is not configured")
        if provider == "apifootball" and not os.getenv("API_FOOTBALL_KEY"): raise RuntimeError("API_FOOTBALL_KEY is not configured")
        kwargs = {}
        if provider == "apifootball":
            now = datetime.datetime.now(datetime.timezone.utc)
            kwargs.update(date=now.date().isoformat(), season=now.year if now.month >= 7 else now.year-1)
        rows = PROVIDERS[provider].fetch(sport, "h2h,totals", **kwargs)
        ts = time.time()
        with db() as c:
            c.executemany("INSERT INTO odds_snapshots(ts,provider,sport,event_id,home,away,commence,bookmaker,market,selection,point,price) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              [(ts,provider,sport,r["event_id"],r["home"],r["away"],r["commence"],r["bookmaker"],r["market"],r["selection"],r.get("point"),r["price"]) for r in rows])
        gate=_latest_gate(sport)
        gate_report=json.loads(gate["report_json"]) if gate else None
        gate_cfg=_gate_config()
        gate_allows=not bool(gate_cfg["enabled"]) or bool(gate_report and gate_report.get("eligible"))
        candidates=[]
        for (ev,mk,pt,sel), cns in consensus(rows).items():
            if not cns["best"] or cns["n"] < int(SCANNER["min_books"]): continue
            odds,bk=cns["best"]
            evv=cns["fair"]*odds-1
            if evv < float(SCANNER["min_ev"]): continue
            if not gate_allows: continue
            confidence=100*min(1,cns["n"]/8)*max(0,1-cns["sd"]/.04)
            # The snapshot is fresh at detection; this is later recomputed from stored timestamps.
            candidates.append(dict(event_id=ev,event=cns["event"],market=mk,selection=sel,point=pt,bookmaker=bk,odds=odds,
                                   fair_prob=cns["fair"],ev=evv,edge=cns["fair"]-1/odds,confidence=confidence,books=cns["n"],odds_age_seconds=0))
        with db() as c:
            for x in candidates:
                key="|".join(map(str,[sport,x["event_id"],x["market"],x["point"],x["selection"]]))
                c.execute("INSERT INTO scanner_opportunities(opportunity_key,detected_at,run_id,sport,event_id,event,market,selection,point,bookmaker,odds,fair_prob,ev,edge,confidence,books,odds_age_seconds,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (key,ts,run_id,sport,x["event_id"],x["event"],x["market"],x["selection"],x["point"],x["bookmaker"],x["odds"],x["fair_prob"],x["ev"],x["edge"],x["confidence"],x["books"],0,"DETECTED"))
                # Alert only when a new opportunity appears or EV/price changes materially.
                prev=c.execute("SELECT odds,ev FROM scanner_opportunities WHERE opportunity_key=? AND id != last_insert_rowid() ORDER BY id DESC LIMIT 1",(key,)).fetchone()
                should_alert = prev is None or abs(float(prev[0])-x["odds"]) >= .02 or abs(float(prev[1])-x["ev"]) >= .01
                if should_alert:
                    msg=f'{x["event"]} | {x["market"]} | {x["selection"]} | {x["bookmaker"]} @ {x["odds"]:.2f} | EV {x["ev"]*100:.1f}% | confidence {x["confidence"]:.0f}'
                    c.execute("INSERT INTO scanner_alerts(opportunity_key,created_at,event,market,selection,bookmaker,odds,ev,message) VALUES(?,?,?,?,?,?,?,?,?)",
                              (key,ts,x["event"],x["market"],x["selection"],x["bookmaker"],x["odds"],x["ev"],msg))
            c.execute("UPDATE scanner_runs SET finished_at=?,rows=?,opportunities=? WHERE id=?",(time.time(),len(rows),len(candidates),run_id))
        SCANNER["last_scan"]=ts; SCANNER["next_scan"]=ts+int(SCANNER["interval_seconds"]); SCANNER["last_error"]=None; _persist_scanner()
        return dict(run_id=run_id, rows=len(rows), opportunities=len(candidates), bookmakers=_bookmaker_coverage(rows), timestamp=ts)
    except Exception as e:
        with db() as c: c.execute("UPDATE scanner_runs SET finished_at=?,error=? WHERE id=?",(time.time(),str(e),run_id))
        SCANNER["last_error"]=str(e); SCANNER["next_scan"]=time.time()+int(SCANNER["interval_seconds"]); _persist_scanner()
        raise

def _scanner_worker():
    while not _scanner_stop.is_set():
        if SCANNER.get("enabled") and time.time() >= (SCANNER.get("next_scan") or 0):
            with _scanner_lock:
                for sport in list(SCANNER["sports"]):
                    if not SCANNER.get("enabled"): break
                    try: run_scanner_once(sport)
                    except Exception: pass
        _scanner_stop.wait(1)

threading.Thread(target=_scanner_worker, name="edge-scanner", daemon=True).start()

@app.get("/scanner/status")
def scanner_status():
    with db() as c:
        providers=c.execute("SELECT bookmaker,COUNT(*) n FROM odds_snapshots GROUP BY bookmaker ORDER BY n DESC").fetchall()
        runs=c.execute("SELECT * FROM scanner_runs ORDER BY id DESC LIMIT 10").fetchall()
        alerts=c.execute("SELECT * FROM scanner_alerts ORDER BY id DESC LIMIT 20").fetchall()
        opps=c.execute("SELECT * FROM scanner_opportunities ORDER BY id DESC LIMIT 100").fetchall()
    gate=_latest_gate(SCANNER["sports"][0] if SCANNER.get("sports") else "soccer_epl")
    return {"enabled":SCANNER["enabled"],"validated_signal_gate":(json.loads(gate["report_json"]) if gate else None),"interval_seconds":SCANNER["interval_seconds"],"provider":SCANNER["provider"],"sports":SCANNER["sports"],"min_ev":SCANNER["min_ev"],"min_books":SCANNER["min_books"],"max_odds_age_seconds":SCANNER["max_odds_age_seconds"],"last_scan":SCANNER.get("last_scan"),"next_scan":SCANNER.get("next_scan"),"last_error":SCANNER.get("last_error"),"bookmakers":[dict(x) for x in providers],"runs":[dict(x) for x in runs],"alerts":[dict(x) for x in alerts],"opportunities":[dict(x) for x in opps]}

class ScannerConfig(BaseModel):
    enabled: bool | None = None
    interval_seconds: int | None = None
    provider: str | None = None
    sports: list[str] | None = None
    min_ev: float | None = None
    min_books: int | None = None
    max_odds_age_seconds: int | None = None

@app.post("/scanner/config")
def scanner_config(cfg: ScannerConfig):
    if cfg.interval_seconds is not None:
        if cfg.interval_seconds < 60: raise HTTPException(400,"Minimum scanner interval is 60 seconds")
        SCANNER["interval_seconds"]=cfg.interval_seconds
    if cfg.provider is not None:
        if cfg.provider not in PROVIDERS: raise HTTPException(400,"Unsupported provider")
        SCANNER["provider"]=cfg.provider
    if cfg.sports is not None:
        if not cfg.sports: raise HTTPException(400,"At least one sport is required")
        SCANNER["sports"]=cfg.sports
    if cfg.min_ev is not None: SCANNER["min_ev"]=max(0,float(cfg.min_ev))
    if cfg.min_books is not None: SCANNER["min_books"]=max(2,int(cfg.min_books))
    if cfg.max_odds_age_seconds is not None: SCANNER["max_odds_age_seconds"]=max(15,int(cfg.max_odds_age_seconds))
    if cfg.enabled is not None:
        SCANNER["enabled"]=bool(cfg.enabled)
        if SCANNER["enabled"]: SCANNER["next_scan"]=0
        else: SCANNER["next_scan"]=None
    _persist_scanner()
    return scanner_status()

@app.post("/scanner/scan")
def scanner_scan(sport: str | None = None):
    with _scanner_lock:
        return run_scanner_once(sport)

@app.get("/scanner/opportunities")
def scanner_opportunities(limit: int = 100, min_ev: float | None = None):
    limit=max(1,min(500,int(limit)))
    with db() as c:
        q="SELECT * FROM scanner_opportunities WHERE status='DETECTED'"; args=[]
        if min_ev is not None: q += " AND ev>=?"; args.append(min_ev)
        q += " ORDER BY ev DESC, confidence DESC LIMIT ?"; args.append(limit)
        return [dict(x) for x in c.execute(q,args)]

@app.get("/scanner/alerts")
def scanner_alerts(limit: int = 50):
    with db() as c: return [dict(x) for x in c.execute("SELECT * FROM scanner_alerts ORDER BY id DESC LIMIT ?",(max(1,min(500,int(limit))),))]


@app.post("/ingest/{sport}")
def ingest(sport: str, provider: str = "theoddsapi", markets: str = "h2h,totals", date: str = "", season: int = 0):
    need = "API_FOOTBALL_KEY" if provider == "apifootball" else "ODDS_API_KEY"
    if not os.getenv(need): raise HTTPException(400, f"Set {need} in environment")
    rows, ts = PROVIDERS[provider].fetch(sport, markets, date=date, season=season), time.time()
    with db() as c:
        c.executemany("INSERT INTO odds_snapshots(ts,provider,sport,event_id,home,away,commence,bookmaker,market,selection,point,price)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      [(ts, provider, sport, r["event_id"], r["home"], r["away"], r["commence"], r["bookmaker"], r["market"],
                        r["selection"], r["point"], r["price"]) for r in rows])
    return dict(snapshot_ts=ts, rows=len(rows))

@app.get("/opportunities/{sport}")
def opportunities(sport: str, min_ev: float = CFG["min_ev"], min_books: int = CFG["min_books"]):
    """Market-consensus fair price vs best soft-book price. NOT a proven edge until CLV data says so."""
    res = []
    for (ev, mk, pt, sel), c in consensus(latest_rows(sport)).items():
        if not c["best"] or c["n"] < min_books: continue
        odds, bk = c["best"]; e = c["fair"] * odds - 1
        if e < min_ev: continue
        conf = round(100 * min(1, c["n"] / 8) * max(0, 1 - c["sd"] / .04))
        tier = ("Strong EV" if e >= CFG["strong_ev"] else "Qualifying EV") if conf >= 60 else "High-uncertainty EV"
        res.append(dict(event_id=ev, event=c["event"], market=mk, point=pt, selection=sel, best_odds=odds, bookmaker=bk,
                        fair_prob=round(c["fair"], 4), fair_odds=round(1 / c["fair"], 3), edge=round(c["fair"] - 1 / odds, 4),
                        ev=round(e, 4), confidence=conf, books=c["n"], tier=tier, kelly=kelly(c["fair"], odds)))
    return sorted(res, key=lambda r: (-r["confidence"] * r["ev"])) or "NO QUALIFYING BET"

@app.get("/movement/{event_id}")
def movement(event_id: str, market: str = "h2h"):
    """Observed price history per bookmaker/selection (no interpretation as 'sharp money')."""
    with db() as c:
        return [dict(r) for r in c.execute("SELECT ts,bookmaker,selection,point,price FROM odds_snapshots "
                                           "WHERE event_id=? AND market=? ORDER BY ts", (event_id, market))]

@app.post("/model/football")
def model_football(m: FBIn): return football(m)

class Paper(BaseModel):
    sport: str; event_id: str; market: str; selection: str; point: float | None = None
    bookmaker: str; odds: float; fair_prob: float; model_prob: float | None = None
    model_version: str = "market-consensus-v0"; stake: float = 0

@app.post("/paper")
def paper(b: Paper):
    with db() as c:
        ev = c.execute("SELECT home||' v '||away FROM odds_snapshots WHERE event_id=? LIMIT 1", (b.event_id,)).fetchone()
        now=time.time()
        cur = c.execute("INSERT INTO paper_bets(ts,sport,event_id,event,market,selection,point,bookmaker,odds,fair_prob,model_prob,model_version,stake)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (now, b.sport, b.event_id, ev[0] if ev else "", b.market,
                        b.selection, b.point, b.bookmaker, b.odds, b.fair_prob, b.model_prob, b.model_version, b.stake))
        c.execute("INSERT INTO immutable_events(entity_type,entity_id,action,ts,payload) VALUES(?,?,?,?,?)",
                  ("paper_bet",str(cur.lastrowid),"PREDICTION",now,json.dumps({"sport":b.sport,"event_id":b.event_id,"market":b.market,"selection":b.selection,"odds":b.odds,"fair_prob":b.fair_prob,"model_prob":b.model_prob,"model_version":b.model_version,"stake":b.stake})))
    return dict(id=cur.lastrowid)

@app.post("/paper/{bet_id}/close")
def close(bet_id: int):
    """Call just before kick-off after a fresh /ingest: stores the closing consensus fair prob and CLV."""
    with db() as c:
        b = c.execute("SELECT * FROM paper_bets WHERE id=?", (bet_id,)).fetchone()
        if not b: raise HTTPException(404)
        row = c.execute("SELECT commence FROM odds_snapshots WHERE event_id=? ORDER BY ts DESC LIMIT 1", (b["event_id"],)).fetchone()
        if row and row["commence"]:
            try:
                kickoff=datetime.datetime.fromisoformat(str(row["commence"]).replace("Z","+00:00")).timestamp()
                if time.time() >= kickoff: raise HTTPException(409, "Kick-off has passed; this cannot be recorded as a closing snapshot")
            except ValueError: pass
        cons = consensus(latest_rows(b["sport"], b["event_id"])).get((b["event_id"], b["market"], b["point"], b["selection"]))
        if not cons: raise HTTPException(409, "No closing snapshot – ingest first")
        clv = b["odds"] * cons["fair"] - 1
        # Prediction rows remain immutable. Store the closing observation as an append-only event.
        c.execute("INSERT INTO immutable_events(entity_type,entity_id,action,ts,payload) VALUES(?,?,?,?,?)",
                  ("paper_bet",str(bet_id),"CLOSE",time.time(),json.dumps({"closing_fair_prob":cons["fair"],"clv":clv})))
    return dict(closing_fair_prob=cons["fair"], clv=clv)

@app.post("/paper/{bet_id}/result")
def result(bet_id: int, won: bool):
    with db() as c:
        if not c.execute("SELECT 1 FROM paper_bets WHERE id=?", (bet_id,)).fetchone(): raise HTTPException(404)
        c.execute("INSERT INTO immutable_events(entity_type,entity_id,action,ts,payload) VALUES(?,?,?,?,?)",
                  ("paper_bet",str(bet_id),"RESULT",time.time(),json.dumps({"won":bool(won)})))
    return "ok"

@app.get("/paper/report")
def report():
    with db() as c:
        bets=[dict(r) for r in c.execute("SELECT * FROM paper_bets")]
        events=[dict(r) for r in c.execute("SELECT entity_id,action,payload,ts FROM immutable_events WHERE entity_type='paper_bet' ORDER BY ts")]
    state={}
    for e in events:
        sid=int(e["entity_id"]); payload=json.loads(e["payload"]); state.setdefault(sid,{})
        if e["action"]=="CLOSE": state[sid].update(payload)
        elif e["action"]=="RESULT": state[sid]["won"]=payload.get("won")
    for b in bets: b.update(state.get(b["id"],{}))
    clv=[b["clv"] for b in bets if b.get("clv") is not None]
    st=[b for b in bets if b.get("won") is not None and b["stake"]]
    prof=sum(b["stake"]*(b["odds"]-1) if b["won"] else -b["stake"] for b in st)
    mp=[(b["model_prob"],b["won"]) for b in bets if b["model_prob"] is not None and b.get("won") is not None]
    n=len(clv); se=statistics.stdev(clv)/math.sqrt(n) if n>2 else None
    verdict="INSUFFICIENT SAMPLE – no edge can be claimed (need several hundred bets with CLV)." if n<300 else ("Positive CLV, statistically significant (>2 s.e.)." if se and statistics.mean(clv)>2*se else "No statistically significant CLV edge.")
    return dict(bets=len(bets),with_clv=n,avg_clv=statistics.mean(clv) if clv else None,clv_std_err=se,
                roi=prof/sum(b["stake"] for b in st) if st else None,
                brier=statistics.mean((p-w)**2 for p,w in mp) if mp else None,verdict=verdict)

# ---------- UFC/MMA: method + round model (anchor win prob to the devigged market; don't guess it) ----------
class MMAIn(BaseModel):
    p_a: float                      # P(A wins), e.g. devigged consensus moneyline adjusted by your view
    a_ko: float; a_sub: float       # A's share of wins by KO/TKO and by submission (rest = decision)
    b_ko: float; b_sub: float
    rounds: int = 3
    finish_round_weights: list[float] | None = None   # P(finish occurs in round r | finish); default is generic, replace with fitted values

@app.post("/model/mma")
def model_mma(m: MMAIn):
    w = m.finish_round_weights or ({3: [.42, .33, .25], 5: [.33, .25, .20, .13, .09]}[m.rounds])
    w = [x / sum(w) for x in w]
    pb = 1 - m.p_a
    a = dict(ko=m.p_a * m.a_ko, sub=m.p_a * m.a_sub, dec=m.p_a * max(0, 1 - m.a_ko - m.a_sub))
    b = dict(ko=pb * m.b_ko, sub=pb * m.b_sub, dec=pb * max(0, 1 - m.b_ko - m.b_sub))
    dist = a["dec"] + b["dec"]; fin = 1 - dist
    over = {f"{r + .5}": dist + fin * sum(w[r + 1:]) for r in range(m.rounds - 1)}
    return dict(a_wins=m.p_a, b_wins=pb, a=a, b=b, goes_distance=dist, ends_inside=fin, over_rounds=over,
                under_rounds={k: 1 - v for k, v in over.items()}, ko_tko=a["ko"] + b["ko"], submission=a["sub"] + b["sub"],
                note="Method splits should blend each fighter's finish rate with the opponent's rate of being finished; validate against results before trusting.")

# ---------- Dashboard ----------
from fastapi.responses import FileResponse
@app.get("/", include_in_schema=False)
def home(): return FileResponse(os.path.join(os.path.dirname(__file__), "dashboard.html"))

# ---------- Phase 3: research/forecasting engine ----------
import re, hashlib

with db() as c:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS football_outcomes(
      id INTEGER PRIMARY KEY, canonical_event_id TEXT UNIQUE NOT NULL, sport TEXT NOT NULL,
      event_id TEXT, kickoff REAL NOT NULL, home TEXT NOT NULL, away TEXT NOT NULL,
      home_team_id TEXT NOT NULL, away_team_id TEXT NOT NULL, home_goals INTEGER NOT NULL,
      away_goals INTEGER NOT NULL, source TEXT, ingested_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_outcomes_kickoff ON football_outcomes(kickoff);
    CREATE TABLE IF NOT EXISTS research_models(
      id INTEGER PRIMARY KEY, fitted_at REAL NOT NULL, sport TEXT NOT NULL, model_version TEXT NOT NULL,
      train_start REAL, train_end REAL, observations INTEGER NOT NULL, brier REAL, logloss REAL,
      elo_weight REAL, dc_weight REAL, params_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS research_predictions(
      id INTEGER PRIMARY KEY, created_at REAL NOT NULL, canonical_event_id TEXT NOT NULL,
      kickoff REAL NOT NULL, home TEXT NOT NULL, away TEXT NOT NULL, model_version TEXT NOT NULL,
      p_home REAL NOT NULL, p_draw REAL NOT NULL, p_away REAL NOT NULL,
      market_home REAL, market_draw REAL, market_away REAL, best_home REAL, best_draw REAL, best_away REAL,
      outcome INTEGER, brier REAL, logloss REAL
    );
    CREATE INDEX IF NOT EXISTS ix_predictions_event ON research_predictions(canonical_event_id);
    """)

TEAM_RE = re.compile(r"[^a-z0-9]+")
def canonical_team_id(name: str) -> str:
    return TEAM_RE.sub("", str(name).lower())

def parse_ts(value):
    if isinstance(value, (int,float)): return float(value)
    s=str(value).replace("Z", "+00:00")
    try: return datetime.datetime.fromisoformat(s).timestamp()
    except Exception: return 0.0

def canonical_event_id(home, away, kickoff):
    # Provider-independent key: normalized teams + kickoff minute. Event IDs differ across feeds.
    raw=f"{canonical_team_id(home)}|{canonical_team_id(away)}|{int(kickoff//60)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:24]

def _outcome_rows(sport="soccer_epl"):
    with db() as c: return [dict(r) for r in c.execute("SELECT * FROM football_outcomes WHERE sport=? ORDER BY kickoff",(sport,))]

def ingest_outcomes(rows, sport="soccer_epl", source="manual"):
    n=0
    with db() as c:
        for r in rows:
            ko=parse_ts(r.get("kickoff") or r.get("commence"))
            if not ko: continue
            h,a=str(r["home"]),str(r["away"]); hg=int(r["home_goals"]); ag=int(r["away_goals"])
            cid=canonical_event_id(h,a,ko)
            c.execute("INSERT OR REPLACE INTO football_outcomes(canonical_event_id,sport,event_id,kickoff,home,away,home_team_id,away_team_id,home_goals,away_goals,source,ingested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (cid,sport,str(r.get("event_id",cid)),ko,h,a,canonical_team_id(h),canonical_team_id(a),hg,ag,source,time.time())); n+=1
    return n

@app.post("/research/outcomes")
def research_outcomes(rows: list[dict], sport: str="soccer_epl"):
    """Ingest settled football results. Payload: [{kickoff,home,away,home_goals,away_goals,event_id?}]."""
    return {"inserted":ingest_outcomes(rows,sport,"manual")}

@app.post("/research/ingest-results")
def research_ingest_results(sport: str="soccer_epl", date: str="", season: int=0):
    if not os.getenv("API_FOOTBALL_KEY"): raise HTTPException(400,"Set API_FOOTBALL_KEY in environment")
    p=PROVIDERS.get("apifootball")
    if not isinstance(p, APIFootball): raise HTTPException(400,"API-Football provider unavailable")
    lg=p.LEAGUES.get(sport)
    if not lg: raise HTTPException(400,"Unsupported football league")
    d=date or datetime.date.today().isoformat(); season=season or (datetime.date.fromisoformat(d).year if datetime.date.fromisoformat(d).month>=7 else datetime.date.fromisoformat(d).year-1)
    H={"x-apisports-key":os.getenv("API_FOOTBALL_KEY","")}
    r=httpx.get("https://v3.football.api-sports.io/fixtures",headers=H,params={"league":lg,"season":season,"date":d},timeout=30); r.raise_for_status(); payload=r.json()
    if payload.get("errors"): raise HTTPException(502,str(payload["errors"]))
    rows=[]
    for x in payload.get("response",[]):
        g=x.get("goals",{}); h=x.get("teams",{}).get("home",{}); a=x.get("teams",{}).get("away",{})
        if g.get("home") is None or g.get("away") is None: continue
        rows.append(dict(event_id=x["fixture"]["id"],kickoff=x["fixture"]["date"],home=h.get("name"),away=a.get("name"),home_goals=g["home"],away_goals=g["away"]))
    return {"date":d,"season":season,"inserted":ingest_outcomes(rows,sport,"api-football")}

class ResearchFitIn(BaseModel):
    sport: str="soccer_epl"
    min_observations: int=30
    validation_fraction: float=.2

class EloState:
    def __init__(self, k=20.0, home_adv=55.0, base=1500.0): self.k=k; self.home_adv=home_adv; self.base=base; self.r={}
    def rating(self,t): return self.r.get(t,self.base)
    def probs(self,h,a):
        d=self.rating(h)+self.home_adv-self.rating(a); ph=1/(1+10**(-d/400));
        # Empirical draw component: highest near equal ratings; keeps a true 3-way distribution.
        draw=.28*math.exp(-abs(d)/180)
        draw=min(draw, ph*.9, (1-ph)*.9)
        return ((1-draw)*ph, draw, (1-draw)*(1-ph))
    def update(self,h,a,result):
        ph,pd,pa=self.probs(h,a); target=1 if result==0 else .5 if result==1 else 0
        expected=ph+.5*pd; delta=self.k*(target-expected); self.r[h]=self.rating(h)+delta; self.r[a]=self.rating(a)-delta

def _elo_train(rows, k=20.0):
    s=EloState(k=k)
    for r in rows:
        result=0 if r["home_goals"]>r["away_goals"] else 1 if r["home_goals"]==r["away_goals"] else 2
        s.update(r["home_team_id"],r["away_team_id"],result)
    return s

def _fit_dc(rows, steps=900, lr=.008, reg=.015):
    teams=sorted({x["home_team_id"] for x in rows}|{r["away_team_id"] for r in rows}); idx={t:i for i,t in enumerate(teams)}; n=len(teams)
    atk=[0.0]*n; de=[0.0]*n; ha=math.log(1.12); base=math.log(max(.2, sum(r["home_goals"]+r["away_goals"] for r in rows)/(2*len(rows))))
    # Regularized Poisson log-likelihood; gradient descent on log-rate parameters.
    for _ in range(steps):
        ga=[0.0]*n; gd=[0.0]*n; gha=0.0; gb=0.0
        for r in rows:
            i,j=idx[r["home_team_id"]],idx[r["away_team_id"]]
            lh=math.exp(base+ha+atk[i]-de[j]); la=math.exp(base+atk[j]-de[i])
            gh=r["home_goals"]-lh; ga[i]+=gh; gd[j]-=gh
            ga[j]+=r["away_goals"]-la; gd[i]-=(r["away_goals"]-la)
            gha+=gh
        for i in range(n): ga[i]-=reg*atk[i]; gd[i]-=reg*de[i]
        for i in range(n): atk[i]+=lr*ga[i]/max(1,len(rows)); de[i]+=lr*gd[i]/max(1,len(rows))
        ha+=lr*gha/max(1,len(rows)); base+=lr*gb/max(1,len(rows))
    return {"atk":dict(zip(teams,atk)),"def":dict(zip(teams,de)),"home_adv":ha,"base":base}

def _dc_probs(params,h,a):
    atk=params["atk"]; de=params["def"]; base=params["base"]; ha=params["home_adv"]
    lh=math.exp(base+ha+atk.get(h,0)-de.get(a,0)); la=math.exp(base+atk.get(a,0)-de.get(h,0))
    M={}
    rho=-0.08
    for i in range(9):
        for j in range(9):
            tau=1.0
            if (i,j)==(0,0): tau=1-lh*la*rho
            elif (i,j)==(0,1): tau=1+lh*rho
            elif (i,j)==(1,0): tau=1+la*rho
            elif (i,j)==(1,1): tau=1-rho
            M[i,j]=pois(lh,i)*pois(la,j)*tau
    z=sum(M.values()); M={k:v/z for k,v in M.items()}
    return (sum(v for (i,j),v in M.items() if i>j),sum(v for (i,j),v in M.items() if i==j),sum(v for (i,j),v in M.items() if i<j))

def _brier(probs,result): return sum((p-(1 if i==result else 0))**2 for i,p in enumerate(probs))/3

def _logloss(probs,result): return -math.log(max(1e-9,probs[result]))

def _fit_ensemble(rows, validation_fraction=.2):
    if len(rows)<2: raise HTTPException(400,"Not enough outcomes")
    cut=max(1,int(len(rows)*(1-validation_fraction))); train=rows[:cut]; val=rows[cut:] or rows[-1:]
    elo=_elo_train(train); dc=_fit_dc(train)
    # Model-only calibration: choose the weight minimizing validation Brier. No market odds are used.
    best=(1e9,.5); candidates=[i/20 for i in range(21)]
    for w in candidates:
        loss=0
        for r in val:
            pe=elo.probs(r["home_team_id"],r["away_team_id"]); pd=_dc_probs(dc,r["home_team_id"],r["away_team_id"])
            p=tuple(w*a+(1-w)*b for a,b in zip(pe,pd)); result=0 if r["home_goals"]>r["away_goals"] else 1 if r["home_goals"]==r["away_goals"] else 2; loss+=_brier(p,result)
        if loss<best[0]: best=(loss,w)
    w=best[1]
    return elo,dc,w,cut

def _snapshot_market(event, kickoff):
    # Match provider-specific odds to provider-independent outcomes by normalized teams + kickoff window.
    with db() as c:
        candidates=[dict(r) for r in c.execute("SELECT * FROM odds_snapshots WHERE ts<=? ORDER BY ts DESC",(kickoff,))]
    target=None
    with db() as c:
        target=c.execute("SELECT home,away FROM football_outcomes WHERE event_id=? LIMIT 1",(str(event),)).fetchone()
    if target:
        th,ta=canonical_team_id(target[0]),canonical_team_id(target[1])
        candidates=[r for r in candidates if canonical_team_id(r["home"])==th and canonical_team_id(r["away"])==ta and abs(parse_ts(r["commence"])-kickoff)<=7200]
    if not candidates: return None
    latest_ts=max(r["ts"] for r in candidates); rows=[r for r in candidates if r["ts"]==latest_ts]
    cns=consensus(rows); out={}
    for (ev,mk,pt,sel),x in cns.items():
        if mk!="h2h": continue
        if x.get("best") and x["fair"]: out[sel]=dict(fair=x["fair"],best=x["best"][0],book=x["best"][1])
    return out

@app.post("/research/fit")
def research_fit(m: ResearchFitIn):
    rows=_outcome_rows(m.sport)
    if len(rows)<m.min_observations: raise HTTPException(400,f"Need at least {m.min_observations} settled outcomes; have {len(rows)}")
    elo,dc,w,cut=_fit_ensemble(rows,m.validation_fraction)
    train=rows[:cut]; val=rows[cut:]
    br=[]; ll=[]
    for r in val:
        pe=elo.probs(r["home_team_id"],r["away_team_id"]); pd=_dc_probs(dc,r["home_team_id"],r["away_team_id"]); p=tuple(w*a+(1-w)*b for a,b in zip(pe,pd)); y=0 if r["home_goals"]>r["away_goals"] else 1 if r["home_goals"]==r["away_goals"] else 2; br.append(_brier(p,y)); ll.append(_logloss(p,y))
    params={"elo_k":elo.k,"elo_home_adv":elo.home_adv,"elo_ratings":elo.r,"dc":dc,"ensemble_elo_weight":w,"ensemble_dc_weight":1-w}
    version=f"ensemble-elo-dc-v1-{int(time.time())}"
    with db() as c: c.execute("INSERT INTO research_models(fitted_at,sport,model_version,train_start,train_end,observations,brier,logloss,elo_weight,dc_weight,params_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(time.time(),m.sport,version,train[0]["kickoff"],train[-1]["kickoff"],len(train),statistics.mean(br) if br else None,statistics.mean(ll) if ll else None,w,1-w,json.dumps(params)))
    return {"model_version":version,"observations":len(train),"validation_observations":len(val),"brier":statistics.mean(br) if br else None,"logloss":statistics.mean(ll) if ll else None,"elo_weight":w,"dc_weight":1-w}

@app.get("/research/status")
def research_status(sport: str="soccer_epl"):
    with db() as c:
        n=c.execute("SELECT COUNT(*) FROM football_outcomes WHERE sport=?",(sport,)).fetchone()[0]
        latest=c.execute("SELECT * FROM research_models WHERE sport=? ORDER BY id DESC LIMIT 1",(sport,)).fetchone()
    return {"sport":sport,"outcomes":n,"latest_model":dict(latest) if latest else None,"note":"Model quality is empirical: use walk-forward results and CLV, not a single in-sample fit."}

@app.post("/research/backtest")
def research_backtest(sport: str="soccer_epl", min_training: int=30, retrain_every: int=25):
    rows=_outcome_rows(sport)
    if len(rows)<min_training+1: raise HTTPException(400,f"Need >{min_training} outcomes; have {len(rows)}")
    predictions=[]; br=[]; ll=[]; value=[]; bets=0
    # Strict walk-forward: each prediction only sees outcomes strictly before kickoff.
    for i in range(min_training,len(rows)):
        train=rows[:i]
        if (i-min_training)%max(1,retrain_every)==0 or not predictions:
            elo,dc,w,_=_fit_ensemble(train,.2)
        r=rows[i]; pe=elo.probs(r["home_team_id"],r["away_team_id"]); pd=_dc_probs(dc,r["home_team_id"],r["away_team_id"]); p=tuple(w*a+(1-w)*b for a,b in zip(pe,pd)); y=0 if r["home_goals"]>r["away_goals"] else 1 if r["home_goals"]==r["away_goals"] else 2
        br.append(_brier(p,y)); ll.append(_logloss(p,y)); market=_snapshot_market(r["event_id"],r["kickoff"])
        best=market or {}; best_home=best.get(r["home"],{}).get("best"); best_draw=best.get("Draw",{}).get("best"); best_away=best.get(r["away"],{}).get("best")
        if best_home and p[0]*best_home-1>=CFG["min_ev"]: value.append(p[0]*best_home-1); bets+=1
        if best_draw and p[1]*best_draw-1>=CFG["min_ev"]: value.append(p[1]*best_draw-1); bets+=1
        if best_away and p[2]*best_away-1>=CFG["min_ev"]: value.append(p[2]*best_away-1); bets+=1
        predictions.append((r,p,market))
    with db() as c:
        for r,p,market in predictions:
            y=0 if r["home_goals"]>r["away_goals"] else 1 if r["home_goals"]==r["away_goals"] else 2
            c.execute("INSERT INTO research_predictions(created_at,canonical_event_id,kickoff,home,away,model_version,p_home,p_draw,p_away,market_home,market_draw,market_away,best_home,best_draw,best_away,outcome,brier,logloss) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (time.time(),canonical_event_id(r["home"],r["away"],r["kickoff"]),r["kickoff"],r["home"],r["away"],"walk-forward-elo-dc-v1",*p,
                       (market or {}).get(r["home"],{}).get("fair"),(market or {}).get("Draw",{}).get("fair"),(market or {}).get(r["away"],{}).get("fair"),
                       (market or {}).get(r["home"],{}).get("best"),(market or {}).get("Draw",{}).get("best"),(market or {}).get(r["away"],{}).get("best"),y,_brier(p,y),_logloss(p,y)))
    return {"predictions":len(predictions),"brier":statistics.mean(br) if br else None,"logloss":statistics.mean(ll) if ll else None,"qualifying_model_ev_observations":bets,"mean_qualifying_ev":statistics.mean(value) if value else None,"note":"This is a walk-forward research result. It is not a profitability guarantee; execution, limits, line movement and selection availability are not simulated."}

@app.get("/research/predict/{event_id}")
def research_predict(event_id: str, sport: str="soccer_epl"):
    rows=_outcome_rows(sport)
    with db() as c: latest=c.execute("SELECT * FROM research_models WHERE sport=? ORDER BY id DESC LIMIT 1",(sport,)).fetchone()
    if not latest: raise HTTPException(404,"Fit a research model first")
    params=json.loads(latest["params_json"]); elo=EloState(k=params.get("elo_k",20)); elo.r=params["elo_ratings"]; dc=params["dc"]; w=params["ensemble_elo_weight"]
    with db() as c: snap=c.execute("SELECT * FROM odds_snapshots WHERE event_id=? ORDER BY ts DESC LIMIT 1",(event_id,)).fetchone()
    if not snap: raise HTTPException(404,"Event odds not found")
    h,a=snap["home"],snap["away"]; pe=elo.probs(canonical_team_id(h),canonical_team_id(a)); pd=_dc_probs(dc,canonical_team_id(h),canonical_team_id(a)); p=tuple(w*x+(1-w)*y for x,y in zip(pe,pd))
    return {"event_id":event_id,"home":h,"away":a,"model_version":latest["model_version"],"p_home":p[0],"p_draw":p[1],"p_away":p[2],"fair_odds":{"home":1/p[0],"draw":1/p[1],"away":1/p[2]},"note":"Probabilities are model estimates; compare with current verified prices and later CLV before treating as an edge."}

# ---------- Phase 4: calibration, bookmaker diagnostics, uncertainty & performance ----------
with db() as c:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS research_runs(
      id INTEGER PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL, sport TEXT, kind TEXT,
      observations INTEGER, metrics_json TEXT, error TEXT
    );
    CREATE TABLE IF NOT EXISTS bookmaker_metrics(
      id INTEGER PRIMARY KEY, calculated_at REAL NOT NULL, sport TEXT NOT NULL, bookmaker TEXT NOT NULL,
      observations INTEGER NOT NULL, brier REAL, logloss REAL, closing_brier REAL, closing_logloss REAL,
      mean_margin REAL, calibration_error REAL
    );
    CREATE INDEX IF NOT EXISTS ix_bm_metrics ON bookmaker_metrics(sport, bookmaker, calculated_at);
    CREATE TABLE IF NOT EXISTS collector_config(
      id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
      interval_seconds INTEGER NOT NULL DEFAULT 300, provider TEXT NOT NULL DEFAULT 'theoddsapi',
      sports TEXT NOT NULL DEFAULT 'soccer_epl', markets TEXT NOT NULL DEFAULT 'h2h,totals',
      lookahead_hours INTEGER NOT NULL DEFAULT 48, last_run REAL, next_run REAL, last_error TEXT, updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS collector_runs(
      id INTEGER PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL, provider TEXT, sport TEXT,
      mode TEXT, rows INTEGER DEFAULT 0, events INTEGER DEFAULT 0, error TEXT
    );
    CREATE TABLE IF NOT EXISTS data_quality_runs(
      id INTEGER PRIMARY KEY, calculated_at REAL NOT NULL, sport TEXT NOT NULL,
      report_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS canonical_events(
      id INTEGER PRIMARY KEY, sport TEXT NOT NULL, canonical_id TEXT NOT NULL UNIQUE,
      home TEXT NOT NULL, away TEXT NOT NULL, kickoff REAL NOT NULL, source_event_ids TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_canonical_events ON canonical_events(sport, kickoff);
    CREATE TABLE IF NOT EXISTS research_calibration(
      id INTEGER PRIMARY KEY, calculated_at REAL NOT NULL, sport TEXT NOT NULL, model_version TEXT,
      temperature REAL NOT NULL, ece REAL, max_ece REAL, params_json TEXT NOT NULL
    );
    """)

def _softmax_temp(probs, temperature):
    vals=[math.log(max(1e-12,p))/max(1e-6,temperature) for p in probs]
    m=max(vals); ex=[math.exp(x-m) for x in vals]; z=sum(ex)
    return tuple(x/z for x in ex)

def _fit_temperature(pred_rows, grid=None):
    if not pred_rows: return 1.0
    grid=grid or [0.50+i*0.05 for i in range(61)]
    best=(float('inf'),1.0)
    for t in grid:
        loss=0.0
        for p,y in pred_rows: loss += _logloss(_softmax_temp(p,t),y)
        loss/=len(pred_rows)
        if loss<best[0]: best=(loss,t)
    return best[1]

def _ece(pred_rows, bins=10):
    if not pred_rows: return None, None
    groups=[[] for _ in range(bins)]
    for p,y in pred_rows:
        conf=max(p); pred=max(range(3),key=lambda i:p[i]); idx=min(bins-1,int(conf*bins)); groups[idx].append((conf,pred==y))
    ece=0.0; maxe=0.0; n=len(pred_rows)
    for g in groups:
        if not g: continue
        acc=sum(int(x[1]) for x in g)/len(g); conf=sum(x[0] for x in g)/len(g); gap=abs(acc-conf)
        ece += len(g)/n*gap; maxe=max(maxe,gap)
    return ece,maxe

def _bootstrap(values, stat=lambda xs: statistics.mean(xs), n=2000):
    if not values: return None
    import random
    rng=random.Random(20260924)
    out=[]; m=len(values)
    for _ in range(min(n,10000)):
        sample=[values[rng.randrange(m)] for _ in range(m)]
        out.append(stat(sample))
    out.sort(); return dict(point=stat(values), lo=out[max(0,int(.025*len(out)))], hi=out[min(len(out)-1,int(.975*len(out))-1)])

def _outcome_lookup():
    with db() as c: return [dict(r) for r in c.execute("SELECT * FROM football_outcomes ORDER BY kickoff")]

def _market_for_outcome(outcome, closing=False):
    ko=outcome["kickoff"]; th=canonical_team_id(outcome["home"]); ta=canonical_team_id(outcome["away"])
    with db() as c:
        rows=[dict(r) for r in c.execute("SELECT * FROM odds_snapshots WHERE ts<=?",(ko,))]
    rows=[r for r in rows if canonical_team_id(r["home"])==th and canonical_team_id(r["away"])==ta and abs(parse_ts(r["commence"])-ko)<=7200 and r["market"]=="h2h"]
    if not rows: return {}
    # Providers are normally polled at slightly different instants. Choose the latest
    # pre-kickoff snapshot independently for each bookmaker rather than requiring one
    # global timestamp to match across providers.
    latest_by_book={}
    for r in rows:
        bk=r["bookmaker"]
        if bk not in latest_by_book or r["ts"]>latest_by_book[bk]["ts"]:
            latest_by_book[bk]=r
    rows=[r for bk in latest_by_book for r in rows if r["bookmaker"]==bk and r["ts"]==latest_by_book[bk]["ts"]]
    by={}
    for bk in sorted(latest_by_book):
        br=[r for r in rows if r["bookmaker"]==bk]
        names={r["selection"] for r in br};
        if len(names)<2: continue
        ordered=[]
        for name in (outcome["home"],"Draw",outcome["away"]):
            rr=next((r for r in br if r["selection"]==name),None)
            if rr: ordered.append(rr)
        if len(ordered)!=3: continue
        fair=devig([r["price"] for r in ordered])
        by[bk]={"prices":{r["selection"]:r["price"] for r in ordered},"fair":dict(zip([outcome["home"],"Draw",outcome["away"]],fair)),"ts":latest_by_book[bk]["ts"]}
    return by

def _result_index(r): return 0 if r["home_goals"]>r["away_goals"] else 1 if r["home_goals"]==r["away_goals"] else 2

def _closing_price_for_bookmaker(outcome,bk):
    m=_market_for_outcome(outcome,closing=True).get(bk); 
    if not m: return None
    key=[outcome["home"],"Draw",outcome["away"]][_result_index(outcome)]
    return m["prices"].get(key)

def _model_from_train(train):
    elo,dc,w,_=_fit_ensemble(train,.2)
    return elo,dc,w

def _predict_rows(train, rows, temperature=1.0):
    elo,dc,w=_model_from_train(train); out=[]
    for r in rows:
        pe=elo.probs(r["home_team_id"],r["away_team_id"]); pd=_dc_probs(dc,r["home_team_id"],r["away_team_id"])
        p=tuple(w*a+(1-w)*b for a,b in zip(pe,pd)); y=_result_index(r); out.append((r,_softmax_temp(p,temperature),y))
    return out

class BacktestIn(BaseModel):
    sport: str="soccer_epl"; min_training: int=60; retrain_every: int=20; min_ev: float=.03; stake_fraction: float=.01

@app.post("/research/calibrate")
def research_calibrate(sport: str="soccer_epl", min_training: int=60):
    rows=_outcome_rows(sport)
    if len(rows)<min_training+10: raise HTTPException(400,f"Need at least {min_training+10} outcomes; have {len(rows)}")
    cut=min_training
    train=rows[:cut]; val=rows[cut:]
    elo,dc,w=_model_from_train(train); raw=[]
    for r in val:
        pe=elo.probs(r["home_team_id"],r["away_team_id"]); pd=_dc_probs(dc,r["home_team_id"],r["away_team_id"])
        raw.append((tuple(w*a+(1-w)*b for a,b in zip(pe,pd)),_result_index(r)))
    t=_fit_temperature(raw); calibrated=[(_softmax_temp(p,t),y) for p,y in raw]; ece,maxe=_ece(calibrated)
    version=f"calibration-{int(time.time())}"; params={"temperature":t}
    with db() as c: c.execute("INSERT INTO research_calibration(calculated_at,sport,model_version,temperature,ece,max_ece,params_json) VALUES(?,?,?,?,?,?,?)",(time.time(),sport,version,t,ece,maxe,json.dumps(params)))
    return {"model_version":version,"temperature":t,"ece":ece,"max_ece":maxe,"validation_observations":len(val),"note":"Calibration is fit on a held-out chronological segment; it should be re-fit as new seasons arrive."}

@app.get("/research/bookmakers")
def research_bookmakers(sport: str="soccer_epl", min_observations: int=30):
    outcomes=_outcome_rows(sport); accum=defaultdict(lambda:{"p":[],"y":[],"margin":[]})
    for r in outcomes:
        markets=_market_for_outcome(r)
        y=_result_index(r); names=[r["home"],"Draw",r["away"]]
        for bk,m in markets.items():
            p=[m["fair"].get(n) for n in names]
            if any(x is None for x in p): continue
            accum[bk]["p"].append(p); accum[bk]["y"].append(y)
            raw=[1/m["prices"][n] for n in names]; accum[bk]["margin"].append(sum(raw)-1)
    result=[]
    for bk,a in accum.items():
        if len(a["p"])<min_observations: continue
        br=[_brier(p,y) for p,y in zip(a["p"],a["y"])]
        ll=[_logloss(p,y) for p,y in zip(a["p"],a["y"])]
        rows=[(p,y) for p,y in zip(a["p"],a["y"])]
        ece,maxe=_ece(rows); item=dict(bookmaker=bk,observations=len(br),brier=statistics.mean(br),logloss=statistics.mean(ll),mean_margin=statistics.mean(a["margin"]),calibration_error=ece,max_calibration_error=maxe)
        result.append(item)
        with db() as c: c.execute("INSERT INTO bookmaker_metrics(calculated_at,sport,bookmaker,observations,brier,logloss,closing_brier,closing_logloss,mean_margin,calibration_error) VALUES(?,?,?,?,?,?,?,?,?,?)",(time.time(),sport,bk,len(br),item["brier"],item["logloss"],None,None,item["mean_margin"],ece))
    return {"sport":sport,"bookmakers":result,"note":"Lower error metrics describe historical probability accuracy in this dataset; they are not a ranking of bookmakers or a guarantee of future performance."}

@app.post("/research/backtest-v2")
def research_backtest_v2(m: BacktestIn):
    rows=_outcome_rows(m.sport)
    if len(rows)<m.min_training+1: raise HTTPException(400,f"Need >{m.min_training} outcomes; have {len(rows)}")
    predictions=[]; bets=[]; br=[]; ll=[]; pnl=[]; clv=[]; equity=1.0; peak=1.0; dd=[]; model_version="walk-forward-elo-dc-v2"
    elo=dc=w=None
    for i in range(m.min_training,len(rows)):
        if i==m.min_training or (i-m.min_training)%max(1,m.retrain_every)==0: elo,dc,w=_model_from_train(rows[:i])
        r=rows[i]; pe=elo.probs(r["home_team_id"],r["away_team_id"]); pd=_dc_probs(dc,r["home_team_id"],r["away_team_id"]); p=tuple(w*a+(1-w)*b for a,b in zip(pe,pd)); y=_result_index(r); br.append(_brier(p,y)); ll.append(_logloss(p,y))
        market=_market_for_outcome(r); names=[r["home"],"Draw",r["away"]]
        for j,name in enumerate(names):
            prices=[]
            for bk,mk in market.items():
                if name in mk["prices"]: prices.append((mk["prices"][name],bk))
            if not prices: continue
            odds,bk=max(prices); ev=p[j]*odds-1
            if ev < m.min_ev: continue
            stake=min(max(m.stake_fraction,0),.05); win=1 if y==j else 0; ret=(odds-1)*stake if win else -stake; equity+=ret; peak=max(peak,equity); dd.append((peak-equity)/peak); pnl.append(ret)
            close_odds=_closing_price_for_bookmaker(r,bk)
            if close_odds: clv.append((1/odds)-(1/close_odds))
            bets.append({"event":f"{r['home']} v {r['away']}","selection":name,"bookmaker":bk,"odds":odds,"model_prob":p[j],"ev":ev,"won":bool(win),"return":ret,"closing_odds":close_odds})
        predictions.append((r,p,y))
    bboot=_bootstrap(br); lboot=_bootstrap(ll); pboot=_bootstrap(pnl) if pnl else None; cboot=_bootstrap(clv) if clv else None
    wins=sum(1 for x in pnl if x>0); total_staked=sum(m.stake_fraction for _ in pnl); roi=sum(pnl)/total_staked if total_staked else None
    return {"predictions":len(predictions),"brier":statistics.mean(br),"logloss":statistics.mean(ll),"brier_ci95":bboot,"logloss_ci95":lboot,"bets":len(bets),"wins":wins,"roi":roi,"roi_ci95":(dict(point=pboot["point"]/m.stake_fraction,lo=pboot["lo"]/m.stake_fraction,hi=pboot["hi"]/m.stake_fraction) if pboot and m.stake_fraction else None),"max_drawdown":max(dd) if dd else 0,"mean_clv":statistics.mean(clv) if clv else None,"clv_ci95":cboot,"bets_detail":bets[-500:],"note":"Walk-forward only. Historical odds availability, account limits, slippage and execution are not assumed to be frictionless."}


# ---------- Phase 5: historical data acquisition, scheduled capture & data quality ----------
def _canonical_upsert(rows, sport):
    seen={}
    for r in rows:
        try: ko=parse_ts(r["commence"])
        except Exception: continue
        cid=canonical_event_id(r["home"],r["away"],ko)
        seen[cid]=r
    with db() as c:
        for cid,r in seen.items():
            ko=parse_ts(r["commence"])
            old=c.execute("SELECT source_event_ids FROM canonical_events WHERE canonical_id=?",(cid,)).fetchone()
            ids=set(json.loads(old[0]) if old else [])
            ids.add(str(r["event_id"]))
            c.execute("INSERT INTO canonical_events(sport,canonical_id,home,away,kickoff,source_event_ids) VALUES(?,?,?,?,?,?) ON CONFLICT(canonical_id) DO UPDATE SET source_event_ids=excluded.source_event_ids",(sport,cid,r["home"],r["away"],ko,json.dumps(sorted(ids))))
    return len(seen)

def _store_rows(rows, provider, sport, ts=None):
    ts=ts or time.time()
    with db() as c:
        c.executemany("INSERT INTO odds_snapshots(ts,provider,sport,event_id,home,away,commence,bookmaker,market,selection,point,price) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
          [(ts,provider,sport,r["event_id"],r["home"],r["away"],r["commence"],r["bookmaker"],r["market"],r["selection"],r.get("point"),r["price"]) for r in rows])
    _canonical_upsert(rows,sport)
    return ts

def _capture_once(sport, provider="theoddsapi", markets="h2h,totals"):
    started=time.time()
    with db() as c: run=c.execute("INSERT INTO collector_runs(started_at,provider,sport,mode) VALUES(?,?,?,?)",(started,provider,sport,"LIVE_CAPTURE")).lastrowid
    try:
        if provider not in PROVIDERS: raise RuntimeError(f"Unknown provider: {provider}")
        if provider=="theoddsapi" and not os.getenv("ODDS_API_KEY"): raise RuntimeError("ODDS_API_KEY is not configured")
        if provider=="apifootball" and not os.getenv("API_FOOTBALL_KEY"): raise RuntimeError("API_FOOTBALL_KEY is not configured")
        rows=PROVIDERS[provider].fetch(sport,markets)
        ts=_store_rows(rows,provider,sport)
        events=len({r["event_id"] for r in rows})
        with db() as c: c.execute("UPDATE collector_runs SET finished_at=?,rows=?,events=? WHERE id=?",(time.time(),len(rows),events,run))
        return {"run_id":run,"rows":len(rows),"events":events,"timestamp":ts}
    except Exception as e:
        with db() as c: c.execute("UPDATE collector_runs SET finished_at=?,error=? WHERE id=?",(time.time(),str(e),run))
        raise

def _historical_import(sport, start_date, end_date, interval_hours=6):
    if not isinstance(PROVIDERS.get("theoddsapi"),TheOddsAPI): raise RuntimeError("The Odds API provider unavailable")
    start=datetime.datetime.fromisoformat(start_date.replace("Z","+00:00")).replace(tzinfo=datetime.timezone.utc) if "T" not in start_date else datetime.datetime.fromisoformat(start_date.replace("Z","+00:00"))
    end=datetime.datetime.fromisoformat(end_date.replace("Z","+00:00")).replace(tzinfo=datetime.timezone.utc) if "T" not in end_date else datetime.datetime.fromisoformat(end_date.replace("Z","+00:00"))
    cur=start; total=0; snaps=0; events=set()
    while cur<=end:
        rows,stamp=PROVIDERS["theoddsapi"].fetch_historical(sport,cur.isoformat().replace("+00:00","Z"),"h2h,totals")
        _store_rows(rows,"theoddsapi",sport,parse_ts(stamp) if isinstance(stamp,str) else float(stamp)); total+=len(rows); events.update(r["event_id"] for r in rows); snaps+=1
        cur += datetime.timedelta(hours=max(1,int(interval_hours)))
    return {"snapshots":snaps,"rows":total,"events":len(events),"start":start.isoformat(),"end":end.isoformat()}

def _load_collector():
    with db() as c:
        row=c.execute("SELECT * FROM collector_config WHERE id=1").fetchone()
        if not row:
            c.execute("INSERT INTO collector_config(id,enabled,interval_seconds,provider,sports,markets,lookahead_hours,updated_at) VALUES(1,0,300,?,?,?,?,?)",("theoddsapi","soccer_epl","h2h,totals",48,time.time()))
            row=c.execute("SELECT * FROM collector_config WHERE id=1").fetchone()
    return dict(row)

def _collector_worker():
    while not _scanner_stop.is_set():
        cfg=_load_collector()
        if cfg["enabled"] and time.time()>=(cfg["next_run"] or 0):
            for sport in str(cfg["sports"]).split(","):
                sport=sport.strip()
                if not sport: continue
                try: _capture_once(sport,cfg["provider"],cfg["markets"])
                except Exception as e:
                    with db() as c: c.execute("UPDATE collector_config SET last_error=?,next_run=?,updated_at=? WHERE id=1",(str(e),time.time()+int(cfg["interval_seconds"]),time.time()))
            with db() as c: c.execute("UPDATE collector_config SET last_run=?,next_run=?,last_error=NULL,updated_at=? WHERE id=1",(time.time(),time.time()+int(cfg["interval_seconds"]),time.time()))
        _scanner_stop.wait(2)

threading.Thread(target=_collector_worker,name="edge-data-collector",daemon=True).start()

class CollectorConfigIn(BaseModel):
    enabled: bool|None=None; interval_seconds:int|None=None; provider:str|None=None; sports:list[str]|None=None; markets:str|None=None; lookahead_hours:int|None=None

@app.get("/data/status")
def data_status(sport:str="soccer_epl"):
    cfg=_load_collector()
    with db() as c:
        runs=[dict(r) for r in c.execute("SELECT * FROM collector_runs ORDER BY id DESC LIMIT 20")]
        n=c.execute("SELECT COUNT(*) FROM odds_snapshots WHERE sport=?",(sport,)).fetchone()[0]
        ev=c.execute("SELECT COUNT(*) FROM canonical_events WHERE sport=?",(sport,)).fetchone()[0]
        spans=c.execute("SELECT MIN(ts),MAX(ts) FROM odds_snapshots WHERE sport=?",(sport,)).fetchone()
    return {"config":cfg,"sport":sport,"odds_rows":n,"canonical_events":ev,"first_snapshot":spans[0],"last_snapshot":spans[1],"runs":runs}

@app.post("/data/config")
def data_config(m:CollectorConfigIn):
    cfg=_load_collector()
    if m.interval_seconds is not None:
        if m.interval_seconds<60: raise HTTPException(400,"Minimum capture interval is 60 seconds")
        cfg["interval_seconds"]=int(m.interval_seconds)
    if m.provider is not None:
        if m.provider not in PROVIDERS: raise HTTPException(400,"Unsupported provider")
        cfg["provider"]=m.provider
    if m.sports is not None and m.sports: cfg["sports"]=','.join(m.sports)
    if m.markets is not None: cfg["markets"]=m.markets
    if m.lookahead_hours is not None: cfg["lookahead_hours"]=max(1,min(168,int(m.lookahead_hours)))
    if m.enabled is not None: cfg["enabled"]=int(m.enabled); cfg["next_run"]=0 if m.enabled else None
    with db() as c: c.execute("UPDATE collector_config SET enabled=?,interval_seconds=?,provider=?,sports=?,markets=?,lookahead_hours=?,next_run=?,updated_at=? WHERE id=1",(cfg["enabled"],cfg["interval_seconds"],cfg["provider"],cfg["sports"],cfg["markets"],cfg["lookahead_hours"],cfg["next_run"],time.time()))
    return data_status()

@app.post("/data/capture")
def data_capture(sport:str="soccer_epl", provider:str="theoddsapi", markets:str="h2h,totals"):
    return _capture_once(sport,provider,markets)

class HistoricalImportIn(BaseModel):
    sport:str="soccer_epl"; start_date:str; end_date:str; interval_hours:int=6

@app.post("/data/historical-odds")
def historical_odds_import(m:HistoricalImportIn):
    try: return _historical_import(m.sport,m.start_date,m.end_date,m.interval_hours)
    except Exception as e: raise HTTPException(502,str(e))

@app.post("/data/results")
def data_results(sport:str="soccer_epl", season:int=2025):
    if not os.getenv("API_FOOTBALL_KEY"): raise HTTPException(400,"Set API_FOOTBALL_KEY in environment")
    rows=PROVIDERS["apifootball"].fetch_results(sport,season)
    inserted=ingest_outcomes(rows,sport,"api-football")
    return {"season":season,"sport":sport,"results":inserted}

class ResultsRangeIn(BaseModel):
    sport:str="soccer_epl"; seasons:list[int]

@app.post("/data/results-range")
def data_results_range(m:ResultsRangeIn):
    if not os.getenv("API_FOOTBALL_KEY"): raise HTTPException(400,"Set API_FOOTBALL_KEY in environment")
    if not m.seasons or len(m.seasons)>20: raise HTTPException(400,"Provide 1-20 seasons")
    out=[]
    for season in sorted(set(int(x) for x in m.seasons)):
        rows=PROVIDERS["apifootball"].fetch_results(m.sport,season)
        out.append({"season":season,"results":ingest_outcomes(rows,m.sport,"api-football")})
    return {"sport":m.sport,"seasons":out,"total_results":sum(x["results"] for x in out)}

@app.get("/data/quality")
def data_quality(sport:str="soccer_epl"):
    with db() as c:
        odds=c.execute("SELECT COUNT(*) FROM odds_snapshots WHERE sport=?",(sport,)).fetchone()[0]
        events=c.execute("SELECT COUNT(*) FROM canonical_events WHERE sport=?",(sport,)).fetchone()[0]
        outcomes=c.execute("SELECT COUNT(*) FROM football_outcomes WHERE sport=?",(sport,)).fetchone()[0]
        books=c.execute("SELECT COUNT(DISTINCT bookmaker) FROM odds_snapshots WHERE sport=?",(sport,)).fetchone()[0]
        dup=c.execute("SELECT COUNT(*) FROM (SELECT ts,event_id,bookmaker,market,selection,COALESCE(point,-999) p,COUNT(*) n FROM odds_snapshots WHERE sport=? GROUP BY ts,event_id,bookmaker,market,selection,p HAVING n>1)",(sport,)).fetchone()[0]
        future=c.execute("SELECT COUNT(*) FROM odds_snapshots WHERE sport=? AND ts>strftime('%s','now')+60",(sport,)).fetchone()[0]
    report={"odds_rows":odds,"canonical_events":events,"settled_outcomes":outcomes,"bookmakers":books,"duplicate_groups":dup,"future_timestamp_rows":future,"event_link_coverage":(events>0),"notes":["Historical Odds API availability depends on subscription/retention.","API-Football odds history is limited; live capture is required for durable timestamped history."]}
    with db() as c: c.execute("INSERT INTO data_quality_runs(calculated_at,sport,report_json) VALUES(?,?,?)",(time.time(),sport,json.dumps(report)))
    return report


# ---------- Phase 8: research campaign, stability analysis & production gate hardening ----------
with db() as c:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS research_campaign_runs(
      id INTEGER PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL,
      sports_json TEXT NOT NULL, seasons_json TEXT NOT NULL, historical_odds INTEGER NOT NULL DEFAULT 0,
      status TEXT NOT NULL, report_json TEXT, error TEXT
    );
    CREATE TABLE IF NOT EXISTS research_stability_runs(
      id INTEGER PRIMARY KEY, calculated_at REAL NOT NULL, sport TEXT NOT NULL,
      run_id INTEGER, seasons_tested INTEGER NOT NULL, seasons_positive INTEGER NOT NULL,
      bookmakers_tested INTEGER NOT NULL, bookmakers_positive INTEGER NOT NULL,
      season_json TEXT NOT NULL, bookmaker_json TEXT NOT NULL, eligible INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_stability ON research_stability_runs(sport, calculated_at);
    """)

class CampaignIn(BaseModel):
    sports: list[str] = ["soccer_epl"]
    seasons: list[int] = []
    ingest_results: bool = True
    historical_odds: bool = False
    odds_start_date: str | None = None
    odds_end_date: str | None = None
    odds_interval_hours: int = 6
    factory: dict | None = None

class StabilityIn(BaseModel):
    sport: str = "soccer_epl"
    min_season_bets: int = 20
    min_bookmaker_bets: int = 20
    min_positive_seasons: int = 2
    min_positive_bookmakers: int = 2
    run_id: int | None = None

def _latest_factory_report(sport, run_id=None):
    with db() as c:
        if run_id is not None:
            r=c.execute("SELECT * FROM research_factory_runs WHERE id=? AND sport=? AND status='COMPLETE'",(run_id,sport)).fetchone()
        else:
            r=c.execute("SELECT * FROM research_factory_runs WHERE sport=? AND status='COMPLETE' ORDER BY id DESC LIMIT 1",(sport,)).fetchone()
    return (dict(r), json.loads(r["report_json"] or "{}")) if r else (None,None)

def _stability_calc(m: StabilityIn):
    row, rep = _latest_factory_report(m.sport, m.run_id)
    if not row: return {"eligible":False,"status":"INSUFFICIENT_DATA","reasons":["No completed factory run"]}
    bets=rep.get("bets_detail",[])
    season_groups=defaultdict(list); book_groups=defaultdict(list)
    # bets_detail carries the canonical event ID; recover season from stored outcomes.
    with db() as c:
        outcomes={r["canonical_id"]:dict(r) for r in c.execute("SELECT canonical_id,home,away,kickoff FROM canonical_events WHERE sport=?",(m.sport,))}
        # canonical_events may not be populated for older manually-ingested outcomes; fall back to outcomes.
        for r in c.execute("SELECT home,away,kickoff FROM football_outcomes WHERE sport=?",(m.sport,)):
            outcomes.setdefault(canonical_event_id(r["home"],r["away"],r["kickoff"]),dict(r))
    for b in bets:
        oid=b.get("event_id")
        o=outcomes.get(oid)
        if not o: continue
        season=_season_label(o["kickoff"])
        season_groups[season].append(b); book_groups[b.get("bookmaker","unknown")].append(b)
    def group_stats(groups, minimum):
        out=[]
        for key,items in sorted(groups.items()):
            if len(items)<minimum: continue
            returns=[float(x.get("return",0)) for x in items]
            stake=float(rep.get("controls",{}).get("stake_fraction",.01))
            roi=sum(returns)/(len(returns)*stake) if stake else None
            clv=[float(x["clv"]) for x in items if x.get("clv") is not None]
            out.append({"segment":key,"bets":len(items),"roi":roi,"mean_clv":statistics.mean(clv) if clv else None,
                        "positive_roi":bool(roi is not None and roi>0),"positive_clv":bool(clv and statistics.mean(clv)>0)})
        return out
    ss=group_stats(season_groups,m.min_season_bets); bs=group_stats(book_groups,m.min_bookmaker_bets)
    pos_s=sum(1 for x in ss if x["positive_roi"] and x["positive_clv"])
    pos_b=sum(1 for x in bs if x["positive_roi"] and x["positive_clv"])
    reasons=[]
    if len(ss)<m.min_positive_seasons: reasons.append(f"tested_seasons<{m.min_positive_seasons}")
    if len(bs)<m.min_positive_bookmakers: reasons.append(f"tested_bookmakers<{m.min_positive_bookmakers}")
    if pos_s<m.min_positive_seasons: reasons.append(f"positive_seasons<{m.min_positive_seasons}")
    if pos_b<m.min_positive_bookmakers: reasons.append(f"positive_bookmakers<{m.min_positive_bookmakers}")
    eligible=not reasons
    report={"eligible":eligible,"status":"STABLE" if eligible else "UNSTABLE","run_id":row["id"],"seasons":ss,"bookmakers":bs,
            "positive_seasons":pos_s,"positive_bookmakers":pos_b,"criteria":m.model_dump(),
            "note":"Stability is descriptive evidence, not a guarantee of future profitability. Segments below the minimum sample are excluded rather than treated as failures."}
    with db() as c:
        c.execute("INSERT INTO research_stability_runs(calculated_at,sport,run_id,seasons_tested,seasons_positive,bookmakers_tested,bookmakers_positive,season_json,bookmaker_json,eligible) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (time.time(),m.sport,row["id"],len(ss),pos_s,len(bs),pos_b,json.dumps(ss),json.dumps(bs),int(eligible)))
    return report

@app.post("/research/campaign")
def research_campaign(m: CampaignIn):
    if not m.sports or len(m.sports)>20: raise HTTPException(400,"Provide 1-20 sports/leagues")
    seasons=sorted(set(int(x) for x in m.seasons))
    if m.ingest_results and not seasons: raise HTTPException(400,"Provide seasons when ingest_results=true")
    if m.historical_odds and (not m.odds_start_date or not m.odds_end_date): raise HTTPException(400,"Historical odds require odds_start_date and odds_end_date")
    started=time.time()
    with db() as c: run=c.execute("INSERT INTO research_campaign_runs(started_at,sports_json,seasons_json,historical_odds,status) VALUES(?,?,?,?,?)",(started,json.dumps(m.sports),json.dumps(seasons),int(m.historical_odds),"RUNNING")).lastrowid
    reports=[]
    try:
        for sport in m.sports:
            result_ingest=[]; odds_import=None
            if m.ingest_results:
                if not os.getenv("API_FOOTBALL_KEY"): raise RuntimeError("API_FOOTBALL_KEY is required for result ingestion")
                for season in seasons:
                    rows=PROVIDERS["apifootball"].fetch_results(sport,season)
                    result_ingest.append({"season":season,"results":ingest_outcomes(rows,sport,"api-football")})
            if m.historical_odds:
                odds_import=_historical_import(sport,m.odds_start_date,m.odds_end_date,m.odds_interval_hours)
            fm=FactoryIn(**(m.factory or {}), sport=sport)
            factory=_research_factory_run(fm)
            stability=_stability_calc(StabilityIn(sport=sport))
            reports.append({"sport":sport,"results":result_ingest,"historical_odds":odds_import,"factory_run_id":factory.get("run_id"),"factory":factory,"stability":stability})
        report={"campaign_id":run,"sports":reports,"note":"Campaign orchestration does not bypass provider subscription limits or create historical odds that were never captured."}
        with db() as c: c.execute("UPDATE research_campaign_runs SET finished_at=?,status=?,report_json=? WHERE id=?",(time.time(),"COMPLETE",json.dumps(report),run))
        return report
    except Exception as e:
        with db() as c: c.execute("UPDATE research_campaign_runs SET finished_at=?,status=?,error=? WHERE id=?",(time.time(),"ERROR",str(e),run))
        raise HTTPException(502,str(e))

@app.get("/research/campaign/runs")
def research_campaign_runs(limit:int=20):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT id,started_at,finished_at,sports_json,seasons_json,historical_odds,status,error FROM research_campaign_runs ORDER BY id DESC LIMIT ?",(max(1,min(100,limit)),))]

@app.post("/research/stability")
def research_stability(m: StabilityIn):
    try: return _stability_calc(m)
    except Exception as e: raise HTTPException(500,str(e))

@app.get("/research/stability")
def research_stability_get(sport:str="soccer_epl"):
    with db() as c:
        r=c.execute("SELECT * FROM research_stability_runs WHERE sport=? ORDER BY id DESC LIMIT 1",(sport,)).fetchone()
    return dict(r) if r else {"status":"NO_RUN"}

# ---------- Phase 6: research factory, leakage controls & execution-aware validation ----------
with db() as c:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS research_factory_runs(
      id INTEGER PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL,
      sport TEXT NOT NULL, min_training INTEGER NOT NULL, retrain_every INTEGER NOT NULL,
      min_ev REAL NOT NULL, max_odds_age_seconds INTEGER NOT NULL, min_books INTEGER NOT NULL,
      slippage_bps REAL NOT NULL, predictions INTEGER DEFAULT 0, bets INTEGER DEFAULT 0,
      status TEXT NOT NULL, report_json TEXT
    );
    CREATE TABLE IF NOT EXISTS research_signal_stats(
      id INTEGER PRIMARY KEY, calculated_at REAL NOT NULL, sport TEXT NOT NULL,
      model_version TEXT NOT NULL, bucket TEXT NOT NULL, observations INTEGER NOT NULL,
      bets INTEGER NOT NULL, mean_ev REAL, roi REAL, mean_clv REAL, win_rate REAL,
      max_drawdown REAL, brier REAL, logloss REAL, ece REAL
    );
    CREATE INDEX IF NOT EXISTS ix_signal_stats ON research_signal_stats(sport, model_version, bucket);
    CREATE TABLE IF NOT EXISTS research_gate_config(
      id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
      min_bets INTEGER NOT NULL DEFAULT 100, min_predictions INTEGER NOT NULL DEFAULT 500,
      min_seasons INTEGER NOT NULL DEFAULT 3, min_roi_ci REAL NOT NULL DEFAULT 0.0,
      min_clv_ci REAL NOT NULL DEFAULT 0.0, max_market_brier_gap REAL NOT NULL DEFAULT 0.0,
      min_stable_seasons INTEGER NOT NULL DEFAULT 2, min_stable_bookmakers INTEGER NOT NULL DEFAULT 2,
      max_gate_age_hours REAL NOT NULL DEFAULT 168,
      updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS research_gate_runs(
      id INTEGER PRIMARY KEY, calculated_at REAL NOT NULL, sport TEXT NOT NULL,
      status TEXT NOT NULL, eligible INTEGER NOT NULL, report_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS research_season_stats(
      id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, sport TEXT NOT NULL, season TEXT NOT NULL,
      predictions INTEGER NOT NULL, bets INTEGER NOT NULL, brier REAL, logloss REAL, market_brier REAL,
      market_logloss REAL, roi REAL, mean_clv REAL, max_drawdown REAL, roi_ci_lo REAL, roi_ci_hi REAL,
      clv_ci_lo REAL, clv_ci_hi REAL
    );
    CREATE INDEX IF NOT EXISTS ix_season_stats ON research_season_stats(sport,season,run_id);
    """)


class FactoryIn(BaseModel):
    sport: str = "soccer_epl"
    min_training: int = 80
    retrain_every: int = 20
    min_ev: float = .03
    min_books: int = 4
    max_odds_age_seconds: int = 300
    slippage_bps: float = 10.0
    stake_fraction: float = .01
    max_bets_per_event: int = 1
    min_season_bets: int = 0


def _market_snapshot_for_outcome(outcome):
    ko=outcome["kickoff"]; th=canonical_team_id(outcome["home"]); ta=canonical_team_id(outcome["away"])
    with db() as c:
        rows=[dict(r) for r in c.execute("SELECT * FROM odds_snapshots WHERE ts<=? AND market='h2h'",(ko,))]
    rows=[r for r in rows if canonical_team_id(r["home"])==th and canonical_team_id(r["away"])==ta and abs(parse_ts(r["commence"])-ko)<=7200]
    latest={}
    for r in rows:
        bk=r["bookmaker"]
        latest[bk]=r if bk not in latest or r["ts"]>latest[bk]["ts"] else latest[bk]
    out={}
    names=[outcome["home"],"Draw",outcome["away"]]
    for bk in latest:
        br=[r for r in rows if r["bookmaker"]==bk and r["ts"]==latest[bk]["ts"]]
        chosen=[]
        for name in names:
            rr=next((x for x in br if x["selection"]==name),None)
            if rr: chosen.append(rr)
        if len(chosen)!=3: continue
        fair=devig([x["price"] for x in chosen])
        out[bk]={"prices":dict(zip(names,[x["price"] for x in chosen])),"fair":dict(zip(names,fair)),"ts":max(x["ts"] for x in chosen),"age":max(0,ko-max(x["ts"] for x in chosen))}
    return out


def _market_consensus_probs(market, names):
    if not market: return None
    vals=[]
    for name in names:
        ps=[m["fair"][name] for m in market.values() if name in m["fair"]]
        if not ps: return None
        vals.append(statistics.median(ps))
    z=sum(vals)
    return tuple(x/z for x in vals) if z else None


def _calibrated_temperature(sport):
    with db() as c:
        r=c.execute("SELECT temperature FROM research_calibration WHERE sport=? ORDER BY id DESC LIMIT 1",(sport,)).fetchone()
    return float(r[0]) if r else 1.0


def _execution_price(price, slippage_bps):
    return price * max(0.95, 1.0 - max(0.0,slippage_bps)/10000.0)


def _signal_bucket(ev):
    if ev < .03: return "<3%"
    if ev < .05: return "3-5%"
    if ev < .08: return "5-8%"
    if ev < .12: return "8-12%"
    return "12%+"



def _season_label(ts):
    d=datetime.datetime.fromtimestamp(float(ts),tz=datetime.timezone.utc)
    return f"{d.year}-{d.year+1}" if d.month>=7 else f"{d.year-1}-{d.year}"

def _bootstrap_mean_ci(values, n=2000, seed=20260924):
    if not values: return (None,None)
    rng=random.Random(seed); m=len(values); vals=[]
    for _ in range(min(n,10000)):
        vals.append(statistics.mean(values[rng.randrange(m)] for _ in range(m)))
    vals.sort(); return vals[max(0,int(.025*len(vals)))], vals[min(len(vals)-1,int(.975*len(vals))-1)]

def _signflip_pvalue(values, n=4000, seed=20260924):
    if len(values)<2: return 1.0
    observed=statistics.mean(values); rng=random.Random(seed); ge=0
    for _ in range(min(n,10000)):
        x=statistics.mean((v if rng.random()<.5 else -v) for v in values)
        if x>=observed: ge+=1
    return (ge+1)/(min(n,10000)+1)

def _bh_adjust(pvals):
    # Benjamini-Hochberg FDR correction, preserving input order.
    n=len(pvals); order=sorted(range(n),key=lambda i:pvals[i]); adj=[1.0]*n; running=1.0
    for rank,i in reversed(list(enumerate(order,1))):
        running=min(running,pvals[i]*n/rank); adj[i]=min(1.0,running)
    return adj

def _gate_config():
    with db() as c:
        r=c.execute("SELECT * FROM research_gate_config WHERE id=1").fetchone()
        if not r:
            c.execute("INSERT INTO research_gate_config(id,updated_at) VALUES(1,?)",(time.time(),))
            r=c.execute("SELECT * FROM research_gate_config WHERE id=1").fetchone()
    return dict(r)

def _latest_gate(sport):
    with db() as c:
        r=c.execute("SELECT * FROM research_gate_runs WHERE sport=? ORDER BY id DESC LIMIT 1",(sport,)).fetchone()
    return dict(r) if r else None

def _research_factory_run(m: FactoryIn):
    rows=_outcome_rows(m.sport)
    if len(rows)<m.min_training+1: raise HTTPException(400,f"Need >{m.min_training} settled outcomes; have {len(rows)}")
    started=time.time()
    with db() as c:
        run=c.execute("INSERT INTO research_factory_runs(started_at,sport,min_training,retrain_every,min_ev,max_odds_age_seconds,min_books,slippage_bps,status) VALUES(?,?,?,?,?,?,?,?,?)",
                      (started,m.sport,m.min_training,m.retrain_every,m.min_ev,m.max_odds_age_seconds,m.min_books,m.slippage_bps,"RUNNING")).lastrowid
    predictions=[]; candidates=[]; bets=[]; brier=[]; logloss=[]; market_brier=[]; market_logloss=[]; clv=[]
    elo=dc=w=None; temperature=_calibrated_temperature(m.sport)
    for i in range(m.min_training,len(rows)):
        if i==m.min_training or (i-m.min_training)%max(1,m.retrain_every)==0:
            elo,dc,w=_model_from_train(rows[:i])
        r=rows[i]; names=[r["home"],"Draw",r["away"]]
        pe=elo.probs(r["home_team_id"],r["away_team_id"]); pd=_dc_probs(dc,r["home_team_id"],r["away_team_id"])
        raw=tuple(w*a+(1-w)*b for a,b in zip(pe,pd)); p=_softmax_temp(raw,temperature); y=_result_index(r)
        brier.append(_brier(p,y)); logloss.append(_logloss(p,y)); predictions.append((r,p,y))
        market=_market_snapshot_for_outcome(r); mp=_market_consensus_probs(market,names)
        if mp:
            market_brier.append(_brier(mp,y)); market_logloss.append(_logloss(mp,y))
        opts=[]
        for j,name in enumerate(names):
            eligible=[]
            for bk,mk in market.items():
                if name not in mk["prices"]: continue
                if len(market)<m.min_books: continue
                if mk["age"]>m.max_odds_age_seconds: continue
                ep=_execution_price(mk["prices"][name],m.slippage_bps)
                ev=p[j]*ep-1
                eligible.append((ev,ep,bk,mk["age"],mk["prices"][name]))
            if eligible:
                ev,ep,bk,age,raw_odds=max(eligible)
                if ev>=m.min_ev:
                    opts.append({"event":f"{r['home']} v {r['away']}","event_id":r["canonical_event_id"],"selection":name,"bookmaker":bk,"raw_odds":raw_odds,"execution_odds":ep,"model_prob":p[j],"market_prob":mp[j] if mp else None,"ev":ev,"odds_age_seconds":age,"won":y==j})
        # Prevent the same match producing multiple simultaneous positions.
        if opts:
            best=max(opts,key=lambda x:x["ev"]); candidates.append(best)
    # Only one bet per event and preserve chronological order.
    used=set()
    for x in candidates:
        if x["event_id"] in used: continue
        used.add(x["event_id"]); stake=max(0,min(.05,m.stake_fraction)); ret=(x["execution_odds"]-1)*stake if x["won"] else -stake
        x["stake"]=stake; x["return"]=ret
        bets.append(x)
        market=_market_snapshot_for_outcome(next(r for r in rows if r["canonical_event_id"]==x["event_id"]))
        bm=market.get(x["bookmaker"])
        if bm:
            close=bm["prices"].get(x["selection"])
            if close: x["closing_odds"]=close; clv.append((1/x["raw_odds"])-(1/close))
    pnl=[x["return"] for x in bets]; equity=1.0; peak=1.0; maxdd=0
    for v in pnl:
        equity+=v; peak=max(peak,equity); maxdd=max(maxdd,(peak-equity)/peak)
    roi=sum(pnl)/(len(pnl)*m.stake_fraction) if pnl and m.stake_fraction else None
    signal=[]
    for bucket in ["<3%","3-5%","5-8%","8-12%","12%+"]:
        bb=[x for x in bets if _signal_bucket(x["ev"])==bucket]; vals=[x["return"] for x in bb]
        signal.append(dict(bucket=bucket,observations=sum(1 for r,p,y in predictions if _signal_bucket(max(p)-1)==bucket),bets=len(bb),mean_ev=statistics.mean([x["ev"] for x in bb]) if bb else None,roi=(sum(vals)/(len(vals)*m.stake_fraction) if vals and m.stake_fraction else None),mean_clv=None,win_rate=(sum(x["won"] for x in bb)/len(bb) if bb else None)))
    # Season-level robustness: the signal must not rely on a single season.
    season_stats=[]
    for season in sorted({_season_label(r["kickoff"]) for r,_,_ in predictions}):
        sp=[(r,p,y) for r,p,y in predictions if _season_label(r["kickoff"])==season]
        sb=[x for x in bets if _season_label(next(r["kickoff"] for r in rows if r["canonical_event_id"]==x["event_id"]))==season]
        sr=[x["return"] for x in sb]; sc=[x for x in clv]
        sbri=[_brier(p,y) for _,p,y in sp]; sll=[_logloss(p,y) for _,p,y in sp]
        roi_s=sum(sr)/(len(sr)*m.stake_fraction) if sr and m.stake_fraction else None
        rlo,rhi=_bootstrap_mean_ci([v/m.stake_fraction for v in sr]) if sr and m.stake_fraction else (None,None)
        season_stats.append({"season":season,"predictions":len(sp),"bets":len(sb),"brier":statistics.mean(sbri) if sbri else None,"logloss":statistics.mean(sll) if sll else None,"roi":roi_s,"roi_ci_lo":rlo,"roi_ci_hi":rhi})
    bucket_returns=[]; bucket_p=[]
    for s in signal:
        bb=[x["return"] for x in bets if _signal_bucket(x["ev"])==s["bucket"]]
        s["roi_ci95"]=(dict(point=statistics.mean(bb)/m.stake_fraction,lo=min(_bootstrap_mean_ci([v/m.stake_fraction for v in bb])) if bb and m.stake_fraction else None,hi=max(_bootstrap_mean_ci([v/m.stake_fraction for v in bb])) if bb and m.stake_fraction else None) if bb and m.stake_fraction else None)
        s["p_value"]=_signflip_pvalue(bb) if len(bb)>=10 else 1.0
        bucket_p.append(s["p_value"])
    adj=_bh_adjust(bucket_p)
    for s,q in zip(signal,adj): s["q_value"]=q
    roi_ci=None
    if pnl and m.stake_fraction:
        rlo,rhi=_bootstrap_mean_ci([v/m.stake_fraction for v in pnl]); roi_ci=dict(point=roi,lo=rlo,hi=rhi)
    report={"run_id":run,"predictions":len(predictions),"brier":statistics.mean(brier),"logloss":statistics.mean(logloss),"brier_ci95":_bootstrap(brier),"logloss_ci95":_bootstrap(logloss),"market_brier":statistics.mean(market_brier) if market_brier else None,"market_logloss":statistics.mean(market_logloss) if market_logloss else None,"bets":len(bets),"roi":roi,"roi_ci95":roi_ci,"max_drawdown":maxdd,"mean_clv":statistics.mean(clv) if clv else None,"clv_ci95":_bootstrap(clv) if clv else None,"temperature":temperature,"signal_buckets":signal,"season_stats":season_stats,"multiple_testing":{"method":"Benjamini-Hochberg","q_values":[s["q_value"] for s in signal]},"bets_detail":bets[-500:],"controls":{"min_training":m.min_training,"retrain_every":m.retrain_every,"min_ev":m.min_ev,"min_books":m.min_books,"max_odds_age_seconds":m.max_odds_age_seconds,"slippage_bps":m.slippage_bps,"stake_fraction":m.stake_fraction,"max_bets_per_event":m.max_bets_per_event},"notes":["Predictions are trained only on outcomes before each prediction timestamp.","One position per canonical event is enforced.","Execution odds include configurable slippage.","Season-level results and multiple-testing correction are reported to reduce selection bias.","A qualifying signal is not evidence of future profitability; require repeated out-of-sample samples, CLV, and the signal gate."]}
    with db() as c:
        c.execute("UPDATE research_factory_runs SET finished_at=?,predictions=?,bets=?,status=?,report_json=? WHERE id=?",(time.time(),len(predictions),len(bets),"COMPLETE",json.dumps(report),run))
        for s in signal:
            c.execute("INSERT INTO research_signal_stats(calculated_at,sport,model_version,bucket,observations,bets,mean_ev,roi,mean_clv,win_rate,max_drawdown,brier,logloss,ece) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (time.time(),m.sport,"factory-v1",s["bucket"],s["observations"],s["bets"],s["mean_ev"],s["roi"],s["mean_clv"],s["win_rate"],maxdd,statistics.mean(brier),statistics.mean(logloss),_ece([(p,y) for _,p,y in predictions])[0]))
    return report

class GateConfigIn(BaseModel):
    enabled: bool|None=None; min_bets:int|None=None; min_predictions:int|None=None; min_seasons:int|None=None
    min_roi_ci:float|None=None; min_clv_ci:float|None=None; max_market_brier_gap:float|None=None; min_stable_seasons:int|None=None; min_stable_bookmakers:int|None=None; max_gate_age_hours:float|None=None

@app.get("/research/gate")
def research_gate(sport:str="soccer_epl"):
    cfg=_gate_config(); latest=_latest_gate(sport)
    return {"config":cfg,"latest":latest,"status":(json.loads(latest["report_json"]) if latest else None)}

@app.post("/research/gate/config")
def research_gate_config(m:GateConfigIn):
    cfg=_gate_config()
    vals={"enabled":cfg["enabled"],"min_bets":cfg["min_bets"],"min_predictions":cfg["min_predictions"],"min_seasons":cfg["min_seasons"],"min_roi_ci":cfg["min_roi_ci"],"min_clv_ci":cfg["min_clv_ci"],"max_market_brier_gap":cfg["max_market_brier_gap"],"min_stable_seasons":cfg.get("min_stable_seasons",2),"min_stable_bookmakers":cfg.get("min_stable_bookmakers",2),"max_gate_age_hours":cfg.get("max_gate_age_hours",168)}
    for k,v in m.model_dump(exclude_none=True).items(): vals[k]=v
    vals["enabled"]=int(bool(vals["enabled"]))
    with db() as c: c.execute("UPDATE research_gate_config SET enabled=?,min_bets=?,min_predictions=?,min_seasons=?,min_roi_ci=?,min_clv_ci=?,max_market_brier_gap=?,min_stable_seasons=?,min_stable_bookmakers=?,max_gate_age_hours=?,updated_at=? WHERE id=1",(vals["enabled"],max(1,int(vals["min_bets"])),max(1,int(vals["min_predictions"])),max(1,int(vals["min_seasons"])),float(vals["min_roi_ci"]),float(vals["min_clv_ci"]),float(vals["max_market_brier_gap"]),max(1,int(vals["min_stable_seasons"])),max(1,int(vals["min_stable_bookmakers"])),max(1,float(vals["max_gate_age_hours"])),time.time()))
    return research_gate()

def _evaluate_gate(sport):
    cfg=_gate_config()
    with db() as c:
        runs=[dict(r) for r in c.execute("SELECT * FROM research_factory_runs WHERE sport=? AND status='COMPLETE' ORDER BY id DESC",(sport,))]
    if not runs: return {"eligible":False,"status":"INSUFFICIENT_DATA","reasons":["No completed research runs"]}
    reps=[json.loads(r["report_json"] or "{}") for r in runs]
    latest=reps[0]; seasons={s["season"] for rep in reps for s in rep.get("season_stats",[]) if s.get("predictions",0)>0}
    # Aggregate only completed out-of-sample bets across the most recent run for an auditable gate.
    bets=latest.get("bets_detail",[]); returns=[float(x["return"]) for x in bets]
    roi=sum(returns)/(len(returns)*float(latest.get("controls",{}).get("stake_fraction",.01))) if returns else None
    roi_lo=latest.get("roi_ci95",{}).get("lo") if latest.get("roi_ci95") else None
    clv_lo=latest.get("clv_ci95",{}).get("lo") if latest.get("clv_ci95") else None
    market_gap=(latest.get("brier")-latest.get("market_brier")) if latest.get("market_brier") is not None else None
    reasons=[]
    if latest.get("bets",0)<cfg["min_bets"]: reasons.append(f"bets<{cfg['min_bets']}")
    if latest.get("predictions",0)<cfg["min_predictions"]: reasons.append(f"predictions<{cfg['min_predictions']}")
    if len(seasons)<cfg["min_seasons"]: reasons.append(f"seasons<{cfg['min_seasons']}")
    if roi_lo is None or roi_lo<=cfg["min_roi_ci"]: reasons.append("ROI lower CI threshold not met")
    if clv_lo is None or clv_lo<=cfg["min_clv_ci"]: reasons.append("CLV lower CI threshold not met")
    if market_gap is None or market_gap>cfg["max_market_brier_gap"]: reasons.append("model-vs-market Brier threshold not met")
    stability=_stability_calc(StabilityIn(sport=sport))
    if stability.get("eligible") is not True:
        reasons.append("stability criteria not met")
    else:
        if stability.get("positive_seasons",0)<cfg.get("min_stable_seasons",2): reasons.append("stable seasons threshold not met")
        if stability.get("positive_bookmakers",0)<cfg.get("min_stable_bookmakers",2): reasons.append("stable bookmakers threshold not met")
    latest_finished=latest.get("run_id")
    with db() as c:
        gr=c.execute("SELECT finished_at FROM research_factory_runs WHERE id=?",(latest_finished,)).fetchone()
    if gr and time.time()-float(gr[0])>float(cfg.get("max_gate_age_hours",168))*3600: reasons.append("latest research run is stale")
    eligible=not reasons
    status="ELIGIBLE" if eligible else "RESEARCH_ONLY"
    report={"status":status,"eligible":eligible,"reasons":reasons,"latest_run_id":latest.get("run_id"),"predictions":latest.get("predictions"),"bets":latest.get("bets"),"seasons":sorted(seasons),"roi":roi,"roi_ci_lower":roi_lo,"clv_ci_lower":clv_lo,"model_minus_market_brier":market_gap,"criteria":cfg,"stability":stability,"note":"Eligibility is a statistical research gate, not a guarantee of profitability or future availability."}
    with db() as c: c.execute("INSERT INTO research_gate_runs(calculated_at,sport,status,eligible,report_json) VALUES(?,?,?,?,?)",(time.time(),sport,status,int(eligible),json.dumps(report)))
    return report

@app.post("/research/gate/evaluate")
def research_gate_evaluate(sport:str="soccer_epl"):
    return _evaluate_gate(sport)

@app.post("/research/factory")
def research_factory(m: FactoryIn):
    try: return _research_factory_run(m)
    except Exception as e:
        raise HTTPException(500,str(e))

@app.get("/research/factory/runs")
def research_factory_runs(limit:int=20, sport:str="soccer_epl"):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT id,started_at,finished_at,sport,min_training,retrain_every,min_ev,max_odds_age_seconds,min_books,slippage_bps,predictions,bets,status FROM research_factory_runs WHERE sport=? ORDER BY id DESC LIMIT ?",(sport,max(1,min(100,limit))))]

@app.get("/research/leakage")
def research_leakage(sport:str="soccer_epl"):
    with db() as c:
        future=c.execute("SELECT COUNT(*) FROM odds_snapshots o JOIN football_outcomes f ON o.event_id=f.event_id WHERE o.sport=? AND o.ts>f.kickoff",(sport,)).fetchone()[0]
        bad_outcomes=c.execute("SELECT COUNT(*) FROM football_outcomes WHERE sport=? AND kickoff>strftime('%s','now')",(sport,)).fetchone()[0]
        duplicate_events=c.execute("SELECT COUNT(*) FROM (SELECT canonical_event_id,COUNT(*) n FROM football_outcomes WHERE sport=? GROUP BY canonical_event_id HAVING n>1)",(sport,)).fetchone()[0]
    return {"future_odds_rows_for_source_events":future,"future_outcomes":bad_outcomes,"duplicate_canonical_outcomes":duplicate_events,"pass":future==0 and bad_outcomes==0 and duplicate_events==0,"note":"Odds captured after kickoff are retained for movement analysis but excluded from pre-match research."}

@app.get("/research/benchmark")
def research_benchmark(sport:str="soccer_epl"):
    with db() as c:
        runs=[dict(r) for r in c.execute("SELECT id,started_at,finished_at,predictions,bets,status,report_json FROM research_factory_runs WHERE sport=? AND status='COMPLETE' ORDER BY id DESC LIMIT 10",(sport,))]
    out=[]
    for r in runs:
        rep=json.loads(r["report_json"] or "{}")
        out.append({"run_id":r["id"],"started_at":r["started_at"],"predictions":r["predictions"],"bets":r["bets"],"model_brier":rep.get("brier"),"market_brier":rep.get("market_brier"),"model_logloss":rep.get("logloss"),"market_logloss":rep.get("market_logloss"),"roi":rep.get("roi"),"mean_clv":rep.get("mean_clv"),"max_drawdown":rep.get("max_drawdown")})
    return {"sport":sport,"runs":out,"interpretation":"Compare model and market metrics over repeated out-of-sample runs; no single run establishes a durable betting edge."}
