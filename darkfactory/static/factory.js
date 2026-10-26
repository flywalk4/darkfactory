'use strict';

// The factory page: how much got done, how often review sent work back, where the time goes, what the
// Director decided and which roles spend the tokens.
let factoryDays=7;
const factoryNumber=value=>value==null?'—':value>=1e6?(value/1e6).toFixed(1)+'M':value>=1e3?Math.round(value/1e3)+'k':String(Math.round(value*10)/10);
const factoryDecisions={resume:tr('Продолжил'),rereview:tr('Вернул на ревью'),allow_hosts:tr('Открыл адреса'),cancel:tr('Отменил'),escalate:tr('Позвал тебя'),fold:tr('Передал цель')};
const factoryPhases={plan:tr('План'),implement:tr('Разработка'),checks:tr('Проверки'),review:tr('Ревью'),integrate:tr('Объединение'),verify:tr('Проверка результата'),merge:tr('Слияние'),publish:tr('Запись'),prepare:tr('Подготовка'),collect:tr('Сбор подзадач')};

async function factoryPage(){
  const host=$('#factory-workspace');if(!projectId){host.innerHTML='<p class="muted">'+tr('Выбери проект.')+'</p>';return;}
  host.innerHTML='<p class="muted">'+tr('Считаю…')+'</p>';
  const m=await api(`/projects/${projectId}/metrics?days=${factoryDays}`);
  const tiles=[[tr('Слито задач'),m.merged,tr('из {tasks} за {days} дн.',{tasks:m.tasks,days:m.days})],[tr('Время задачи (медиана)'),m.task_minutes_median==null?'—':Math.round(m.task_minutes_median)+' '+tr('мин'),tr('работа агентов без ожидания')],[tr('Возвратов ревью на задачу'),m.corrections_per_task==null?'—':m.corrections_per_task.toFixed(2),tr('меньше — лучше')],[tr('Без человека'),m.no_human_share==null?'—':Math.round(m.no_human_share*100)+'%',tr('задач без остановки на тебя')],[tr('Токенов на задачу'),factoryNumber(m.input_per_task),tr('вход, слитые задачи')]];
  const maxPhase=Math.max(1,...m.phases.map(p=>p.sum||0));
  const director=Object.entries(m.director||{}).sort((a,b)=>b[1]-a[1]);
  const profiles=Object.entries(m.profiles||{}).sort((a,b)=>b[1].input-a[1].input);
  host.innerHTML=`<div class="page-title"><div><div class="eyebrow">${tr('ФАБРИКА')}</div><h1>${tr('Как работает фабрика')}</h1><p>${tr('Сводка по проекту за выбранный период. Обновляется раз в минуту.')}</p></div><select id="factory-days">${[1,7,30].map(d=>`<option value="${d}" ${d===factoryDays?'selected':''}>${d===1?tr('Сутки'):d+' '+tr('дней')}</option>`).join('')}</select></div>
  <div class="metrics factory-tiles">${tiles.map(([label,value,note])=>`<div class="metric"><span>${esc(label)}</span><strong>${esc(String(value))}</strong><em>${esc(note)}</em></div>`).join('')}</div>
  <div class="settings-grid">
    <div class="panel"><div class="panel-title"><h2>${tr('Где уходит время')}</h2></div>${m.phases.length?m.phases.map(p=>`<div class="factory-bar"><span>${esc(factoryPhases[p.phase]||p.phase)}</span><i style="width:${Math.round((p.sum||0)/maxPhase*100)}%"></i><small>${tr('{n} мин · медиана {median}',{n:Math.round(p.sum||0),median:p.median==null?'—':p.median.toFixed(1)})}</small></div>`).join(''):'<p>'+tr('Нет данных.')+'</p>'}</div>
    <div class="panel"><div class="panel-title"><h2>${tr('Решения директора')}</h2></div>${director.length?director.map(([k,v])=>`<div class="setting-row"><strong>${esc(factoryDecisions[k]||k)}: ${v}</strong></div>`).join(''):'<p>'+tr('Остановок не было.')+'</p>'}</div>
    <div class="panel"><div class="panel-title"><h2>${tr('Токены по ролям')}</h2></div>${profiles.length?profiles.map(([name,row])=>`<div class="setting-row"><strong>${esc(name)}</strong><small>${tr('{runs} запусков · вход {input} · выход {output}',{runs:row.runs,input:factoryNumber(row.input),output:factoryNumber(row.output)})}</small></div>`).join(''):'<p>'+tr('Нет данных: роль пишется в запуски начиная с этого обновления.')+'</p>'}</div>
    <div class="panel"><div class="panel-title"><h2>${tr('Запуски по этапам')}</h2></div>${Object.entries(m.roles||{}).map(([role,row])=>`<div class="setting-row"><strong>${esc(transcriptRoles?.[role]||role)}</strong><small>${tr('{runs} запусков · вход {input} · медиана {median}',{runs:row.runs,input:factoryNumber(row.input),median:factoryNumber(row.median)})}</small></div>`).join('')||'<p>'+tr('Нет данных.')+'</p>'}</div>
  </div>`;
  $('#factory-days').onchange=event=>{factoryDays=Number(event.target.value);factoryPage().catch(error=>toast(error.message));};
}
