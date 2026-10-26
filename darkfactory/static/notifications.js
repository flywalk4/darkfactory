/* Owner-managed destinations. Saving a channel never sends a test message. */
const notificationLabels = {
  clarification:'Нужен ответ на вопрос', limit:'Исчерпан лимит задачи',
  stopped:'Задача остановлена', execution_error:'Ошибка исполнителя', network_blocked:'Агенту закрыта сеть',review_repeated:'Ревью повторяет замечание',environment_blocked:'Среде агента не хватает инструментов',toolchain_failed:'Окружение проекта не собралось',checks_adopted:'Проверки проекта назначены',egress_denied:'Сеть отклонила запрос',egress_allowed:'Разрешены адреса',stalled:'Агент завис (нет активности)', stall_restart:'Перезапуск после зависания', budget_warning:'Задача долгая', token_warning:'Задача тратит много токенов', review_required:'Нужно слияние человеком', supervisor_escalated:'Директор просит решения', network_unstable:'Сеть до реестров нестабильна',disk_low:'Мало места на диске',
  needs_human:'Нужно решение человека', accepted:'Проверенный результат принят',
  merged:'Изменения в preprod', incident_detected:'Новый инцидент preprod',
  incident_reopened:'Инцидент preprod повторился', human_message:'Сообщение по задаче',
};

async function notificationSettings() {
  const panel=$('#notification-channels');
  if(!panel) return;
  panel.hidden=!isOwner();
  if(!isOwner()) { panel.replaceChildren(); return; }
  const [channels,events]=await Promise.all([api('/notification-channels'),api('/notification-events')]);
  const projectName=id=>projects.find(project=>project.id===id)?.name||'Проект недоступен';
  panel.innerHTML='<h3>Уведомления людям</h3><p>Вопросы, остановки, принятые изменения и инциденты — в Rocket.Chat или Telegram.</p>'+
    channels.map(channel=>`<div class="setting-row"><strong>${esc(channel.name)}</strong><small>${({telegram:'Telegram',jira:'Jira'})[channel.provider]||'Rocket.Chat'} · ${esc(channel.project_id?projectName(channel.project_id):'Вся организация')} · ${channel.enabled?'включён':'отключён'}${channel.recipient?' · '+esc(channel.recipient):''}${channel.commands?' · команды /director':''}</small>${channel.commands&&channel.provider==='rocketchat'?`<small>Исходящий вебхук: ${esc(location.origin+'/api/chat-commands/rocketchat/'+channel.id)}</small>`:''}<small>${esc(channel.events.map(kind=>notificationLabels[kind]||kind).join(', ')||'События не выбраны')}</small><div class="setting-actions"><button class="quiet" data-notification-edit="${esc(channel.id)}">Настроить канал →</button></div></div>`).join('')+
    '<button class="secondary" id="add-notification-channel">+ Добавить канал</button>';
  panel.querySelectorAll('[data-notification-edit]').forEach(button=>button.onclick=()=>notificationChannelForm(channels.find(channel=>channel.id===button.dataset.notificationEdit),events));
  const add=panel.querySelector('#add-notification-channel');
  add.disabled=!isOrganizationOwner()&&!projects.length;
  add.onclick=()=>notificationChannelForm(null,events);
}

function notificationChannelForm(channel,events) {
  if(!isOwner()) return;
  const scopes=[...(isOrganizationOwner()?[['','Вся организация']]:[]),...projects.map(project=>[project.id,project.name])];
  const credentialsHint=channel?'Пустые реквизиты сохраняют текущие. При смене сервиса введи новые.':'Реквизиты хранятся зашифрованными. После сохранения они не отображаются.';
  modal(channel?'Настроить уведомления':'Новый канал уведомлений',
    field('name','Название',{value:channel?.name||''})+
    field('project_id','Область уведомлений',{type:'select',choices:scopes,value:channel?.project_id||scopes[0]?.[0]||'',required:false})+
    field('provider','Сервис',{type:'select',choices:[['rocketchat','Rocket.Chat'],['telegram','Telegram'],['jira','Jira: комментарии и статус задачи']],value:channel?.provider||'rocketchat'})+
    field('webhook_url','Rocket.Chat: HTTPS URL входящего вебхука',{type:'password',required:false,placeholder:'https://chat.company/hooks/…'})+
    field('recipient','Rocket.Chat: получатель',{value:channel?.recipient||'',required:false,placeholder:'@username или #channel'})+
    field('bot_token','Telegram: токен бота',{type:'password',required:false})+
    field('chat_id','Telegram: chat_id или @channel',{type:'password',required:false})+
    field('jira_url','Jira: адрес',{required:false,placeholder:'https://jira.company.ru'})+
    field('jira_reviewer','Jira: на кого назначать проверку (логин)',{required:false,placeholder:'ivanov'})+
    field('jira_status','Jira: статус проверки',{required:false,placeholder:'Ожидает проверку'})+
    field('jira_work','Jira: статус работы (из «Новая» сюда при старте)',{required:false,placeholder:'В работе'})+
    field('jira_people','Jira: доп. поля исполнителя (id через запятую)',{required:false,placeholder:'customfield_13210'})+
    field('jira_keys','Jira: ключи проектов для ссылок без адреса, через запятую',{required:false,placeholder:'ABC'})+
    field('jira_token','Jira: токен (пусто — токен Jira MCP проекта)',{type:'password',required:false})+
    field('director_token','Команды из чата: API-токен оператора (пусто — не менять)',{type:'password',required:false})+
    field('outgoing_token','Rocket.Chat: токен исходящего вебхука',{type:'password',required:false})+
    `<p>${esc(credentialsHint)}</p><p>Rocket.Chat: укажи @пользователя или #канал и разреши смену канала во входящем вебхуке. Telegram-боту заранее открой доступ к нужному чату.</p>`+
    `<p>С токеном команд «director текст» (в Telegram — «/director текст») в чате передаёт поручение директору, а ответ в треде Rocket.Chat или ответом на сообщение в Telegram продолжает остановленную задачу. Для Rocket.Chat создай исходящий вебхук «Сообщение отправлено» на адрес из списка каналов.</p>`+
    field('events','Когда уведомлять',{type:'multiselect',choices:events.map(kind=>[kind,notificationLabels[kind]||kind]),value:channel?.events||events,required:false})+
    field('enabled','Канал включён',{type:'select',choices:boolChoices,value:String(channel?.enabled??true)}),
    async values=>{
      if(!isOwner()) return;
      let credentials=null;
      if(values.provider==='rocketchat'&&values.webhook_url.trim()) credentials={webhook_url:values.webhook_url.trim()};
      if(values.provider==='jira'&&values.jira_url.trim()) {
        // A factory task finds its issue by a …/browse/KEY-1 link or a bare key of a listed project.
        credentials={base_url:values.jira_url.trim(),reviewer:values.jira_reviewer.trim(),review_status:values.jira_status.trim(),work_status:values.jira_work.trim(),assignee_fields:values.jira_people.split(',').map(f=>f.trim()).filter(Boolean),project_keys:values.jira_keys.split(',').map(key=>key.trim().toUpperCase()).filter(Boolean)};
        if(values.jira_token.trim()) credentials.token=values.jira_token.trim();
      }
      if(values.provider==='telegram'&&(values.bot_token.trim()||values.chat_id.trim())) {
        if(!values.bot_token.trim()||!values.chat_id.trim()) throw Error('Для Telegram укажи токен бота и chat_id вместе.');
        credentials={bot_token:values.bot_token.trim(),chat_id:values.chat_id.trim()};
      }
      const commands={director_token:values.director_token.trim(),outgoing_token:values.provider==='rocketchat'?values.outgoing_token.trim():''};
      if(commands.director_token||commands.outgoing_token) {
        if(!credentials&&channel?.provider!==values.provider) throw Error('Укажи реквизиты выбранного сервиса.');
        // The server keeps the current webhook or bot when only command tokens change.
        credentials={...(credentials||{}),...Object.fromEntries(Object.entries(commands).filter(([,value])=>value))};
      }
      if((!channel||channel.provider!==values.provider)&&!credentials) throw Error('Укажи реквизиты выбранного сервиса.');
      const selected=Array.isArray(values.events)?values.events:[];
      if(selected.some(kind=>!events.includes(kind))) throw Error('Неизвестное событие уведомления.');
      await api('/notification-channels'+(channel?'/'+channel.id:''),channel?'PUT':'POST',{
        name:values.name,project_id:values.project_id||null,provider:values.provider,
        recipient:values.provider==='rocketchat'?values.recipient.trim():null,
        events:selected,enabled:values.enabled==='true',credentials,
      });
      $('#modal-fields').querySelectorAll('input[type="password"]').forEach(input=>input.value='');
      toast('Канал уведомлений сохранён');
      await notificationSettings();
    });
  const updateCredentials=()=>{
    const provider=$('#modal-fields [name="provider"]').value;
    const fields={rocketchat:['webhook_url','recipient','outgoing_token'],telegram:['bot_token','chat_id'],jira:['jira_url','jira_reviewer','jira_status','jira_work','jira_people','jira_keys','jira_token']};
    $('#modal-fields [name="director_token"]').closest('label').hidden=provider==='jira';
    for(const name of Object.values(fields).flat()) {
      const input=$(`#modal-fields [name="${name}"]`);
      const visible=fields[provider].includes(name);
      input.closest('label').hidden=!visible;
      const needed=['webhook_url','bot_token','chat_id','jira_url'].includes(name)&&(!channel||channel.provider!==provider);
      input.required=visible&&(name==='recipient'||needed);
      if(['webhook_url','bot_token','chat_id','outgoing_token','jira_token'].includes(name)) input.autocomplete='new-password';
      if(!visible) input.value='';
    }
  };
  $('#modal-fields [name="provider"]').onchange=updateCredentials;
  updateCredentials();
}
