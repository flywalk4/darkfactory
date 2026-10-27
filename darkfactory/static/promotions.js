'use strict';

// The branch a promotion carries preprod into: the project's seed branch, main by default.
function seedBranch(id=projectId){return projects?.find(p=>p.id===id)?.seed_branch||'main';}

// Board banner: everything already in preprod that only the owner can carry into the seed branch.
function renderMainPending(taskList) {
  const box=$('#main-pending');if(!box)return;
  const pending=taskList.filter(t=>t.promotion_pending);
  box.hidden=!pending.length;if(!pending.length){box.innerHTML='';return;}
  const branch=seedBranch();
  box.innerHTML=`<div class="main-pending-head"><b>${tr('Ждут переноса в {branch}: {n}',{n:pending.length,branch:esc(branch)})}</b><span>${tr('Уже в preprod. {branch} обновляешь ты — посмотри изменения и влей.',{branch:esc(branch)})}</span></div><div class="main-pending-list">${pending.map(t=>`<button class="main-pending-item" data-promote="${t.id}"><span>${esc(t.title)}</span><small>${new Date(t.updated*1000).toLocaleDateString(LOCALE)} · ${tr('Посмотреть и влить →')}</small></button>`).join('')}</div>`;
  box.querySelectorAll('[data-promote]').forEach(b=>b.onclick=()=>openPromotion(b.dataset.promote).catch(error=>toast(error.message)));
}

function renderPromotion(task) {
  const proposal=task.promotion;
  if(!proposal)return;
  const branch=esc(proposal.target||seedBranch(task.project_id));
  const section=document.createElement('section');section.className='detail-section';
  const state={pending:tr('Ждёт твоего решения'),merged:tr('Влито владельцем'),rejected:tr('Отклонено владельцем'),superseded:tr('Уже в {branch} вместе с более поздним переносом',{branch})}[proposal.state];
  section.innerHTML=`<h3>${tr('Перенос в {branch}',{branch})}</h3><p>${esc(state)}. ${tr('Коммит')} <code>${esc(proposal.source_sha)}</code>.</p><p>${tr('Включает принятые изменения preprod до этого коммита и результаты подзадач. Развёртывание приложения не запускается.')}</p>${proposal.state==='pending'?'<div class="detail-actions"><button data-review-main>'+tr('Посмотреть изменения →')+'</button><button class="secondary" data-reject-main>'+tr('Отклонить перенос')+'</button></div>':''}`;
  if(proposal.state==='pending'){section.classList.add('main-pending-section');($('#detail-body').querySelector('.detail-grid')||$('#detail-body')).before(section);}
  else ($('#detail-extra')||$('#detail-body')).append(section);
  const review=section.querySelector('[data-review-main]');
  if(review)review.onclick=()=>openPromotion(task.id).catch(error=>toast(error.message));
  const reject=section.querySelector('[data-reject-main]');
  if(reject){reject.hidden=!isOwner();reject.onclick=()=>rejectPromotion(task.id,proposal).catch(error=>toast(error.message));}
}

async function rejectPromotion(id,proposal) {
  await api(`/tasks/${id}/promotion`,'POST',{action:'reject',proposal_id:proposal.id,source_sha:proposal.source_sha});
  if($('#modal').open)$('#modal').close();
  await showDetail(id);toast(tr('Перенос в {branch} отклонён. Изменения в preprod сохранены.',{branch:proposal.target||seedBranch()}));
}

// What the owner is about to carry into the seed branch, task by task: risky paths first, with the reviewer's open notes.
function includedTasks(tasks) {
  if(!tasks?.length)return '';
  const risky=tasks.filter(t=>t.risky.length).length,noted=tasks.filter(t=>t.notes.length).length;
  const rows=[...tasks].sort((a,b)=>b.risky.length-a.risky.length||b.notes.length-a.notes.length).map(t=>`<li class="${t.risky.length?'promotion-risky':''}"><a href="?task=${esc(t.task_id)}" data-promotion-task="${esc(t.task_id)}">${esc(t.title)}</a> <small>DF-${esc(t.task_id.slice(0,6).toUpperCase())} · ${tr('файлов: {n}',{n:t.files})}</small>${t.risky.length?`<div><small>${tr('Рискованные пути:')} ${t.risky.map(p=>`<code>${esc(p)}</code>`).join(', ')}</small></div>`:''}${t.notes.length?`<details><summary>${tr('Замечания ревью: {n}',{n:t.notes.length})}</summary><ul>${t.notes.map(n=>`<li>${esc(n)}</li>`).join('')}</ul></details>`:''}</li>`).join('');
  return `<h3>${tr('Задачи в переносе: {n}',{n:tasks.length})}</h3><p class="small">${tr('С рискованными путями (авторизация, секреты, миграции, инфраструктура): {risky}. С открытыми замечаниями ревью: {noted}.',{risky,noted})}</p><ul class="promotion-tasks">${rows}</ul>`;
}

async function openPromotion(id) {
  const proposal=await api(`/tasks/${id}/promotion`);
  const branch=proposal.target||seedBranch();
  if(proposal.state!=='pending'){await showDetail(id);toast(proposal.state==='merged'?tr('Уже влито в {branch}',{branch}):proposal.state==='superseded'?tr('Уже перенесено в {branch} вместе с более поздней задачей',{branch}):tr('Перенос отклонён'));return;}
  const warning=!proposal.can_merge?tr('Изменения конфликтуют с {branch}: {files}. Разреши конфликт в Git или поставь задачу на интеграцию.',{branch,files:(proposal.conflicts||[]).join(', ')}):
    proposal.mode==='merge'?tr('В {branch} есть свои коммиты. Будет создан merge-коммит; ниже — что изменится в {branch}.',{branch}):
    tr('Ревью относится к этому коммиту; {branch} перемотается вперёд.',{branch});
  modal(tr('Слияние в {branch}',{branch:esc(branch)}),`<p>${esc(warning)}</p><p>${tr('Текущая {branch}:',{branch:esc(branch)})} <code>${esc(proposal.main_sha)}</code></p><p>${tr('Принятый коммит:')} <code>${esc(proposal.source_sha)}</code></p><p>${tr('Будут перенесены все изменения preprod до этого коммита, включая подзадачи. Развёртывание не запускается.')}</p>${includedTasks(proposal.tasks)}<details><summary>${tr('Файлы и diff')}</summary><pre>${esc(proposal.diff_stat||tr('Изменений нет'))}</pre><pre>${esc(proposal.diff||tr('{branch} уже содержит этот коммит',{branch}))}</pre>${proposal.truncated?'<p>'+tr('Diff показан частично. Для полного просмотра используй Git.')+'</p>':''}</details>${isOwner()?'<button type="button" id="reject-main-modal" class="secondary">'+tr('Отклонить перенос')+'</button>':''}`,async()=>{
    await api(`/tasks/${id}/promotion`,'POST',{action:'merge',proposal_id:proposal.id,source_sha:proposal.source_sha,main_sha:proposal.main_sha});
    await showDetail(id);toast(tr('Принятый коммит влит в {branch}.',{branch}));
  });
  $('#submit-modal').textContent=proposal.already_merged?tr('Подтвердить: уже в {branch}',{branch}):tr('Влить в {branch}',{branch});
  $('#submit-modal').hidden=!isOwner();$('#submit-modal').disabled=!proposal.can_merge;
  document.querySelectorAll('[data-promotion-task]').forEach(link=>link.onclick=event=>{event.preventDefault();$('#modal').close();showDetail(link.dataset.promotionTask).catch(error=>toast(error.message));});
  if($('#reject-main-modal'))$('#reject-main-modal').onclick=()=>rejectPromotion(id,proposal).catch(error=>$('#form-error').textContent=error.message);
}
