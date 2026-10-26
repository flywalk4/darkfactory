'use strict';
// Live agent activity: polls the redacted transcript the executor writes for each run.
// `next` is a byte offset into the run's transcript file, so each poll reads only what is new.
const transcriptKinds={message:'Агент',reasoning:'Размышление',tool:'Действие',output:'Вывод',status:'Статус',hint:'Подсказка оператора',files:'Изменены файлы'};
function usageLabel(u) {
  const k=n=>n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?Math.round(n/1e3)+'k':String(n||0);
  return `${k(u.input_tokens)} вход / ${k(u.output_tokens)} выход`+(u.cost_usd!=null?` · ≈$${u.cost_usd.toFixed(2)}`:'');
}
const transcriptRoles={locate:'Поиск файлов',plan:'План',implement:'Разработка',review:'Ревью',checks:'Проверки',director:'Директор: остановка'};
let transcriptState=null;
function transcriptDialog() {
  let dialog=$('#transcript');
  if(dialog)return dialog;
  dialog=document.createElement('dialog');dialog.id='transcript';
  dialog.innerHTML='<div class="dialog-heading"><h2>Чат агента</h2><button type="button" id="close-transcript" class="quiet">✕</button></div><div class="transcript-bar"><select id="transcript-run" aria-label="Запуск"></select><span id="transcript-live"></span><button type="button" id="transcript-changes" class="quiet">Все изменения</button></div><div id="transcript-diff" class="transcript-diff" hidden></div><div id="transcript-log" class="transcript-log"></div><form id="transcript-hint" class="transcript-hint-form" hidden><textarea id="transcript-hint-text" rows="2" maxlength="4000" placeholder="Подсказать агенту: уточнение или поправка к текущей работе" required></textarea><button type="submit">Отправить</button><small id="transcript-hint-status">Codex и API-агент получат сразу, Claude — после текущего шага.</small></form>';
  document.body.append(dialog);
  $('#close-transcript').onclick=()=>dialog.close();
  dialog.addEventListener('close',()=>{if(transcriptState)clearTimeout(transcriptState.timer);transcriptState=null;});
  $('#transcript-hint').onsubmit=sendTranscriptHint;
  $('#transcript-changes').onclick=showTaskChanges;
  $('#transcript-log').onclick=event=>{const file=event.target.closest('[data-diff]');if(file)showFileDiff(file.dataset.diff);};
  $('#transcript-diff').onclick=event=>{const file=event.target.closest('[data-diff]');if(file)showFileDiff(file.dataset.diff);if(event.target.closest('#close-diff'))$('#transcript-diff').hidden=true;};
  $('#transcript-run').onchange=event=>{const id=transcriptState.task;openTranscriptRun(id,event.target.value);};
  return dialog;
}
function transcriptEntry(entry) {
  const at=new Date(entry.at*1000).toLocaleTimeString('ru-RU');
  const body=entry.kind==='files'?`<div class="diff-files">${entry.text.split('\n').filter(Boolean).map(fileChip).join('')}</div>`:`<pre>${esc(entry.text)}</pre>`;
  return `<div class="transcript-entry transcript-${esc(entry.kind)}"><small>${esc(transcriptKinds[entry.kind]||entry.kind)} · ${esc(at)}</small>${body}</div>`;
}
// Changed files open their diff against the task's base; the workspace is live while the agent works.
const workspacePath=path=>path.replace(/^\/workspace\//,'');
const fileChip=(path,status)=>`<button type="button" class="diff-file" data-diff="${esc(workspacePath(path))}" title="Показать изменения">${status?`<b class="diff-status diff-${esc(status)}">${esc(status)}</b>`:''}${esc(workspacePath(path))}</button>`;
function diffLines(diff) {
  return diff.split('\n').map(line=>{
    const kind=line.startsWith('@@')?'hunk':line.startsWith('+++')||line.startsWith('---')||line.startsWith('diff ')||line.startsWith('index ')?'meta':line.startsWith('+')?'add':line.startsWith('-')?'del':'';
    return `<span class="diff-line ${kind}">${esc(line)||' '}</span>`;
  }).join('');
}
async function showFileDiff(path) {
  const panel=$('#transcript-diff'),task=transcriptState?.task;if(!task)return;
  panel.hidden=false;panel.innerHTML='<p>Загрузка…</p>';
  try{
    const data=await api(`/tasks/${encodeURIComponent(task)}/changes?path=${encodeURIComponent(path)}`);
    panel.innerHTML=`<div class="diff-head"><button type="button" class="quiet" data-back-changes>← Все изменения</button><code>${esc(data.path)}</code><button type="button" id="close-diff" class="quiet">✕</button></div>`+
      (data.diff?`<pre class="diff-body">${diffLines(data.diff)}</pre>${data.truncated?'<small>Показано начало — изменение слишком большое.</small>':''}`:'<p>Отличий от исходной версии нет — файл вернули обратно или он удалён.</p>');
    panel.querySelector('[data-back-changes]').onclick=showTaskChanges;
  }catch(error){panel.innerHTML=`<p>${esc(error.message)}</p>`;}
}
async function showTaskChanges() {
  const panel=$('#transcript-diff'),task=transcriptState?.task;if(!task)return;
  panel.hidden=false;panel.innerHTML='<p>Загрузка…</p>';
  try{
    const data=await api(`/tasks/${encodeURIComponent(task)}/changes`);
    panel.innerHTML=`<div class="diff-head"><strong>Изменения задачи · ${data.files.length}</strong><button type="button" id="close-diff" class="quiet">✕</button></div>`+
      (data.files.length?`<div class="diff-files">${data.files.map(f=>fileChip(f.path,f.status)).join('')}</div>`:'<p>Агент пока ничего не изменил.</p>');
  }catch(error){panel.innerHTML=`<p>${esc(error.message)}</p>`;}
}
async function pollTranscript() {
  const state=transcriptState;if(!state)return;
  state.timer=null;
  try {
    const query=new URLSearchParams({after:state.next});if(state.generation)query.set('generation',state.generation);
    const data=await api(`/tasks/${encodeURIComponent(state.task)}/transcript?${query}`);
    if(state!==transcriptState)return;
    if(!state.generation&&data.generation){state.generation=data.generation;}
    const select=$('#transcript-run');
    select.innerHTML=data.runs.length?data.runs.slice().reverse().map(run=>`<option value="${esc(run.generation)}" ${run.generation===state.generation?'selected':''}>${esc(transcriptRoles[run.role]||run.role)} · ${esc(new Date(run.at*1000).toLocaleString('ru-RU'))}${run.usage?' · '+esc(usageLabel(run.usage)):''}</option>`).join(''):'<option value="">Запусков пока нет</option>';
    $('#transcript-live').innerHTML=data.live?'<span class="dot"></span> идёт сейчас':'<small>завершён</small>';
    const log=$('#transcript-log'),pinned=log.scrollHeight-log.scrollTop-log.clientHeight<40;
    if(!state.next&&!data.entries.length)log.innerHTML=`<p>${data.generation?'Агент ещё ничего не написал.':'Агент ещё не запускался по этой задаче.'}</p>`;
    else if(data.entries.length){if(!state.next)log.innerHTML='';log.insertAdjacentHTML('beforeend',data.entries.map(transcriptEntry).join(''));}
    $('#transcript-hint').hidden=!data.live;
    state.next=data.next;
    if(pinned)log.scrollTop=log.scrollHeight;
    if(data.more){state.timer=setTimeout(pollTranscript,0);return;}
    state.live=data.live;
  } catch(error){$('#transcript-live').innerHTML=`<small>${esc(error.message)}</small>`;}
  // A hidden tab keeps the cursor and polls slowly; it catches up when shown again.
  if(state===transcriptState&&(state.live||!state.generation))state.timer=setTimeout(pollTranscript,document.hidden?15000:2000);
}
async function sendTranscriptHint(event) {
  event.preventDefault();
  const state=transcriptState,field=$('#transcript-hint-text'),status=$('#transcript-hint-status');
  if(!state||!field.value.trim())return;
  try{await api(`/tasks/${encodeURIComponent(state.task)}/hint`,'POST',{text:field.value.trim()});field.value='';status.textContent='Отправлено. Появится в ленте, когда агент её прочитает.';}
  catch(error){status.textContent=error.message;}
}
function openTranscriptRun(task,generation) {
  if(transcriptState)clearTimeout(transcriptState.timer);
  transcriptState={task,generation:generation||null,next:0,timer:null,live:false};
  $('#transcript-log').innerHTML='<p>Загрузка…</p>';$('#transcript-diff').hidden=true;
  pollTranscript();
}
function showAgentTranscript(task) {
  const dialog=transcriptDialog();
  if(!dialog.open)dialog.showModal();
  openTranscriptRun(task,null);
}
document.addEventListener('visibilitychange',()=>{const state=transcriptState;if(document.hidden||!state||!state.timer)return;clearTimeout(state.timer);state.timer=null;pollTranscript();});
