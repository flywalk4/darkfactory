// Backlog: owner drops notes; an agent groups related ones into tasks on demand or on a timer.
const backlogStates={queued:tr('В очереди'),running:tr('В работе'),merged:tr('Принята'),needs_human:tr('Нужен человек'),review_required:tr('Ждёт слияния'),cancelled:tr('Отменена'),checked:tr('Проверка пройдена'),waiting_dependency:tr('Ждёт зависимость'),recovering:tr('Восстанавливается')};
const backlogTime=at=>at?new Date(at*1000).toLocaleString(LOCALE,{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}):'';
function backlogSchedule(data,open){
  if(data.requested)return '<span class="backlog-status busy">'+tr('Агент группирует заметки в задачи…')+'</span>';
  if(!open)return '<span class="backlog-status">'+tr('Новых заметок нет')+'</span>';
  if(!data.cron)return '<span class="backlog-status">'+tr('Расписания нет — только по кнопке. Расписание настраивается на странице «Расписание».')+'</span>';
  return `<span class="backlog-status">${tr('Автоматически')} ${data.next_at?tr('около {time}',{time:esc(backlogTime(data.next_at))}):''} · ${tr('по расписанию {cron}',{cron:esc(data.cron)})}</span>`;
}
async function backlogPage(){
  const root=$('#backlog-workspace');if(!projectId){root.innerHTML='<p>'+tr('Сначала подключи проект.')+'</p>';return;}
  const data=await api(`/projects/${projectId}/backlog`),open=data.items.filter(i=>i.state==='open'),planned=data.items.filter(i=>i.state!=='open');
  const groups={};for(const item of planned){const key=item.task?.id||'none';(groups[key]=groups[key]||{task:item.task,items:[]}).items.push(item);}
  const readonly=currentUser.role==='observer'||currentUser.token_role==='observer';
  const typing=document.activeElement?.id==='backlog-text',draft=$('#backlog-text')?.value||'';
  root.innerHTML=`<div class="page-title"><div><div class="eyebrow">${tr('БЭКЛОГ')}</div><h1>${tr('Заметки и идеи')}</h1><p>${tr('Кидай сюда баги, идеи и мелочи как есть. Агент объединит связанные заметки по разделам и создаст задачи.')}</p></div><div><button id="backlog-plan" ${open.length&&!data.requested&&!readonly?'':'disabled'}>${tr('Перевести бэклог в работу')}${open.length?` (${open.length})`:''}</button></div></div>
  <div class="backlog-bar">${backlogSchedule(data,open.length)}${data.last_error?`<span class="backlog-status bad">${tr('Прошлая попытка: {error}',{error:esc(data.last_error)})}</span>`:''}</div>
  ${readonly?'':`<form id="backlog-form" class="backlog-form"><textarea id="backlog-text" maxlength="4000" placeholder="${tr('Например: на мобильном съезжает кнопка «Сохранить» в настройках воркспейса')}"></textarea><div class="backlog-form-actions"><small>${tr('Ctrl+Enter — добавить')}</small><button>${tr('Добавить')}</button></div></form>`}
  <section class="backlog-section"><h2>${tr('Новые')} <span class="count">${open.length}</span></h2>${open.length?open.map(i=>`<div class="backlog-item"><p>${esc(i.text)}</p><div class="backlog-meta"><small>${esc(backlogTime(i.created))}</small>${readonly||data.requested?'':`<button class="quiet" data-backlog-remove="${i.id}" title="${tr('Удалить заметку')}">✕</button>`}</div></div>`).join(''):'<p class="backlog-empty">'+tr('Пусто. Добавь первую заметку выше.')+'</p>'}</section>
  ${planned.length?`<section class="backlog-section"><h2>${tr('Переведены в задачи')} <span class="count">${planned.length}</span></h2>${Object.values(groups).map(g=>`<div class="backlog-group">${g.task?`<button class="backlog-task" data-backlog-task="${g.task.id}"><span class="subtask-dot state-${esc(g.task.state)}"></span><span>${esc(g.task.title)}</span><small>${esc(backlogStates[g.task.state]||g.task.state)}</small></button>`:`<div class="backlog-task"><span>${g.items.some(i=>i.state==='dismissed')?tr('Агент не взял в задачи'):tr('Задача удалена')}</span></div>`}${g.items.map(i=>`<p class="backlog-source">${esc(i.text)}</p>`).join('')}</div>`).join('')}</section>`:''}`;
  if(typing){const t=$('#backlog-text');t.value=draft;t.focus();}
  const form=$('#backlog-form');
  if(form){const text=$('#backlog-text');text.onkeydown=e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey))form.requestSubmit();};
    form.onsubmit=async e=>{e.preventDefault();const value=text.value.trim();if(!value)return;try{await api(`/projects/${projectId}/backlog`,'POST',{text:value});text.value='';await backlogPage();$('#backlog-text')?.focus();}catch(error){toast(error.message);}};}
  $('#backlog-plan').onclick=async()=>{try{await api(`/projects/${projectId}/backlog/plan`,'POST');toast(tr('Агент разбирает бэклог: задачи появятся на доске'));await backlogPage();}catch(error){toast(error.message);}};
  root.querySelectorAll('[data-backlog-remove]').forEach(b=>b.onclick=async()=>{try{await api(`/projects/${projectId}/backlog/${b.dataset.backlogRemove}`,'DELETE');await backlogPage();}catch(error){toast(error.message);}});
  root.querySelectorAll('[data-backlog-task]').forEach(b=>b.onclick=()=>showDetail(b.dataset.backlogTask).catch(e=>toast(e.message)));
}
