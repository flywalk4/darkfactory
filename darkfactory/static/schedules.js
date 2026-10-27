// Schedules: recurring project jobs (stand testing, backlog, main sync, a task from a saved request) by cron.
// Labels name the project's seed branch: syncing and auto-promotion work with it, main by default.
const scheduleKinds=(branch=seedBranch())=>({self_report:tr('Отчёт о сбоях фабрики'),stand_qa:tr('Тестирование стенда'),stand_build:tr('Сборка приложения на стенде'),backlog:tr('Бэклог в задачи'),main_sync:tr('Влить {branch} в preprod',{branch}),auto_promote:tr('Автоперенос preprod в {branch}',{branch}),lessons:tr('Уроки проекта'),task:tr('Задача')});
const schedulePresets=[['0 * * * *',tr('Каждый час')],['0 */4 * * *',tr('Каждые 4 часа')],['0 9 * * *',tr('Каждый день в 9:00')],['0 9 * * 1-5',tr('По будням в 9:00')],['0 22 * * *',tr('Каждую ночь в 22:00')],['0 9 * * 1',tr('По понедельникам в 9:00')]];
const scheduleTime=at=>at?new Date(at*1000).toLocaleString(LOCALE,{weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}):'—';
const browserZone=()=>{try{return Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC';}catch(_){return 'UTC';}};
const syncStates={merged:tr('влиты новые коммиты'),up_to_date:tr('изменений нет'),conflict:tr('конфликт — создана задача'),failed:tr('ошибка')};
function jobLine(kind,job){
  if(!job)return '';
  const line=(text,cls='')=>`<small class="schedule-job ${cls}">${text}</small>`;
  if(kind==='stand_qa'){
    if(job.running)return line(tr('Идёт тестирование стенда с {time}: агент проходит сценарии в браузере, найденные дефекты появятся задачами на доске.',{time:esc(scheduleTime(job.running))}),'busy');
    if(job.requested)return line(tr('В очереди: тестирование начнётся, как только освободится подписка.'),'busy');
    if(!job.at)return '';
    if(job.error)return line(tr('Прошлый прогон {time} не удался: {error}',{time:esc(scheduleTime(job.at)),error:esc(job.error)}),'bad');
    const tasks=(job.tasks||[]).map(t=>`<button class="schedule-chip" data-schedule-task="${t.id}"><span class="subtask-dot state-${esc(t.state)}"></span>${esc(t.title)}</button>`).join('');
    return line(tr('Прошлый прогон {time}: задач по дефектам — {n}.',{time:esc(scheduleTime(job.at)),n:job.found||0})+(job.summary?' '+esc(job.summary):''))+(tasks?`<div class="schedule-chips">${tasks}</div>`:'');
  }
  if(kind==='backlog'){
    if(job.requested)return line(tr('Агент группирует заметки в задачи…'),'busy');
    if(!job.at)return '';
    return job.error?line(tr('Прошлый разбор {time} не удался: {error}',{time:esc(scheduleTime(job.at)),error:esc(job.error)}),'bad'):line(tr('Прошлый разбор {time}: создано задач — {n}.',{time:esc(scheduleTime(job.at)),n:job.found||0}));
  }
  if(kind==='self_report'){
    if(job.requested)return line(tr('Отчёт о сбоях фабрики поставлен в очередь'),'busy');
    return job.at?line(tr('Последний отчёт {time}: заметок — {n}.',{time:esc(scheduleTime(job.at)),n:job.found||0})):'';
  }
  if(kind==='stand_build'){
    if(job.running)return line(tr('Идёт сборка с {time}: тестировщик просит платформу собрать приложение и проверяет результат.',{time:esc(scheduleTime(job.running))}),'busy');
    if(job.requested)return line(tr('В очереди: сборка начнётся, как только освободится подписка.'),'busy');
    if(job.error&&job.at)return line(tr('Прошлая сборка {time} не удалась: {error}',{time:esc(scheduleTime(job.at)),error:esc(job.error)}),'bad');
    if(!job.last)return '';
    const s=job.last.scores||{},avg=job.average;
    return line(tr('Прошлая сборка {time}: {built}, полнота {c}, правильность {r}, удобство {u}, вид {v}; проблем {n}.',{time:esc(scheduleTime(job.last.at)),built:job.last.built?tr('собралось'):tr('не собралось'),c:s.completeness,r:s.correctness,u:s.usability,v:s.visual,n:job.last.problems}))+(avg?line(tr('За {runs} сборок: собралось {share}%, средние оценки {c} / {r} / {u} / {v}.',{runs:avg.runs,share:Math.round(avg.built*100),c:avg.scores?.completeness??'—',r:avg.scores?.correctness??'—',u:avg.scores?.usability??'—',v:avg.scores?.visual??'—'})):'');
  }
  if(kind==='auto_promote'){
    if(job.requested)return line(tr('В очереди: проверка выдержки и перенос начнутся в течение минуты.'),'busy');
    return job.at?line(tr('Прошлая проверка {time}: {result}',{time:esc(scheduleTime(job.at)),result:esc(job.result||'')})):'';
  }
  if(kind==='main_sync'){
    if(job.requested)return line(tr('В очереди: синхронизация начнётся в течение минуты.'),'busy');
    if(!job.at)return '';
    return line(tr('Последняя синхронизация {time}: {state}',{time:esc(scheduleTime(job.at)),state:esc(syncStates[job.state]||job.state||'')})+(job.error?' · '+esc(job.error):''),job.error?'bad':'');
  }
  return '';
}
function cronLabel(cron){const preset=schedulePresets.find(([value])=>value===cron);return preset?preset[1]:cron;}
function scheduleForm(schedule){
  const kinds=Object.entries(scheduleKinds());
  const fields=field('kind',tr('Что запускать'),{type:'select',value:schedule?.kind||'task',choices:kinds})+
    field('title',tr('Название'),{value:schedule&&schedule.title!==window.scheduleKindTitles?.[schedule.kind]?schedule.title:'',required:false,placeholder:tr('Для задачи — её заголовок')})+
    field('preset',tr('Готовое расписание'),{type:'select',required:false,value:schedule?.cron||'',choices:[['',tr('Своё (cron ниже)')],...schedulePresets]})+
    field('cron',tr('Cron: минута час день месяц день недели'),{value:schedule?.cron||'0 9 * * 1-5',placeholder:'0 9 * * 1-5'})+
    field('timezone',tr('Часовой пояс'),{value:schedule?.timezone||browserZone()})+
    field('enabled',tr('Включено'),{type:'select',value:String(schedule?.enabled!==false),choices:boolChoices})+
    `<div id="schedule-task-fields">`+field('request',tr('Текст задачи: цель и критерии приёмки'),{type:'textarea',required:false,value:schedule?.request||''})+
    field('priority',tr('Приоритет'),{type:'select',value:schedule?.priority||'normal',choices:[['critical',tr('Критичный')],['high',tr('Высокий')],['normal',tr('Обычный')],['low',tr('Низкий')]]})+`</div>`;
  modal(schedule?tr('Расписание'):tr('Новое расписание'),fields,async values=>{
    const body={kind:values.kind,title:values.title.trim()||null,cron:values.cron.trim(),timezone:values.timezone.trim()||null,enabled:values.enabled==='true',request:values.request||null,priority:values.priority};
    await api(schedule?`/schedules/${schedule.id}`:`/projects/${projectId}/schedules`,schedule?'PUT':'POST',body);
    toast(tr('Расписание сохранено'));await schedulesPage();
  });
  const form=$('#modal-fields'),kind=form.querySelector('[name=kind]'),taskFields=$('#schedule-task-fields');
  const sync=()=>{taskFields.hidden=kind.value!=='task';};kind.onchange=sync;sync();
  form.querySelector('[name=preset]').onchange=e=>{if(e.target.value)form.querySelector('[name=cron]').value=e.target.value;};
}
async function schedulesPage(){
  const root=$('#schedules-workspace');if(!projectId){root.innerHTML='<p>'+tr('Сначала подключи проект.')+'</p>';return;}
  const data=await api(`/projects/${projectId}/schedules`);
  const owner=currentUser.role==='owner'&&currentUser.token_role==='owner',operator=owner||(currentUser.role!=='observer'&&currentUser.token_role!=='observer');
  window.scheduleKindTitles=data.kinds;
  root.innerHTML=`<div class="page-title"><div><div class="eyebrow">${tr('РАСПИСАНИЕ')}</div><h1>${tr('Регулярные работы проекта')}</h1><p>${tr('Тестирование стенда, разбор бэклога, синхронизация {branch} и любые повторяющиеся задачи — по расписанию cron.',{branch:esc(seedBranch())})}</p></div><div>${owner?`<button id="schedule-add">${tr('+ Расписание')}</button>`:''}</div></div>
  <section class="backlog-section">${data.items.length?data.items.map(s=>{const labels=scheduleKinds(),builtin=s.title===data.kinds[s.kind];return `<div class="backlog-item schedule-item ${s.enabled?'':'off'}"><div class="schedule-main"><strong>${esc(builtin?labels[s.kind]:s.title)}</strong><small>${builtin?'':esc(labels[s.kind]||s.kind)+' · '}<code>${esc(s.cron)}</code>${cronLabel(s.cron)!==s.cron?' · '+esc(cronLabel(s.cron)):''} · ${esc(s.timezone)}</small><small>${s.enabled?tr('Следующий запуск: {time}',{time:esc(scheduleTime(s.next_at))}):tr('Выключено')}${s.last_at?' · '+tr('прошлый: {time}',{time:esc(scheduleTime(s.last_at))}):''}</small>${s.last_result&&!s.queued?`<small class="schedule-result">${esc(s.last_result)}</small>`:''}${jobLine(s.kind,data.jobs?.[s.kind])}${s.kind==='task'&&s.request?`<p>${esc(s.request.slice(0,300))}</p>`:''}</div><div class="backlog-meta">${s.last_task_id?`<button class="quiet" data-schedule-task="${s.last_task_id}">${tr('Задача')}</button>`:''}${operator?`<button class="quiet" data-schedule-run="${s.id}">${tr('Запустить сейчас')}</button>`:''}${owner?`<button class="quiet" data-schedule-edit="${s.id}">${tr('Изменить')}</button><button class="quiet" data-schedule-delete="${s.id}" title="${tr('Удалить расписание')}">✕</button>`:''}</div></div>`;}).join(''):'<p class="backlog-empty">'+tr('Расписаний нет.')+'</p>'}</section>`;
  if($('#schedule-add'))$('#schedule-add').onclick=()=>scheduleForm(null);
  root.querySelectorAll('[data-schedule-edit]').forEach(b=>b.onclick=()=>scheduleForm(data.items.find(s=>s.id===b.dataset.scheduleEdit)));
  root.querySelectorAll('[data-schedule-run]').forEach(b=>b.onclick=async()=>{try{const result=await api(`/schedules/${b.dataset.scheduleRun}/run`,'POST');toast(result.last_result||tr('Запущено'));await schedulesPage();}catch(e){toast(e.message);}});
  root.querySelectorAll('[data-schedule-delete]').forEach(b=>b.onclick=async()=>{if(!confirm(tr('Удалить расписание?')))return;try{await api(`/schedules/${b.dataset.scheduleDelete}`,'DELETE');await schedulesPage();}catch(e){toast(e.message);}});
  root.querySelectorAll('[data-schedule-task]').forEach(b=>b.onclick=()=>showDetail(b.dataset.scheduleTask).catch(e=>toast(e.message)));
}
