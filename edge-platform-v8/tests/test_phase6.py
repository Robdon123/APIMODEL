import os, tempfile, datetime, random
os.environ['DATABASE_URL']=tempfile.mktemp(suffix='.db')
import app

def _seed():
    teams=['A','B','C','D','E','F']
    rows=[]
    odds=[]
    base=datetime.datetime(2024,1,1,tzinfo=datetime.timezone.utc)
    for i in range(110):
        h=teams[i%len(teams)]; a=teams[(i*3+1)%len(teams)]
        if h==a: a=teams[(i*3+2)%len(teams)]
        ko=base+datetime.timedelta(days=i)
        hg=(i*7)%4; ag=(i*5)%3
        event=f'e{i}'
        rows.append(dict(event_id=event,kickoff=ko.isoformat(),home=h,away=a,home_goals=hg,away_goals=ag))
        ts=ko.timestamp()-120
        # Four bookmakers with a coherent, slightly varied 1X2 market.
        prices={'A':2.2,'D':3.4,'B':3.3}
        for k in range(4):
            names=[h,'Draw',a]
            ps=[2.1+0.02*k,3.4+0.01*k,3.5-0.02*k]
            for name,price in zip(names,ps):
                odds.append(dict(event_id=event,home=h,away=a,commence=ko.isoformat(),bookmaker=f'b{k}',market='h2h',selection=name,price=price,point=None,ts=ts))
    app.ingest_outcomes(rows,'soccer_epl_phase6','test')
    with app.db() as c:
        c.executemany('INSERT INTO odds_snapshots(ts,provider,sport,event_id,home,away,commence,bookmaker,market,selection,point,price) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
          [(r['ts'],'test','soccer_epl_phase6',r['event_id'],r['home'],r['away'],r['commence'],r['bookmaker'],r['market'],r['selection'],r['point'],r['price']) for r in odds])

def test_factory_is_walk_forward_and_execution_aware():
    _seed()
    out=app._research_factory_run(app.FactoryIn(sport='soccer_epl_phase6',min_training=60,retrain_every=20,min_ev=0.0,min_books=4,max_odds_age_seconds=300,slippage_bps=20,stake_fraction=.01))
    assert out['predictions']>=49
    assert out['bets']>0
    assert out['controls']['slippage_bps']==20
    assert out['max_drawdown']>=0

def test_leakage_audit_passes_for_pre_kickoff_data():
    _seed()
    q=app.research_leakage('soccer_epl_phase6')
    assert q['pass'] is True
