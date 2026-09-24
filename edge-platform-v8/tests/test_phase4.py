import os, tempfile, unittest, math

DB=tempfile.NamedTemporaryFile(suffix='.db', delete=False).name
os.environ['DATABASE_URL']='sqlite:///'+DB
from app import _softmax_temp, _fit_temperature, _ece, ingest_outcomes, _outcome_rows, _market_for_outcome

class Phase4Tests(unittest.TestCase):
    def test_temperature_preserves_distribution_and_normalizes(self):
        p=(.7,.2,.1)
        q=_softmax_temp(p,1.7)
        self.assertAlmostEqual(sum(q),1.0,places=10)
        self.assertGreater(q[0],q[1]); self.assertGreater(q[1],q[2])

    def test_temperature_fit_finds_reasonable_direction(self):
        rows=[]
        for i in range(30): rows.append(((.9,.06,.04),0))
        for i in range(10): rows.append(((.34,.33,.33),1))
        t=_fit_temperature(rows)
        self.assertGreaterEqual(t,0.5)

    def test_ece_zero_for_perfect_confidence(self):
        e,m=_ece([((1,0,0),0),((0,1,0),1),((0,0,1),2)])
        self.assertAlmostEqual(e,0.0)
        self.assertAlmostEqual(m,0.0)

    def test_canonical_result_ingest(self):
        n=ingest_outcomes([dict(kickoff='2026-01-01T15:00:00+00:00',home='Alpha FC',away='Beta FC',home_goals=2,away_goals=1,event_id='x')], 'soccer_epl','test')
        self.assertEqual(n,1)
        self.assertEqual(len(_outcome_rows('soccer_epl')),1)

if __name__=='__main__': unittest.main()
