/* Guided subscription login. OAuth credentials never enter browser responses. */
let loginPoll;
async function subscriptionForm() {
  if(!poolList.length) {toast(tr('Сначала создай пул исполнителей'));return;}
  modal(tr('Подключить подписку'),
    field('provider',tr('Подписка'),{type:'select',choices:currentUser.personal_mode?[['codex','Codex'],['claude','Claude']]:[['claude','Claude']]})+
    field('name',tr('Название подключения'))+
    field('account_key',tr('Email аккаунта Claude; для Codex определим автоматически'),{required:false})+
    field('pool_ids',tr('Добавить в пулы'),{type:'multiselect',choices:poolList.map(p=>[p.id,p.name]),value:[poolList[0].id]})+
    field('model',tr('Модель; пусто — стандартная модель исполнителя'),{required:false})+
    '<p>'+tr('Откроется ссылка провайдера. Войди нужным аккаунтом. Для Claude затем вставь одноразовый код из браузера.')+'</p>'+
    (!currentUser.personal_mode?'<p>'+tr('Для корпоративного Codex используй подключение SIWC через форму реквизитов.')+'</p>':''),
    async values=>{
      values.model||=null;
      const session=await api('/login-sessions','POST',values);
      setTimeout(()=>showSubscriptionLogin(session.id).catch(error=>toast(error.message)),0);
    });
}
async function showSubscriptionLogin(id) {
  clearInterval(loginPoll);
  const session=await api(`/login-sessions/${id}`);
  const manual=session.provider==='claude';
  modal(tr('Вход: {name}',{name:session.name}),
    `<div id="subscription-state" data-session="${esc(id)}"></div>`+
    (manual?field('code',tr('Одноразовый код из браузера (code#state)'),{type:'password',required:true}):'')+
    '<button type="button" class="secondary" id="stop-subscription-login">'+tr('Отменить вход')+'</button>',
    async values=>{
      await api(`/login-sessions/${id}/code`,'POST',{code:values.code});
      values.code='';
      setTimeout(()=>showSubscriptionLogin(id).catch(error=>toast(error.message)),0);
    });
  $('#submit-modal').hidden=!manual;
  $('#submit-modal').textContent=tr('Передать код');
  $('#cancel-modal').textContent=tr('Закрыть');
  const render=row=>{
    const state=$('#subscription-state');
    if(!state||state.dataset.session!==id||!$('#modal').open)return;
    state.innerHTML=`<p>${esc(row.prompt)}</p>`+
      (safeHref(row.verification_url)?`<p><a href="${esc(row.verification_url)}" target="_blank" rel="noopener noreferrer">${tr('Открыть вход {provider} ↗',{provider:esc(row.provider)})}</a></p>`:'')+
      (row.user_code?`<p>${tr('Одноразовый код:')} <strong>${esc(row.user_code)}</strong></p>`:'');
    $('#submit-modal').disabled=row.status!=='waiting';
    if(['done','failed','expired','cancelled'].includes(row.status)) {
      clearInterval(loginPoll);$('#submit-modal').hidden=true;$('#stop-subscription-login').hidden=true;
      if(row.status==='done') {toast(tr('Подписка подключена к пулу'));load().catch(error=>toast(error.message));}
    }
  };
  $('#stop-subscription-login').onclick=async()=>{
    try {render(await api(`/login-sessions/${id}/cancel`,'POST'));}
    catch(error){toast(error.message);}
  };
  render(session);
  if(['queued','starting','waiting'].includes(session.status)) {
    let pending=false;
    loginPoll=setInterval(async()=>{
      // The server expires the login itself; past that moment there is nothing left to wait for.
      const expired=Number.isFinite(session.expires)&&Date.now()/1000>session.expires+5;
      if(pending||document.hidden&&!expired)return;pending=true;
      if(expired)clearInterval(loginPoll);
      try {render(await api(`/login-sessions/${id}`));}
      catch(error){clearInterval(loginPoll);toast(error.message);}
      finally{pending=false;}
    },2000);
  }
}
async function subscriptionSettings() {
  const panel=$('#subscription-sessions');
  if(!isOrganizationOwner()){panel.innerHTML='';return;}
  const rows=await api('/login-sessions');
  panel.innerHTML=rows.filter(row=>['queued','starting','waiting'].includes(row.status)).map(row=>
    `<div class="setting-row"><strong>${esc(row.name)}</strong><small>${esc(row.prompt)}</small><button class="quiet" data-login-session="${row.id}">${tr('Продолжить вход →')}</button></div>`).join('');
  panel.querySelectorAll('[data-login-session]').forEach(button=>button.onclick=()=>showSubscriptionLogin(button.dataset.loginSession).catch(error=>toast(error.message)));
}
$('#modal').addEventListener('close',()=>{if(!$('#modal').open)clearInterval(loginPoll);});
$('#connect-subscription').onclick=()=>subscriptionForm().catch(error=>toast(error.message));
