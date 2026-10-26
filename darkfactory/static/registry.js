'use strict';

const artifactKinds=[['requirements','Требования'],['analysis','Аналитика'],['architecture','Архитектура'],['contract','Контракт API'],['report','Отчёт'],['document','Документ']];
const artifactStatuses={draft:'Черновик',accepted:'Принята',stale:'Источник устарел',superseded:'Заменена',revoked:'Отозвана'};
const artifactReadonly=()=>currentUser?.role==='observer'||currentUser?.token_role==='observer';

async function acceptedArtifactChoices(project) {
  const artifacts=await api(`/projects/${project}/artifacts`);
  return artifacts.flatMap(artifact=>artifact.versions.filter(v=>v.status==='accepted').map(v=>[v.id,`${artifact.title} · v${v.number}`]));
}

async function registryPage() {
  const root=$('#artifact-workspace');
  if(!projectId){root.innerHTML='<h1>Артефакты проекта</h1><p>Сначала подключи проект.</p>';return;}
  const project=projectId;
  const artifacts=await api(`/projects/${project}/artifacts`);
  if(project!==projectId)return;
  const opened=new Set(Array.from(root.querySelectorAll('details[open]'),el=>el.dataset.history));
  root.innerHTML=`<div class="page-title"><div><div class="eyebrow">ЗНАНИЯ ПРОЕКТА</div><h1>Артефакты проекта</h1><p>Требования, аналитика и контракты. Каждая задача использует выбранную версию.</p></div><div class="artifact-tools"><button id="initialize-project" class="secondary" ${isOwner()?'':'hidden'}>Индексировать проект</button><button id="new-project-artifact" ${artifactReadonly()?'hidden':''}>+ Артефакт</button></div></div>
    <p class="muted">Новые версии сохраняются как черновики. Владелец принимает версию; замена или отзыв останавливают зависимые задачи.</p>
    <div class="artifact-grid">${artifacts.map(artifact=>`<article class="panel artifact-card">
      <div class="artifact-card-heading"><div><small>${esc(artifactKinds.find(([kind])=>kind===artifact.kind)?.[1]||artifact.kind)} · ${esc(artifact.key)}</small><h2>${esc(artifact.title)}</h2></div><span class="card-tag">${artifact.versions.some(v=>v.status==='accepted')?'Есть принятая версия':artifact.accepted_version_id?'Источник устарел':'Нет принятой версии'}</span></div>
      <div class="detail-actions">${artifactReadonly()?'':`<button class="secondary" data-artifact-revise="${artifact.id}">+ Версия</button>`}${isOwner()&&artifact.accepted_version_id?`<button class="quiet" data-artifact-revoke="${artifact.id}">Отозвать приёмку</button>`:''}</div>
      <details data-history="${artifact.id}" ${opened.has(artifact.id)?'open':''}><summary>История версий · ${artifact.versions.length}</summary>${artifact.versions.map(v=>`<div class="artifact-version"><div><strong>v${v.number}</strong> <span class="card-tag">${esc(artifactStatuses[v.status])}</span><small>${new Date(v.created*1000).toLocaleString('ru-RU')} · ${v.characters} символов</small>${v.source_task_id?`<button class="quiet artifact-source" data-source-task="${v.source_task_id}">Задача-источник ${esc(v.source_task_id.slice(0,6))} ↗</button><code>${esc(v.source_sha)}</code>`:'<small>Добавлено вручную</small>'}${v.reason?`<p class="error">${esc(v.reason)}</p>`:''}</div><div class="artifact-version-actions"><button class="secondary" data-version-read="${v.id}" data-artifact-id="${artifact.id}">Открыть</button>${isOwner()&&v.status!=='accepted'?`<button data-version-accept="${v.id}" data-artifact-id="${artifact.id}">Принять v${v.number}</button>`:''}</div></div>`).join('')}</details>
      <div class="artifact-consumers"><h3>Задачи-потребители · ${artifact.consumers.length}</h3>${artifact.consumers.map(c=>`<button class="artifact-consumer" data-source-task="${c.task_id}"><span>${esc(c.title)}</span><small>v${c.number} · ${c.stale?'Вход устарел':esc(names[c.state]||c.state)}</small></button>`).join('')||'<p class="muted">Пока нет. Выбери версию при создании задачи или в её карточке.</p>'}</div>
    </article>`).join('')||'<div class="empty artifact-empty"><div class="empty-symbol">▤</div><h2>Здесь будут знания проекта</h2><p>Добавь требования, описание архитектуры или контракт API.<br>Прими версию и передай её задаче.</p></div>'}</div>`;
  $('#new-project-artifact').onclick=()=>artifactForm().catch(e=>toast(e.message));
  $('#initialize-project').onclick=async()=>{try{const task=await api(`/projects/${project}/initialize`,'POST',{});await load();await showDetail(task.id);toast('Задача инициализации: индекс и четыре Markdown-документа. Используются подключения и лимиты проекта.');}catch(e){toast(e.message);}};
  root.querySelectorAll('[data-artifact-revise]').forEach(b=>b.onclick=()=>artifactForm(artifacts.find(a=>a.id===b.dataset.artifactRevise)).catch(e=>toast(e.message)));
  root.querySelectorAll('[data-version-read]').forEach(b=>b.onclick=()=>readProjectVersion(b.dataset.artifactId,b.dataset.versionRead).catch(e=>toast(e.message)));
  root.querySelectorAll('[data-source-task]').forEach(b=>b.onclick=()=>showDetail(b.dataset.sourceTask).catch(e=>toast(e.message)));
  root.querySelectorAll('[data-version-accept]').forEach(b=>b.onclick=async()=>{try{await api(`/project-artifacts/${b.dataset.artifactId}/versions/${b.dataset.versionAccept}/accept`,'POST',{});await registryPage();toast('Версия принята');}catch(e){toast(e.message);}});
  root.querySelectorAll('[data-artifact-revoke]').forEach(b=>b.onclick=async()=>{try{await api(`/project-artifacts/${b.dataset.artifactRevoke}/revoke`,'POST',{});await registryPage();toast('Приёмка отозвана');}catch(e){toast(e.message);}});
}

async function artifactForm(artifact) {
  const project=projectId;
  const tasks=await api(`/projects/${project}/tasks?all=1`);
  const sourceChoices=[['','Без задачи — добавить вручную'],...tasks.filter(t=>t.candidate_sha&&t.reviewed_sha===t.candidate_sha&&['merged','review_required'].includes(t.state)).map(t=>[t.id,t.title])];
  const previous=artifact?.versions[0];
  const content=previous?(await api(`/project-artifacts/${artifact.id}/versions/${previous.id}`)).content:'';
  modal(artifact?`Новая версия: ${artifact.title}`:'Новый артефакт проекта',
    (artifact?'':field('key','Ключ',{placeholder:'api-contract'})+field('title','Название',{placeholder:'Контракт API заказов'})+field('kind','Тип',{type:'select',choices:artifactKinds,value:'document'}))+
    field('source_task_id','Задача-источник',{type:'select',choices:sourceChoices,required:false})+
    field('content','Содержимое (Markdown или текст)',{type:'textarea',value:content}),
    async values=>{values.source_task_id||=null;await api(artifact?`/project-artifacts/${artifact.id}/versions`:`/projects/${project}/artifacts`,'POST',values);toast('Черновик сохранён. Теперь можно принять версию.');});
  const contentField=$('#modal-fields [name="content"]');contentField.maxLength=100000;contentField.rows=14;
  const keyField=$('#modal-fields [name="key"]');if(keyField){keyField.pattern='[a-zA-Z0-9](?:[a-zA-Z0-9_.]|-){0,99}';keyField.maxLength=100;}
}

async function readProjectVersion(artifactId,versionId) {
  const version=await api(`/project-artifacts/${artifactId}/versions/${versionId}`);
  modal(`Версия ${version.number} · ${artifactStatuses[version.status]}`,`<p>Версия неизменяема. Изменения сохраняются новой версией.</p><pre class="artifact-content">${esc(version.content)}</pre>`,async()=>{});
  $('#submit-modal').hidden=true;$('#cancel-modal').textContent='Закрыть';
}

async function renderArtifactInputs(task,readonly) {
  const selected=await api(`/tasks/${task.id}/artifact-inputs`);
  if(detailId!==task.id)return;
  const editable=!readonly&&['queued','waiting_pool','waiting_dependency','waiting_integration','needs_human'].includes(task.state);
  const section=document.createElement('section');section.className='detail-section';
  section.innerHTML=`<div class="section-head"><h3>Входные артефакты</h3>${editable?'<button class="quiet" data-bind-artifact>+ Выбрать версию</button>':''}</div>${selected.map(v=>`<div class="artifact-input"><div><strong>${esc(v.title)} · v${v.number}</strong><small>${esc(artifactStatuses[v.status])}</small>${v.blocked_reason?`<p class="error">${esc(v.blocked_reason)}</p>`:''}</div><button class="quiet" data-input-read="${v.id}" data-input-artifact="${v.artifact_id}">Открыть</button>${editable?`<button class="quiet" data-input-remove="${v.artifact_id}">Убрать</button>`:''}</div>`).join('')||'<p class="muted small">Не выбраны. Задача получит содержимое только явно выбранных принятых версий.</p>'}`;
  ($('#detail-extra')||$('#detail-body')).append(section);
  section.querySelectorAll('[data-input-read]').forEach(b=>b.onclick=()=>readProjectVersion(b.dataset.inputArtifact,b.dataset.inputRead).catch(e=>toast(e.message)));
  section.querySelectorAll('[data-input-remove]').forEach(b=>b.onclick=async()=>{try{await api(`/tasks/${task.id}/artifact-inputs/${b.dataset.inputRemove}`,'DELETE');await showDetail(task.id);await load();}catch(e){toast(e.message);}});
  const bind=section.querySelector('[data-bind-artifact]');if(!bind)return;
  bind.onclick=async()=>{try{
    const artifacts=await api(`/projects/${task.project_id}/artifacts`);
    const choices=artifacts.flatMap(a=>a.versions.filter(v=>v.status==='accepted').map(v=>[v.id,`${a.title} · v${v.number}`]));
    if(!choices.length){toast('Сначала прими версию в разделе «Артефакты проекта»');return;}
    modal('Входная версия артефакта',field('version_id','Принятая версия',{type:'select',choices}),async values=>{
      const artifact=artifacts.find(a=>a.versions.some(v=>v.id===values.version_id));
      await api(`/tasks/${task.id}/artifact-inputs/${artifact.id}`,'PUT',values);await showDetail(task.id);
    });
  }catch(e){toast(e.message);}};
}
