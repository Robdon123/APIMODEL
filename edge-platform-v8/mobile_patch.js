'use strict';

(function(){const st=document.createElement('style');st.textContent=`.fight-detail{margin-top:10px;padding-top:10px;border-top:1px solid #1b324b}.detailgrid{display:grid;grid-template-columns:1fr;gap:8px}.mini{background:#071421;border:1px solid #1b324b;border-radius:10px;padding:9px}.hist{display:flex;gap:7px;align-items:flex-start;padding:6px 0;border-bottom:1px solid #142a40}.hist:last-child{border-bottom:0}.result{display:inline-grid;place-items:center;min-width:25px;height:25px;border-radius:7px;font-weight:900;background:#26394d}.result.W{background:#124733;color:#9aebc6}.result.L{background:#54222a;color:#ffb0b7}.propgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}.propitem{background:#0b1a2a;border:1px solid #213a54;border-radius:9px;padding:8px}.propitem b{font-size:14px}.location{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}@media(min-width:760px){.detailgrid{grid-template-columns:1fr 1fr}.propgrid{grid-template-columns:repeat(4,minmax(0,1fr))}}`;document.head.appendChild(st)})();
async function connect(){
  $('connectBtn').innerHTML='<span class="spinner"></span> Connecting'; savePrefs(); state.backend=backend();state.token=token();
  try{const h=await fetch(backend()+'/health').then(r=>r.json()); const p=await api('/platform/status'); conn(true,'Connected · '+(p.version||h.version||'live')); try{const hs=await api('/data/mma/ufcstats/status'); if(Number(hs.results||0)<100){conn(true,'Connected · building UFC history…'); await api('/mma/bootstrap-research-data',{method:'POST'}); conn(true,'Connected · '+(p.version||h.version||'live'));}}catch(_e){} await Promise.allSettled([loadUfc(),loadBackend(),loadReadiness()]);}
  catch(e){conn(false,'Connection failed');$('fightList').innerHTML=errBox(e)} finally{$('connectBtn').textContent='Connect'}
}

async function loadUfc(){
  $('fightList').innerHTML='<div class="muted"><span class="spinner"></span> Loading card…</div>';
  try{
    const card=await api('/mma/upcoming-card/current'); state.card=card;
    const ev=card.event||{}; const when=ev.event_date?new Date(ev.event_date*1000).toLocaleString():'';
    const place=[ev.venue,ev.city,ev.country].filter(Boolean).join(' · ');
    $('eventMeta').innerHTML=`<b>${esc(ev.event_name||'UFC')}</b><div class="location"><span class="chip">📍 ${esc(place||'Venue not stored')}</span>${when?`<span class="chip">🗓 ${esc(when)}</span>`:''}<span class="chip">${card.fight_count??(card.fights||[]).length} fights</span></div>`;
    const fights=card.fights||[]; if(!fights.length){$('fightList').innerHTML='<div class="notice"><b>No fights are currently stored.</b><br>Use Controls → Refresh UFC history/model data, then refresh this card. If it remains empty, check Backend Doctor.</div>';return}
    $('fightList').innerHTML=fights.map((f,i)=>{
      const pa=f.projection_a||{},pb=f.projection_b||{};
      const proj=(p,n)=>p.status==='BASELINE_OK'?`${n}: ${p.sig_strikes_expected??'—'} sig str · ${p.takedowns_expected??'—'} TD · ${p.submission_attempts_expected??'—'} sub att · ${p.confidence||''}`:`${n}: ${p.status||'model pending'}`;
      const did='detail-'+String(f.fight_key||i).replace(/[^a-zA-Z0-9_-]/g,'_');
      return `<div class="fight"><div class="row"><div><div class="fight-title">${esc(f.fighter_a)} <span class="muted">vs</span> ${esc(f.fighter_b)}</div><div class="muted">${esc(f.card_section||'')} · ${esc(f.weight_class||'')} · ${esc(f.rounds||3)} rounds</div></div><span class="chip">#${esc(f.bout_order??i+1)}</span></div><div class="chips"><span class="chip ${pa.status==='BASELINE_OK'?'green':'amber'}">${esc(proj(pa,f.fighter_a))}</span><span class="chip ${pb.status==='BASELINE_OK'?'green':'amber'}">${esc(proj(pb,f.fighter_b))}</span></div><div id="${did}" class="fight-detail"><div class="muted"><span class="spinner"></span> Loading last 5 fights + fight props…</div></div></div>`
    }).join('');
    await Promise.allSettled(fights.map((f,i)=>loadFightDetail(f,i)));
  }catch(e){$('eventMeta').textContent='Could not load';$('fightList').innerHTML=errBox(e)}
}
function marketName(k){return String(k||'').replace(/^fighter_/,'').replace(/_/g,' ').replace(/\bko tko\b/i,'KO/TKO').replace(/\bsub\b/i,'Sub')}
function renderHistory(name,rows){
  if(!rows||!rows.length)return `<div class="mini"><b>${esc(name)} — last 5</b><div class="notice" style="margin-top:7px">Historical UFC results are not loaded for this fighter yet.</div></div>`;
  return `<div class="mini"><b>${esc(name)} — last ${rows.length}</b>`+rows.map(r=>`<div class="hist"><span class="result ${esc(r.result)}">${esc(r.result)}</span><div><b>${esc(r.opponent||'—')}</b><div class="muted">${esc(r.date||'')} · ${esc(r.method||'—')}${r.round?` · R${esc(r.round)}`:''}${r.time?` ${esc(r.time)}`:''}</div></div></div>`).join('')+'</div>';
}
function renderBaseline(name,p){
  if(!p||p.status!=='BASELINE_OK')return `<div class="propitem"><b>${esc(name)}</b><div class="muted">${esc((p&&p.status)||'No baseline')}</div></div>`;
  return `<div class="propitem"><b>${esc(name)}</b><div class="muted">${esc(p.expected_minutes||'—')} min</div><div>${esc(p.sig_strikes_expected??'—')} sig strikes</div><div>${esc(p.takedowns_expected??'—')} TD</div><div>${esc(p.submission_attempts_expected??'—')} sub attempts</div></div>`;
}
async function loadFightDetail(f,i){
  const did='detail-'+String(f.fight_key||i).replace(/[^a-zA-Z0-9_-]/g,'_'), box=$(did); if(!box)return;
  try{
    const d=await api('/mobile-api/fight-detail?event_key='+encodeURIComponent(f.event_key)+'&fight_key='+encodeURIComponent(f.fight_key));
    const h=d.last5||{}, base=d.baseline||{}, models=(d.model_props||[]).filter(x=>x.status==='OK'&&x.probability!=null), quotes=d.quoted_props||[];
    const keyModels=models.filter(x=>['fight_winner','fighter_by_ko_tko','fighter_by_submission','fighter_by_decision'].includes(x.market_key));
    box.innerHTML=`<div class="detailgrid">${renderHistory(f.fighter_a,h[f.fighter_a])}${renderHistory(f.fighter_b,h[f.fighter_b])}</div>
      <h3 style="margin-top:12px">Fight props & projections</h3>
      <div class="propgrid">${renderBaseline(f.fighter_a,base[f.fighter_a])}${renderBaseline(f.fighter_b,base[f.fighter_b])}${keyModels.map(x=>`<div class="propitem"><b>${esc(x.fighter)}</b><div>${esc(marketName(x.market_key))}</div><div class="chip green">${(100*x.probability).toFixed(1)}%</div></div>`).join('')}</div>
      <div style="margin-top:10px"><b>Captured bookmaker props</b>${quotes.length?`<div class="tablewrap" style="margin-top:6px"><table class="tbl"><thead><tr><th>Book</th><th>Fighter</th><th>Market</th><th>Line/selection</th><th>Odds</th><th>Model P</th><th>EV</th></tr></thead><tbody>${quotes.map(q=>`<tr><td>${esc(q.bookmaker||q.provider||'')}</td><td>${esc(q.fighter||'')}</td><td>${esc(marketName(q.market_key))}</td><td>${esc(q.line??'')} ${esc(q.selection||'')}</td><td>${esc(q.price??'—')}</td><td>${q.model_probability==null?'—':(100*q.model_probability).toFixed(1)+'%'}</td><td>${q.ev==null?'—':(100*q.ev).toFixed(1)+'%'}</td></tr>`).join('')}</tbody></table></div>`:'<div class="notice" style="margin-top:6px">No captured bookmaker prop lines for this fight yet. Model projections above still show; provider sync will populate bookmaker lines when your feeds expose them.</div>'}</div>`;
  }catch(e){box.innerHTML=errBox(e)}
}

$('connectBtn').onclick=connect;
