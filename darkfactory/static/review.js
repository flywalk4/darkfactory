'use strict';

// The task page's review block: the reviewer's checklist, findings and per-file verdicts as it gave them.
const reviewChecks={acceptance_met:'Задача выполнена',no_regression_in_touched_code:'Нет регрессий в затронутом коде',no_secrets_or_unsafe_input:'Нет секретов и небезопасного ввода',tests_cover_change:'Тесты покрывают изменение',no_unrelated_changes:'Нет посторонних изменений'};
const reviewBlocking=new Set(['acceptance_met','no_regression_in_touched_code','no_secrets_or_unsafe_input']);
const severityNames={blocker:'Блокер',major:'Серьёзно',minor:'Мелочь'};
const reviewCache=new Map();
const filedNotes=new Set();
// Muting is offered only on notes that never block: a blocker is never "not useful".
const noteActions=(text,mutable=true)=>artifactReadonly()?'':`<div class="note-actions"><button class="quiet" data-note-backlog="${esc(text)}"${filedNotes.has(text)?' disabled':''}>${filedNotes.has(text)?'В бэклоге':'В бэклог'}</button>${mutable?`<button class="quiet" data-note-mute="${esc(text)}"${filedNotes.has('mute:'+text)?' disabled':''}>${filedNotes.has('mute:'+text)?'Ревьюер больше не будет':'Не полезно'}</button>`:''}</div>`;

// Review notes and writer's [LATER] notes stay on the task; the owner picks what becomes work.
function bindNoteActions(root,task) {
  root.querySelectorAll('[data-note-backlog]').forEach(b=>b.onclick=async()=>{try{await api(`/projects/${task.project_id}/backlog`,'POST',{text:`${b.dataset.noteBacklog} (DF-${task.id.slice(0,6).toUpperCase()})`});filedNotes.add(b.dataset.noteBacklog);b.disabled=true;b.textContent='В бэклоге';}catch(e){toast(e.message);}});
  root.querySelectorAll('[data-note-mute]').forEach(b=>b.onclick=async()=>{try{await api(`/projects/${task.project_id}/review-muted`,'POST',{text:b.dataset.noteMute});filedNotes.add('mute:'+b.dataset.noteMute);b.disabled=true;b.textContent='Ревьюер больше не будет';}catch(e){toast(e.message);}});
}

async function loadReview(task) {
  const key=`${task.phase}:${task.state}:${task.corrections}:${task.reviewed_sha||''}`;
  const cached=reviewCache.get(task.id);
  if(cached&&cached.key===key)return cached.value;
  let value=null;
  try{
    const response=await fetch(`/api/tasks/${encodeURIComponent(task.id)}/artifacts/review.json`,{headers:{Authorization:'Bearer '+token}});
    if(response.ok)value=await response.json();
  }catch{}
  reviewCache.set(task.id,{key,value});
  return value;
}

async function renderReview(task) {
  const review=await loadReview(task);
  if(!review||detailId!==task.id)return;
  const verdict=review.verdict||{},checklist=verdict.checklist||{},findings=verdict.findings||[],files=verdict.files||[];
  const section=document.createElement('section');section.className='detail-section review-block';
  const checks=Object.keys(reviewChecks).filter(k=>k in checklist).map(k=>`<li class="${checklist[k]?'ok':reviewBlocking.has(k)?'bad':'warn'}"><span>${checklist[k]?'✓':'✗'}</span>${esc(reviewChecks[k])}${!checklist[k]&&!reviewBlocking.has(k)?' <small>не блокирует</small>':''}</li>`).join('');
  const noteText=f=>(f.file?`${f.file}${f.line?':'+Number(f.line):''} — `:'')+f.text;
  const found=findings.length?findings.map(f=>`<li class="severity-${esc(f.severity)}"><div><span class="chip">${esc(severityNames[f.severity]||f.severity)} · ${Number(f.confidence)||0}%</span>${f.env?'<span class="chip">окружение</span>':''}${f.file?` <code>${esc(f.file)}${f.line?':'+Number(f.line):''}</code>`:''}</div><p>${esc(f.text)}</p>${noteActions(noteText(f),f.severity==='minor'||Number(f.confidence)<80||f.env)}</li>`).join(''):
    (review.findings||[]).map(text=>`<li><p>${esc(text)}</p>${noteActions(text.replace(/^\[(LATER|ENV)\] /,''),/^\[(LATER|ENV)\] /.test(text))}</li>`).join('');
  const issues=files.filter(f=>f.verdict==='issue');
  const filesBlock=files.length?`<details><summary>Проверено файлов: ${files.length}${issues.length?` · с замечаниями: ${issues.length}`:''}</summary><ul class="review-files">${files.map(f=>`<li class="${f.verdict==='issue'?'bad':'ok'}"><code>${esc(f.path)}</code><p>${esc(f.evidence)}</p></li>`).join('')}</ul></details>`:'';
  const uncovered=review.uncovered?.length?`<p class="review-uncovered">Ревью не проверило: ${review.uncovered.map(p=>`<code>${esc(p)}</code>`).join(', ')}</p>`:'';
  section.innerHTML=`<div class="section-head"><h3>Ревью</h3><span class="chip ${review.approved?'review-approved':'review-rejected'}">${review.approved?'Одобрено':'Вернуло на доработку'}</span></div>${checks?`<ul class="review-checks">${checks}</ul>`:''}${found?`<ul class="review-findings">${found}</ul>`:'<p class="muted small">Замечаний нет.</p>'}${uncovered}${filesBlock}`;
  bindNoteActions(section,task);
  if(detailId!==task.id)return;
  document.querySelectorAll('.review-block').forEach(old=>old.remove());
  ($('#detail-extra')||$('#detail-body')).append(section);
}
