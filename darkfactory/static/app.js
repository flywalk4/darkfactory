'use strict';
const $ = s => document.querySelector(s);
const remembered=key=>{try{return localStorage.getItem(key)||'';}catch{return '';}},remember=(key,value)=>{try{value?localStorage.setItem(key,value):localStorage.removeItem(key);}catch{}};
function route(task){const url=new URL(location.href);for(const key of ['commit','promotion'])url.searchParams.delete(key);task?url.searchParams.set('task',task):url.searchParams.delete('task');history.replaceState(null,'',url);}
let token = sessionStorage.getItem('df-token') || '', projectId = remembered('df-project'), projects = [], poolList = [], taskList = [], boardConnections = null, currentUser;
let submitForm, detailId, refreshBusy = false;
const names = {queued:'В очереди',recovering:'Восстанавливается',waiting_pool:'Ждёт подключение',waiting_dependency:'Ждёт зависимость',waiting_integration:'Ждёт интеграцию',running:'В работе',needs_human:'Нужно решение',review_required:'Без автослияния',merged:'Принята',cancelled:'Отменена',duplicate:'Дубликат',checked:'Проверка пройдена'};
const phases = {prepare:'Подготовка',plan:'План и уточнения',collect:'Сбор результатов подзадач',implement:'Разработка',checks:'Проверки',integrate:'Объединение',verify:'Проверка результата',review:'Ревью',publish:'Запись изменений',merge:'Слияние'};
const providers = [['codex','Codex'],['claude','Claude'],['api','API']];
const roles = [['observer','Наблюдатель'],['operator','Оператор'],['owner','Владелец']];
const boolChoices = [['true','Да'],['false','Нет']];
const taskPriorities = [['critical','Критический'],['high','Высокий'],['normal','Обычный'],['low','Низкий']];
const activityLabel = t => {
  if(t.state!=='running'||!Number.isFinite(t.last_activity))return null;
  const idle=Math.max(0,Date.now()/1000-t.last_activity),stalled=idle>=t.stall_seconds;
  const ago=idle<60?'только что':Math.floor(idle/60)+' мин назад';
  return {stalled,text:stalled?`⚠ Нет активности агента ${Math.floor(idle/60)} мин`:'Агент активен · '+ago};
};
const reasonNames={review_incomplete:'Ревью не смогло проверить все файлы кода',correction_limit:'Ревью отклонило задачу максимальное число раз',review_repeated:'Ревью повторно отклоняет по той же причине',network_blocked:'Агенту закрыта сеть',environment_blocked:'Среде агента чего-то не хватает',stalled:'Агент завис',token_limit:'Исчерпан бюджет токенов'};
// A task picks profiles for its own stages; the Director is project-wide.
const taskStages = () => agentStages.filter(([key]) => key !== 'director');
const stateLabel = t => t.state==='needs_human'&&t.awaits_director?'Решает директор':names[t.state]||t.state;
const reasonLabel = reason => reasonNames[reason]||reason||'';
const blockedHosts = t => {if(t.state!=='needs_human'||t.reason!=='network_blocked')return [];const e=[...(t.events||[])].reverse().find(e=>e.kind==='network_blocked');return e?.data?.hosts||[];};
const priorityLabel = priority => taskPriorities.find(([key]) => key === priority)?.[1] || 'Обычный';
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
  else if(type === 'multiselect') control=`<select name="${name}" multiple size="${Math.min(5,Math.max(2,choices.length))}" ${required?'required':''}>${choices.map(([v,t])=>`<option value="${esc(v)}" ${value.includes(v)?'selected':''}>${esc(t)}</option>`).join('')}</select><small>Можно выбрать несколько. На компьютере: Ctrl / ⌘.</small>`;
  else control=`<input name="${name}" type="${type}" value="${esc(value)}" placeholder="${esc(placeholder)}" ${min!==undefined?`min="${min}"`:''} ${max!==undefined?`max="${max}"`:''} ${step!==undefined?`step="${step}"`:''} ${required?'required':''}>`;
  return `<label>${esc(label)}${control}</label>`;
}
function modal(title,fields,callback) {$('#submit-modal').hidden=false;$('#submit-modal').disabled=false;$('#submit-modal').textContent='Сохранить';$('#cancel-modal').textContent='Отмена';$('#modal-title').textContent=title;$('#modal-fields').innerHTML=fields;$('#form-error').textContent='';submitForm=callback;$('#modal').showModal();}
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
function collapseNav(collapsed){document.body.classList.toggle('nav-collapsed',collapsed);const b=$('#collapse-nav');b.textContent=collapsed?'»':'«';b.title=collapsed?'Развернуть меню':'Свернуть меню';b.setAttribute('aria-label',b.title);b.setAttribute('aria-expanded',String(!collapsed));document.querySelectorAll('.nav,.project-item').forEach(n=>collapsed?n.title=n.textContent.trim():n.removeAttribute('title'));}
$('#collapse-nav').onclick=()=>{const collapsed=!document.body.classList.contains('nav-collapsed');remember('df-nav-collapsed',collapsed?'1':'');collapseNav(collapsed);};collapseNav(remembered('df-nav-collapsed')==='1');
$('#logout').onclick=()=>{sessionStorage.removeItem('df-token');location.reload();};
document.querySelectorAll('[data-view]').forEach(button=>button.onclick=()=>{remember('df-view',button.dataset.view);document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b===button));for(const view of ['board','factory','backlog','settings','agents','artifacts'])$('#'+view+'-view').hidden=button.dataset.view!==view;if(button.dataset.view==='settings') settings().catch(e=>toast(e.message));if(button.dataset.view==='agents')agentsPage().catch(e=>toast(e.message));if(button.dataset.view==='artifacts')registryPage().catch(e=>toast(e.message));if(button.dataset.view==='backlog')backlogPage().catch(e=>toast(e.message));if(button.dataset.view==='factory')factoryPage().catch(e=>toast(e.message));});
async function load() {
  projects=await api('/projects');poolList=await api('/pools');
  if(!projects.some(p=>p.id===projectId)) projectId=projects[0]?.id||'';remember('df-project',projectId);
  $('#project-list').innerHTML=projects.map(p=>`<button class="project-item ${p.id===projectId?'selected':''}" data-project="${p.id}"><span class="project-icon">${esc((p.name.trim()[0]||'◆').toUpperCase())}</span><span class="nav-text">${esc(p.name)}</span></button>`).join('');collapseNav(document.body.classList.contains('nav-collapsed'));
  document.querySelectorAll('[data-project]').forEach(b=>b.onclick=()=>{projectId=b.dataset.project;document.querySelector('[data-view="board"]').click();load().catch(e=>toast(e.message));});
  const project=projects.find(p=>p.id===projectId);$('#current-project').textContent=project?.name||'Проекты';$('#project-heading').textContent=project?.name||'Твоя фабрика разработки';
  $('#project-description').textContent=project?`${project.remote} · preprod · ${project.checks.length ? project.checks.length+' обязательных проверок' : project.merge_without_checks===false ? 'в preprod сливает человек: нет проверок' : 'в preprod автоматически после ревью'} · main — вручную`:'Подключи первый проект, чтобы начать работу.';
  $('#new-incident').disabled=$('#new-task').disabled=!project;$('#empty').hidden=!!project;$('#board').hidden=!project;$('#metrics').hidden=!project;$('.board-heading').hidden=!project;
  [taskList,boardConnections]=project?await Promise.all([api(`/projects/${projectId}/tasks`),api('/connections').catch(()=>null)]):[[],null];renderBoard();
  if(!$('#settings-view').hidden) await settings();
  if(!$('#artifacts-view').hidden) await registryPage();
  if(!$('#backlog-view').hidden&&!$('#backlog-text')?.value) await backlogPage();
}
const openParents=new Set();
function descendants(id,childrenOf,seen=new Set()){return (childrenOf[id]||[]).flatMap(c=>seen.has(c.id)?[]:(seen.add(c.id),[c,...descendants(c.id,childrenOf,seen)]));}
function subtaskRows(children,childrenOf,depth,seen){return children.map(c=>{const nested=seen.has(c.id)?[]:childrenOf[c.id]||[],open=openParents.has(c.id),all=descendants(c.id,childrenOf),done=all.filter(x=>x.state==='merged').length;seen.add(c.id);return `<div class="subtask-row" style="--depth:${depth}">${nested.length?`<button class="subtask-expand" data-subtasks="${c.id}" aria-expanded="${open}" title="${open?'Свернуть':'Развернуть'} подзадачи">${open?'▾':'▸'}</button>`:'<span class="subtask-expand"></span>'}<button class="subtask" data-task="${c.id}" title="${esc(c.title)}"><i class="subtask-dot state-${esc(c.state)}"></i><span class="subtask-title" title="${esc(c.title)}">${esc(c.title)}</span>${nested.length?`<span class="subtask-count">${done}/${all.length}</span>`:''}${c.role_config?.tier==='light'?'<span class="tier-light tier-mark" title="Лёгкая подзадача: дешёвая модель">⚡</span>':''}<span class="subtask-state" title="${esc(c.waiting_for?'Ждёт '+c.waiting_for:'')}">${esc(c.waiting_for?'ждёт '+c.waiting_for:stateLabel(c))}</span></button></div>${open&&nested.length?subtaskRows(nested,childrenOf,depth+1,seen):''}`;}).join('');}
function childRows(t,childrenOf){const children=childrenOf[t.id]||[];if(!children.length)return '';const all=descendants(t.id,childrenOf),done=all.filter(c=>c.state==='merged').length,open=openParents.has(t.id);return `<div class="subtasks${open?' open':''}"><button class="subtasks-toggle" data-subtasks="${t.id}" aria-expanded="${open}"><span>${open?'▾':'▸'} Подзадачи</span><span class="subtasks-progress"><i style="width:${Math.round(done/all.length*100)}%"></i></span><span>${done}/${all.length}</span></button>${open?subtaskRows(children,childrenOf,0,new Set([t.id])):''}</div>`;}
function renderBoard() {
  const ids=new Set(taskList.map(t=>t.id)),childrenOf={};
  taskList.forEach(t=>{if(t.parent_id&&ids.has(t.parent_id))(childrenOf[t.parent_id]||=[]).push(t);});
  Object.values(childrenOf).forEach(list=>list.sort((a,b)=>a.created-b.created));
  // Subtasks live inside their parent card, but active ones also get their own card to watch them.
  const byId=Object.fromEntries(taskList.map(t=>[t.id,t])),active=t=>['running','recovering','needs_human','review_required'].includes(t.state);
  const topLevel=taskList.filter(t=>!(t.parent_id&&ids.has(t.parent_id))||active(t));
  const waiting=t=>t.state.startsWith('waiting')||t.state==='queued';
  const queue=topLevel.filter(waiting).sort((a,b)=>Number(b.incident)-Number(a.incident) || taskPriorities.findIndex(([key])=>key===(a.priority||'normal'))-taskPriorities.findIndex(([key])=>key===(b.priority||'normal')) || a.created-b.created || a.id.localeCompare(b.id));
  const groups=[['К запуску',queue],['В работе',topLevel.filter(t=>['running','recovering'].includes(t.state))],['Нужно внимание',topLevel.filter(t=>['needs_human','review_required'].includes(t.state))],['Завершены',topLevel.filter(t=>['merged','cancelled','duplicate','checked'].includes(t.state))]];
  // Summary answers "does anything need me?": live work, decisions, silent agents and subscription headroom.
  const stalled=taskList.filter(t=>activityLabel(t)?.stalled).length,poolWait=taskList.filter(t=>t.state==='waiting_pool').length;
  const ready=(boardConnections||[]).filter(c=>c.status==='ready'),left=ready.map(c=>c.quota_details?.remaining_percent).filter(Number.isFinite);
  const quota=boardConnections===null?'—':!ready.length?'нет':left.length?Math.max(...left)+'%':'?';
  const counts=[['В работе',taskList.filter(t=>['running','recovering'].includes(t.state)).length,'●',''],['Ждут решения',taskList.filter(t=>['needs_human','review_required'].includes(t.state)).length,'◇',''],['Зависли',stalled,'⚠',stalled?'metric-alert':''],
    ['Квота',quota,poolWait?`ждут: ${poolWait}`:`${ready.length} подкл.`,poolWait||(boardConnections&&!ready.length)?'metric-alert':'']];
  $('#metrics').innerHTML=counts.map(([label,count,note,cls])=>`<div class="metric ${cls}"><span>${label}</span><strong>${count}</strong><em>${note}</em></div>`).join('');$('#task-count').textContent=taskList.length;
  renderMainPending(taskList);
  renderAttention(taskList);
  const card=t=>`<div class="card-group"><button class="card" data-task="${t.id}"><div class="card-id"><span>DF-${t.id.slice(0,6).toUpperCase()}</span><span>${t.incident?'ИНЦИДЕНТ':'•••'}</span></div>${t.parent_id&&byId[t.parent_id]?`<span class="card-parent" role="link" tabindex="0" data-open-parent="${t.parent_id}" title="Открыть родительскую задачу">↖ ${esc(byId[t.parent_id].title)}</span>`:''}<h3>${esc(t.title)}</h3><span class="card-tag">${esc(stateLabel(t))}</span> <span class="card-tag task-priority-${esc(t.priority||'normal')}">${esc(priorityLabel(t.priority))}</span>${t.role_config?.tier==='light'?'<span class="tier-light" title="Лёгкая подзадача: дешёвая модель, любая свободная подписка">лёгкая</span>':''}${t.promotion_pending?'<span class="main-badge" title="Слито в preprod, ждёт твоего решения о переносе в main">→ main</span>':''}${t.reason?`<div class="card-reason">${esc(reasonLabel(t.reason))}</div>`:t.waiting_for?`<div class="card-reason">Ждёт ${esc(t.waiting_for)}</div>`:''}${t.branch&&t.state!=='done'?`<div class="card-branch" title="Ветка агента"><code>${esc(t.branch)}</code></div>`:''}${activityLabel(t)?`<div class="card-activity${activityLabel(t).stalled?' stalled':''}">${esc(activityLabel(t).text)}</div>`:''}<div class="card-bottom"><span class="agent">${esc(phases[t.phase]||t.phase)}</span><span>${Math.round(t.active_seconds/60)} мин · ${t.corrections} исправл.</span></div></button>${childRows(t,childrenOf)}</div>`;
  patchBoard(groups.map(([name,tasks])=>({title:`<i class="state-dot"></i>${name}<span class="count">${tasks.length}</span>`,cards:tasks.map(t=>[t.id,card(t)])})));
}
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
  const parent=event.target.closest('[data-open-parent]'),toggle=event.target.closest('[data-subtasks]'),task=event.target.closest('[data-task]');
  if(parent){event.stopPropagation();showDetail(parent.dataset.openParent).catch(e=>toast(e.message));}
  else if(toggle){event.stopPropagation();const id=toggle.dataset.subtasks;openParents.has(id)?openParents.delete(id):openParents.add(id);renderBoard();}
  else if(task)showDetail(task.dataset.task).catch(e=>toast(e.message));
});
function environmentLine(p){
  const e=p.environment||{},spec=Object.entries(e.spec||{}).map(([k,v])=>k+' '+v).join(', ');
  const status={ready:'готово',failed:'не собралось',base:'базовое'}[e.status]||'ещё не готовилось';
  const source=e.declared?'по .darkfactory/environment.json':e.status?'по файлам проекта':'';
  return `<small class="project-environment${e.status==='failed'?' failed':''}" title="${esc(e.error||'')}">окружение: ${esc(status)}${spec?' · '+esc(spec):''}${source?' · '+esc(source):''}${e.feeds?.length?' · реестры: '+esc(e.feeds.join(', ')):''}</small>`;
}
function projectForm(project) {
  if(!project) {
    const fields=field('name','Название проекта')+
      field('remote','Git-репозиторий',{placeholder:'https://git.company.com/team/project.git или /repos/project'})+
      field('git_token','Токен доступа',{type:'password',required:false,placeholder:'Не нужен для открытого или локального репозитория'})+
      '<p>Менеджер, аналитик, разработчик и ревьюер появятся сразу. Подписки берутся из общего пула организации. Остальное можно изменить в настройках проекта.</p>';
    modal('Новый проект',fields,async values=>{
      const result=await api('/projects','POST',values);
      projectId=result.id;
      toast('Проект создан. Агенты готовы, индексация поставлена в очередь.');
    });
    $('#submit-modal').textContent='Создать проект';
    return;
  }
  const choices=[['','Не выбран'],...poolList.map(p=>[p.id,p.name])];
  let fields=field('name','Название',{value:project?.name||''})+field('remote','Git URL или путь /repos/…',{value:project?.remote||'',placeholder:'https://git.company.com/team/project.git'})+field('seed_branch','Исходная ветка',{value:project?.seed_branch||'main'})+field('checks','Обязательные проверки, каждая с новой строки',{type:'textarea',value:project?.checks.join('\n')||'',placeholder:'npm ci\nnpm test',required:false})+field('writer_pool','Пул разработки',{type:'select',choices,value:project?.writer_pool||'',required:false})+field('reviewer_pool','Пул ревью',{type:'select',choices,value:project?.reviewer_pool||'',required:false})+field('git_token','Git-токен для приватного HTTPS-репозитория',{type:'password',required:false})+field('forge','Запись изменений',{type:'select',choices:[['git','Внутренняя запись Git'],['github','GitHub PR'],['gitlab','GitLab MR']],value:project?.forge||'git'})+field('forge_url','API URL сервера PR/MR',{required:false,value:project?.forge_url||'',placeholder:'https://api.github.com или https://gitlab.company.com/api/v4'})+field('forge_repo','Путь репозитория на сервере',{required:false,value:project?.forge_repo||'',placeholder:'team/project'})+field('forge_token','Токен сервера PR/MR',{type:'password',required:false});
  fields+=field('writer','Основной разработчик',{type:'select',value:project?.writer||'codex',choices:[['codex','Codex'],['claude','Claude'],['api','API']]})+field('reviewer','Основной ревьюер',{type:'select',value:project?.reviewer||'claude',choices:[['codex','Codex'],['claude','Claude'],['api','API']]})+field('max_seconds','Время на задачу (и на каждую подзадачу), секунд',{type:'number',value:project?.max_seconds||3600})+field('max_tree_hours','Потолок времени дерева задач, часов (продление идёт само, пока есть продвижение)',{type:'number',value:Math.round((project?.max_tree_seconds||43200)/3600)})+field('max_corrections','Циклов исправления после первой попытки',{type:'number',value:project?.max_corrections??3})+field('executor_image','Образ исполнителя с зависимостями проекта',{value:project?.executor_image||'',required:false,placeholder:'По умолчанию darkfactory-executor:local'})+field('merge_without_checks','Без обязательных проверок сливать в preprod после одобренного ревью',{type:'select',value:String(project?.merge_without_checks!==false),choices:boolChoices})+field('director_autonomy','Директор решает остановки вместо тебя',{type:'select',value:String(project?.director_autonomy!==false),choices:boolChoices})+field('director_limit','Решений директора на дерево задач',{type:'number',value:project?.director_limit||6})+(project?field('review_muted','Замечания ревью, отмеченные «Не полезно» (по одному на строке; удали строку, чтобы вернуть)',{type:'textarea',required:false,value:(project.review_muted||[]).join('\n')}):'')+field('backlog_hours','Переводить бэклог в задачи каждые N часов (0 — только по кнопке)',{type:'number',value:project?.backlog_hours??4})+field('package_registries','Открыть публичные реестры пакетов (npm, PyPI, NuGet, Maven, Go, crates)',{type:'select',value:String(project?.package_registries!==false),choices:boolChoices})+field('allowed_hosts','Разрешённые тестовые HTTPS-адреса, host:port по одному на строке',{type:'textarea',required:false,value:project?.allowed_hosts?.join('\n')||''})+field('ssh_private_key','SSH private key (необязательно)',{type:'textarea',required:false})+field('ssh_known_hosts','SSH known_hosts, проверенный отпечаток сервера',{type:'textarea',required:false})+field('allow_paid_fallback','Разрешить платный API при недоступности подписок (только в пределах бюджета задачи)',{type:'select',value:String(project?.allow_paid_fallback===true),choices:boolChoices})+field('test_env','Тестовые переменные, JSON; пусто — сохранить',{type:'textarea',required:false,placeholder:project?.test_env_keys?.length?'Настроены: '+project.test_env_keys.join(', '):'{"TEST_API_TOKEN":"…"}'});
  if(!project)fields+=field('initialize','Первичная индексация и Markdown-документы',{type:'select',value:'true',choices:boolChoices})+field('prepare_environment','Агент подготовит окружение: toolchain, зависимости, проверки',{type:'select',value:'true',choices:boolChoices});
  modal(project?'Настройки проекта':'Новый проект',fields,async values=>{if(!project){values.initialize=values.initialize==='true';values.prepare_environment=values.prepare_environment==='true';}values.allow_paid_fallback=values.allow_paid_fallback==='true';values.package_registries=values.package_registries==='true';values.merge_without_checks=values.merge_without_checks==='true';values.director_autonomy=values.director_autonomy==='true';values.director_limit=Math.max(1,Math.min(20,Number(values.director_limit)||6));if(typeof values.review_muted==='string')values.review_muted=values.review_muted.split('\n').map(v=>v.trim()).filter(Boolean).slice(-30);values.backlog_hours=Math.max(0,Math.min(168,Number(values.backlog_hours)||0));values.max_seconds=Number(values.max_seconds);values.max_tree_seconds=Math.round(Number(values.max_tree_hours)*3600);delete values.max_tree_hours;values.max_corrections=Number(values.max_corrections);values.allowed_hosts=values.allowed_hosts.split('\n').map(v=>v.trim()).filter(Boolean);if(values.test_env)values.test_env=JSON.parse(values.test_env);else delete values.test_env;values.executor_image=values.executor_image.trim();values.checks=values.checks.split('\n').map(v=>v.trim()).filter(Boolean);values.writer_pool||=null;values.reviewer_pool||=null;values.forge_url||=null;values.forge_repo||=null;const result=await api('/projects'+(project?'/'+project.id:''),project?'PUT':'POST',values);projectId=result.id;toast(!project&&(values.initialize||values.prepare_environment)?'Проект сохранён. Индексация и подготовка окружения в очереди.':'Проект сохранён');});
}
$('#add-project').onclick=$('#empty-add').onclick=()=>projectForm();
async function taskForm(incident=false) {
  const artifactChoices=await acceptedArtifactChoices(projectId);
  const agentConfig=await api(`/projects/${projectId}/agents`);
  const profileChoices=[["","Настройки проекта"],...agentConfig.profiles.map(p=>[p.name,p.name])];
  const choices=[['','Настройки проекта'],...providers];
  modal(incident?'Поломка preprod':'Новая задача',
    field('title','Что нужно сделать?')+
    field('priority','Приоритет',{type:'select',choices:taskPriorities,value:'normal'})+
    (incident?'':field('scope','Объём',{type:'select',choices:[['','Обычная задача — сразу в разработку'],['plan','Большая задача — сначала план и подзадачи']],required:false}))+
    field('request','Описание и критерии приёмки',{type:'textarea',placeholder:'Что происходит сейчас? Как должно работать? Как проверить результат?'})+attachField()+
    (artifactChoices.length?field('artifact_version_ids','Входные артефакты — принятые версии',{type:'multiselect',choices:artifactChoices,value:[],required:false}):'')+
    field('execution','Режим выполнения',{type:'select',choices:[['subscriptions','Подписки и правила проекта'],['api','Платный API для всех ролей']]})+
    field('writer','Разработчик этой задачи',{type:'select',choices,required:false})+
    field('reviewer','Ревьюер этой задачи',{type:'select',choices,required:false})+
    taskStages().map(([key,label])=>field('profile_'+key,label+' — профиль',{type:'select',choices:profileChoices,required:false})).join('')+
    field('api_budget_usd','Предел расходов API, USD',{type:'number',value:'0',min:0,max:1000,step:'any'}),
    async values=>{
      values.artifact_version_ids=Array.from($('#modal-fields [name="artifact_version_ids"]')?.selectedOptions||[],option=>option.value);
      values.agent_profiles=Object.fromEntries(taskStages().filter(([key])=>values['profile_'+key]).map(([key])=>[key,values['profile_'+key]]));
      taskStages().forEach(([key])=>delete values['profile_'+key]);
      values.explicit_api=values.execution==='api';delete values.execution;values.plan=values.scope==='plan';delete values.scope;
      values.api_budget_usd=Number(values.api_budget_usd);
      for(const key of ['writer','reviewer']) if(!values[key]) delete values[key];
      const project=projects.find(p=>p.id===projectId);
      if((values.explicit_api||(values.writer||project?.writer)==='api'||(values.reviewer||project?.reviewer)==='api')&&values.api_budget_usd<=0) throw new Error('Для назначения API нужен положительный бюджет.');
      values.attachments=await takeAttachments();
      return api(`/projects/${projectId}/${incident?'incidents':'tasks'}`,'POST',values);
    });
}
$('#new-task').onclick=()=>taskForm();
$('#new-incident').onclick=()=>taskForm(true);
function poolForm(pool) {
  const fields=field('name','Название пула',{value:pool?.name||'',placeholder:'Основные подписки'})+
    field('cross_runtime','Автоматическая замена Codex ↔ Claude',{type:'select',choices:boolChoices,value:String(pool?.cross_runtime??true)})+
    field('allowed_providers','Разрешённые исполнители',{type:'multiselect',choices:providers,value:pool?.allowed_providers||['codex','claude','api']})+
    field('allowed_models','Разрешённые модели, по одной на строке; пусто — все',{type:'textarea',required:false,value:(pool?.allowed_models||[]).join('\n')})+
    field('max_active','Одновременных запусков в пуле',{type:'number',value:pool?.max_active??2,min:1,max:100})+
    field('max_switches','Предел переключений подключения',{type:'number',value:pool?.max_switches??8,min:1,max:100})+
    field('retry_seconds','Повторная проверка квоты через, секунд',{type:'number',value:pool?.retry_seconds??300,min:30,max:86400})+
    field('min_remaining_percent','Запас квоты перед запуском, %',{type:'number',value:pool?.min_remaining_percent??10,min:0,max:100})+
    field('quota_max_age_seconds','Свежесть данных квоты, секунд',{type:'number',value:pool?.quota_max_age_seconds??300,min:30,max:86400})+
    field('require_known_quota','Ждать, если доступная квота неизвестна',{type:'select',choices:boolChoices,value:String(pool?.require_known_quota??false)})+
    field('sessions_per_connection','Параллельных задач на одну подписку',{type:'number',value:pool?.sessions_per_connection??3,min:1,max:10})+
    field('light_codex','Дешёвая модель Codex для лёгких подзадач; пусто — модель подключения',{required:false,value:pool?.light_models?.codex||''})+
    field('light_claude','Дешёвая модель Claude для лёгких подзадач; пусто — haiku',{required:false,value:pool?.light_models?.claude||''});
  modal(pool?'Политика пула':'Новый пул',fields,async values=>{
    values.cross_runtime=values.cross_runtime==='true';values.require_known_quota=values.require_known_quota==='true';
    values.allowed_models=values.allowed_models.split('\n').map(v=>v.trim()).filter(Boolean);
    for(const key of ['max_active','max_switches','retry_seconds','min_remaining_percent','quota_max_age_seconds','sessions_per_connection']) values[key]=Number(values[key]);
    values.light_models={codex:values.light_codex,claude:values.light_claude};delete values.light_codex;delete values.light_claude;
    await api('/pools'+(pool?'/'+pool.id:''),pool?'PUT':'POST',values);
    toast('Пул сохранён');
  });
}
$('#add-pool').onclick=()=>poolForm();
function deletePool(pool) {
  const linked=projects.filter(p=>p.writer_pool===pool.id||p.reviewer_pool===pool.id);
  modal('Удалить пул «'+pool.name+'»?',`<p>Подписки и их авторизация сохранятся. Удалится только пул и его связи с подписками.</p>${linked.length?`<p>Сначала выбери другой пул в проектах: ${linked.map(p=>esc(p.name)).join(', ')}.</p>`:''}`,async()=>{
    await api('/pools/'+pool.id,'DELETE');
    toast('Пул удалён');
  });
  $('#submit-modal').textContent='Удалить пул';
  $('#submit-modal').disabled=linked.length>0;
}
const credentialsPlaceholder='Claude: {"oauth_token":"…"}\nCodex: {"auth_method":"siwc","accessToken":"…"}\nAPI: {"api_key":"…","base_url":"https://…/v1","input_usd_per_million":0,"output_usd_per_million":0}';
function connectionForm(connection) {
  const creating=!connection;
  let fields=field('name','Название',{value:connection?.name||''});
  if(creating) fields+=field('provider','Исполнитель',{type:'select',choices:providers})+field('account_key','Постоянный ID аккаунта у провайдера');
  fields+=field('pool_ids','Пулы подключения',{type:'multiselect',choices:poolList.map(p=>[p.id,p.name]),value:connection?.pool_ids||(poolList[0]?[poolList[0].id]:[])})+
    field('priority','Приоритет: меньшее число выбирается раньше',{type:'number',value:connection?.priority??100})+
    field('model','Модель (для API обязательна)',{value:connection?.model||'',required:false});
  if(creating) fields+=field('credentials','Реквизиты подключения, JSON',{type:'textarea',placeholder:credentialsPlaceholder});
  else fields+=field('enabled','Подключение включено',{type:'select',choices:boolChoices,value:String(connection.status!=='disabled')});
  modal(creating?'Подключить исполнителя':'Настройки подключения',fields,async values=>{
    if((connection?.provider||values.provider)==='api'&&!values.model.trim()) throw new Error('Для API укажи модель.');
    values.model=values.model.trim()||null;values.priority=Number(values.priority);
    if(creating) values.credentials=JSON.parse(values.credentials);
    else values.enabled=values.enabled==='true';
    await api('/connections'+(connection?'/'+connection.id:''),connection?'PUT':'POST',values);
    toast('Подключение сохранено');
  });
}
function reconnectForm(connection) {
  modal('Обновить авторизацию: '+connection.name,
    '<p>Введи новые реквизиты. Сохранённые секреты здесь не отображаются.</p>'+
    field('credentials','Новые реквизиты, JSON',{type:'textarea',placeholder:credentialsPlaceholder}),
    async values=>{await api('/connections/'+connection.id+'/credentials','PUT',JSON.parse(values.credentials));toast('Авторизация обновлена');});
}
$('#add-connection').onclick=()=>connectionForm();
function memberForm(member) {
  const allowedScope=currentUser.project_ids.length?currentUser.project_ids:currentUser.token_projects;
  const defaultProjects=member?.project_ids||(allowedScope.length?projects.map(p=>p.id):[]);
  const fields=field('name','Имя',{value:member?.name||''})+
    field('role','Роль',{type:'select',choices:roles,value:member?.role||'observer'})+
    field('project_ids','Разрешённые проекты; пусто — все',{type:'multiselect',choices:projects.map(p=>[p.id,p.name]),value:defaultProjects,required:false})+
    (member?field('enabled','Доступ включён',{type:'select',choices:boolChoices,value:String(member.enabled)}):'');
  modal(member?'Доступ участника':'Добавить участника',fields,async values=>{
    if(member) values.enabled=values.enabled==='true';
    const result=await api('/members'+(member?'/'+member.id:''),member?'PUT':'POST',values);
    if(!member) showToken('Токен участника',result.token);
    else if(member.id===currentUser.id) await login();
    else toast('Доступ участника сохранён');
  });
}
$('#add-member').onclick=()=>memberForm();
function organizationForm() {
  modal('Настройки организации',field('name','Название',{value:currentUser.organization.name})+
    field('max_active','Одновременных задач организации',{type:'number',value:currentUser.organization.max_active??2,min:1,max:100}),
    async values=>{values.max_active=Number(values.max_active);currentUser.organization=await api('/organization','PUT',values);$('#org-name').textContent=currentUser.organization.name;toast('Организация сохранена');});
}
function quotaSummary(connection) {
  const quota=connection.quota_details||{};
  const parts=[Number.isFinite(quota.remaining_percent)?`остаток квоты: ${quota.remaining_percent}%`:'остаток квоты неизвестен'];
  if(Number.isFinite(quota.reset_at)) parts.push('сброс: '+new Date(quota.reset_at*1000).toLocaleString('ru-RU'));
  if(Number.isFinite(connection.quota_observed)) parts.push('данные от '+new Date(connection.quota_observed*1000).toLocaleString('ru-RU'));
  return parts.join(' · ');
}
async function settings() {
  const owner=isOwner(),orgOwner=isOrganizationOwner();
  const organization=orgOwner?`<div class="setting-row"><strong>${esc(currentUser.organization.name)}</strong><small>Одновременных задач: ${currentUser.organization.max_active??2}</small><div class="setting-actions"><button class="quiet" id="edit-organization">Настроить организацию →</button></div></div>`:'';
  const poolRows=organization+(poolList.length?poolList.map(p=>`<div class="setting-row"><strong>${esc(p.name)}</strong><small>${p.cross_runtime?'Codex ↔ Claude разрешён':'Один исполнитель'} · до ${p.max_active??2} запусков · ${esc((p.allowed_providers||['codex','claude','api']).join(', '))}</small>${orgOwner?`<div class="setting-actions"><button class="quiet" data-edit-pool="${p.id}">Настроить пул →</button><button class="quiet" data-delete-pool="${p.id}">Удалить пул</button></div>`:''}</div>`).join(''):'<p>Создай пул и подключи аккаунты исполнителей.</p>');
  if(owner) {
    const [connections,members]=await Promise.all([api('/connections'),api('/members')]);
    $('#connections').innerHTML=connections.map(c=>`<div class="setting-row"><strong>${esc(c.name)} · ${esc(c.provider)}</strong><small>${esc(c.status)} · ${esc(quotaSummary(c))} · источник: ${esc(c.quota_source)} · приоритет ${c.priority}${c.lease_task?' · занят':''}${c.retry_at?' · повтор '+esc(new Date(c.retry_at*1000).toLocaleString('ru-RU')):''}</small>${orgOwner?`<div class="setting-actions"><button class="quiet" data-edit-connection="${c.id}" ${c.lease_task?'disabled':''}>Настроить</button>${c.status==='quota'?`<button class="quiet" data-recheck="${c.id}">Лимит сброшен — проверить сейчас</button>`:''}<button class="quiet" data-reconnect="${c.id}" ${c.lease_task?'disabled':''}>Обновить авторизацию</button></div>`:''}</div>`).join('')||'<p>Подключений пока нет. Задачи будут ждать пул.</p>';
    const manageable=m=>[currentUser.project_ids,currentUser.token_projects].every(scope=>!scope.length||(m.project_ids.length&&m.project_ids.every(id=>scope.includes(id))));
    $('#members').innerHTML=members.map(m=>`<div class="setting-row"><strong>${esc(m.name)}</strong><small>${esc(m.role)} · ${m.enabled?'активен':'отключён'} · ${m.project_ids.length?'проектов: '+m.project_ids.length:'все проекты'}</small>${manageable(m)?`<div class="setting-actions"><button class="quiet" data-edit-member="${m.id}">Изменить доступ →</button>${m.id!==currentUser.id?`<button class="quiet" data-reissue-member="${m.id}">Перевыпустить токен</button>`:''}</div>`:''}</div>`).join('');
    document.querySelectorAll('[data-edit-connection]').forEach(b=>b.onclick=()=>connectionForm(connections.find(c=>c.id===b.dataset.editConnection)));
    document.querySelectorAll('[data-reconnect]').forEach(b=>b.onclick=()=>reconnectForm(connections.find(c=>c.id===b.dataset.reconnect)));
    document.querySelectorAll('[data-recheck]').forEach(b=>b.onclick=async()=>{await api('/connections/'+b.dataset.recheck+'/recheck','POST');toast('Квота будет проверена при следующем запуске');settings();});
    document.querySelectorAll('[data-edit-member]').forEach(b=>b.onclick=()=>memberForm(members.find(m=>m.id===b.dataset.editMember)));
    document.querySelectorAll('[data-reissue-member]').forEach(b=>b.onclick=async()=>{const m=members.find(m=>m.id===b.dataset.reissueMember);if(!confirm(`Прежние токены ${m.name} перестанут работать. Продолжить?`))return;const result=await api('/members/'+m.id+'/token','POST');showToken('Новый токен '+m.name,result.token);});
  } else {
    $('#connections').innerHTML='<p>Подключениями управляет владелец организации.</p>';
    $('#members').innerHTML='<p>Доступом участников управляет владелец.</p>';
  }
  $('#pools').innerHTML=poolRows+projects.map(p=>`<div class="setting-row"><strong>${esc(p.name)}</strong><small>${esc(p.remote)}</small>${environmentLine(p)}<small class="project-health">preprod: ${esc(p.incident_id?'идёт исправление':({healthy:'проверена',broken:'проверки не прошли'}[p.health?.state]||'ожидает проверки'))}</small>${owner?`<div class="setting-actions"><button class="quiet" data-agents="${p.id}">Роли, скиллы и MCP</button><button class="quiet" data-environment="${p.id}">Подготовить окружение</button><button class="quiet" data-edit="${p.id}">Настроить проект →</button></div>`:''}</div>`).join('');
  if($('#edit-organization')) $('#edit-organization').onclick=organizationForm;
  document.querySelectorAll('[data-edit-pool]').forEach(b=>b.onclick=()=>poolForm(poolList.find(p=>p.id===b.dataset.editPool)));
  document.querySelectorAll('[data-delete-pool]').forEach(b=>b.onclick=()=>deletePool(poolList.find(p=>p.id===b.dataset.deletePool)));
  document.querySelectorAll('[data-agents]').forEach(b=>b.onclick=()=>agentSettings(projects.find(p=>p.id===b.dataset.agents)).catch(e=>toast(e.message)));
  document.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>projectForm(projects.find(p=>p.id===b.dataset.edit)));
  document.querySelectorAll('[data-environment]').forEach(b=>b.onclick=async()=>{const task=await api('/projects/'+b.dataset.environment+'/environment','POST');toast('Агент готовит окружение: '+task.title);});
  await Promise.all([tokenSettings(),subscriptionSettings(),notificationSettings()]);
}
const relationNames={depends_on:['Зависит от','Блокирует'],related_to:['Связана с','Связана с'],duplicate:['Дубликат задачи','Дублируется задачей'],parent:['Подзадача','Родительская задача']};
const eventNames={created:'Создана',later_notes:'Заметки на потом',deferred:'Ждёт свободные файлы',switch:'Смена подключения',implementation_summary:'Итог разработчика',review_incomplete:'Ревью не проверило все файлы',folded:'Директор передал цель ожидающим задачам',backlog:'Создана из бэклога',dependencies_changed:'Входы обновлены',dependencies_changed_after_merge:'Входы изменились при слиянии',dependencies_interrupted:'Входы изменились во время работы',dependency_dropped:'Снята отменённая зависимость',target_changed:'Preprod сдвинулся',later_dropped:'Заметки не ушли в бэклог',environment_retry:'Окружение пересобирается',health_retry:'Повтор проверки preprod',health_environment:'Проверка preprod упала на окружении',executor_session:'Сессия исполнителя',budget_reserved:'Бюджет API зарезервирован',decomposed:'Подзадачи сохранены',chat_command:'Команда из чата',priority_changed:'Приоритет изменён',accepted:'Слита в preprod',promotion_merged:'Влито в main',promotion_rejected:'Перенос в main отклонён',review_repeated:'Повтор замечаний ревью',environment_requested:'Запрошено исправление окружения',supervisor:'Решение директора',supervisor_escalated:'Директор просит решения',toolchain_ready:'Окружение собрано',toolchain_failed:'Окружение не собралось',checks_adopted:'Проверки проекта назначены',environment_blocked:'Среде агента не хватает',phase_started:'Этап начат',phase_finished:'Этап завершён',correction:'Ревью вернуло на доработку',clarification:'Вопрос владельцу',split_requested:'Разбита на подзадачи',human_message:'Сообщение',agent_hint:'Подсказка агенту',attachments:'Вложения',relationship:'Связь добавлена',relationship_removed:'Связь убрана',delegated:'Созданы подзадачи',delegated_cancelled:'Подзадачи отменены',merge:'Слияние',recovered:'Восстановлена после перезапуска',execution_error:'Ошибка исполнителя',stopped:'Остановлена',limit:'Исчерпан лимит',time_slice:'Пауза по кванту времени',quota_observed:'Квота подписки',stalled:'Агент завис',activity_resumed:'Агент снова активен',budget_warning:'Задача долгая',token_warning:'Много токенов',time_extended:'Время продлено',stall_restart:'Перезапуск после зависания',review_required:'Нужно слияние человеком',egress_denied:'Сеть отклонила запрос',network_blocked:'Агенту закрыта сеть',egress_allowed:'Разрешены адреса',checks_changed:'Изменены проверки',agent_settings_changed:'Изменены роли',artifact_input:'Входной артефакт',integration_preempted:'Интеграция перенесена',incident_detected:'Инцидент',incident_reopened:'Инцидент повторился',health_check:'Проверка здоровья',health_network:'Сетевой сбой проверки',remote_unreachable:'Git-сервер недоступен',tidied:'Убран вывод прогонов',checks_passed:'Проверки пройдены в сессии',network_unstable:'Сеть нестабильна',disk_low:'Мало места на диске',resume:'Продолжена владельцем',cancel:'Отменена'};
const eventTone=kind=>['execution_error','review_repeated','stalled','network_blocked','environment_blocked','limit','stopped','incident_detected','incident_reopened'].includes(kind)?'bad':['correction','clarification','review_required','egress_denied','stall_restart'].includes(kind)?'warn':['merge','phase_finished','egress_allowed','activity_resumed'].includes(kind)?'good':'';
const duration=seconds=>seconds<60?Math.round(seconds)+' с':seconds<3600?Math.round(seconds/60)+' мин':(seconds/3600).toFixed(1)+' ч';
function relationRow(r){return `<div class="relation" data-open-task="${r.task_id}" role="button" tabindex="0"><i class="subtask-dot state-${esc(r.state)}"></i>${r.kind==='parent'?'':`<span class="relation-kind">${esc(relationNames[r.kind]?.[r.outgoing?0:1]||r.kind)}</span>`}<span class="relation-title">${esc(r.title)}</span><span class="relation-id">DF-${r.task_id.slice(0,6).toUpperCase()}</span><span class="chip state state-${esc(r.state)}">${esc(names[r.state]||r.state)}</span><button class="quiet relation-remove" data-remove-link="${r.id}" data-owner="${r.owner_id}" title="Убрать связь" aria-label="Убрать связь">×</button></div>`;}
function eventRow(e){return `<div class="event ${eventTone(e.kind)}"><small>${new Date(e.at*1000).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})} · ${esc(eventNames[e.kind]||e.kind)}${e.repeat?` ×${e.repeat} · ${duration(e.at-e.since)}`:''}</small>${e.message&&e.message!==e.kind?`<p>${esc((e.message.length>600?e.message.slice(0,600)+'…':e.message).replace(/\b[0-9a-f]{32}\b/g,id=>'DF-'+id.slice(0,6).toUpperCase()).replace(/^(parent|depends_on|related_to|duplicate): /,(m,k)=>(relationNames[k]?.[0]||k)+': '))}</p>`:''}${e.kind==='later_notes'&&e.data?.items?.length?`<ul class="later-notes">${e.data.items.map(text=>`<li><p>${esc(text)}</p>${typeof noteActions==='function'?noteActions(text):''}</li>`).join('')}</ul>`:''}</div>`;}
async function showDetail(id) {
  detailId=id;route(id);const t=await api('/tasks/'+id);$('#detail-title').textContent=t.title;
  const rel=t.relations||[],children=rel.filter(r=>r.kind==='parent'&&r.outgoing),parent=rel.find(r=>r.kind==='parent'&&!r.outgoing),other=rel.filter(r=>r.kind!=='parent');
  const done=children.filter(c=>c.state==='merged').length,files=['plan.md','diff.patch','checks.txt','review.txt','handoff.md',...(t.role_config?.initialization?['repository-index.md']:[])];
  const history=compactHistory(t.events.slice().reverse(),historyAll),shown=history.slice(0,12),total=t.event_count??history.length;
  $('#detail-body').innerHTML=`${parent?`<button class="detail-parent" data-open-task="${parent.task_id}">↖ Подзадача для <strong>${esc(parent.title)}</strong></button>`:''}<div class="detail-meta"><span class="chip">DF-${t.id.slice(0,6).toUpperCase()}</span><span class="chip state state-${esc(t.state)}">${esc(stateLabel(t))}</span><span class="chip">${esc(phases[t.phase]||t.phase)}</span><span class="chip">${duration(t.active_seconds)} · ${t.corrections} исправл.</span><span class="chip task-priority-${esc(t.priority||'normal')}">${esc(priorityLabel(t.priority))}</span>${t.usage&&window.usageLabel?`<span class="chip" title="Сумма по всем запускам; для подписок — эквивалент цены API">${esc(usageLabel(t.usage))}</span>`:''}${activityLabel(t)?`<span class="chip card-activity${activityLabel(t).stalled?' stalled':''}">${esc(activityLabel(t).text)}</span>`:''}</div><div class="detail-now">${esc(nowLine(t))}</div>${blockedHosts(t).length?`<div class="network-blocked"><p>Агент упёрся в закрытую сеть: ${blockedHosts(t).map(h=>`<code>${esc(h)}</code>`).join(', ')}. Ещё один цикл без доступа ничего не исправит.</p><button id="allow-egress">Разрешить и продолжить</button></div>`:''}<div class="detail-toolbar">${t.state==='review_required'&&t.candidate_sha&&t.reviewed_sha===t.candidate_sha?'<button id="merge-task">Слить в preprod</button>':''}<button id="resume-task">Продолжить / уточнить</button><button id="open-transcript" class="${t.state==='running'?'live':'secondary'}">${t.state==='running'?'● Что агент делает сейчас':'Чат агента'}</button><button id="open-evidence" class="secondary">Браузерные проверки</button><span class="toolbar-gap"></span><button id="change-priority" class="quiet">Приоритет</button><button id="cancel-task" class="quiet danger">Отменить</button></div><div class="detail-grid"><div class="detail-section"><h3>Задача</h3><div class="detail-request">${esc(t.request)}</div></div><div class="detail-facts">${t.branch?`<div><small>Ветка агента</small><code>${esc(t.branch)}</code></div>`:''}${safeHref(t.change_url)?`<div><small>Запрос на слияние</small><a href="${esc(t.change_url)}" target="_blank" rel="noopener">PR / MR ↗</a></div>`:''}${t.result_sha?`<div><small>Принятый коммит</small>${safeHref(t.commit_url)||/^\/api\/tasks\/[0-9a-f]+$/.test(t.commit_url||'')?`<a href="${esc(t.commit_url)}" target="_blank" rel="noopener"><code>${esc(t.result_sha.slice(0,12))}</code></a>`:`<code>${esc(t.result_sha.slice(0,12))}</code>`}</div>`:''}<div><small>Файлы задачи</small><div class="file-chips">${files.map(name=>`<button class="file-chip" data-artifact="${name}">${name}</button>`).join('')}</div></div>${t.attachments?.length?`<div><small>Вложения</small><div class="file-chips">${t.attachments.map(f=>`<button class="file-chip" data-attachment="${esc(f.name)}">📎 ${esc(f.name)}</button>`).join('')}</div></div>`:''}</div></div>${children.length?`<div class="detail-section"><div class="section-head"><h3>Подзадачи</h3><span class="subtasks-progress"><i style="width:${Math.round(done/children.length*100)}%"></i></span><small>${done} из ${children.length} слито</small></div><div class="relation-list">${children.map(relationRow).join('')}</div></div>`:''}<div class="detail-section"><div class="section-head"><h3>Связи</h3><button id="link-task" class="quiet">+ Связать</button></div>${other.length?`<div class="relation-list">${other.map(relationRow).join('')}</div>`:'<p class="muted small">Нет зависимостей и связанных задач.</p>'}</div><div id="detail-extra"></div><div class="detail-section"><div class="section-head"><h3>История</h3><small>${total} событий</small><button id="history-mode" class="quiet">${historyAll?'Только важное':'Все события'}</button></div><div class="timeline">${shown.map(eventRow).join('')}</div>${total>shown.length?`<button id="more-history" class="quiet">Показать всю историю</button>`:''}</div>`;
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
  $('#change-priority').onclick=()=>modal('Приоритет задачи',field('priority','Приоритет',{type:'select',choices:taskPriorities,value:t.priority||'normal'}),async values=>{await api('/tasks/'+id+'/priority','PUT',values);await showDetail(id);});
  if($('#merge-task')){$('#merge-task').disabled=readonly;$('#merge-task').onclick=async()=>{if(!confirm('Слить проверенные ревьюером изменения в preprod?'))return;try{await api('/tasks/'+id+'/actions','POST',{action:'merge'});await showDetail(id);await load();}catch(e){toast(e.message);}};}$('#cancel-task').onclick=async()=>{try{await api('/tasks/'+id+'/actions','POST',{action:'cancel'});await showDetail(id);await load();}catch(e){toast(e.message);}};
  $('#resume-task').onclick=()=>{modal('Продолжить задачу',field('message','Уточнение для исполнителя',{type:'textarea',required:false})+attachField()+field('extend_seconds','Добавить активного времени, секунд',{type:'number',value:'0'})+field('extra_corrections','Добавить циклов исправления',{type:'number',value:'0'})+field('extra_api_budget_usd','Добавить бюджет API, USD',{type:'number',value:'0'}),async values=>{await api('/tasks/'+id+'/actions','POST',{action:'resume',attachments:await takeAttachments(),...values,extend_seconds:Number(values.extend_seconds),extra_corrections:Number(values.extra_corrections),extra_api_budget_usd:Number(values.extra_api_budget_usd)});await showDetail(id);});};
  $('#link-task').onclick=()=>modal('Связать задачи',field('other_id','ID второй задачи')+field('kind','Связь',{type:'select',choices:[['depends_on','Зависит от результата второй задачи'],['parent','Вторая задача — обязательная подзадача'],['related_to','Связаны по смыслу'],['duplicate','Текущая задача — дубликат второй']]}),async values=>{await api('/tasks/'+id+'/links','POST',values);await showDetail(id);});
  document.querySelectorAll('[data-remove-link]').forEach(b=>{b.disabled=readonly;b.onclick=async event=>{event.stopPropagation();if(!confirm('Убрать связь?'))return;try{await api(`/tasks/${b.dataset.owner}/links/${b.dataset.removeLink}`,'DELETE');await showDetail(id);}catch(e){toast(e.message);}};});
  const allow=$('#allow-egress');if(allow) allow.onclick=async()=>{const hosts=blockedHosts(t);if(!confirm('Разрешить проекту доступ к:\n'+hosts.join('\n')+'\n\nЗадача продолжится с дополнительным циклом исправления.'))return;try{await api('/tasks/'+id+'/allow-egress','POST',{hosts});await api('/tasks/'+id+'/actions','POST',{action:'resume',extra_corrections:1,message:'Сеть открыта для: '+hosts.join(', ')+'. Повтори сборку и проверки.'});toast('Адреса разрешены, задача продолжена');showDetail(id);}catch(e){toast(e.message);}};
  document.querySelectorAll('[data-attachment]').forEach(b=>b.onclick=async()=>{try{const response=await fetch(`/api/tasks/${id}/attachments/${encodeURIComponent(b.dataset.attachment)}`,{headers:{Authorization:'Bearer '+token}});if(!response.ok) throw Error('Файл не найден');const url=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=url;if(!(response.headers.get('content-type')||'').startsWith('image/')) link.download=b.dataset.attachment;else link.target='_blank';link.click();setTimeout(()=>URL.revokeObjectURL(url),60000);}catch(e){toast(e.message);}});
  document.querySelectorAll('[data-artifact]').forEach(b=>b.onclick=async()=>{try{const response=await fetch(`/api/tasks/${id}/artifacts/${b.dataset.artifact}`,{headers:{Authorization:'Bearer '+token}});if(!response.ok) throw Error('Артефакт ещё не создан');const text=await response.text();modal(b.dataset.artifact,`<pre>${esc(text)}</pre>`,async()=>{});}catch(e){toast(e.message);}});
  $('#open-transcript').onclick=()=>showAgentTranscript(id);$('#open-evidence').onclick=()=>showBrowserEvidence(id);
  await renderArtifactInputs(t,readonly);
  renderPromotion(t);
  bindNoteActions($('#detail-body'),t);
  renderReview(t).catch(()=>{});
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
      if(commit!==task.result_sha)throw Error('Этот коммит больше не является принятым результатом задачи');
      const response=await fetch(`/api/tasks/${encodeURIComponent(id)}/artifacts/diff.patch`,{headers:{Authorization:'Bearer '+token}});
      if(!response.ok)throw Error('Diff коммита ещё недоступен');
      modal('Коммит '+commit,`<pre>${esc(await response.text())}</pre>`,async()=>{});
      $('#submit-modal').hidden=true;$('#cancel-modal').textContent='Закрыть';
    }
  } catch(error){toast(error.message);}
}
async function showBrowserEvidence(id) {
  try {
    const data=await api(`/tasks/${id}/evidence`);
    modal('Браузерные проверки',data.files.length?`<p>Скриншоты, трассы и отчёты отдельных запусков. Результат обязательных проверок смотри в checks.txt.</p>${data.files.map((file,index)=>`<div class="setting-row"><span style="overflow-wrap:anywhere">${esc(file.name)} · ${Math.ceil(file.bytes/1024)} КБ</span><button type="button" class="secondary" data-evidence-index="${index}">${/\.(png|jpe?g|webp)$/i.test(file.name)?'Открыть':'Скачать'}</button></div>`).join('')}${data.truncated?'<p>Список ограничен. Показаны первые 200 файлов.</p>':''}`:'<p>Браузер ещё не сохранял результаты для этой задачи.</p>',async()=>{});
    $('#submit-modal').hidden=true;$('#cancel-modal').textContent='Закрыть';
    document.querySelectorAll('[data-evidence-index]').forEach(button=>button.onclick=async()=>{
      try {
        const file=data.files[Number(button.dataset.evidenceIndex)];
        const response=await fetch(`/api/tasks/${id}/evidence/${file.name.split('/').map(encodeURIComponent).join('/')}`,{headers:{Authorization:'Bearer '+token}});
        if(!response.ok)throw Error('Файл проверки недоступен');
        const blob=await response.blob();const url=URL.createObjectURL(blob);
        if(/\.(png|jpe?g|webp)$/i.test(file.name)) {
          modal(file.name,`<img alt="Скриншот браузерной проверки" style="max-width:100%;height:auto" src="${url}">`,async()=>{});
          $('#submit-modal').hidden=true;$('#cancel-modal').textContent='Закрыть';
          $('#modal-fields img').onload=()=>URL.revokeObjectURL(url);
          $('#modal-fields img').onerror=()=>URL.revokeObjectURL(url);
        } else {
          const link=document.createElement('a');link.href=url;link.download=file.name.split('/').pop();link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
        }
      } catch(error){toast(error.message);}
    });
  } catch(error){toast(error.message);}
}
function diskWarning(d,held){const box=$('#disk-warning');if(!box)return;const show=d&&d.paused;box.hidden=!show&&!held;box.classList.toggle('hold-warning',!show&&!!held);if(!show&&held)box.innerHTML='<b>Пауза перед обновлением фабрики</b> · новые задачи не стартуют, начатые шаги доделываются.';if(show)box.innerHTML=`<b>${d.alert?'Диск почти заполнен':'Мало места на диске'}</b> · свободно ${(d.free_bytes/1024**3).toFixed(1)} ГБ (${Math.round(d.free_percent)}%). Новые задачи на паузе, пока не освободится место в Docker.`;}
// A background tab is not polled; it catches up the moment it becomes visible again.
async function poll(){if(!token||$('#shell').hidden||refreshBusy||$('#modal').open||document.hidden)return;refreshBusy=true;try{api('/me').then(me=>diskWarning(me.disk,me.held)).catch(()=>{});await load();if(detailId&&$('#detail').open)await showDetail(detailId);}catch(e){console.warn(e.message);}finally{refreshBusy=false;}}
setInterval(poll,5000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});
if(token)login().catch(e=>toast(e.message));

function showToken(title,value){setTimeout(()=>{modal(title,'<p>Сохрани токен сейчас. Повторно получить его нельзя.</p><textarea readonly>'+esc(value)+'</textarea>',async()=>{});},0);}
$('#add-organization').onclick=()=>modal('Новая организация',field('name','Название'),async values=>{const created=await api('/organizations','POST',values);showToken('Токен владельца '+created.organization.name,created.token);});
async function tokenSettings(){const items=await api('/tokens');$('#tokens').innerHTML='<h3>Твои API-токены</h3>'+items.map(t=>`<div class="setting-row"><small>${esc(t.id)} · ${esc(t.role)} · ${t.project_ids.length?'ограничен проектами':'все доступные проекты'}</small><button class="quiet" data-revoke="${t.id}">Отозвать</button></div>`).join('')+'<button id="issue-token" class="secondary">Создать API-токен</button>';document.querySelectorAll('[data-revoke]').forEach(b=>b.onclick=async()=>{try{await api('/tokens/'+b.dataset.revoke,'DELETE');await settings();}catch(e){toast(e.message);}});$('#issue-token').onclick=()=>modal('API-токен',field('role','Роль',{type:'select',choices:[['observer','Наблюдатель'],['operator','Оператор'],['owner','Владелец']]})+field('project_ids','ID проектов через запятую; пусто — все доступные',{required:false}),async values=>{values.project_ids=values.project_ids.split(',').map(v=>v.trim()).filter(Boolean);const result=await api('/tokens','POST',values);showToken('Новый API-токен',result.token);});}
