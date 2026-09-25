"""Mobile-only API additions for the hosted full Edge Platform UI."""
from __future__ import annotations
import datetime, re, time
from fastapi import HTTPException


def install(core):
    def norm(name):
        fn=getattr(core,'_norm_fighter',None)
        if fn:
            try: return fn(name)
            except Exception: pass
        return re.sub(r'[^a-z0-9]+','',str(name or '').lower())

    def last5(name, cutoff):
        target=norm(name)
        with core.db() as c:
            rows=[dict(r) for r in c.execute('''SELECT fighter_a,fighter_b,winner,method,round,time_seconds,kickoff,source,source_url
                                                FROM mma_results
                                                WHERE organization='UFC' AND winner IS NOT NULL AND kickoff IS NOT NULL AND kickoff<?
                                                ORDER BY kickoff DESC LIMIT 2500''',(float(cutoff),)).fetchall()]
        out=[]
        for r in rows:
            a,b=norm(r.get('fighter_a')),norm(r.get('fighter_b'))
            if target not in (a,b): continue
            is_a=target==a
            opp=r.get('fighter_b') if is_a else r.get('fighter_a')
            w=norm(r.get('winner'))
            result='W' if w==target else ('L' if w in (a,b) else 'D')
            sec=int(r.get('time_seconds') or 0)
            out.append({'date':datetime.datetime.fromtimestamp(float(r['kickoff']),datetime.timezone.utc).date().isoformat(),
                        'kickoff':r.get('kickoff'),'opponent':opp,'result':result,'method':r.get('method') or '—',
                        'round':r.get('round'),'time':f'{sec//60}:{sec%60:02d}' if sec else None,
                        'source':r.get('source'),'source_url':r.get('source_url')})
            if len(out)>=5: break
        return out

    def common_model_props(f, ev):
        cutoff=float((ev or {}).get('event_date') or time.time())
        rounds=int(f.get('rounds') or 3)
        rows=[]
        for fighter,opp in ((f['fighter_a'],f['fighter_b']),(f['fighter_b'],f['fighter_a'])):
            markets=[('fight_winner',fighter),('fighter_by_ko_tko','Yes'),('fighter_by_submission','Yes'),('fighter_by_decision','Yes')]
            for rnd in range(1,min(5,rounds)+1):
                markets.extend([(f'fighter_by_ko_tko_round_{rnd}','Yes'),(f'fighter_by_submission_round_{rnd}','Yes')])
            for market,selection in markets:
                try:
                    m=core._mma_reinforced_model_core(fighter,opp,market,None,'UFC',3,20,cutoff,f['event_key'],f['fight_key'],selection)
                    rows.append({'fighter':fighter,'market_key':market,'selection':selection,'status':m.get('status'),
                                 'probability':m.get('probability'),'model':m.get('model'),'winner_source':m.get('winner_source')})
                except Exception as e:
                    rows.append({'fighter':fighter,'market_key':market,'selection':selection,'status':'ERROR','error':str(e)})
        return rows

    def quoted_props(f):
        with core.db() as c:
            qs=[dict(r) for r in c.execute('''SELECT * FROM mma_prop_odds WHERE event_key=? AND fight_key=? ORDER BY ts DESC,id DESC LIMIT 250''',
                                            (f['event_key'],f['fight_key'])).fetchall()]
        seen=set(); out=[]
        for q in qs:
            key=(q.get('bookmaker'),q.get('market_key'),norm(q.get('fighter')),q.get('line'),q.get('selection'))
            if key in seen: continue
            seen.add(key)
            try: m=core._mma_probability_for_quote(q,3)
            except Exception as e: m={'status':'ERROR','error':str(e)}
            p=m.get('quote_probability',m.get('probability'))
            out.append({'bookmaker':q.get('bookmaker'),'market_key':q.get('market_key'),'fighter':q.get('fighter'),
                        'line':q.get('line'),'selection':q.get('selection'),'price':q.get('effective_price') or q.get('price'),
                        'raw_price':q.get('price'),'provider':q.get('provider') or q.get('source'),'ts':q.get('ts'),
                        'model_status':m.get('status'),'model_probability':p,
                        'ev':None if p is None or not q.get('price') else p*float(q['price'])-1})
        return out

    @core.app.get('/mobile-api/fight-detail')
    def mobile_fight_detail(event_key:str, fight_key:str):
        with core.db() as c:
            fr=c.execute('SELECT * FROM mma_card_fights WHERE event_key=? AND fight_key=?',(event_key,fight_key)).fetchone()
            er=c.execute('SELECT * FROM mma_card_events WHERE event_key=?',(event_key,)).fetchone()
        if not fr: raise HTTPException(404,'fight not found')
        f=dict(fr); ev=dict(er) if er else {}; cutoff=float(ev.get('event_date') or time.time())
        pa=core._p16_projection(f['fighter_a'],f['fighter_b'],int(f.get('rounds') or 3))
        pb=core._p16_projection(f['fighter_b'],f['fighter_a'],int(f.get('rounds') or 3))
        h_a=last5(f['fighter_a'],cutoff); h_b=last5(f['fighter_b'],cutoff)
        return {'event':{'event_key':event_key,'event_name':ev.get('event_name'),'event_date':ev.get('event_date'),
                         'venue':ev.get('venue'),'city':ev.get('city'),'country':ev.get('country')},
                'fight':f,'last5':{f['fighter_a']:h_a,f['fighter_b']:h_b},'history_loaded':bool(h_a or h_b),
                'baseline':{f['fighter_a']:pa,f['fighter_b']:pb},'model_props':common_model_props(f,ev),
                'quoted_props':quoted_props(f),'research_only':True,'real_money_execution':False}

    core.mobile_fight_detail=mobile_fight_detail
