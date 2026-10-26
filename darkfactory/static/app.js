'use strict';
const $ = s => document.querySelector(s);
const remembered=key=>{try{return localStorage.getItem(key)||'';}catch{return '';}},remember=(key,value)=>{try{value?localStorage.setItem(key,value):localStorage.removeItem(key);}catch{}};
function route(task){const url=new URL(location.href);for(const key of ['commit','promotion'])url.searchParams.delete(key);task?url.searchParams.set('task',task):url.searchParams.delete('task');history.replaceState(null,'',url);}
let token = sessionStorage.getItem('df-token') || '', projectId = remembered('df-project'), projects = [], poolList = [], taskList = [], boardConnections = null, currentUser;
let submitForm, detailId, refreshBusy = false;
const names = {queued:tr('В очереди'),recovering:tr('Восстанавливается'),waiting_pool:tr('Ждёт подключение'),waiting_dependency:tr('Ждёт зависимость'),waiting_integration:tr('Ждёт интеграцию'),running:tr('В работе'),needs_human:tr('Нужно решение'),review_required:tr('Без автослияния'),merged:tr('Принята'),cancelled:tr('Отменена'),duplicate:tr('Дубликат'),checked:tr('Проверка пройдена')};
const phases = {prepare:tr('Подготовка'),plan:tr('План и уточнения'),collect:tr('Сбор результатов подзадач'),implement:tr('Разработка'),checks:tr('Проверки'),integrate:tr('Объединение'),verify:tr('Проверка результата'),review:tr('Ревью'),publish:tr('Запись изменений'),merge:tr('Слияние')};
const providers = [['codex','Codex'],['claude','Claude'],['api','API']];
const roles = [['observer',tr('Наблюдатель')],['operator',tr('Оператор')],['owner',tr('Владелец')]];
const boolChoices = [['true',tr('Да')],['false',tr('Нет')]];
const taskPriorities = [['critical',tr('Критический')],['high',tr('Высокий')],['normal',tr('Обычный')],['low',tr('Низкий')]];
const activityLabel = t => {
  if(t.state!=='running'||!Number.isFinite(t.last_activity))return null;
  const idle=Math.max(0,Date.now()/1000-t.last_activity),stalled=idle>=t.stall_seconds;
  const ago=idle<60?tr('только что'):tr('{n} мин назад',{n:Math.floor(idle/60)});
  return {stalled,text:stalled?tr('⚠ Нет активности агента {n} мин',{n:Math.floor(idle/60)}):tr('Агент активен · {ago}',{ago})};
};
const reasonNames={review_incomplete:tr('Ревью не смогло проверить все файлы кода'),correction_limit:tr('Ревью отклонило задачу максимальное число раз'),review_repeated:tr('Ревью повторно отклоняет по той же причине'),network_blocked:tr('Агенту закрыта сеть'),environment_blocked:tr('Среде агента чего-то не хватает'),stalled:tr('Агент завис'),token_limit:tr('Исчерпан бюджет токенов')};
// A task picks profiles for its own stages; the Director is project-wide.
const taskStages = () => agentStages.filter(([key]) => key !== 'director');
const stateLabel = t => t.state==='needs_human'&&t.awaits_director?tr('Решает директор'):names[t.state]||t.state;
const reasonLabel = reason => reasonNames[reason]||reason||'';
const blockedHosts = t => {if(t.state!=='needs_human'||t.reason!=='network_blocked')return [];const e=[...(t.events||[])].reverse().find(e=>e.kind==='network_blocked');return e?.data?.hosts||[];};
const priorityLabel = priority => taskPriorities.find(([key]) => key === priority)?.[1] || tr('Обычный');
const isOwner = () => currentUser?.role==='owner' && currentUser?.token_role==='owner';
const isOrganizationOwner = () => isOwner() && !currentUser.project_ids.length && !currentUser.token_projects.length;
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// Links from agents, forges and providers become hrefs only when they are plain web addresses.
const safeHref = url => typeof url==='string' && /^https?:\/\//i.test(url);
async function api(path, method='GET', body) {
  const response = await fetch('/api' + path, {method, headers:{Authorization:'Bearer ' + token,...(body ? {'Content-Type':'application/json'} : {})},body:body ? JSON.stringify(body) : undefined});
  if (!response.ok) {
    let error; try {error = (await response.json()).detail;} catch {error = response.statusText;}
    if (response.status === 401) { sessionStorage.removeItem('df-token'); $('#login').hidden=false; $('#shell').hidden=true; }
    throw new Error(typeof error === 'string' ? error : JSON.stringify(error));
  }
  return response.json();
}
function toast(text) { $('#toast').textContent=text; $('#toast').hidden=false; setTimeout(()=>$('#toast').hidden=true,6000); }
function field(name,label,options={}) {
  const {type='text',value='',placeholder='',required=true,choices=[],min,max,step} = options;
  let control;
  if(type === 'textarea') control=`<textarea name="${name}" ${required?'required':''} placeholder="${esc(placeholder)}">${esc(value)}</textarea>`;
  else if(type === 'select') control=`<select name="${name}" ${required?'required':''}>${choices.map(([v,t])=>`<option value="${esc(v)}" ${v===value?'selected':''}>${esc(t)}</option>`).join('')}</select>`;
  else if(type === 'multiselect') control=`<select name="${name}" multiple size="${Math.min(5,Math.max(2,choices.length))}" ${required?'required':''}>${choices.map(([v,t])=>`<option value="${esc(v)}" ${value.includes(v)?'selected':''}>${esc(t)}</option>`).join('')}</select><small>${tr('Можно выбрать несколько. На компьютере: Ctrl / ⌘.')}</small>`;
  else control=`<input name="${name}" type="${type}" value="${esc(value)}" placeholder="${esc(placeholder)}" ${min!==undefined?`min="${min}"`:''} ${max!==undefined?`max="${max}"`:''} ${step!==undefined?`step="${step}"`:''} ${required?'required':''}>`;
  return `<label>${esc(label)}${control}</label>`;
}
function modal(title,fields,callback) {$('#submit-modal').hidden=false;$('#submit-modal').disabled=false;$('#submit-modal').textContent=tr('Сохранить');$('#cancel-modal').textContent=tr('Отмена');$('#modal-title').textContent=title;$('#modal-fields').innerHTML=fields;$('#form-error').textContent='';submitForm=callback;$('#modal').showModal();}
$('#close-modal').onclick=$('#cancel-modal').onclick=()=>$('#modal').close();
$('#close-detail').onclick=()=>{detailId=null;$('#detail').close();};$('#detail').addEventListener('close',()=>{detailId=null;route(null);});
$('#modal-form').onsubmit=async e=>{e.preventDefault();$('#submit-modal').disabled=true;try{const data=new FormData(e.target),values=Object.fromEntries(data);e.target.querySelectorAll('select[multiple]').forEach(control=>values[control.name]=data.getAll(control.name));await submitForm(values);$('#modal').close();await load();}catch(error){$('#form-error').textContent=error.message;}finally{$('#submit-modal').disabled=false;}};
async function login() {
  currentUser=await api('/me');diskWarning(currentUser.disk,currentUser.held);$('#login').hidden=true;$('#shell').hidden=false;$('#who').textContent=currentUser.name+' · '+currentUser.role;
  $('#org-name').textContent=currentUser.organization.name;
  const owner=currentUser.role==='owner' && currentUser.token_role==='owner';
  const orgOwner=owner&&!currentUser.project_ids.length&&!currentUser.token_projects.length;
  $('#add-organization').hidden=!orgOwner;
  ['#add-project','#empty-add','#add-pool','#add-connection','#connect-subscription'].forEach(s=>$(s).hidden=!orgOwner);$('#add-member').hidden=!owner;
  $('#new-incident').hidden=$('#new-task').hidden=currentUser.role==='observer'||currentUser.token_role==='observer';await load();const view=document.querySelector(`[data-view="${remembered('df-view')}"]`);if(view&&!view.hidden)view.click();await openNotificationLink();
}
$('#login-form').onsubmit=async e=>{e.preventDefault();token=$('#token').value;try{await login();sessionStorage.setItem('df-token',token);$('#token').value='';}catch(error){toast(error.message);}};
function collapseNav(collapsed){document.body.classList.toggle('nav-collapsed',collapsed);const b=$('#collapse-nav');b.textContent=collapsed?'»':'«';b.title=collapsed?tr('Развернуть меню'):tr('Свернуть меню');b.setAttribute('aria-label',b.title);b.setAttribute('aria-expanded',String(!collapsed));document.querySelectorAll('.nav,.project-item').forEach(n=>collapsed?n.title=n.textContent.trim():n.removeAttribute('title'));}
$('#collapse-nav').onclick=()=>{const collapsed=!document.body.classList.contains('nav-collapsed');remember('df-nav-collapsed',collapsed?'1':'');collapseNav(collapsed);};collapseNav(remembered('df-nav-collapsed')==='1');
$('#logout').onclick=()=>{sessionStorage.removeItem('df-token');location.reload();};
document.querySelectorAll('[data-view]').forEach(button=>button.onclick=()=>{remember('df-view',button.dataset.view);document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b===button));for(const view of ['board','factory','backlog','schedules','settings','agents','artifacts'])$('#'+view+'-view').hidden=button.dataset.view!==view;if(button.dataset.view==='settings') settings().catch(e=>toast(e.message));if(button.dataset.view==='agents')agentsPage().catch(e=>toast(e.message));if(button.dataset.view==='artifacts')registryPage().catch(e=>toast(e.message));if(button.dataset.view==='backlog')backlogPage().catch(e=>toast(e.message));if(button.dataset.view==='schedules')schedulesPage().catch(e=>toast(e.message));if(button.dataset.view==='factory')factoryPage().catch(e=>toast(e.message));});
async function load() {
  projects=await api('/projects');poolList=await api('/pools');
  if(!projects.some(p=>p.id===projectId)) projectId=projects[0]?.id||'';remember('df-project',projectId);
  $('#project-list').innerHTML=projects.map(p=>`<button class="project-item ${p.id===projectId?'selected':''}" data-project="${p.id}"><span class="project-icon">${esc((p.name.trim()[0]||'◆').toUpperCase())}</span><span class="nav-text">${esc(p.name)}</span></button>`).join('');collapseNav(document.body.classList.contains('nav-collapsed'));
  document.querySelectorAll('[data-project]').forEach(b=>b.onclick=()=>{projectId=b.dataset.project;document.querySelector('[data-view="board"]').click();load().catch(e=>toast(e.message));});
  const project=projects.find(p=>p.id===projectId);$('#current-project').textContent=project?.name||tr('Проекты');$('#project-heading').textContent=project?.name||tr('Твоя фабрика разработки');
  $('#project-description').textContent=project?`${project.remote} · preprod · ${project.checks.length ? tr('{n} обязательных проверок',{n:project.checks.length}) : project.merge_without_checks===false ? tr('в preprod сливает человек: нет проверок') : tr('в preprod автоматически после ревью')} · ${tr('main — вручную')}`:tr('Подключи первый проект, чтобы начать работу.');
  $('#new-incident').disabled=$('#new-task').disabled=!project;$('#empty').hidden=!!project;$('#board').hidden=!project;$('#metrics').hidden=!project;$('.board-heading').hidden=!project;
  [taskList,boardConnections]=project?await Promise.all([api(`/projects/${projectId}/tasks`),api('/connections').catch(()=>null)]):[[],null];renderBoard();
  if(!$('#settings-view').hidden) await settings();
  if(!$('#artifacts-view').hidden) await registryPage();
  if(!$('#backlog-view').hidden&&!$('#backlog-text')?.value) await backlogPage();
  if(!$('#schedules-view').hidden&&!$('#modal').open) await schedulesPage();
}
const openParents=new Set();
function descendants(id,childrenOf,seen=new Set()){return (childrenOf[id]||[]).flatMap(c=>seen.has(c.id)?[]:(seen.add(c.id),[c,...descendants(c.id,childrenOf,seen)]));}
function subtaskRows(children,childrenOf,depth,seen){return children.map(c=>{const nested=seen.has(c.id)?[]:childrenOf[c.id]||[],open=openParents.has(c.id),all=descendants(c.id,childrenOf),done=all.filter(x=>x.state==='merged').length;seen.add(c.id);return `<div class="subtask-row" style="--depth:${depth}">${nested.length?`<button class="subtask-expand" data-subtasks="${c.id}" aria-expanded="${open}" title="${open?tr('Свернуть подзадачи'):tr('Развернуть подзадачи')}">${open?'▾':'▸'}</button>`:'<span class="subtask-expand"></span>'}<button class="subtask" data-task="${c.id}" title="${esc(c.title)}"><i class="subtask-dot state-${esc(c.state)}"></i><span class="subtask-title" title="${esc(c.title)}">${esc(c.title)}</span>${nested.length?`<span class="subtask-count">${done}/${all.length}</span>`:''}${c.role_config?.tier==='light'?'<span class="tier-light tier-mark" title="'+tr('Лёгкая подзадача: дешёвая модель')+'">⚡</span>':''}<span class="subtask-state" title="${esc(c.waiting_for?tr('Ждёт {name}',{name:c.waiting_for}):'')}">${esc(c.waiting_for?tr('ждёт {name}',{name:c.waiting_for}):stateLabel(c))}</span></button></div>${open&&nested.length?subtaskRows(nested,childrenOf,depth+1,seen):''}`;}).join('');}
function childRows(t,childrenOf){const children=childrenOf[t.id]||[];if(!children.length)return '';const all=descendants(t.id,childrenOf),done=all.filter(c=>c.state==='merged').length,open=openParents.has(t.id);return `<div class="subtasks${open?' open':''}"><button class="subtasks-toggle" data-subtasks="${t.id}" aria-expanded="${open}"><span>${open?'▾':'▸'} ${tr('Подзадачи')}</span><span class="subtasks-progress"><i style="width:${Math.round(done/all.length*100)}%"></i></span><span>${done}/${all.length}</span></button>${open?subtaskRows(children,childrenOf,0,new Set([t.id])):''}</div>`;}
function renderBoard() {
  const ids=new Set(taskList.map(t=>t.id)),childrenOf={};
  taskList.forEach(t=>{if(t.parent_id&&ids.has(t.parent_id))(childrenOf[t.parent_id]||=[]).push(t);});
  Object.values(childrenOf).forEach(list=>list.sort((a,b)=>a.created-b.created));
  // Subtasks live inside their parent card, but active ones also get their own card to watch them.
  const byId=Object.fromEntries(taskList.map(t=>[t.id,t])),active=t=>['running','recovering','needs_human','review_required'].includes(t.state);
  const topLevel=taskList.filter(t=>(!(t.parent_id&&ids.has(t.parent_id))||active(t))&&shown(t));
  const waiting=t=>t.state.startsWith('waiting')||t.state==='queued';
  const queue=topLevel.filter(waiting).sort((a,b)=>Number(b.incident)-Number(a.incident) || taskPriorities.findIndex(([key])=>key===(a.priority||'normal'))-taskPriorities.findIndex(([key])=>key===(b.priority||'normal')) || a.created-b.created || a.id.localeCompare(b.id));
  const groups=[[tr('К запуску'),queue],[tr('В работе'),topLevel.filter(t=>['running','recovering'].includes(t.state))],[tr('Нужно внимание'),topLevel.filter(t=>['needs_human','review_required'].includes(t.state))],[tr('Завершены'),topLevel.filter(t=>['merged','cancelled','duplicate','checked'].includes(t.state)).sort((a,b)=>b.updated-a.updated)]];
  // Summary answers "does anything need me?": live work, decisions, silent agents and subscription headroom.
  const stalled=taskList.filter(t=>activityLabel(t)?.stalled).length,poolWait=taskList.filter(t=>t.state==='waiting_pool').length;
  const ready=(boardConnections||[]).filter(c=>c.status==='ready'),left=ready.map(c=>c.quota_details?.remaining_percent).filter(Number.isFinite);
  const quota=boardConnections===null?'—':!ready.length?tr('нет'):left.length?Math.max(...left)+'%':'?';
  const counts=[[tr('В работе'),taskList.filter(t=>['running','recovering'].includes(t.state)).length,'●',''],[tr('Ждут решения'),taskList.filter(t=>['needs_human','review_required'].includes(t.state)).length,'◇',''],[tr('Зависли'),stalled,'⚠',stalled?'metric-alert':''],
    [tr('Квота'),quota,poolWait?tr('ждут: {n}',{n:poolWait}):tr('{n} подкл.',{n:ready.length}),poolWait||(boardConnections&&!ready.length)?'metric-alert':'']];
  $('#metrics').innerHTML=counts.map(([label,count,note,cls])=>`<div class="metric ${cls}"><span>${label}</span><strong>${count}</strong><em>${note}</em></div>`).join('');$('#task-count').textContent=topLevel.length===taskList.length?taskList.length:tr('{shown} из {total}',{shown:topLevel.length,total:taskList.length});renderBoardFilters();
  renderMainPending(taskList);
  renderAttention(taskList);
  const card=t=>`<div class="card-group"><button class="card" data-task="${t.id}"><div class="card-id"><span>DF-${t.id.slice(0,6).toUpperCase()}</span><span class="card-author" title="${t.author?tr('Поставил(а): {name}',{name:esc(t.author)}):tr('Поставила фабрика')}">${t.incident?tr('ИНЦИДЕНТ')+' · ':''}${esc(t.author||tr('фабрика'))}</span></div>${t.parent_id&&byId[t.parent_id]?`<span class="card-parent" role="link" tabindex="0" data-open-parent="${t.parent_id}" title="${tr('Открыть родительскую задачу')}">↖ ${esc(byId[t.parent_id].title)}</span>`:''}<h3>${esc(t.title)}</h3><span class="card-tag">${esc(stateLabel(t))}</span> <span class="card-tag task-priority-${esc(t.priority||'normal')}">${esc(priorityLabel(t.priority))}</span>${t.role_config?.tier==='light'?'<span class="tier-light" title="'+tr('Лёгкая подзадача: дешёвая модель, любая свободная подписка')+'">'+tr('лёгкая')+'</span>':''}${t.promotion_pending?'<span class="main-badge" title="'+tr('Слито в preprod, ждёт твоего решения о переносе в main')+'">→ main</span>':''}${t.reason?`<div class="card-reason">${esc(reasonLabel(t.reason))}</div>`:t.waiting_for?`<div class="card-reason">${esc(tr('Ждёт {name}',{name:t.waiting_for}))}</div>`:''}${t.branch&&t.state!=='done'?`<div class="card-branch" title="${tr('Ветка агента')}"><code>${esc(t.branch)}</code></div>`:''}${activityLabel(t)?`<div class="card-activity${activityLabel(t).stalled?' stalled':''}">${esc(activityLabel(t).text)}</div>`:''}<div class="card-bottom"><span class="agent">${esc(phases[t.phase]||t.phase)}</span><span>${tr('{n} мин · {count} исправл.',{n:Math.round(t.active_seconds/60),count:t.corrections})}</span></div></button>${childRows(t,childrenOf)}</div>`;
  const done=groups[3][1],cards=group=>group===groups[3]?doneCards(group[1],card):group[1].map(t=>[t.id,card(t)]);
  patchBoard(groups.map(group=>({title:`<i class="state-dot"></i>${group[0]}<span class="count">${group[1].length}</span>`,cards:cards(group)})));
  if(!done.length)doneLimit=DONE_PAGE;
}
// Finished work is split by day, newest first, and shown a page at a time so the column stays short.
const DONE_PAGE=15;let doneLimit=DONE_PAGE;
function dayLabel(seconds){
  const day=new Date(seconds*1000),today=new Date();today.setHours(0,0,0,0);day.setHours(0,0,0,0);
  const diff=Math.round((today-day)/86400000);
  return diff===0?tr('Сегодня'):diff===1?tr('Вчера'):day.toLocaleDateString(LOCALE,{weekday:'short',day:'numeric',month:'long'});
}
function doneCards(tasks,card){
  const rows=[],counts={};tasks.forEach(t=>{const label=dayLabel(t.updated);counts[label]=(counts[label]||0)+1;});
  let last;
  for(const t of tasks.slice(0,doneLimit)){
    const label=dayLabel(t.updated);
    if(label!==last){rows.push(['day:'+label,`<div class="day-divider"><span>${esc(label)}</span><span>${counts[label]}</span></div>`]);last=label;}
    rows.push([t.id,card(t)]);
  }
  if(tasks.length>doneLimit)rows.push(['more',`<button class="show-more" data-more-done>${tr('Показать ещё {n} из {total}',{n:Math.min(DONE_PAGE,tasks.length-doneLimit),total:tasks.length-doneLimit})}</button>`]);
  return rows;
}
// With many people on one factory the board narrows to one author or a search; the choice survives reloads.
let boardAuthor=remembered('df-board-author'),boardSearch='';
function shown(t){
  if(boardAuthor==='me'&&t.created_by!==currentUser?.id)return false;
  if(boardAuthor==='factory'&&t.created_by)return false;
  if(boardAuthor&&!['me','factory'].includes(boardAuthor)&&t.created_by!==boardAuthor)return false;
  const q=boardSearch.trim().toLowerCase();
  return !q||t.title.toLowerCase().includes(q)||('df-'+t.id.slice(0,6)).includes(q)||(t.author||'').toLowerCase().includes(q);
}
function renderBoardFilters(){
  const people=new Map();taskList.forEach(t=>{if(t.created_by&&t.created_by!==currentUser?.id)people.set(t.created_by,t.author||tr('Без имени'));});
  if(boardAuthor&&!['me','factory'].includes(boardAuthor)&&!people.has(boardAuthor))people.set(boardAuthor,tr('Автор'));
  const choices=[['',tr('Все авторы')],['me',tr('Мои задачи')],...[...people].sort((a,b)=>a[1].localeCompare(b[1],LOCALE)),['factory',tr('Фабрика (без автора)')]];
  const html=choices.map(([v,t])=>`<option value="${esc(v)}" ${v===boardAuthor?'selected':''}>${esc(t)}</option>`).join('');
  const select=$('#board-author');if(select.dataset.html!==html){select.innerHTML=html;select.dataset.html=html;}
}
$('#board-author').onchange=e=>{boardAuthor=e.target.value;remember('df-board-author',boardAuthor);doneLimit=DONE_PAGE;renderBoard();};
$('#board-search').oninput=e=>{boardSearch=e.target.value;doneLimit=DONE_PAGE;renderBoard();};
// The poll re-renders every few seconds: only cards whose markup changed are replaced, so focus,
// scroll and hover survive. Clicks are delegated from #board, which is never rebuilt.
const cardHtml=new WeakMap(),fromHtml=html=>{const template=document.createElement('template');template.innerHTML=html;return template.content.firstElementChild;};
function patchBoard(columns){
  const board=$('#board');
  while(board.children.length>columns.length)board.lastElementChild.remove();
  columns.forEach((column,index)=>{
    let node=board.children[index];
    if(!node){node=fromHtml('<div class="column"><div class="column-title"></div></div>');board.append(node);}
    const title=node.firstElementChild;if(title.innerHTML!==column.title)title.innerHTML=column.title;
    const existing=new Map([...node.children].slice(1).map(child=>[child.dataset.key,child]));
    let previous=title;
    for(const [key,html] of column.cards){
      let child=existing.get(key);existing.delete(key);
      if(!child||cardHtml.get(child)!==html){const fresh=fromHtml(html);fresh.dataset.key=key;cardHtml.set(fresh,html);child?.replaceWith(fresh);child=fresh;}
      if(previous.nextElementSibling!==child)previous.after(child);
      previous=child;
    }
    existing.forEach(child=>child.remove());
  });
}
$('#board').addEventListener('click',event=>{
  if(event.target.closest('[data-more-done]')){doneLimit+=DONE_PAGE;renderBoard();return;}
  const parent=event.target.closest('[data-open-parent]'),toggle=event.target.closest('[data-subtasks]'),task=event.target.closest('[data-task]');
  if(parent){event.stopPropagation();showDetail(parent.dataset.openParent).catch(e=>toast(e.message));}
  else if(toggle){event.stopPropagation();const id=toggle.dataset.subtasks;openParents.has(id)?openParents.delete(id):openParents.add(id);renderBoard();}
  else if(task)showDetail(task.dataset.task).catch(e=>toast(e.message));
});
function environmentLine(p){
  const e=p.environment||{},spec=Object.entries(e.spec||{}).map(([k,v])=>k+' '+v).join(', ');
  const status={ready:tr('готово'),failed:tr('не собралось'),base:tr('базовое')}[e.status]||tr('ещё не готовилось');
  const source=e.declared?tr('по .darkfactory/environment.json'):e.status?tr('по файлам проекта'):'';
  return `<small class="project-environment${e.status==='failed'?' failed':''}" title="${esc(e.error||'')}">${esc(tr('окружение: {status}',{status}))}${spec?' · '+esc(spec):''}${source?' · '+esc(source):''}${e.feeds?.length?' · '+esc(tr('реестры: {list}',{list:e.feeds.join(', ')})):''}</small>`;
}
const deployStates={pipeline:tr('ждёт пайплайн'),build:tr('ждёт сборку образов'),running:tr('идёт'),succeeded:tr('выполнен'),failed:tr('не удался')};
function standLine(p){
  const qa=p.health?.qa||{},deploy=p.health?.deploy||{};let line='';
  if(p.stand_url)line+=`<small class="project-health">${tr('стенд')}: ${qa.running?tr('идёт тестирование'):qa.requested?tr('тестирование в очереди'):qa.at?tr('проверен {time}, задач: {n}',{time:esc(new Date(qa.at*1000).toLocaleString(LOCALE)),n:qa.tasks||0}):tr('ещё не проверялся')}${qa.error?' · '+esc(qa.error):''}</small>`;
  if(p.deploy_job&&deploy.state)line+=`<small class="project-health">${tr('деплой')} ${esc((deploy.sha||'').slice(0,12))}: ${esc(deployStates[deploy.state]||deploy.state)}${deploy.error?' · '+esc(deploy.error):''}${deploy.job_url||deploy.pipeline_url?` · <a href="${esc(deploy.job_url||deploy.pipeline_url)}" target="_blank" rel="noopener">GitLab</a>`:''}</small>`;
  return line;
}
function projectForm(project) {
  if(!project) {
    const fields=field('name',tr('Название проекта'))+
      field('remote',tr('Git-репозиторий'),{placeholder:tr('https://git.company.com/team/project.git или /repos/project')})+
      field('git_token',tr('Токен доступа'),{type:'password',required:false,placeholder:tr('Не нужен для открытого или локального репозитория')})+
      '<p>'+tr('Менеджер, аналитик, разработчик и ревьюер появятся сразу. Подписки берутся из общего пула организации. Остальное можно изменить в настройках проекта.')+'</p>';
    modal(tr('Новый проект'),fields,async values=>{
      const result=await api('/projects','POST',values);
      projectId=result.id;
      toast(tr('Проект создан. Агенты готовы, индексация поставлена в очередь.'));
    });
    $('#submit-modal').textContent=tr('Создать проект');
    return;
  }
  const choices=[['',tr('Не выбран')],...poolList.map(p=>[p.id,p.name])];
  let fields=field('name',tr('Название'),{value:project?.name||''})+field('remote',tr('Git URL или путь /repos/…'),{value:project?.remote||'',placeholder:'https://git.company.com/team/project.git'})+field('seed_branch',tr('Исходная ветка'),{value:project?.seed_branch||'main'})+field('checks',tr('Обязательные проверки, каждая с новой строки'),{type:'textarea',value:project?.checks.join('\n')||'',placeholder:'npm ci\nnpm test',required:false})+field('writer_pool',tr('Пул разработки'),{type:'select',choices,value:project?.writer_pool||'',required:false})+field('reviewer_pool',tr('Пул ревью'),{type:'select',choices,value:project?.reviewer_pool||'',required:false})+field('git_token',tr('Git-токен для приватного HTTPS-репозитория'),{type:'password',required:false})+field('forge',tr('Запись изменений'),{type:'select',choices:[['git',tr('Внутренняя запись Git')],['github','GitHub PR'],['gitlab','GitLab MR']],value:project?.forge||'git'})+field('forge_url',tr('API URL сервера PR/MR'),{required:false,value:project?.forge_url||'',placeholder:tr('https://api.github.com или https://gitlab.company.com/api/v4')})+field('forge_repo',tr('Путь репозитория на сервере'),{required:false,value:project?.forge_repo||'',placeholder:'team/project'})+field('forge_token',tr('Токен сервера PR/MR'),{type:'password',required:false});
  fields+=field('writer',tr('Основной разработчик'),{type:'select',value:project?.writer||'codex',choices:[['codex','Codex'],['claude','Claude'],['api','API']]})+field('reviewer',tr('Основной ревьюер'),{type:'select',value:project?.reviewer||'claude',choices:[['codex','Codex'],['claude','Claude'],['api','API']]})+field('max_seconds',tr('Время на задачу (и на каждую подзадачу), секунд'),{type:'number',value:project?.max_seconds||3600})+field('max_tree_hours',tr('Потолок времени дерева задач, часов (продление идёт само, пока есть продвижение)'),{type:'number',value:Math.round((project?.max_tree_seconds||43200)/3600)})+field('max_corrections',tr('Циклов исправления после первой попытки'),{type:'number',value:project?.max_corrections??3})+field('executor_image',tr('Образ исполнителя с зависимостями проекта'),{value:project?.executor_image||'',required:false,placeholder:tr('По умолчанию darkfactory-executor:local')})+field('merge_without_checks',tr('Без обязательных проверок сливать в preprod после одобренного ревью'),{type:'select',value:String(project?.merge_without_checks!==false),choices:boolChoices})+field('director_autonomy',tr('Директор решает остановки вместо тебя'),{type:'select',value:String(project?.director_autonomy!==false),choices:boolChoices})+field('director_limit',tr('Решений директора на дерево задач'),{type:'number',value:project?.director_limit||6})+(project?field('review_muted',tr('Замечания ревью, отмеченные «Не полезно» (по одному на строке; удали строку, чтобы вернуть)'),{type:'textarea',required:false,value:(project.review_muted||[]).join('\n')}):'')+field('package_registries',tr('Открыть публичные реестры пакетов (npm, PyPI, NuGet, Maven, Go, crates)'),{type:'select',value:String(project?.package_registries!==false),choices:boolChoices})+field('allowed_hosts',tr('Разрешённые тестовые HTTPS-адреса, host:port по одному на строке'),{type:'textarea',required:false,value:project?.allowed_hosts?.join('\n')||''})+field('ssh_private_key',tr('SSH private key (необязательно)'),{type:'textarea',required:false})+field('ssh_known_hosts',tr('SSH known_hosts, проверенный отпечаток сервера'),{type:'textarea',required:false})+field('allow_paid_fallback',tr('Разрешить платный API при недоступности подписок (только в пределах бюджета задачи)'),{type:'select',value:String(project?.allow_paid_fallback===true),choices:boolChoices})+field('test_env',tr('Тестовые переменные, JSON; пусто — сохранить'),{type:'textarea',required:false,placeholder:project?.test_env_keys?.length?tr('Настроены: {keys}',{keys:project.test_env_keys.join(', ')}):'{"TEST_API_TOKEN":"…"}'})+field('stand_url',tr('Адрес стенда для тестировщика (пусто — не тестировать)'),{value:project?.stand_url||'',required:false,placeholder:'https://studio.example.com/'})+field('stand_hosts',tr('Другие адреса стенда, host:port по одному на строке (авторизация, API)'),{type:'textarea',required:false,value:project?.stand_hosts?.join('\n')||''})+field('qa_max_tasks',tr('Задач-исправлений за один обход стенда'),{type:'number',value:project?.qa_max_tasks??3})+field('qa_instructions',tr('Указания тестировщику: что проверять; учётные записи — в тестовых переменных E2E_*'),{type:'textarea',required:false,value:project?.qa_instructions||''})+field('auto_promote_hours',tr('Автоперенос preprod в main после выдержки, часов (0 — только вручную)'),{type:'number',value:project?.auto_promote_hours??0})+field('deploy_job',tr('Job деплоя GitLab после слияния в main (пусто — без деплоя)'),{value:project?.deploy_job||'',required:false,placeholder:'deploy'});
  if(!project)fields+=field('initialize',tr('Первичная индексация и Markdown-документы'),{type:'select',value:'true',choices:boolChoices})+field('prepare_environment',tr('Агент подготовит окружение: toolchain, зависимости, проверки'),{type:'select',value:'true',choices:boolChoices});
  modal(project?tr('Настройки проекта'):tr('Новый проект'),fields,async values=>{if(!project){values.initialize=values.initialize==='true';values.prepare_environment=values.prepare_environment==='true';}values.allow_paid_fallback=values.allow_paid_fallback==='true';values.package_registries=values.package_registries==='true';values.merge_without_checks=values.merge_without_checks==='true';values.director_autonomy=values.director_autonomy==='true';values.director_limit=Math.max(1,Math.min(20,Number(values.director_limit)||6));if(typeof values.review_muted==='string')values.review_muted=values.review_muted.split('\n').map(v=>v.trim()).filter(Boolean).slice(-30);if('stand_url' in values){values.stand_url=values.stand_url.trim();values.stand_hosts=values.stand_hosts.split('\n').map(v=>v.trim()).filter(Boolean);values.qa_max_tasks=Math.max(1,Math.min(10,Number(values.qa_max_tasks)||3));values.deploy_job=values.deploy_job.trim();values.auto_promote_hours=Math.max(0,Math.min(720,Number(values.auto_promote_hours)||0));}values.max_seconds=Number(values.max_seconds);values.max_tree_seconds=Math.round(Number(values.max_tree_hours)*3600);delete values.max_tree_hours;values.max_corrections=Number(values.max_corrections);values.allowed_hosts=values.allowed_hosts.split('\n').map(v=>v.trim()).filter(Boolean);if(values.test_env)values.test_env=JSON.parse(values.test_env);else delete values.test_env;values.executor_image=values.executor_image.trim();values.checks=values.checks.split('\n').map(v=>v.trim()).filter(Boolean);values.writer_pool||=null;values.reviewer_pool||=null;values.forge_url||=null;values.forge_repo||=null;const result=await api('/projects'+(project?'/'+project.id:''),project?'PUT':'POST',values);projectId=result.id;toast(!project&&(values.initialize||values.prepare_environment)?tr('Проект сохранён. Индексация и подготовка окружения в очереди.'):tr('Проект сохранён'));});
}
$('#add-project').onclick=$('#empty-add').onclick=()=>projectForm();
async function taskForm(incident=false) {
  const artifactChoices=await acceptedArtifactChoices(projectId);
  const agentConfig=await api(`/projects/${projectId}/agents`);
  const profileChoices=[['',tr('Настройки проекта')],...agentConfig.profiles.map(p=>[p.name,p.name])];
  const choices=[['',tr('Настройки проекта')],...providers];
  modal(incident?tr('Поломка preprod'):tr('Новая задача'),
    field('title',tr('Что нужно сделать?'))+
    field('priority',tr('Приоритет'),{type:'select',choices:taskPriorities,value:'normal'})+
    (incident?'':field('scope',tr('Объём'),{type:'select',choices:[['',tr('Обычная задача — сразу в разработку')],['plan',tr('Большая задача — сначала план и подзадачи')]],required:false}))+
    field('request',tr('Описание и критерии приёмки'),{type:'textarea',placeholder:tr('Что происходит сейчас? Как должно работать? Как проверить результат?')})+attachField()+
    (artifactChoices.length?field('artifact_version_ids',tr('Входные артефакты — принятые версии'),{type:'multiselect',choices:artifactChoices,value:[],required:false}):'')+
    field('execution',tr('Режим выполнения'),{type:'select',choices:[['subscriptions',tr('Подписки и правила проекта')],['api',tr('Платный API для всех ролей')]]})+
    field('writer',tr('Разработчик этой задачи'),{type:'select',choices,required:false})+
    field('reviewer',tr('Ревьюер этой задачи'),{type:'select',choices,required:false})+
    taskStages().map(([key,label])=>field('profile_'+key,tr('{label} — профиль',{label}),{type:'select',choices:profileChoices,required:false})).join('')+
    field('api_budget_usd',tr('Предел расходов API, USD'),{type:'number',value:'0',min:0,max:1000,step:'any'}),
    async values=>{
      values.artifact_version_ids=Array.from($('#modal-fields [name="artifact_version_ids"]')?.selectedOptions||[],option=>option.value);
      values.agent_profiles=Object.fromEntries(taskStages().filter(([key])=>values['profile_'+key]).map(([key])=>[key,values['profile_'+key]]));
      taskStages().forEach(([key])=>delete values['profile_'+key]);
      values.explicit_api=values.execution==='api';delete values.execution;values.plan=values.scope==='plan';delete values.scope;
      values.api_budget_usd=Number(values.api_budget_usd);
      for(const key of ['writer','reviewer']) if(!values[key]) delete values[key];
      const project=projects.find(p=>p.id===projectId);
      if((values.explicit_api||(values.writer||project?.writer)==='api'||(values.reviewer||project?.reviewer)==='api')&&values.api_budget_usd<=0) throw new Error(tr('Для назначения API нужен положительный бюджет.'));
      values.attachments=await takeAttachments();
      return api(`/projects/${projectId}/${incident?'incidents':'tasks'}`,'POST',values);
    });
}
$('#new-task').onclick=()=>taskForm();
$('#new-incident').onclick=()=>taskForm(true);
function poolForm(pool) {
  const fields=field('name',tr('Название пула'),{value:pool?.name||'',placeholder:tr('Основные подписки')})+
    field('cross_runtime',tr('Автоматическая замена Codex ↔ Claude'),{type:'select',choices:boolChoices,value:String(pool?.cross_runtime??true)})+
    field('allowed_providers',tr('Разрешённые исполнители'),{type:'multiselect',choices:providers,value:pool?.allowed_providers||['codex','claude','api']})+
    field('allowed_models',tr('Разрешённые модели, по одной на строке; пусто — все'),{type:'textarea',required:false,value:(pool?.allowed_models||[]).join('\n')})+
    field('max_active',tr('Одновременных запусков в пуле'),{type:'number',value:pool?.max_active??2,min:1,max:100})+
    field('max_switches',tr('Предел переключений подключения'),{type:'number',value:pool?.max_switches??8,min:1,max:100})+
    field('retry_seconds',tr('Повторная проверка квоты через, секунд'),{type:'number',value:pool?.retry_seconds??300,min:30,max:86400})+
    field('min_remaining_percent',tr('Запас квоты перед запуском, %'),{type:'number',value:pool?.min_remaining_percent??10,min:0,max:100})+
    field('quota_max_age_seconds',tr('Свежесть данных квоты, секунд'),{type:'number',value:pool?.quota_max_age_seconds??300,min:30,max:86400})+
    field('require_known_quota',tr('Ждать, если доступная квота неизвестна'),{type:'select',choices:boolChoices,value:String(pool?.require_known_quota??false)})+
    field('sessions_per_connection',tr('Параллельных задач на одну подписку'),{type:'number',value:pool?.sessions_per_connection??3,min:1,max:10})+
    field('light_codex',tr('Дешёвая модель Codex для лёгких подзадач; пусто — модель подключения'),{required:false,value:pool?.light_models?.codex||''})+
    field('light_claude',tr('Дешёвая модель Claude для лёгких подзадач; пусто — haiku'),{required:false,value:pool?.light_models?.claude||''});
  modal(pool?tr('Политика пула'):tr('Новый пул'),fields,async values=>{
    values.cross_runtime=values.cross_runtime==='true';values.require_known_quota=values.require_known_quota==='true';
    values.allowed_models=values.allowed_models.split('\n').map(v=>v.trim()).filter(Boolean);
    for(const key of ['max_active','max_switches','retry_seconds','min_remaining_percent','quota_max_age_seconds','sessions_per_connection']) values[key]=Number(values[key]);
    values.light_models={codex:values.light_codex,claude:values.light_claude};delete values.light_codex;delete values.light_claude;
    await api('/pools'+(pool?'/'+pool.id:''),pool?'PUT':'POST',values);
    toast(tr('Пул сохранён'));
  });
}
$('#add-pool').onclick=()=>poolForm();
function deletePool(pool) {
  const linked=projects.filter(p=>p.writer_pool===pool.id||p.reviewer_pool===pool.id);
  modal(tr('Удалить пул «{name}»?',{name:pool.name}),`<p>${tr('Подписки и их авторизация сохранятся. Удалится только пул и его связи с подписками.')}</p>${linked.length?`<p>${tr('Сначала выбери другой пул в проектах: {names}.',{names:linked.map(p=>esc(p.name)).join(', ')})}</p>`:''}`,async()=>{
    await api('/pools/'+pool.id,'DELETE');
    toast(tr('Пул удалён'));
  });
  $('#submit-modal').textContent=tr('Удалить пул');
  $('#submit-modal').disabled=linked.length>0;
}
function deleteProject(project) {
  modal(tr('Удалить проект «{name}»?',{name:project.name}),`<p>${tr('Удалятся задачи проекта, их история, артефакты, бэклог, чаты с Директором и каналы уведомлений проекта. Репозиторий и его ветки останутся. Отменить нельзя.')}</p>`+field('confirm_name',tr('Введи название проекта для подтверждения')),async values=>{
    if(values.confirm_name.trim()!==project.name) throw new Error(tr('Название не совпадает'));
    await api('/projects/'+project.id,'DELETE');
    toast(tr('Проект удалён'));
  });
  $('#submit-modal').textContent=tr('Удалить проект');
}
const credentialsPlaceholder='Claude: {"oauth_token":"…"}\nCodex: {"auth_method":"siwc","accessToken":"…"}\nAPI: {"api_key":"…","base_url":"https://…/v1","input_usd_per_million":0,"output_usd_per_million":0}';
function deleteConnection(connection) {
  modal(tr('Удалить подписку «{name}»?',{name:connection.name}),`<p>${tr('Фабрика забудет этот аккаунт и его авторизацию. История задач сохранится. Чтобы вернуть подписку, её придётся подключить заново.')}</p>`,async()=>{
    await api('/connections/'+connection.id,'DELETE');
    toast(tr('Подписка удалена'));
  });
  $('#submit-modal').textContent=tr('Удалить подписку');
}
function connectionForm(connection) {
  const creating=!connection;
  let fields=field('name',tr('Название'),{value:connection?.name||''});
  if(creating) fields+=field('provider',tr('Исполнитель'),{type:'select',choices:providers})+field('account_key',tr('Постоянный ID аккаунта у провайдера'));
  fields+=field('pool_ids',tr('Пулы подключения'),{type:'multiselect',choices:poolList.map(p=>[p.id,p.name]),value:connection?.pool_ids||(poolList[0]?[poolList[0].id]:[])})+
    field('priority',tr('Приоритет: меньшее число выбирается раньше'),{type:'number',value:connection?.priority??100})+
    field('model',tr('Модель (для API обязательна)'),{value:connection?.model||'',required:false});
  if(creating) fields+=field('credentials',tr('Реквизиты подключения, JSON'),{type:'textarea',placeholder:credentialsPlaceholder});
  else fields+=field('enabled',tr('Подключение включено'),{type:'select',choices:boolChoices,value:String(connection.status!=='disabled')});
  modal(creating?tr('Подключить исполнителя'):tr('Настройки подключения'),fields,async values=>{
    if((connection?.provider||values.provider)==='api'&&!values.model.trim()) throw new Error(tr('Для API укажи модель.'));
    values.model=values.model.trim()||null;values.priority=Number(values.priority);
    if(creating) values.credentials=JSON.parse(values.credentials);
    else values.enabled=values.enabled==='true';
    await api('/connections'+(connection?'/'+connection.id:''),connection?'PUT':'POST',values);
    toast(tr('Подключение сохранено'));
  });
}
function reconnectForm(connection) {
  modal(tr('Обновить авторизацию: {name}',{name:connection.name}),
    '<p>'+tr('Введи новые реквизиты. Сохранённые секреты здесь не отображаются.')+'</p>'+
    field('credentials',tr('Новые реквизиты, JSON'),{type:'textarea',placeholder:credentialsPlaceholder}),
    async values=>{await api('/connections/'+connection.id+'/credentials','PUT',JSON.parse(values.credentials));toast(tr('Авторизация обновлена'));});
}
$('#add-connection').onclick=()=>connectionForm();
function memberForm(member) {
  const allowedScope=currentUser.project_ids.length?currentUser.project_ids:currentUser.token_projects;
  const defaultProjects=member?.project_ids||(allowedScope.length?projects.map(p=>p.id):[]);
  const fields=field('name',tr('Имя'),{value:member?.name||''})+
    field('role',tr('Роль'),{type:'select',choices:roles,value:member?.role||'observer'})+
    field('project_ids',tr('Разрешённые проекты; пусто — все'),{type:'multiselect',choices:projects.map(p=>[p.id,p.name]),value:defaultProjects,required:false})+
    (member?field('enabled',tr('Доступ включён'),{type:'select',choices:boolChoices,value:String(member.enabled)}):'');
  modal(member?tr('Доступ участника'):tr('Добавить участника'),fields,async values=>{
    if(member) values.enabled=values.enabled==='true';
    const result=await api('/members'+(member?'/'+member.id:''),member?'PUT':'POST',values);
    if(!member) showToken(tr('Токен участника'),result.token);
    else if(member.id===currentUser.id) await login();
    else toast(tr('Доступ участника сохранён'));
  });
}
$('#add-member').onclick=()=>memberForm();
function organizationForm() {
  modal(tr('Настройки организации'),field('name',tr('Название'),{value:currentUser.organization.name})+
    field('max_active',tr('Одновременных задач организации'),{type:'number',value:currentUser.organization.max_active??2,min:1,max:100}),
    async values=>{values.max_active=Number(values.max_active);currentUser.organization=await api('/organization','PUT',values);$('#org-name').textContent=currentUser.organization.name;toast(tr('Организация сохранена'));});
}
function quotaSummary(connection) {
  const quota=connection.quota_details||{};
  const parts=[Number.isFinite(quota.remaining_percent)?tr('остаток квоты: {percent}%',{percent:quota.remaining_percent}):tr('остаток квоты неизвестен')];
  if(Number.isFinite(quota.reset_at)) parts.push(tr('сброс: {time}',{time:new Date(quota.reset_at*1000).toLocaleString(LOCALE)}));
  if(Number.isFinite(connection.quota_observed)) parts.push(tr('данные от {time}',{time:new Date(connection.quota_observed*1000).toLocaleString(LOCALE)}));
  return parts.join(' · ');
}
async function settings() {
  const owner=isOwner(),orgOwner=isOrganizationOwner();
  const organization=orgOwner?`<div class="setting-row"><strong>${esc(currentUser.organization.name)}</strong><small>${tr('Одновременных задач: {n}',{n:currentUser.organization.max_active??2})}</small><div class="setting-actions"><button class="quiet" id="edit-organization">${tr('Настроить организацию →')}</button></div></div>`:'';
  const poolRows=organization+(poolList.length?poolList.map(p=>`<div class="setting-row"><strong>${esc(p.name)}</strong><small>${p.cross_runtime?tr('Codex ↔ Claude разрешён'):tr('Один исполнитель')} · ${tr('до {n} запусков',{n:p.max_active??2})} · ${esc((p.allowed_providers||['codex','claude','api']).join(', '))}</small>${orgOwner?`<div class="setting-actions"><button class="quiet" data-edit-pool="${p.id}">${tr('Настроить пул →')}</button><button class="quiet" data-delete-pool="${p.id}">${tr('Удалить пул')}</button></div>`:''}</div>`).join(''):'<p>'+tr('Создай пул и подключи аккаунты исполнителей.')+'</p>');
  if(owner) {
    const [connections,members]=await Promise.all([api('/connections'),api('/members')]);
    $('#connections').innerHTML=connections.map(c=>`<div class="setting-row"><strong>${esc(c.name)} · ${esc(c.provider)}</strong><small>${esc(c.status)} · ${esc(quotaSummary(c))} · ${tr('источник: {source}',{source:esc(c.quota_source)})} · ${tr('приоритет {n}',{n:c.priority})}${c.lease_task?' · '+tr('занят'):''}${c.retry_at?' · '+tr('повтор {time}',{time:esc(new Date(c.retry_at*1000).toLocaleString(LOCALE))}):''}</small>${orgOwner?`<div class="setting-actions"><button class="quiet" data-edit-connection="${c.id}" ${c.lease_task?'disabled':''}>${tr('Настроить')}</button>${c.status==='quota'?`<button class="quiet" data-recheck="${c.id}">${tr('Лимит сброшен — проверить сейчас')}</button>`:''}<button class="quiet" data-reconnect="${c.id}" ${c.lease_task?'disabled':''}>${tr('Обновить авторизацию')}</button><button class="quiet" data-delete-connection="${c.id}" ${c.lease_task?'disabled':''}>${tr('Удалить подписку')}</button></div>`:''}</div>`).join('')||'<p>'+tr('Подключений пока нет. Задачи будут ждать пул.')+'</p>';
    const manageable=m=>[currentUser.project_ids,currentUser.token_projects].every(scope=>!scope.length||(m.project_ids.length&&m.project_ids.every(id=>scope.includes(id))));
    $('#members').innerHTML=members.map(m=>`<div class="setting-row"><strong>${esc(m.name)}</strong><small>${esc(m.role)} · ${m.enabled?tr('активен'):tr('отключён')} · ${m.project_ids.length?tr('проектов: {n}',{n:m.project_ids.length}):tr('все проекты')}</small>${manageable(m)?`<div class="setting-actions"><button class="quiet" data-edit-member="${m.id}">${tr('Изменить доступ →')}</button>${m.id!==currentUser.id?`<button class="quiet" data-reissue-member="${m.id}">${tr('Перевыпустить токен')}</button>`:''}</div>`:''}</div>`).join('');
    document.querySelectorAll('[data-edit-connection]').forEach(b=>b.onclick=()=>connectionForm(connections.find(c=>c.id===b.dataset.editConnection)));
    document.querySelectorAll('[data-reconnect]').forEach(b=>b.onclick=()=>reconnectForm(connections.find(c=>c.id===b.dataset.reconnect)));
    document.querySelectorAll('[data-delete-connection]').forEach(b=>b.onclick=()=>deleteConnection(connections.find(c=>c.id===b.dataset.deleteConnection)));
    document.querySelectorAll('[data-recheck]').forEach(b=>b.onclick=async()=>{await api('/connections/'+b.dataset.recheck+'/recheck','POST');toast(tr('Квота будет проверена при следующем запуске'));settings();});
    document.querySelectorAll('[data-edit-member]').forEach(b=>b.onclick=()=>memberForm(members.find(m=>m.id===b.dataset.editMember)));
    document.querySelectorAll('[data-reissue-member]').forEach(b=>b.onclick=async()=>{const m=members.find(m=>m.id===b.dataset.reissueMember);if(!confirm(tr('Прежние токены {name} перестанут работать. Продолжить?',{name:m.name})))return;const result=await api('/members/'+m.id+'/token','POST');showToken(tr('Новый токен {name}',{name:m.name}),result.token);});
  } else {
    $('#connections').innerHTML='<p>'+tr('Подключениями управляет владелец организации.')+'</p>';
    $('#members').innerHTML='<p>'+tr('Доступом участников управляет владелец.')+'</p>';
  }
  $('#pools').innerHTML=poolRows+projects.map(p=>`<div class="setting-row"><strong>${esc(p.name)}</strong><small>${esc(p.remote)}</small>${environmentLine(p)}<small class="project-health">preprod: ${esc(p.incident_id?tr('идёт исправление'):({healthy:tr('проверена'),broken:tr('проверки не прошли')}[p.health?.state]||tr('ожидает проверки')))}</small>${standLine(p)}${owner?`<div class="setting-actions">${p.stand_url?`<button class="quiet" data-stand-qa="${p.id}">${tr('Тестировать стенд')}</button>`:''}${p.deploy_job?`<button class="quiet" data-deploy="${p.id}">${tr('Деплой main')}</button>`:''}<button class="quiet" data-agents="${p.id}">${tr('Роли, скиллы и MCP')}</button><button class="quiet" data-environment="${p.id}">${tr('Подготовить окружение')}</button><button class="quiet" data-edit="${p.id}">${tr('Настроить проект →')}</button>${orgOwner?`<button class="quiet" data-delete-project="${p.id}">${tr('Удалить проект')}</button>`:''}</div>`:''}</div>`).join('');
  if($('#edit-organization')) $('#edit-organization').onclick=organizationForm;
  document.querySelectorAll('[data-edit-pool]').forEach(b=>b.onclick=()=>poolForm(poolList.find(p=>p.id===b.dataset.editPool)));
  document.querySelectorAll('[data-delete-pool]').forEach(b=>b.onclick=()=>deletePool(poolList.find(p=>p.id===b.dataset.deletePool)));
  document.querySelectorAll('[data-agents]').forEach(b=>b.onclick=()=>agentSettings(projects.find(p=>p.id===b.dataset.agents)).catch(e=>toast(e.message)));
  document.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>projectForm(projects.find(p=>p.id===b.dataset.edit)));
  document.querySelectorAll('[data-stand-qa]').forEach(b=>b.onclick=async()=>{try{await api('/projects/'+b.dataset.standQa+'/qa','POST');toast(tr('Тестировщик скоро откроет стенд; найденные дефекты появятся задачами'));settings();}catch(e){toast(e.message);}});
  document.querySelectorAll('[data-deploy]').forEach(b=>b.onclick=async()=>{if(!confirm(tr('Запустить деплой текущей main через GitLab?')))return;try{await api('/projects/'+b.dataset.deploy+'/deploy','POST');toast(tr('Деплой поставлен в очередь: фабрика дождётся сборки и запустит job'));settings();}catch(e){toast(e.message);}});
  document.querySelectorAll('[data-delete-project]').forEach(b=>b.onclick=()=>deleteProject(projects.find(p=>p.id===b.dataset.deleteProject)));
  document.querySelectorAll('[data-environment]').forEach(b=>b.onclick=async()=>{const task=await api('/projects/'+b.dataset.environment+'/environment','POST');toast(tr('Агент готовит окружение: {title}',{title:task.title}));});
  await Promise.all([tokenSettings(),subscriptionSettings(),notificationSettings()]);
}
const relationNames={depends_on:[tr('Зависит от'),tr('Блокирует')],related_to:[tr('Связана с'),tr('Связана с')],duplicate:[tr('Дубликат задачи'),tr('Дублируется задачей')],parent:[tr('Подзадача'),tr('Родительская задача')]};
const eventNames={created:tr('Создана'),later_notes:tr('Заметки на потом'),deferred:tr('Ждёт свободные файлы'),switch:tr('Смена подключения'),implementation_summary:tr('Итог разработчика'),review_incomplete:tr('Ревью не проверило все файлы'),folded:tr('Директор передал цель ожидающим задачам'),backlog:tr('Создана из бэклога'),dependencies_changed:tr('Входы обновлены'),dependencies_changed_after_merge:tr('Входы изменились при слиянии'),dependencies_interrupted:tr('Входы изменились во время работы'),dependency_dropped:tr('Снята отменённая зависимость'),target_changed:tr('Preprod сдвинулся'),later_dropped:tr('Заметки не ушли в бэклог'),environment_retry:tr('Окружение пересобирается'),health_retry:tr('Повтор проверки preprod'),health_environment:tr('Проверка preprod упала на окружении'),executor_session:tr('Сессия исполнителя'),budget_reserved:tr('Бюджет API зарезервирован'),decomposed:tr('Подзадачи сохранены'),chat_command:tr('Команда из чата'),priority_changed:tr('Приоритет изменён'),accepted:tr('Слита в preprod'),promotion_merged:tr('Влито в main'),scheduled:tr('Создана по расписанию'),deploy_queued:tr('Деплой ждёт пайплайн'),deploy_started:tr('Деплой запущен'),deploy_succeeded:tr('Деплой выполнен'),deploy_failed:tr('Деплой не удался'),qa_reported:tr('Найдено тестировщиком на стенде'),qa_verified:tr('Исправление подтверждено на стенде'),qa_not_fixed:tr('На стенде не исправлено'),promotion_rejected:tr('Перенос в main отклонён'),review_repeated:tr('Повтор замечаний ревью'),environment_requested:tr('Запрошено исправление окружения'),supervisor:tr('Решение директора'),supervisor_escalated:tr('Директор просит решения'),toolchain_ready:tr('Окружение собрано'),toolchain_failed:tr('Окружение не собралось'),checks_adopted:tr('Проверки проекта назначены'),environment_blocked:tr('Среде агента не хватает'),phase_started:tr('Этап начат'),phase_finished:tr('Этап завершён'),correction:tr('Ревью вернуло на доработку'),clarification:tr('Вопрос владельцу'),split_requested:tr('Разбита на подзадачи'),human_message:tr('Сообщение'),agent_hint:tr('Подсказка агенту'),attachments:tr('Вложения'),relationship:tr('Связь добавлена'),relationship_removed:tr('Связь убрана'),delegated:tr('Созданы подзадачи'),delegated_cancelled:tr('Подзадачи отменены'),merge:tr('Слияние'),recovered:tr('Восстановлена после перезапуска'),execution_error:tr('Ошибка исполнителя'),stopped:tr('Остановлена'),limit:tr('Исчерпан лимит'),time_slice:tr('Пауза по кванту времени'),quota_observed:tr('Квота подписки'),stalled:tr('Агент завис'),activity_resumed:tr('Агент снова активен'),budget_warning:tr('Задача долгая'),token_warning:tr('Много токенов'),time_extended:tr('Время продлено'),stall_restart:tr('Перезапуск после зависания'),review_required:tr('Нужно слияние человеком'),egress_denied:tr('Сеть отклонила запрос'),network_blocked:tr('Агенту закрыта сеть'),egress_allowed:tr('Разрешены адреса'),checks_changed:tr('Изменены проверки'),agent_settings_changed:tr('Изменены роли'),artifact_input:tr('Входной артефакт'),integration_preempted:tr('Интеграция перенесена'),incident_detected:tr('Инцидент'),incident_reopened:tr('Инцидент повторился'),health_check:tr('Проверка здоровья'),health_network:tr('Сетевой сбой проверки'),remote_unreachable:tr('Git-сервер недоступен'),tidied:tr('Убран вывод прогонов'),checks_passed:tr('Проверки пройдены в сессии'),network_unstable:tr('Сеть нестабильна'),disk_low:tr('Мало места на диске'),resume:tr('Продолжена владельцем'),cancel:tr('Отменена')};
const eventTone=kind=>['execution_error','review_repeated','stalled','network_blocked','environment_blocked','limit','stopped','incident_detected','incident_reopened'].includes(kind)?'bad':['correction','clarification','review_required','egress_denied','stall_restart'].includes(kind)?'warn':['merge','phase_finished','egress_allowed','activity_resumed'].includes(kind)?'good':'';
const duration=seconds=>seconds<60?tr('{n} с',{n:Math.round(seconds)}):seconds<3600?tr('{n} мин',{n:Math.round(seconds/60)}):tr('{n} ч',{n:(seconds/3600).toFixed(1)});
function relationRow(r){return `<div class="relation" data-open-task="${r.task_id}" role="button" tabindex="0"><i class="subtask-dot state-${esc(r.state)}"></i>${r.kind==='parent'?'':`<span class="relation-kind">${esc(relationNames[r.kind]?.[r.outgoing?0:1]||r.kind)}</span>`}<span class="relation-title">${esc(r.title)}</span><span class="relation-id">DF-${r.task_id.slice(0,6).toUpperCase()}</span><span class="chip state state-${esc(r.state)}">${esc(names[r.state]||r.state)}</span><button class="quiet relation-remove" data-remove-link="${r.id}" data-owner="${r.owner_id}" title="${tr('Убрать связь')}" aria-label="${tr('Убрать связь')}">×</button></div>`;}
function eventRow(e){return `<div class="event ${eventTone(e.kind)}"><small>${new Date(e.at*1000).toLocaleString(LOCALE,{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})} · ${esc(eventNames[e.kind]||e.kind)}${e.repeat?` ×${e.repeat} · ${duration(e.at-e.since)}`:''}</small>${e.message&&e.message!==e.kind?`<p>${esc((e.message.length>600?e.message.slice(0,600)+'…':e.message).replace(/\b[0-9a-f]{32}\b/g,id=>'DF-'+id.slice(0,6).toUpperCase()).replace(/^(parent|depends_on|related_to|duplicate): /,(m,k)=>(relationNames[k]?.[0]||k)+': '))}</p>`:''}${e.kind==='later_notes'&&e.data?.items?.length?`<ul class="later-notes">${e.data.items.map(text=>`<li><p>${esc(text)}</p>${typeof noteActions==='function'?noteActions(text):''}</li>`).join('')}</ul>`:''}</div>`;}
// What the task produced: the writer's full final answer (for research the answer is the result), else its short summary.
function resultSection(t){
  const summary=[...(t.events||[])].reverse().find(e=>e.kind==='implementation_summary')?.message,text=t.report||summary;
  return text?`<div class="detail-section"><h3>${tr('Результат')}</h3><div class="detail-report">${esc(text)}</div></div>`:'';
}
async function showDetail(id) {
  // A refresh of the open task keeps the reader's place: the body holds its height until every part is drawn.
  const dialog=$('#detail'),body=$('#detail-body'),top=dialog.open&&detailId===id?dialog.scrollTop:0;
  detailId=id;route(id);const t=await api('/tasks/'+id);$('#detail-title').textContent=t.title;
  body.style.minHeight=top?body.offsetHeight+'px':'';
  const rel=t.relations||[],children=rel.filter(r=>r.kind==='parent'&&r.outgoing),parent=rel.find(r=>r.kind==='parent'&&!r.outgoing),other=rel.filter(r=>r.kind!=='parent');
  const done=children.filter(c=>c.state==='merged').length,files=['report.md','plan.md','diff.patch','checks.txt','review.txt','handoff.md',...(t.role_config?.initialization?['repository-index.md']:[])];
  const history=compactHistory(t.events.slice().reverse(),historyAll),shown=history.slice(0,12),total=t.event_count??history.length;
  $('#detail-body').innerHTML=`${parent?`<button class="detail-parent" data-open-task="${parent.task_id}">↖ ${tr('Подзадача для <strong>{title}</strong>',{title:esc(parent.title)})}</button>`:''}<div class="detail-meta"><span class="chip">DF-${t.id.slice(0,6).toUpperCase()}</span><span class="chip state state-${esc(t.state)}">${esc(stateLabel(t))}</span><span class="chip">${esc(phases[t.phase]||t.phase)}</span><span class="chip">${tr('{duration} · {count} исправл.',{duration:duration(t.active_seconds),count:t.corrections})}</span><span class="chip task-priority-${esc(t.priority||'normal')}">${esc(priorityLabel(t.priority))}</span>${t.usage&&window.usageLabel?`<span class="chip" title="${tr('Сумма по всем запускам; для подписок — эквивалент цены API')}">${esc(usageLabel(t.usage))}</span>`:''}${activityLabel(t)?`<span class="chip card-activity${activityLabel(t).stalled?' stalled':''}">${esc(activityLabel(t).text)}</span>`:''}</div><div class="detail-now">${esc(nowLine(t))}</div>${blockedHosts(t).length?`<div class="network-blocked"><p>${tr('Агент упёрся в закрытую сеть: {hosts}. Ещё один цикл без доступа ничего не исправит.',{hosts:blockedHosts(t).map(h=>`<code>${esc(h)}</code>`).join(', ')})}</p><button id="allow-egress">${tr('Разрешить и продолжить')}</button></div>`:''}<div class="detail-toolbar">${t.state==='review_required'&&t.candidate_sha&&t.reviewed_sha===t.candidate_sha?'<button id="merge-task">'+tr('Слить в preprod')+'</button>':''}<button id="resume-task">${tr('Продолжить / уточнить')}</button><button id="open-transcript" class="${t.state==='running'?'live':'secondary'}">${t.state==='running'?tr('● Что агент делает сейчас'):tr('Чат агента')}</button><button id="open-evidence" class="secondary">${tr('Браузерные проверки')}</button><span class="toolbar-gap"></span><button id="change-priority" class="quiet">${tr('Приоритет')}</button><button id="cancel-task" class="quiet danger">${tr('Отменить')}</button></div><div class="detail-grid"><div class="detail-section"><h3>${tr('Задача')}</h3><div class="detail-request">${esc(t.request)}</div></div><div class="detail-facts">${t.branch?`<div><small>${tr('Ветка агента')}</small><code>${esc(t.branch)}</code></div>`:''}${safeHref(t.change_url)?`<div><small>${tr('Запрос на слияние')}</small><a href="${esc(t.change_url)}" target="_blank" rel="noopener">PR / MR ↗</a></div>`:''}${t.result_sha?`<div><small>${tr('Принятый коммит')}</small>${safeHref(t.commit_url)||/^\/api\/tasks\/[0-9a-f]+$/.test(t.commit_url||'')?`<a href="${esc(t.commit_url)}" target="_blank" rel="noopener"><code>${esc(t.result_sha.slice(0,12))}</code></a>`:`<code>${esc(t.result_sha.slice(0,12))}</code>`}</div>`:''}<div><small>${tr('Файлы задачи')}</small><div class="file-chips">${files.map(name=>`<button class="file-chip" data-artifact="${name}">${name}</button>`).join('')}</div></div>${t.attachments?.length?`<div><small>${tr('Вложения')}</small><div class="file-chips">${t.attachments.map(f=>`<button class="file-chip" data-attachment="${esc(f.name)}">📎 ${esc(f.name)}</button>`).join('')}</div></div>`:''}</div></div>${resultSection(t)}${children.length?`<div class="detail-section"><div class="section-head"><h3>${tr('Подзадачи')}</h3><span class="subtasks-progress"><i style="width:${Math.round(done/children.length*100)}%"></i></span><small>${tr('{done} из {total} слито',{done,total:children.length})}</small></div><div class="relation-list">${children.map(relationRow).join('')}</div></div>`:''}<div class="detail-section"><div class="section-head"><h3>${tr('Связи')}</h3><button id="link-task" class="quiet">${tr('+ Связать')}</button></div>${other.length?`<div class="relation-list">${other.map(relationRow).join('')}</div>`:'<p class="muted small">'+tr('Нет зависимостей и связанных задач.')+'</p>'}</div><div id="detail-extra"></div><div class="detail-section"><div class="section-head"><h3>${tr('История')}</h3><small>${tr('{n} событий',{n:total})}</small><button id="history-mode" class="quiet">${historyAll?tr('Только важное'):tr('Все события')}</button></div><div class="timeline">${shown.map(eventRow).join('')}</div>${total>shown.length?`<button id="more-history" class="quiet">${tr('Показать всю историю')}</button>`:''}</div>`;
  $('#history-mode').onclick=()=>{historyAll=!historyAll;showDetail(id).catch(e=>toast(e.message));};
  // The task page carries the latest events; older ones are fetched page by page on request.
  if($('#more-history')) $('#more-history').onclick=async()=>{
    const button=$('#more-history');button.disabled=true;
    try{let more=t.events_more,before=t.events[0]?.id;while(more&&before!==undefined){const page=await api(`/tasks/${id}/events?before=${before}&limit=1000`);history.push(...page.events.slice().reverse());more=page.more;before=page.events[0]?.id;}
      $('#detail-body .timeline').innerHTML=compactHistory(history,historyAll).map(eventRow).join('');button.remove();}
    catch(e){button.disabled=false;toast(e.message);}
  };
  document.querySelectorAll('#detail-body [data-open-task]').forEach(b=>{b.onclick=()=>showDetail(b.dataset.openTask).catch(e=>toast(e.message));b.onkeydown=e=>{if(e.key==='Enter')b.click();};});
  if(!$('#detail').open) $('#detail').showModal();
  const readonly=currentUser.role==='observer'||currentUser.token_role==='observer';
  $('#cancel-task').disabled=readonly||['merged','cancelled','duplicate','checked'].includes(t.state);$('#resume-task').disabled=readonly||['running','recovering','merged','duplicate'].includes(t.state);$('#link-task').disabled=readonly;
  $('#change-priority').disabled=readonly;
  $('#change-priority').onclick=()=>modal(tr('Приоритет задачи'),field('priority',tr('Приоритет'),{type:'select',choices:taskPriorities,value:t.priority||'normal'}),async values=>{await api('/tasks/'+id+'/priority','PUT',values);await showDetail(id);});
  if($('#merge-task')){$('#merge-task').disabled=readonly;$('#merge-task').onclick=async()=>{if(!confirm(tr('Слить проверенные ревьюером изменения в preprod?')))return;try{await api('/tasks/'+id+'/actions','POST',{action:'merge'});await showDetail(id);await load();}catch(e){toast(e.message);}};}$('#cancel-task').onclick=async()=>{try{await api('/tasks/'+id+'/actions','POST',{action:'cancel'});await showDetail(id);await load();}catch(e){toast(e.message);}};
  $('#resume-task').onclick=()=>{modal(tr('Продолжить задачу'),field('message',tr('Уточнение для исполнителя'),{type:'textarea',required:false})+attachField()+field('extend_seconds',tr('Добавить активного времени, секунд'),{type:'number',value:'0'})+field('extra_corrections',tr('Добавить циклов исправления'),{type:'number',value:'0'})+field('extra_api_budget_usd',tr('Добавить бюджет API, USD'),{type:'number',value:'0'}),async values=>{await api('/tasks/'+id+'/actions','POST',{action:'resume',attachments:await takeAttachments(),...values,extend_seconds:Number(values.extend_seconds),extra_corrections:Number(values.extra_corrections),extra_api_budget_usd:Number(values.extra_api_budget_usd)});await showDetail(id);});};
  $('#link-task').onclick=()=>modal(tr('Связать задачи'),field('other_id',tr('ID второй задачи'))+field('kind',tr('Связь'),{type:'select',choices:[['depends_on',tr('Зависит от результата второй задачи')],['parent',tr('Вторая задача — обязательная подзадача')],['related_to',tr('Связаны по смыслу')],['duplicate',tr('Текущая задача — дубликат второй')]]}),async values=>{await api('/tasks/'+id+'/links','POST',values);await showDetail(id);});
  document.querySelectorAll('[data-remove-link]').forEach(b=>{b.disabled=readonly;b.onclick=async event=>{event.stopPropagation();if(!confirm(tr('Убрать связь?')))return;try{await api(`/tasks/${b.dataset.owner}/links/${b.dataset.removeLink}`,'DELETE');await showDetail(id);}catch(e){toast(e.message);}};});
  const allow=$('#allow-egress');if(allow) allow.onclick=async()=>{const hosts=blockedHosts(t);if(!confirm(tr('Разрешить проекту доступ к:\n{hosts}\n\nЗадача продолжится с дополнительным циклом исправления.',{hosts:hosts.join('\n')})))return;try{await api('/tasks/'+id+'/allow-egress','POST',{hosts});await api('/tasks/'+id+'/actions','POST',{action:'resume',extra_corrections:1,message:tr('Сеть открыта для: {hosts}. Повтори сборку и проверки.',{hosts:hosts.join(', ')})});toast(tr('Адреса разрешены, задача продолжена'));showDetail(id);}catch(e){toast(e.message);}};
  document.querySelectorAll('[data-attachment]').forEach(b=>b.onclick=async()=>{try{const response=await fetch(`/api/tasks/${id}/attachments/${encodeURIComponent(b.dataset.attachment)}`,{headers:{Authorization:'Bearer '+token}});if(!response.ok) throw Error(tr('Файл не найден'));const url=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=url;if(!(response.headers.get('content-type')||'').startsWith('image/')) link.download=b.dataset.attachment;else link.target='_blank';link.click();setTimeout(()=>URL.revokeObjectURL(url),60000);}catch(e){toast(e.message);}});
  document.querySelectorAll('[data-artifact]').forEach(b=>b.onclick=async()=>{try{const response=await fetch(`/api/tasks/${id}/artifacts/${b.dataset.artifact}`,{headers:{Authorization:'Bearer '+token}});if(!response.ok) throw Error(tr('Артефакт ещё не создан'));const text=await response.text();modal(b.dataset.artifact,`<pre>${esc(text)}</pre>`,async()=>{});}catch(e){toast(e.message);}});
  $('#open-transcript').onclick=()=>showAgentTranscript(id);$('#open-evidence').onclick=()=>showBrowserEvidence(id);
  await renderArtifactInputs(t,readonly);
  renderPromotion(t);
  bindNoteActions($('#detail-body'),t);
  await renderReview(t).catch(()=>{});
  body.style.minHeight='';dialog.scrollTop=top;
}
async function openNotificationLink() {
  const params=new URLSearchParams(location.search),id=params.get('task');
  if(!id)return;
  try {
    const task=await api('/tasks/'+encodeURIComponent(id));
    projectId=task.project_id;await load();await showDetail(id);
    const commit=params.get('commit');
    if(params.get('promotion')==='1')await openPromotion(id);
    else if(commit) {
      if(commit!==task.result_sha)throw Error(tr('Этот коммит больше не является принятым результатом задачи'));
      const response=await fetch(`/api/tasks/${encodeURIComponent(id)}/artifacts/diff.patch`,{headers:{Authorization:'Bearer '+token}});
      if(!response.ok)throw Error(tr('Diff коммита ещё недоступен'));
      modal(tr('Коммит {commit}',{commit}),`<pre>${esc(await response.text())}</pre>`,async()=>{});
      $('#submit-modal').hidden=true;$('#cancel-modal').textContent=tr('Закрыть');
    }
  } catch(error){toast(error.message);}
}
async function showBrowserEvidence(id) {
  try {
    const data=await api(`/tasks/${id}/evidence`);
    modal(tr('Браузерные проверки'),data.files.length?`<p>${tr('Скриншоты, трассы и отчёты отдельных запусков. Результат обязательных проверок смотри в checks.txt.')}</p>${data.files.map((file,index)=>`<div class="setting-row"><span style="overflow-wrap:anywhere">${esc(file.name)} · ${tr('{n} КБ',{n:Math.ceil(file.bytes/1024)})}</span><button type="button" class="secondary" data-evidence-index="${index}">${/\.(png|jpe?g|webp)$/i.test(file.name)?tr('Открыть'):tr('Скачать')}</button></div>`).join('')}${data.truncated?'<p>'+tr('Список ограничен. Показаны первые 200 файлов.')+'</p>':''}`:'<p>'+tr('Браузер ещё не сохранял результаты для этой задачи.')+'</p>',async()=>{});
    $('#submit-modal').hidden=true;$('#cancel-modal').textContent=tr('Закрыть');
    document.querySelectorAll('[data-evidence-index]').forEach(button=>button.onclick=async()=>{
      try {
        const file=data.files[Number(button.dataset.evidenceIndex)];
        const response=await fetch(`/api/tasks/${id}/evidence/${file.name.split('/').map(encodeURIComponent).join('/')}`,{headers:{Authorization:'Bearer '+token}});
        if(!response.ok)throw Error(tr('Файл проверки недоступен'));
        const blob=await response.blob();const url=URL.createObjectURL(blob);
        if(/\.(png|jpe?g|webp)$/i.test(file.name)) {
          modal(file.name,`<img alt="${tr('Скриншот браузерной проверки')}" style="max-width:100%;height:auto" src="${url}">`,async()=>{});
          $('#submit-modal').hidden=true;$('#cancel-modal').textContent=tr('Закрыть');
          $('#modal-fields img').onload=()=>URL.revokeObjectURL(url);
          $('#modal-fields img').onerror=()=>URL.revokeObjectURL(url);
        } else {
          const link=document.createElement('a');link.href=url;link.download=file.name.split('/').pop();link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
        }
      } catch(error){toast(error.message);}
    });
  } catch(error){toast(error.message);}
}
function diskWarning(d,held){const box=$('#disk-warning');if(!box)return;const show=d&&d.paused;box.hidden=!show&&!held;box.classList.toggle('hold-warning',!show&&!!held);if(!show&&held)box.innerHTML='<b>'+tr('Пауза перед обновлением фабрики')+'</b> · '+tr('новые задачи не стартуют, начатые шаги доделываются.');if(show)box.innerHTML=`<b>${d.alert?tr('Диск почти заполнен'):tr('Мало места на диске')}</b> · ${tr('свободно {gb} ГБ ({percent}%). Новые задачи на паузе, пока не освободится место в Docker.',{gb:(d.free_bytes/1024**3).toFixed(1),percent:Math.round(d.free_percent)})}`;}
// A background tab is not polled; it catches up the moment it becomes visible again.
async function poll(){if(!token||$('#shell').hidden||refreshBusy||$('#modal').open||document.hidden)return;refreshBusy=true;try{api('/me').then(me=>diskWarning(me.disk,me.held)).catch(()=>{});await load();if(detailId&&$('#detail').open)await showDetail(detailId);}catch(e){console.warn(e.message);}finally{refreshBusy=false;}}
setInterval(poll,5000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});
if(token)login().catch(e=>toast(e.message));

function showToken(title,value){setTimeout(()=>{modal(title,'<p>'+tr('Сохрани токен сейчас. Повторно получить его нельзя.')+'</p><textarea readonly>'+esc(value)+'</textarea>',async()=>{});},0);}
$('#add-organization').onclick=()=>modal(tr('Новая организация'),field('name',tr('Название')),async values=>{const created=await api('/organizations','POST',values);showToken(tr('Токен владельца {name}',{name:created.organization.name}),created.token);});
async function tokenSettings(){const items=await api('/tokens');$('#tokens').innerHTML='<h3>'+tr('Твои API-токены')+'</h3>'+items.map(t=>`<div class="setting-row"><small>${esc(t.id)} · ${esc(t.role)} · ${t.project_ids.length?tr('ограничен проектами'):tr('все доступные проекты')}</small><button class="quiet" data-revoke="${t.id}">${tr('Отозвать')}</button></div>`).join('')+'<button id="issue-token" class="secondary">'+tr('Создать API-токен')+'</button>';document.querySelectorAll('[data-revoke]').forEach(b=>b.onclick=async()=>{try{await api('/tokens/'+b.dataset.revoke,'DELETE');await settings();}catch(e){toast(e.message);}});$('#issue-token').onclick=()=>modal(tr('API-токен'),field('role',tr('Роль'),{type:'select',choices:[['observer',tr('Наблюдатель')],['operator',tr('Оператор')],['owner',tr('Владелец')]]})+field('project_ids',tr('ID проектов через запятую; пусто — все доступные'),{required:false}),async values=>{values.project_ids=values.project_ids.split(',').map(v=>v.trim()).filter(Boolean);const result=await api('/tokens','POST',values);showToken(tr('Новый API-токен'),result.token);});}
