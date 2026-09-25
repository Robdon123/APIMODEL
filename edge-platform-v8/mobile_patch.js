'use strict';

(function(){const st=document.createElement('style');st.textContent=`.fight-detail{margin-top:10px;padding-top:10px;border-top:1px solid #1b324b}.detailgrid{display:grid;grid-template-columns:1fr;gap:8px}.mini{background:#071421;border:1px solid #1b324b;border-radius:10px;padding:9px}.hist{display:flex;gap:7px;align-items:flex-start;padding:6px 0;border-bottom:1px solid #142a40}.hist:last-child{border-bottom:0}.result{display:inline-grid;place-items:center;min-width:25px;height:25px;border-radius:7px;font-weight:900;background:#26394d}.result.W{background:#124733;color:#9aebc6}.result.L{background:#54222a;color:#ffb0b7}.propgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}.propitem{background:#0b1a2a;border:1px solid #213a54;border-radius:9px;padding:8px}.propitem b{font-size:14px}.location{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}.loadingbar{height:4px;background:#10243a;border-radius:999px;overflow:hidden;margin:8px 0}.loadingbar span{display:block;height:100%;background:#4a7dff;transition:width .25s}.fight-actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:9px}@media(min-width:760px){.detailgrid{grid-template-columns:1fr 1fr}.propgrid{grid-template-columns:repeat(4,minmax(0,1fr))}}`;document.head.appendChild(st)})();

let propsGeneration=0;
function didFor(f,i,prefix='detail'){return prefix+'-'+String(f.fight_key||i).replace(/[^a-zA-Z0-9_-]/g,'_')}
function marketName(k){return String(k||'').replace(/^fighter_/,'').replace(/_/g,' ').replace(/\bko tko\b/i,'KO/TKO').replace(/\bsub\b/i,'Sub').replace(/\bko\b/i,'KO')}
function renderHistory(name,rows){
  if(!rows||!rows.length)return `<div class="mini"><b>${esc(name)} — last 5</b><div class="notice" style="margin-top:7px">No stored UFC results yet for this fighter. This is a data-availability state, not a hidden error.</div></div>`;
  return `<div class="mini"><b>${esc(name)} — last ${rows.length}</b>`+rows.map(r=>`<div class="hist"><span class="result ${esc(r.result)}">${esc(r.result)}</span><div><b>${esc(r.opponent||'—')}</b><div class="muted">${esc(r.date||'')} · ${esc(r.method||'—')}${r.round?` · R${esc(r.round)}`:''}${r.time?` ${esc(r.time)}`:''}</div></div></div>`).join('')+'</div>';
}
function renderBaseline(name,p){
  if(!p||p.status!=='BASELINE_OK')return `<div class="propitem"><b>${esc(name)}</b><div class="muted">${esc((p&&p.status)||'No baseline')}</div></div>`;
  return `<div class="propitem"><b>${esc(name)}</b><div class="muted">${esc(p.expected_minutes||'—')} min expected</div><div>${esc(p.sig_strikes_expected??'—')} sig strikes</div><div>${esc(p.takedowns_expected??'—')} TD</div><div>${esc(p.submission_attempts_expected??'—')} sub attempts</div></div>`;
}
function modelPropHtml(x){
  const prob=x.probability==null?null:Number(x.probability);
  const status=x.status||'UNKNOWN';
  return `<div class="propitem"><b>${esc(x.fighter||'')}</b><div>${esc(marketName(x.market_key))}</div>${prob==null?`<div class="chip amber">${esc(status)}</div>`:`<div class="chip green">${(100*prob).toFixed(1)}%</div>`}<div class="muted">${esc(x.winner_source||x.winner_model_status||'')}</div></div>`;
}

async function connect(){
  $('connectBtn').innerHTML='<span class="spinner"></span> Connecting'; savePrefs(); state.backend=backend();state.token=token();
  try{
    const h=await fetch(backend()+'/health').then(r=>r.json());
    const p=await api('/platform/status');
    conn(true,'Connected · '+(p.version||h.version||'live'));
    await loadUfc();
  }catch(e){conn(false,'Connection failed');$('fightList').innerHTML=errBox(e)}
  finally{$('connectBtn').textContent='Connect'}
}

async function loadUfc(){
  const generation=++propsGeneration;
  $('fightList').innerHTML='<div class="muted"><span class="spinner"></span> Loading UFC card + last five…</div>';
  try{
    const card=await api('/mma/upcoming-card/current'); state.card=card;
    const ev=card.event||{}; const when=ev.event_date?new Date(ev.event_date*1000).toLocaleString():'';
    const place=[ev.venue,ev.city,ev.country].filter(Boolean).join(' · ');
    $('eventMeta').innerHTML=`<b>${esc(ev.event_name||'UFC')}</b><div class="location"><span class="chip">📍 ${esc(place||'Venue not stored')}</span>${when?`<span class="chip">🗓 ${esc(when)}</span>`:''}<span class="chip">${card.fight_count??(card.fights||[]).length} fights</span></div>`;
    const fights=card.fights||[];
    if(!fights.length){$('fightList').innerHTML='<div class="notice"><b>No fights are currently stored.</b><br>Use Controls → Refresh UFC history/model data, then refresh this card.</div>';return}

    let histories={}; let historyMeta='';
    try{
      const h=await api('/mobile-api/card-history?event_key='+encodeURIComponent(ev.event_key||fights[0].event_key));
      histories=h.last5||{};
      historyMeta=`<div class="chips"><span class="chip ${h.fighters_with_history?'green':'amber'}">Last-5 history: ${esc(h.fighters_with_history||0)}/${esc(h.fighters||fights.length*2)} fighters</span><span class="chip">One bulk history request</span></div>`;
    }catch(e){historyMeta=`<div class="notice">Last-five history request failed: ${esc(e.message||e)}</div>`}

    $('fightList').innerHTML=historyMeta+fights.map((f,i)=>{
      const pa=f.projection_a||{},pb=f.projection_b||{};
      const proj=(p,n)=>p.status==='BASELINE_OK'?`${n}: ${p.sig_strikes_expected??'—'} sig str · ${p.takedowns_expected??'—'} TD · ${p.submission_attempts_expected??'—'} sub att`:`${n}: ${p.status||'model pending'}`;
      const pdid=didFor(f,i,'props'), rdid=didFor(f,i,'rounds');
      return `<div class="fight" data-event="${esc(f.event_key||ev.event_key||'')}" data-fight="${esc(f.fight_key||'')}">
        <div class="row"><div><div class="fight-title">${esc(f.fighter_a)} <span class="muted">vs</span> ${esc(f.fighter_b)}</div><div class="muted">${esc(f.card_section||'')} · ${esc(f.weight_class||'')} · ${esc(f.rounds||3)} rounds</div></div><span class="chip">#${esc(f.bout_order??i+1)}</span></div>
        <div class="chips"><span class="chip ${pa.status==='BASELINE_OK'?'green':'amber'}">${esc(proj(pa,f.fighter_a))}</span><span class="chip ${pb.status==='BASELINE_OK'?'green':'amber'}">${esc(proj(pb,f.fighter_b))}</span></div>
        <div class="fight-detail"><div class="detailgrid">${renderHistory(f.fighter_a,histories[f.fighter_a])}${renderHistory(f.fighter_b,histories[f.fighter_b])}</div>
          <div class="fight-actions"><button class="btn secondary" data-propbtn="1" onclick="loadFightPropsButton(this)">Load props now</button></div>
          <div id="${pdid}" class="propsbox"><div class="muted">Props queued — loading one fight at a time so the backend stays responsive.</div></div>
          <div id="${rdid}" class="roundbox"></div>
        </div>
      </div>`;
    }).join('');

    // Populate props sequentially. Last-five histories are already visible immediately.
    for(let i=0;i<fights.length;i++){
      if(generation!==propsGeneration) return;
      await loadFightProps(fights[i],i,true);
      await new Promise(r=>setTimeout(r,60));
    }
  }catch(e){$('eventMeta').textContent='Could not load';$('fightList').innerHTML=errBox(e)}
}

async function loadFightPropsButton(btn){
  const fightEl=btn.closest('.fight'); if(!fightEl)return;
  const f=(state.card&&state.card.fights||[]).find(x=>String(x.fight_key)===String(fightEl.dataset.fight));
  if(!f)return;
  const i=(state.card.fights||[]).indexOf(f);
  await loadFightProps(f,i,false,true);
}

async function loadFightProps(f,i,background=false,force=false){
  const box=$(didFor(f,i,'props')); if(!box)return;
  if(box.dataset.loaded==='1'&&!force)return;
  if(box.dataset.loading==='1')return;
  box.dataset.loading='1';
  if(!background||force) box.innerHTML='<div class="muted"><span class="spinner"></span> Calculating fight props…</div>';
  try{
    const d=await api('/mobile-api/fight-props?event_key='+encodeURIComponent(f.event_key)+'&fight_key='+encodeURIComponent(f.fight_key));
    const base=d.baseline||{}, models=d.model_props||[], quotes=d.quoted_props||[];
    box.dataset.loaded='1';
    box.innerHTML=`<h3 style="margin-top:12px">Fight props & projections</h3>
      <div class="propgrid">${renderBaseline(f.fighter_a,base[f.fighter_a])}${renderBaseline(f.fighter_b,base[f.fighter_b])}${models.map(modelPropHtml).join('')}</div>
      <div class="fight-actions"><button class="btn secondary" onclick="loadRoundPropsButton(this)">Load round-by-round props</button></div>
      <div style="margin-top:10px"><b>Captured bookmaker props</b>${quotes.length?`<div class="tablewrap" style="margin-top:6px"><table class="tbl"><thead><tr><th>Book</th><th>Fighter</th><th>Market</th><th>Line/selection</th><th>Odds</th><th>Model P</th><th>EV</th></tr></thead><tbody>${quotes.map(q=>`<tr><td>${esc(q.bookmaker||q.provider||'')}</td><td>${esc(q.fighter||'')}</td><td>${esc(marketName(q.market_key))}</td><td>${esc(q.line??'')} ${esc(q.selection||'')}</td><td>${esc(q.price??'—')}</td><td>${q.model_probability==null?'—':(100*q.model_probability).toFixed(1)+'%'}</td><td>${q.ev==null?'—':(100*q.ev).toFixed(1)+'%'}</td></tr>`).join('')}</tbody></table></div>`:'<div class="notice" style="margin-top:6px">No captured bookmaker prop line is stored for this fight yet. The model props above are still shown; this message is not a model failure.</div>'}</div>`;
  }catch(e){box.innerHTML=errBox(e)}
  finally{delete box.dataset.loading}
}

async function loadRoundPropsButton(btn){
  const fightEl=btn.closest('.fight'); if(!fightEl)return;
  const f=(state.card&&state.card.fights||[]).find(x=>String(x.fight_key)===String(fightEl.dataset.fight)); if(!f)return;
  const i=(state.card.fights||[]).indexOf(f); const box=$(didFor(f,i,'rounds')); if(!box)return;
  if(box.dataset.loaded==='1')return;
  box.innerHTML='<div class="muted"><span class="spinner"></span> Loading round props…</div>';
  try{
    const d=await api('/mobile-api/fight-round-props?event_key='+encodeURIComponent(f.event_key)+'&fight_key='+encodeURIComponent(f.fight_key));
    const rows=d.round_props||[]; box.dataset.loaded='1';
    box.innerHTML=`<h3 style="margin-top:12px">Round-by-round model props</h3><div class="propgrid">${rows.map(modelPropHtml).join('')||'<div class="notice">No validated round model is available for this fight yet.</div>'}</div>`;
  }catch(e){box.innerHTML=errBox(e)}
}

$('connectBtn').onclick=connect;
