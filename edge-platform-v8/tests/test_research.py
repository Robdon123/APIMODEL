import os, sys, tempfile, unittest
os.environ['DATABASE_URL'] = 'sqlite:///' + tempfile.mktemp('.db')
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import app

class Research(unittest.TestCase):
    def test_canonical_event_is_provider_independent(self):
        self.assertEqual(app.canonical_event_id('Arsenal','Chelsea',1760000000), app.canonical_event_id('ARSENAL','Chelsea',1760000000))
    def test_elo_probs_sum_to_one(self):
        p=app.EloState().probs('a','b'); self.assertAlmostEqual(sum(p),1,places=9); self.assertTrue(all(0<x<1 for x in p))
    def test_dc_probs_sum_to_one(self):
        rows=[dict(home_team_id=f't{i%6}',away_team_id=f't{(i+1)%6}',home_goals=1+(i%3==0),away_goals=(i%2==0)) for i in range(36)]
        p=app._dc_probs(app._fit_dc(rows,steps=50),'t0','t1'); self.assertAlmostEqual(sum(p),1,places=6)
    def test_ensemble_is_model_only(self):
        rows=[dict(home_team_id=f't{i%4}',away_team_id=f't{(i+1)%4}',home_goals=1+(i%3==0),away_goals=(i%2==0)) for i in range(40)]
        elo,dc,w,cut=app._fit_ensemble(rows,.2); self.assertGreaterEqual(w,0); self.assertLessEqual(w,1); self.assertEqual(cut,32)

if __name__=='__main__': unittest.main()
