'use strict';

// Readable task state: a one-line "now", a history without bookkeeping events, and what needs the owner.
const HISTORY_NOISE=new Set(['phase_started','phase_finished','executor_session','quota_observed','budget_reserved','time_slice']);
let historyAll=false;

function blockingFindings(message){try{return (JSON.parse(message).findings||[]).filter(f=>!/^\[(LATER|ENV)\]/.test(String(f))).length;}catch{return 0;}}

function nowLine(t){
  const events=t.events||[],last=kinds=>[...events].reverse().find(e=>kinds.includes(e.kind));
  if(t.state==='running'||t.state==='recovering'){
    const activity=activityLabel(t),fixing=t.phase==='implement'&&t.corrections>0;
    return (fixing?'Разработчик исправляет замечания ревью':phases[t.phase]||t.phase)+(activity?' · '+activity.text:'');
  }
  if(t.state==='needs_human')return t.awaits_director?'Решает директор':'Нужно твоё решение'+(t.reason?': '+reasonLabel(t.reason):'');
  if(t.waiting_for)return 'Ждёт '+t.waiting_for;
  if(t.state==='queued'){
    const correction=last(['correction','phase_started']);
    if(correction?.kind==='correction'){const n=blockingFindings(correction.message);return `Ревью вернуло${n?' '+n+' замеч.':''}, разработчик исправит`;}
    return 'В очереди: '+(phases[t.phase]||t.phase);
  }
  if(t.state==='review_required')return 'Ревью одобрило, нужно твоё слияние в preprod';
  if(t.state==='merged')return t.promotion?.state==='pending'?'Слита в preprod, ждёт переноса в main':'Слита в preprod';
  if(t.state==='cancelled')return 'Отменена'+(t.reason?': '+t.reason:'');
  return names[t.state]||t.state;
}

// Newest first. Repeats of one kind in a row become one row with a count and the time they span.
function compactHistory(history,all){
  const rows=[];
  for(const e of history){
    if(!all&&HISTORY_NOISE.has(e.kind))continue;
    const previous=rows[rows.length-1];
    if(previous&&previous.kind===e.kind&&!['human_message','correction','supervisor','supervisor_escalated','clarification','stopped','limit','execution_error'].includes(e.kind)){previous.repeat=(previous.repeat||1)+1;previous.since=e.at;continue;}
    rows.push({...e});
  }
  return rows;
}

// What the owner must act on, above the board: decisions the Director left to them and merges they approve.
function renderAttention(taskList){
  const box=$('#attention');if(!box)return;
  const items=taskList.filter(t=>(t.state==='needs_human'&&!t.awaits_director)||t.state==='review_required');
  box.hidden=!items.length;if(!items.length){box.innerHTML='';return;}
  box.innerHTML=`<div class="main-pending-head"><b>Ждут тебя: ${items.length}</b><span>Директор не решил это сам или нужно твоё слияние.</span></div><div class="main-pending-list">${items.map(t=>`<button class="main-pending-item attention-item" data-attention="${t.id}"><span>${esc(t.title)}</span><small>${esc(t.state==='review_required'?'Слить в preprod':reasonLabel(t.reason)||'Нужно решение')}</small></button>`).join('')}</div>`;
  box.querySelectorAll('[data-attention]').forEach(b=>b.onclick=()=>showDetail(b.dataset.attention).catch(error=>toast(error.message)));
}
