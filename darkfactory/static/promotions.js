'use strict';

// Board banner: everything already in preprod that only the owner can carry into main.
function renderMainPending(taskList) {
  const box=$('#main-pending');if(!box)return;
  const pending=taskList.filter(t=>t.promotion_pending);
  box.hidden=!pending.length;if(!pending.length){box.innerHTML='';return;}
  box.innerHTML=`<div class="main-pending-head"><b>Ждут переноса в main: ${pending.length}</b><span>Уже в preprod. Main обновляешь ты — посмотри изменения и влей.</span></div><div class="main-pending-list">${pending.map(t=>`<button class="main-pending-item" data-promote="${t.id}"><span>${esc(t.title)}</span><small>${new Date(t.updated*1000).toLocaleDateString('ru-RU')} · Посмотреть и влить →</small></button>`).join('')}</div>`;
  box.querySelectorAll('[data-promote]').forEach(b=>b.onclick=()=>openPromotion(b.dataset.promote).catch(error=>toast(error.message)));
}

function renderPromotion(task) {
  const proposal=task.promotion;
  if(!proposal)return;
  const section=document.createElement('section');section.className='detail-section';
  const state={pending:'Ждёт твоего решения',merged:'Влито владельцем',rejected:'Отклонено владельцем',superseded:'Уже в main вместе с более поздним переносом'}[proposal.state];
  section.innerHTML=`<h3>Перенос в main</h3><p>${esc(state)}. Коммит <code>${esc(proposal.source_sha)}</code>.</p><p>Включает принятые изменения preprod до этого коммита и результаты подзадач. Развёртывание приложения не запускается.</p>${proposal.state==='pending'?'<div class="detail-actions"><button data-review-main>Посмотреть изменения →</button><button class="secondary" data-reject-main>Отклонить перенос</button></div>':''}`;
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
  await showDetail(id);toast('Перенос в main отклонён. Изменения в preprod сохранены.');
}

// What the owner is about to carry into main, task by task: risky paths first, with the reviewer's open notes.
function includedTasks(tasks) {
  if(!tasks?.length)return '';
  const risky=tasks.filter(t=>t.risky.length).length,noted=tasks.filter(t=>t.notes.length).length;
  const rows=[...tasks].sort((a,b)=>b.risky.length-a.risky.length||b.notes.length-a.notes.length).map(t=>`<li class="${t.risky.length?'promotion-risky':''}"><a href="?task=${esc(t.task_id)}" data-promotion-task="${esc(t.task_id)}">${esc(t.title)}</a> <small>DF-${esc(t.task_id.slice(0,6).toUpperCase())} · файлов: ${t.files}</small>${t.risky.length?`<div><small>Рискованные пути: ${t.risky.map(p=>`<code>${esc(p)}</code>`).join(', ')}</small></div>`:''}${t.notes.length?`<details><summary>Замечания ревью: ${t.notes.length}</summary><ul>${t.notes.map(n=>`<li>${esc(n)}</li>`).join('')}</ul></details>`:''}</li>`).join('');
  return `<h3>Задачи в переносе: ${tasks.length}</h3><p class="small">С рискованными путями (авторизация, секреты, миграции, инфраструктура): ${risky}. С открытыми замечаниями ревью: ${noted}.</p><ul class="promotion-tasks">${rows}</ul>`;
}

async function openPromotion(id) {
  const proposal=await api(`/tasks/${id}/promotion`);
  if(proposal.state!=='pending'){await showDetail(id);toast(proposal.state==='merged'?'Уже влито в main':proposal.state==='superseded'?'Уже перенесено в main вместе с более поздней задачей':'Перенос отклонён');return;}
  const warning=!proposal.can_merge?'Изменения конфликтуют с main: '+(proposal.conflicts||[]).join(', ')+'. Разреши конфликт в Git или поставь задачу на интеграцию.':
    proposal.mode==='merge'?'В main есть свои коммиты. Будет создан merge-коммит; ниже — что изменится в main.':
    'Ревью относится к этому коммиту; main перемотается вперёд.';
  modal('Слияние в main',`<p>${esc(warning)}</p><p>Текущая main: <code>${esc(proposal.main_sha)}</code></p><p>Принятый коммит: <code>${esc(proposal.source_sha)}</code></p><p>Будут перенесены все изменения preprod до этого коммита, включая подзадачи. Развёртывание не запускается.</p>${includedTasks(proposal.tasks)}<details><summary>Файлы и diff</summary><pre>${esc(proposal.diff_stat||'Изменений нет')}</pre><pre>${esc(proposal.diff||'main уже содержит этот коммит')}</pre>${proposal.truncated?'<p>Diff показан частично. Для полного просмотра используй Git.</p>':''}</details>${isOwner()?'<button type="button" id="reject-main-modal" class="secondary">Отклонить перенос</button>':''}`,async()=>{
    await api(`/tasks/${id}/promotion`,'POST',{action:'merge',proposal_id:proposal.id,source_sha:proposal.source_sha,main_sha:proposal.main_sha});
    await showDetail(id);toast('Принятый коммит влит в main.');
  });
  $('#submit-modal').textContent=proposal.already_merged?'Подтвердить: уже в main':'Влить в main';
  $('#submit-modal').hidden=!isOwner();$('#submit-modal').disabled=!proposal.can_merge;
  document.querySelectorAll('[data-promotion-task]').forEach(link=>link.onclick=event=>{event.preventDefault();$('#modal').close();showDetail(link.dataset.promotionTask).catch(error=>toast(error.message));});
  if($('#reject-main-modal'))$('#reject-main-modal').onclick=()=>rejectPromotion(id,proposal).catch(error=>$('#form-error').textContent=error.message);
}
