import os, time, sqlite3, json, math
from collections import defaultdict
from typing import Optional
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# Reuse the mature football/scanner/research engine already in app.py.
from app import app, db, PROVIDERS, devig, kelly, CFG

CLOUD_VERSION = "24.0-cloud"
MMA_SPORT = "mma_mixed_martial_arts"

with db() as c:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS mma_cloud_quotes(
      id INTEGER PRIMARY KEY, ts REAL NOT NULL, event_id TEXT NOT NULL,
      fighter_a TEXT, fighter_b TEXT, bookmaker TEXT NOT NULL, provider TEXT,
      market TEXT NOT NULL, fighter TEXT, selection TEXT NOT NULL,
      line REAL, decimal_odds REAL NOT NULL, available_stake REAL, source_url TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_mma_cloud_quote ON mma_cloud_quotes(event_id,market,fighter,selection,line,ts);
    CREATE TABLE IF NOT EXISTS mma_cloud_paper(
      id INTEGER PRIMARY KEY, created_at REAL NOT NULL, event_id TEXT NOT NULL,
      market TEXT NOT NULL, fighter TEXT, selection TEXT NOT NULL, line REAL,
      bookmaker TEXT NOT NULL, decimal_odds REAL NOT NULL, model_prob REAL NOT NULL,
      ev REAL NOT NULL, stake_fraction REAL NOT NULL, result INTEGER,
      closing_odds REAL, clv REAL, status TEXT NOT NULL DEFAULT 'OPEN'
    );
    CREATE TABLE IF NOT EXISTS mma_cloud_model_evidence(
      id INTEGER PRIMARY KEY, created_at REAL NOT NULL, model_key TEXT NOT NULL,
      n_predictions INTEGER NOT NULL, n_bets INTEGER NOT NULL, roi REAL,
      roi_low REAL, mean_clv REAL, clv_low REAL, brier REAL, market_brier REAL,
      max_drawdown REAL, independent_periods INTEGER, books INTEGER,
      leakage_ok INTEGER NOT NULL DEFAULT 0, note TEXT
    );
    """)

class MMAQuote(BaseModel):
    event_id: str
    fighter_a: str = ""
    fighter_b: str = ""
    bookmaker: str
    provider: str = "manual"
    market: str
    fighter: Optional[str] = None
    selection: str
    line: Optional[float] = None
    decimal_odds: float
    available_stake: Optional[float] = None
    source_url: Optional[str] = None
    ts: Optional[float] = None

class MMAEval(BaseModel):
    event_id: str
    market: str
    fighter: Optional[str] = None
    selection: str
    line: Optional[float] = None
    model_prob: float
    bankroll_fraction_cap: float = 0.01
    max_age_seconds: int = 600
    paper: bool = False

class EvidenceIn(BaseModel):
    model_key: str
    n_predictions: int
    n_bets: int
    roi: Optional[float] = None
    roi_low: Optional[float] = None
    mean_clv: Optional[float] = None
    clv_low: Optional[float] = None
    brier: Optional[float] = None
    market_brier: Optional[float] = None
    max_drawdown: Optional[float] = None
    independent_periods: int = 0
    books: int = 0
    leakage_ok: bool = False
    note: str = ""

MMA_MARKETS = [
    "h2h","fight_goes_distance","fight_total_rounds",
    "fighter_sig_strikes","fighter_head_strikes","fighter_body_strikes","fighter_leg_strikes",
    "fighter_takedowns","fighter_takedown_attempts","fighter_submission_attempts",
    "fighter_knockdowns","fighter_control_time","most_sig_strikes","most_takedowns",
    "fighter_by_decision","fighter_by_ko_tko","fighter_by_submission","fighter_method_round",
] + [f"fighter_ko_tko_r{i}" for i in range(1,6)] + [f"fighter_submission_r{i}" for i in range(1,6)] + [f"fighter_win_r{i}" for i in range(1,6)]

@app.get("/health")
def cloud_health():
    try:
        with db() as c:
            c.execute("SELECT 1").fetchone()
        return {"ok": True, "version": CLOUD_VERSION, "db": "ok", "mode": "research-paper"}
    except Exception as e:
        raise HTTPException(503, f"database unavailable: {e}")

@app.get("/cloud/status")
def cloud_status():
    with db() as c:
        q = c.execute("SELECT COUNT(*) n, MAX(ts) last_ts FROM mma_cloud_quotes").fetchone()
        p = c.execute("SELECT COUNT(*) n, SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) open_n FROM mma_cloud_paper").fetchone()
    return {
        "ok": True, "version": CLOUD_VERSION, "phone_ready": True,
        "mma_quotes": int(q[0] or 0), "last_mma_quote": q[1],
        "paper_bets": int(p[0] or 0), "open_paper": int(p[1] or 0),
        "automatic_real_money_execution": False,
        "profitability_claim": False,
    }

@app.get("/mma/cloud/markets")
def mma_cloud_markets():
    return {"count": len(MMA_MARKETS), "markets": MMA_MARKETS}

@app.post("/mma/cloud/quotes")
def mma_cloud_quote(x: MMAQuote):
    if x.decimal_odds <= 1.0:
        raise HTTPException(400, "decimal_odds must be > 1")
    ts = float(x.ts or time.time())
    with db() as c:
        cur = c.execute("""INSERT INTO mma_cloud_quotes(ts,event_id,fighter_a,fighter_b,bookmaker,provider,market,fighter,selection,line,decimal_odds,available_stake,source_url)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (ts,x.event_id,x.fighter_a,x.fighter_b,x.bookmaker,x.provider,x.market,x.fighter,x.selection,x.line,x.decimal_odds,x.available_stake,x.source_url))
        return {"ok": True, "id": cur.lastrowid, "ts": ts}

@app.get("/mma/cloud/best-price")
def mma_best_price(event_id: str, market: str, selection: str, fighter: Optional[str]=None, line: Optional[float]=None, max_age_seconds: int=600):
    cutoff = time.time() - max(1, max_age_seconds)
    with db() as c:
        rows = [dict(r) for r in c.execute("""SELECT * FROM mma_cloud_quotes
          WHERE event_id=? AND market=? AND selection=? AND ts>=?
          AND COALESCE(fighter,'')=COALESCE(?, '')
          AND ((line IS NULL AND ? IS NULL) OR line=?) ORDER BY decimal_odds DESC, ts DESC""",
          (event_id,market,selection,cutoff,fighter,line,line))]
    if not rows:
        return {"found": False, "reason": "NO_FRESH_MATCHING_QUOTES"}
    latest_by_book = {}
    for r in sorted(rows, key=lambda z:z["ts"], reverse=True):
        latest_by_book.setdefault(r["bookmaker"], r)
    ranked = sorted(latest_by_book.values(), key=lambda z:z["decimal_odds"], reverse=True)
    return {"found": True, "best": ranked[0], "alternatives": ranked[1:10], "books": len(ranked)}

def _profit_gate(model_key="mma_cloud"):
    with db() as c:
        r = c.execute("SELECT * FROM mma_cloud_model_evidence WHERE model_key=? ORDER BY created_at DESC LIMIT 1",(model_key,)).fetchone()
    if not r:
        return {"eligible": False, "reason": "NO_REAL_OUT_OF_SAMPLE_EVIDENCE"}
    r = dict(r)
    checks = {
        "predictions": r["n_predictions"] >= 500,
        "bets": r["n_bets"] >= 150,
        "roi_lower_bound_positive": r["roi_low"] is not None and r["roi_low"] > 0,
        "clv_lower_bound_positive": r["clv_low"] is not None and r["clv_low"] > 0,
        "beats_market_brier": r["brier"] is not None and r["market_brier"] is not None and r["brier"] <= r["market_brier"],
        "drawdown_bounded": r["max_drawdown"] is not None and r["max_drawdown"] <= 0.25,
        "independent_periods": r["independent_periods"] >= 3,
        "books": r["books"] >= 3,
        "leakage": bool(r["leakage_ok"]),
    }
    return {"eligible": all(checks.values()), "checks": checks, "evidence": r}

@app.get("/mma/cloud/profitability-gate")
def mma_cloud_gate(model_key: str="mma_cloud"):
    return _profit_gate(model_key)

@app.post("/mma/cloud/model-evidence")
def mma_cloud_evidence(x: EvidenceIn):
    with db() as c:
        cur=c.execute("""INSERT INTO mma_cloud_model_evidence(created_at,model_key,n_predictions,n_bets,roi,roi_low,mean_clv,clv_low,brier,market_brier,max_drawdown,independent_periods,books,leakage_ok,note)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(time.time(),x.model_key,x.n_predictions,x.n_bets,x.roi,x.roi_low,x.mean_clv,x.clv_low,x.brier,x.market_brier,x.max_drawdown,x.independent_periods,x.books,1 if x.leakage_ok else 0,x.note))
    return {"ok": True, "id": cur.lastrowid, "gate": _profit_gate(x.model_key)}

@app.post("/mma/cloud/evaluate")
def mma_cloud_evaluate(x: MMAEval):
    if not 0 < x.model_prob < 1:
        raise HTTPException(400,"model_prob must be between 0 and 1")
    bp = mma_best_price(x.event_id,x.market,x.selection,x.fighter,x.line,x.max_age_seconds)
    if not bp.get("found"):
        return {"eligible": False, "reason": bp.get("reason"), "best_price": bp}
    best=bp["best"]; odds=float(best["decimal_odds"]); ev=x.model_prob*odds-1.0
    full=max(0.0,(x.model_prob*odds-1)/(odds-1)); stake=min(x.bankroll_fraction_cap, full*0.25)
    gate=_profit_gate("mma_cloud")
    out={"eligible": gate["eligible"] and ev>0, "research_only": True, "best_price": best,
         "books_checked": bp["books"], "model_prob": x.model_prob, "implied_prob": 1/odds,
         "ev": ev, "quarter_kelly": full*0.25, "stake_fraction": stake, "profitability_gate": gate}
    if x.paper and out["eligible"]:
        with db() as c:
            cur=c.execute("""INSERT INTO mma_cloud_paper(created_at,event_id,market,fighter,selection,line,bookmaker,decimal_odds,model_prob,ev,stake_fraction,status)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,'OPEN')""",(time.time(),x.event_id,x.market,x.fighter,x.selection,x.line,best["bookmaker"],odds,x.model_prob,ev,stake))
        out["paper_bet_id"]=cur.lastrowid
    return out

@app.post("/mma/cloud/sync-moneylines")
def mma_sync_moneylines():
    provider=PROVIDERS.get("theoddsapi")
    if not provider:
        raise HTTPException(503,"The Odds API provider unavailable")
    try:
        rows=provider.fetch(MMA_SPORT,"h2h")
    except Exception as e:
        raise HTTPException(502,str(e))
    now=time.time(); inserted=0
    with db() as c:
        for r in rows:
            c.execute("""INSERT INTO mma_cloud_quotes(ts,event_id,fighter_a,fighter_b,bookmaker,provider,market,fighter,selection,line,decimal_odds)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(now,r.get("event_id"),r.get("home",""),r.get("away",""),r.get("bookmaker","unknown"),"theoddsapi","h2h",r.get("selection"),r.get("selection"),r.get("point"),float(r.get("price"))))
            inserted+=1
    return {"ok": True, "rows": inserted, "ts": now}

MOBILE_HTML = '''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Edge Platform Cloud</title><style>body{font-family:system-ui;background:#08111f;color:#eef5ff;margin:0;padding:18px}.card{background:#111d30;border:1px solid #273955;border-radius:14px;padding:14px;margin:12px 0}button{background:#2a72ff;color:white;border:0;border-radius:9px;padding:11px 14px;font-weight:700}input{width:100%;box-sizing:border-box;padding:10px;margin:5px 0;background:#0a1424;color:white;border:1px solid #344764;border-radius:8px}pre{white-space:pre-wrap;font-size:12px}.ok{color:#6ee7a5}.warn{color:#ffd66b}</style></head><body><h2>Edge Platform Cloud</h2><div class=card><b>Phone mode</b><p id=s>Checking backend…</p><button onclick=sync()>Sync UFC moneylines</button></div><div class=card><b>UFC best-price checker</b><input id=e placeholder="event id"><input id=m value="h2h"><input id=f placeholder="fighter/selection"><button onclick=best()>Check best price</button><pre id=o></pre></div><div class=card><b>Evidence gate</b><p>This cloud build will not promote a paper signal until real out-of-sample ROI/CLV/calibration evidence passes the gate.</p><button onclick=gate()>Check gate</button><pre id=g></pre></div><script>async function j(u,o){let r=await fetch(u,o);let t=await r.text();try{return JSON.parse(t)}catch{return {status:r.status,text:t}}}async function status(){let x=await j('/cloud/status');s.innerHTML=x.ok?'<span class=ok>ONLINE '+x.version+'</span>':'<span class=warn>OFFLINE</span>'}async function sync(){o.textContent=JSON.stringify(await j('/mma/cloud/sync-moneylines',{method:'POST'}),null,2)}async function best(){let u='/mma/cloud/best-price?event_id='+encodeURIComponent(e.value)+'&market='+encodeURIComponent(m.value)+'&selection='+encodeURIComponent(f.value);o.textContent=JSON.stringify(await j(u),null,2)}async function gate(){g.textContent=JSON.stringify(await j('/mma/cloud/profitability-gate'),null,2)}status()</script></body></html>'''

@app.get("/mobile", response_class=HTMLResponse)
def mobile():
    return MOBILE_HTML
