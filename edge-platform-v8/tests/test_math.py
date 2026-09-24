"""Invariant tests for app.py. Stubs fastapi/httpx/pydantic so they run without those installed (they still work if installed).
Run: python -m unittest discover -s tests -v"""
import os, sys, types, tempfile, unittest, time
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
try:
    import fastapi, httpx, pydantic  # noqa
except ImportError:
    def mod(name, **kw):
        m = types.ModuleType(name); m.__dict__.update(kw); sys.modules[name] = m; return m
    class HTTPException(Exception):
        def __init__(self, status_code, detail=""): self.status_code, self.detail = status_code, detail
    class FastAPI:
        def __init__(self, **k): pass
        def _d(self, *a, **k): return lambda f: f
        get = post = _d
    class BaseModel:
        def __init__(self, **kw): [setattr(self, k, v) for k, v in kw.items()]
    mod("fastapi", FastAPI=FastAPI, HTTPException=HTTPException); mod("fastapi.responses", FileResponse=object)
    mod("httpx"); mod("pydantic", BaseModel=BaseModel)
import app

class Devig(unittest.TestCase):
    def test_sums_to_one_and_bounds(self):
        for odds in ([1.9, 2.05], [2.1, 3.4, 3.6], [1.25, 4.0, 9.0], [1.01, 30.0]):
            f = app.devig(odds); self.assertAlmostEqual(sum(f), 1, places=6)
            self.assertTrue(all(0 < x < 1 for x in f))
    def test_fair_book_unchanged(self):
        f = app.devig([2.0, 2.0]); self.assertAlmostEqual(f[0], .5, places=6)
    def test_order_preserved_and_longshot_shaded(self):
        odds = [1.3, 3.8, 9.0]; f = app.devig(odds); prop = [(1 / o) / sum(1 / x for x in odds) for o in odds]
        self.assertEqual(sorted(range(3), key=lambda i: f[i]), sorted(range(3), key=lambda i: prop[i]))
        self.assertLess(f[2], prop[2])  # power method gives longshot less than proportional

class Staking(unittest.TestCase):
    def test_no_stake_when_negative_ev(self): self.assertEqual(app.kelly(.4, 2.0)["full"], 0)
    def test_full_kelly_formula(self): self.assertAlmostEqual(app.kelly(.6, 2.0)["full"], .2)
    def test_cap(self): self.assertLessEqual(app.kelly(.9, 3.0)["recommended"], app.CFG["stake_cap"] + 1e-12)
    def test_fractions(self):
        k = app.kelly(.55, 2.1); self.assertAlmostEqual(k["quarter"], k["full"] / 4); self.assertAlmostEqual(k["half"], k["full"] / 2)

class Football(unittest.TestCase):
    def setUp(self): self.r = app.football(app.FBIn(home_xg_for=1.8, home_xg_against=1.0, away_xg_for=1.3, away_xg_against=1.4))
    def test_1x2_sums_to_one(self): self.assertAlmostEqual(self.r["home"] + self.r["draw"] + self.r["away"], 1, places=6)
    def test_over_lines_monotone(self):
        v = [self.r["over"][k] for k in ("0.5", "1.5", "2.5", "3.5", "4.5")]; self.assertEqual(v, sorted(v, reverse=True))
    def test_probs_in_range(self):
        for p in [self.r["btts"], *self.r["over"].values()]: self.assertTrue(0 <= p <= 1)
    def test_symmetric_teams(self):
        r = app.football(app.FBIn(home_xg_for=1.4, home_xg_against=1.4, away_xg_for=1.4, away_xg_against=1.4, home_adv=1.0))
        self.assertAlmostEqual(r["home"], r["away"], places=6)
    def test_scores_sorted(self): p = [s["p"] for s in self.r["top_scores"]]; self.assertEqual(p, sorted(p, reverse=True))

class MMA(unittest.TestCase):
    def test_partition(self):
        for rounds in (3, 5):
            r = app.model_mma(app.MMAIn(p_a=.62, a_ko=.45, a_sub=.2, b_ko=.35, b_sub=.15, rounds=rounds))
            tot = sum(r["a"].values()) + sum(r["b"].values()); self.assertAlmostEqual(tot, 1, places=9)
            self.assertAlmostEqual(r["goes_distance"] + r["ends_inside"], 1, places=9)
            v = list(r["over_rounds"].values()); self.assertEqual(v, sorted(v, reverse=True))

def rows(prices_by_book, market="h2h", point=None):
    out = []
    for bk, pr in prices_by_book.items():
        for sel, px in pr.items():
            out.append(dict(event_id="E1", home="A", away="B", commence="2099-01-01T00:00:00Z", bookmaker=bk, market=market, selection=sel, point=point, price=px))
    return out

class Consensus(unittest.TestCase):
    def test_fair_sums_to_one(self):
        r = rows({b: {"A": 2.0 + i * .02, "Draw": 3.4, "B": 3.9} for i, b in enumerate(["pinnacle", "b1", "b2", "b3", "b4"])})
        c = app.consensus(r); self.assertAlmostEqual(sum(v["fair"] for v in c.values()), 1, places=6)
    def test_best_price_excludes_sharp(self):
        r = rows({"pinnacle": {"A": 2.5, "B": 1.6}, "b1": {"A": 2.1, "B": 1.8}, "b2": {"A": 2.2, "B": 1.75}})
        c = app.consensus(r); self.assertEqual(c[("E1", "h2h", None, "A")]["best"], (2.2, "b2"))
    def test_one_sided_market_never_certain(self):
        r = rows({"b1": {"Yes": 3.0}, "b2": {"Yes": 3.2}}, market="player_goal_scorer_anytime")
        for v in app.consensus(r).values(): self.assertLess(v["fair"], .9)

class KnownIssues(unittest.TestCase):
    def test_close_rejected_after_kickoff(self):
        """CLV close rejects snapshots after kick-off."""
        ts = time.time()
        with app.db() as c:
            c.executemany("INSERT INTO odds_snapshots(ts,provider,sport,event_id,home,away,commence,bookmaker,market,selection,point,price) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [(ts, "t", "s", "E1", "A", "B", "2000-01-01T00:00:00Z", b, "h2h", sel, None, px) for b in ("b1", "b2", "b3", "b4") for sel, px in (("A", 2.0), ("B", 1.9))])
            c.execute("INSERT INTO paper_bets(ts,sport,event_id,market,selection,odds,fair_prob,stake) VALUES(?,?,?,?,?,?,?,?)", (ts, "s", "E1", "h2h", "A", 2.1, .5, 0))
            bid = c.execute("SELECT MAX(id) FROM paper_bets").fetchone()[0]
        with self.assertRaises(Exception): app.close(bid)
    def test_paper_bets_immutable(self):
        """Prediction lifecycle is append-only."""
        import inspect; self.assertNotIn("UPDATE paper_bets", inspect.getsource(app))

if __name__ == "__main__": unittest.main()

