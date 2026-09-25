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

    # Cache expensive historical model inputs for five minutes. These are all based on
    # pre-fight historical data, so a short TTL avoids retraining the same event/fighter
    # pair dozens of times while keeping refreshes responsive to newly imported history.
    ttl=300
    caches={k:{} for k in ('results','winner','fighter_history','splits','rounds')}
    def bucket(): return int(time.time()//ttl)
    def memo(cache_name,key,fn):
        ck=(bucket(),)+tuple(key)
        c=caches[cache_name]
        if ck not in c:
            # keep only current bucket to cap memory
            if len(c)>256: c.clear()
            c[ck]=fn()
        return c[ck]

    if hasattr(core,'_mma_results_before'):
        orig_results=core._mma_results_before
        def cached_results(organization='UFC',cutoff=None):
            c=float(cutoff or time.time())
            return memo('results',(organization,c),lambda:orig_results(organization,c))
        core._mma_results_before=cached_results

    if hasattr(core,'_mma_winner_model'):
        orig_winner=core._mma_winner_model
        def cached_winner(fighter,opponent,cutoff=None,organization='UFC',min_training=20):
            c=float(cutoff or time.time())
            return memo('winner',(fighter,opponent,c,organization,int(min_training)),lambda:orig_winner(fighter,opponent,c,organization,min_training))
        core._mma_winner_model=cached_winner

    if hasattr(core,'_fighter_method_history'):
        orig_fh=core._fighter_method_history
        def cached_fh(name,cutoff,organization='UFC'):
            c=float(cutoff or time.time())
            return memo('fighter_history',(name,c,organization),lambda:orig_fh(name,c,organization))
        core._fighter_method_history=cached_fh

    if hasattr(core,'_method_splits'):
        orig_splits=core._method_splits
        def cached_splits(fighter,opponent,cutoff,organization='UFC'):
            c=float(cutoff or time.time())
            return memo('splits',(fighter,opponent,c,organization),lambda:orig_splits(fighter,opponent,c,organization))
        core._method_splits=cached_splits

    if hasattr(core,'_round_weights'):
        orig_rounds=core._round_weights
        def cached_rounds(fighter,opponent,cutoff,method=None,rounds=3,organization='UFC'):
            c=float(cutoff or time.time())
            return memo('rounds',(fighter,opponent,c,method,int(rounds),organization),lambda:orig_rounds(fighter,opponent,c,method,rounds,organization))
        core._round_weights=cached_rounds

    def event_and_fights(event_key):
        with core.db() as c:
            er=c.execute('SELECT * FROM mma_card_events WHERE event_key=?',(event_key,)).fetchone()
            fights=[dict(r) for r in c.execute('SELECT * FROM mma_card_fights WHERE event_key=? ORDER BY bout_order',(event_key,)).fetchall()]
        return (dict(er) if er else {}),fights

    def history_for_names(names,cutoff):
        display={norm(n):n for n in names if n}
        wanted=set(display)
        out={n:[] for n in names if n}
        if not wanted: return out,0
        with core.db() as c:
            rows=[dict(r) for r in c.execute('''SELECT fighter_a,fighter_b,winner,method,round,time_seconds,kickoff,source,source_url
                                                FROM mma_results
                                                WHERE organization='UFC' AND winner IS NOT NULL AND kickoff IS NOT NULL AND kickoff<?
                                                ORDER BY kickoff DESC LIMIT 3000''',(float(cutoff),)).fetchall()]
        for r in rows:
            a,b=norm(r.get('fighter_a')),norm(r.get('fighter_b'))
            for key,is_a in ((a,True),(b,False)):
                if key not in wanted: continue
                name=display[key]
                if len(out[name])>=5: continue
                opp=r.get('fighter_b') if is_a else r.get('fighter_a')
                w=norm(r.get('winner'))
                result='W' if w==key else ('L' if w in (a,b) else 'D')
                sec=int(r.get('time_seconds') or 0)
                out[name].append({'date':datetime.datetime.fromtimestamp(float(r['kickoff']),datetime.timezone.utc).date().isoformat(),
                                  'kickoff':r.get('kickoff'),'opponent':opp,'result':result,'method':r.get('method') or '—',
                                  'round':r.get('round'),'time':f'{sec//60}:{sec%60:02d}' if sec else None,
                                  'source':r.get('source'),'source_url':r.get('source_url')})
        return out,len(rows)

    def model_summary(f,ev):
        cutoff=float((ev or {}).get('event_date') or time.time())
        results=[]; lookup={}
        for fighter,opp in ((f['fighter_a'],f['fighter_b']),(f['fighter_b'],f['fighter_a'])):
            try:
                win=core._mma_winner_model(fighter,opp,cutoff,'UFC')
            except Exception as e:
                win={'status':'ERROR','error':str(e)}
            anchor=None
            if win.get('status')=='OK':
                pwin=win.get('probability'); source='reinforced_model'
            else:
                try: anchor=core._market_anchor_win_prob(f['event_key'],f['fight_key'],fighter)
                except Exception: anchor=None
                pwin=anchor.get('probability') if anchor else None; source='market_anchor' if anchor else None
            try: splits=core._method_splits(fighter,opp,cutoff,'UFC')
            except Exception: splits=None
            statuses=[]
            if pwin is None: statuses.append(win.get('status') or 'INSUFFICIENT_HISTORY')
            if splits is None: statuses.append('INSUFFICIENT_METHOD_HISTORY')
            status='OK' if pwin is not None and splits is not None else (statuses[-1] if statuses else 'MODEL_NOT_READY')
            probs={
                'fight_winner':pwin,
                'fighter_by_ko_tko':None if pwin is None or not splits else pwin*splits.get('ko',0),
                'fighter_by_submission':None if pwin is None or not splits else pwin*splits.get('sub',0),
                'fighter_by_decision':None if pwin is None or not splits else pwin*splits.get('dec',0),
            }
            for market,p in probs.items():
                row={'fighter':fighter,'market_key':market,'selection':fighter if market=='fight_winner' else 'Yes',
                     'status':status if p is None else 'OK','probability':p,'winner_source':source,
                     'winner_model_status':win.get('status'),'model':'cached chronological winner + method split summary v24'}
                results.append(row); lookup[(norm(fighter),market)]=p
        return results,lookup

    def quoted_props_raw(f,lookup):
        with core.db() as c:
            qs=[dict(r) for r in c.execute('''SELECT * FROM mma_prop_odds WHERE event_key=? AND fight_key=? ORDER BY ts DESC,id DESC LIMIT 250''',
                                            (f['event_key'],f['fight_key'])).fetchall()]
        seen=set(); out=[]
        for q in qs:
            key=(q.get('bookmaker'),q.get('market_key'),norm(q.get('fighter')),q.get('line'),q.get('selection'))
            if key in seen: continue
            seen.add(key)
            p=lookup.get((norm(q.get('fighter')),q.get('market_key')))
            price=q.get('effective_price') or q.get('price')
            out.append({'bookmaker':q.get('bookmaker'),'market_key':q.get('market_key'),'fighter':q.get('fighter'),
                        'line':q.get('line'),'selection':q.get('selection'),'price':price,'raw_price':q.get('price'),
                        'provider':q.get('provider') or q.get('source'),'ts':q.get('ts'),'model_probability':p,
                        'ev':None if p is None or not price else p*float(price)-1})
        return out

    @core.app.get('/mobile-api/card-history')
    def mobile_card_history(event_key:str):
        ev,fights=event_and_fights(event_key)
        if not ev and not fights: raise HTTPException(404,'event not found')
        cutoff=float(ev.get('event_date') or time.time())
        names=[]
        for f in fights: names.extend([f.get('fighter_a'),f.get('fighter_b')])
        histories,scanned=history_for_names(names,cutoff)
        loaded=sum(1 for n in names if histories.get(n))
        return {'event':{'event_key':event_key,'event_name':ev.get('event_name'),'event_date':ev.get('event_date'),
                         'venue':ev.get('venue'),'city':ev.get('city'),'country':ev.get('country')},
                'last5':histories,'fighters':len(names),'fighters_with_history':loaded,'results_scanned':scanned,
                'cache_ttl_seconds':ttl,'research_only':True,'real_money_execution':False}

    @core.app.get('/mobile-api/fight-props')
    def mobile_fight_props(event_key:str, fight_key:str):
        with core.db() as c:
            fr=c.execute('SELECT * FROM mma_card_fights WHERE event_key=? AND fight_key=?',(event_key,fight_key)).fetchone()
            er=c.execute('SELECT * FROM mma_card_events WHERE event_key=?',(event_key,)).fetchone()
        if not fr: raise HTTPException(404,'fight not found')
        f=dict(fr); ev=dict(er) if er else {}
        pa=core._p16_projection(f['fighter_a'],f['fighter_b'],int(f.get('rounds') or 3))
        pb=core._p16_projection(f['fighter_b'],f['fighter_a'],int(f.get('rounds') or 3))
        models,lookup=model_summary(f,ev)
        return {'event':{'event_key':event_key,'event_name':ev.get('event_name'),'event_date':ev.get('event_date'),
                         'venue':ev.get('venue'),'city':ev.get('city'),'country':ev.get('country')},
                'fight':f,'baseline':{f['fighter_a']:pa,f['fighter_b']:pb},'model_props':models,
                'quoted_props':quoted_props_raw(f,lookup),'cache_ttl_seconds':ttl,
                'research_only':True,'real_money_execution':False}

    @core.app.get('/mobile-api/fight-round-props')
    def mobile_fight_round_props(event_key:str, fight_key:str):
        with core.db() as c:
            fr=c.execute('SELECT * FROM mma_card_fights WHERE event_key=? AND fight_key=?',(event_key,fight_key)).fetchone()
            er=c.execute('SELECT * FROM mma_card_events WHERE event_key=?',(event_key,)).fetchone()
        if not fr: raise HTTPException(404,'fight not found')
        f=dict(fr); ev=dict(er) if er else {}; cutoff=float(ev.get('event_date') or time.time()); rounds=int(f.get('rounds') or 3)
        rows=[]
        for fighter,opp in ((f['fighter_a'],f['fighter_b']),(f['fighter_b'],f['fighter_a'])):
            try: win=core._mma_winner_model(fighter,opp,cutoff,'UFC')
            except Exception as e: win={'status':'ERROR','error':str(e)}
            anchor=None
            if win.get('status')=='OK': pwin=win.get('probability'); source='reinforced_model'
            else:
                try: anchor=core._market_anchor_win_prob(event_key,fight_key,fighter)
                except Exception: anchor=None
                pwin=anchor.get('probability') if anchor else None; source='market_anchor' if anchor else None
            try: splits=core._method_splits(fighter,opp,cutoff,'UFC')
            except Exception: splits=None
            if pwin is None or not splits:
                rows.append({'fighter':fighter,'status':'INSUFFICIENT_METHOD_HISTORY','winner_model_status':win.get('status')}); continue
            rko=core._round_weights(fighter,opp,cutoff,'ko',rounds,'UFC')
            rsub=core._round_weights(fighter,opp,cutoff,'sub',rounds,'UFC')
            pko=pwin*splits.get('ko',0); psub=pwin*splits.get('sub',0)
            for rnd in range(1,min(5,rounds)+1):
                rows.append({'fighter':fighter,'round':rnd,'market_key':f'fighter_by_ko_tko_round_{rnd}','status':'OK','probability':pko*rko[rnd-1],'winner_source':source})
                rows.append({'fighter':fighter,'round':rnd,'market_key':f'fighter_by_submission_round_{rnd}','status':'OK','probability':psub*rsub[rnd-1],'winner_source':source})
                rows.append({'fighter':fighter,'round':rnd,'market_key':f'fighter_win_round_{rnd}','status':'OK','probability':pko*rko[rnd-1]+psub*rsub[rnd-1],'winner_source':source})
        return {'fight':f,'round_props':rows,'cache_ttl_seconds':ttl,'research_only':True,'real_money_execution':False}

    # Backward-compatible endpoint; now uses the optimized split paths instead of running
    # every reinforced market and quote model in one request.
    @core.app.get('/mobile-api/fight-detail')
    def mobile_fight_detail(event_key:str, fight_key:str):
        props=mobile_fight_props(event_key,fight_key)
        f=props['fight']; ev=props['event']; cutoff=float(ev.get('event_date') or time.time())
        histories,_=history_for_names([f['fighter_a'],f['fighter_b']],cutoff)
        return {**props,'last5':histories,'history_loaded':bool(histories.get(f['fighter_a']) or histories.get(f['fighter_b']))}

    core.mobile_fight_detail=mobile_fight_detail
