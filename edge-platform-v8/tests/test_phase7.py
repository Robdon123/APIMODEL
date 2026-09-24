import os, tempfile, datetime
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
                odds.append((ko.timestamp()-90,'test','soccer_epl_phase7',f'e{i}',h,a,ko.isoformat(),f'b{k}','h2h',name,None,price))
    app.ingest_outcomes(rows,'soccer_epl_phase7','test')
    with app.db() as c:
        c.executemany('INSERT INTO odds_snapshots(ts,provider,sport,event_id,home,away,commence,bookmaker,market,selection,point,price) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',odds)

def test_factory_has_seasons_and_multiple_testing():
    seed(); r=app._research_factory_run(app.FactoryIn(sport='soccer_epl_phase7',min_training=80,retrain_every=30,min_ev=0,min_books=4))
    assert r['predictions']>100
    assert len(r['season_stats'])>=3
    assert 'multiple_testing' in r
    assert all('q_value' in x for x in r['signal_buckets'])
    assert r['roi_ci95'] is not None

def test_gate_refuses_insufficient_evidence():
    app._gate_config()
    r=app._evaluate_gate('soccer_epl_phase7')
    assert r['eligible'] is False
    assert r['status']=='RESEARCH_ONLY'

def test_gate_config_defaults_disabled():
    c=app._gate_config(); assert c['enabled']==0
