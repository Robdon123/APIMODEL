import os, tempfile
os.environ['DATABASE_URL']=tempfile.mktemp(suffix='.db')
os.environ['ODDS_API_KEY']='test'
import app

def test_canonical_event_stable():
    a=app.canonical_event_id('Arsenal','Chelsea',1790000000)
    b=app.canonical_event_id('arsenal','chelsea',1790000000)
    assert a==b

def test_capture_and_quality():
    rows=[
      dict(event_id='x',home='Arsenal',away='Chelsea',commence='2026-09-24T18:00:00Z',bookmaker='book1',market='h2h',selection='Arsenal',price=2.2),
      dict(event_id='x',home='Arsenal',away='Chelsea',commence='2026-09-24T18:00:00Z',bookmaker='book1',market='h2h',selection='Draw',price=3.5),
      dict(event_id='x',home='Arsenal',away='Chelsea',commence='2026-09-24T18:00:00Z',bookmaker='book1',market='h2h',selection='Chelsea',price=3.1)]
    class Fake:
        def fetch(self,*args,**kwargs): return rows
    old=app.PROVIDERS['theoddsapi']; app.PROVIDERS['theoddsapi']=Fake()
    try:
        out=app._capture_once('soccer_epl')
        q=app.data_quality('soccer_epl')
        assert out['rows']==3 and q['odds_rows']==3 and q['canonical_events']==1
    finally:
        app.PROVIDERS['theoddsapi']=old
