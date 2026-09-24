import os, tempfile, datetime, unittest
os.environ['DATABASE_URL']=tempfile.mktemp(suffix='.db')
import app

def seed():
    teams=['A','B','C','D','E','F']
    rows=[]; odds=[]; base=datetime.datetime(2021,8,1,tzinfo=datetime.timezone.utc)
    for i in range(240):
        h=teams[i%6]; a=teams[(i*2+1)%6]
        if h==a: a=teams[(i*2+2)%6]
        ko=base+datetime.timedelta(days=i*5)
        rows.append(dict(event_id=f'e{i}',kickoff=ko.isoformat(),home=h,away=a,home_goals=(i*7)%4,away_goals=(i*5)%3))
        for k in range(5):
            for name,price in [(h,2.0+0.01*k),('Draw',3.5+0.01*k),(a,3.8-0.01*k)]:
                odds.append((ko.timestamp()-90,'test','soccer_epl_phase8',f'e{i}',h,a,ko.isoformat(),f'b{k}','h2h',name,None,price))
    app.ingest_outcomes(rows,'soccer_epl_phase8','test')
    with app.db() as c:
        c.executemany('INSERT INTO odds_snapshots(ts,provider,sport,event_id,home,away,commence,bookmaker,market,selection,point,price) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',odds)

class Phase8Tests(unittest.TestCase):
    def test_campaign_requires_real_ingestion_inputs(self):
        with self.assertRaises(Exception) as cm:
            app.research_campaign(app.CampaignIn(sports=['soccer_epl_phase8'],seasons=[]))
        self.assertIn('seasons', str(cm.exception).lower())

    def test_stability_runs_on_latest_factory(self):
        seed()
        r=app._research_factory_run(app.FactoryIn(sport='soccer_epl_phase8',min_training=80,retrain_every=30,min_ev=0,min_books=4))
        st=app._stability_calc(app.StabilityIn(sport='soccer_epl_phase8',min_season_bets=5,min_bookmaker_bets=5,min_positive_seasons=1,min_positive_bookmakers=1))
        self.assertEqual(st['run_id'],r['run_id'])
        self.assertTrue(st['seasons'])
        self.assertTrue(st['bookmakers'])

    def test_gate_now_requires_stability(self):
        seed()
        app._research_factory_run(app.FactoryIn(sport='soccer_epl_phase8',min_training=80,retrain_every=30,min_ev=0,min_books=4))
        app._gate_config()
        st=app._evaluate_gate('soccer_epl_phase8')
        self.assertIn('stability', st)
        self.assertFalse(st['eligible'])

if __name__ == '__main__':
    unittest.main()
