'use strict';

/* One authenticated conversation panel. All assistant text comes from the API. */
(() => {
  if (window.DarkfactoryChat) return;
  const shell = document.getElementById('shell');
  if (!shell) return;
  const mobile = window.matchMedia('(max-width: 980px)');
  const state = {mounted:false, open:false, busy:false, sending:false, polling:false, changing:false,
    context:'', organization:'', thread:'', active:null, activeStatus:null, signature:'', epoch:0, timer:null, images:[], due:0, delay:2000};
  const imageTypes = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'];
  const imageCache = new Map();
  async function imageSource(path) {
    if (!imageCache.has(path)) imageCache.set(path, fetch('/api' + path.replace(/^\/api/, ''), {headers:{Authorization:'Bearer ' + token}})
      .then(response => response.ok ? response.blob() : Promise.reject(new Error(response.statusText)))
      .then(blob => URL.createObjectURL(blob)).catch(error => { imageCache.delete(path); throw error; }));
    return imageCache.get(path);
  }
  function attach(files) {
    for (const file of files) {
      if (!imageTypes.includes(file.type)) continue;
      if (state.images.length >= 4) { ui.error.textContent = tr('Не больше 4 картинок в сообщении'); ui.error.hidden = false; break; }
      if (file.size > 5 * 1024 * 1024) { ui.error.textContent = tr('Картинка больше 5 МБ'); ui.error.hidden = false; continue; }
      const reader = new FileReader();
      reader.onload = () => {
        if (state.images.length >= 4) return;
        state.images.push({media_type:file.type, data:String(reader.result).split(',')[1], preview:String(reader.result)});
        renderAttachments();
      };
      reader.readAsDataURL(file);
    }
  }
  function renderAttachments() {
    ui.attachments.hidden = !state.images.length;
    ui.attachments.replaceChildren(...state.images.map((image, index) => {
      const item = element('div', 'df-chat-attachment');
      const img = element('img');
      img.src = image.preview;
      img.alt = tr('Картинка {n}', {n: index + 1});
      const remove = element('button', 'df-chat-attachment-remove', '×');
      remove.type = 'button';
      remove.title = tr('Убрать картинку');
      remove.onclick = () => { state.images.splice(index, 1); renderAttachments(); };
      item.append(img, remove);
      return item;
    }));
    controls();
  }
  const ui = {};
  const presets = [
    [tr('Проекты'), tr('Помоги создать и настроить проект')],
    [tr('Команда'), tr('Настрой команду проекта')],
    [tr('Пулы'), tr('Покажи пулы подписок и помоги настроить их')],
    [tr('Задачи'), tr('Помоги поставить задачу и проверить её статус')],
    ['MCP', tr('Какие MCP доступны?')],
    [tr('Скиллы'), tr('Создай скилл')],
  ];
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const authenticated = () => !shell.hidden && typeof currentUser !== 'undefined' && !!currentUser;
  const maySend = () => authenticated() && currentUser.role !== 'observer' && currentUser.token_role !== 'observer';
  const selectedContext = () => ui.context?.value || '';

  function controls() {
    const readonly = !maySend();
    ui.input.disabled = readonly || state.sending;
    ui.send.disabled = readonly || state.sending || state.changing || state.busy || (!ui.input.value.trim() && !state.images.length);
    ui.presets.querySelectorAll('button').forEach(button => button.disabled = readonly || state.sending || state.busy);
    ui.context.disabled = state.sending || state.changing;
    ui.threads.disabled = state.sending || state.changing;
    ui.newChat.disabled = readonly || state.sending || state.changing;
    ui.stop.hidden = !state.busy;
    ui.stop.disabled = readonly || state.changing || !state.active || state.activeStatus === 'cancelling';
    ui.stop.textContent = state.activeStatus === 'cancelling' ? tr('Останавливаем…') : tr('Отменить сообщение');
    ui.send.textContent = state.sending ? tr('Отправляем…') : tr('Отправить');
    ui.status.textContent = state.sending ? tr('Отправляем сообщение…') : state.activeStatus === 'cancelling'
      ? tr('Останавливаем исполнителя. Уже начатые действия могут завершиться.') : state.busy ? tr('Директор работает…')
      : readonly ? tr('Чтение диалога. Отправка доступна владельцу и оператору.') : '';
    ui.status.hidden = !ui.status.textContent;
    ui.panel.setAttribute('aria-busy', String(state.busy || state.sending));
    ui.toggle.classList.toggle('df-chat-working', state.busy);
  }

  function setOpen(open, focus = false) {
    state.open = !!open;
    ui.panel.hidden = !state.open;
    ui.toggle.hidden = state.open;
    ui.backdrop.hidden = !state.open || !mobile.matches;
    ui.toggle.setAttribute('aria-expanded', String(state.open));
    shell.classList.toggle('chat-open', state.open);
    ui.panel.setAttribute('role', mobile.matches ? 'dialog' : 'region');
    if (mobile.matches && state.open) ui.panel.setAttribute('aria-modal', 'true');
    else ui.panel.removeAttribute('aria-modal');
    try { localStorage.setItem('df-chat-open', state.open ? '1' : '0'); } catch {}
    if (focus) (state.open ? maySend() ? ui.input : ui.context : ui.toggle).focus();
    if (state.open) { state.due = 0; tick(); }
  }

  function syncContext() {
    const available = typeof projects !== 'undefined' && Array.isArray(projects) ? projects : [];
    const current = typeof projectId !== 'undefined' ? projectId : '';
    const options = [['', tr('Вся фабрика')], ...available.map(project => [project.id, project.name])];
    const signature = JSON.stringify(options);
    if (ui.context.dataset.options !== signature) {
      let saved = '';
      try { saved = localStorage.getItem('df-director-context') || ''; } catch {}
      const previous = ui.context.dataset.options ? selectedContext() : saved;
      ui.context.replaceChildren(...options.map(([value, label]) => {
        const option = element('option', '', label);
        option.value = value;
        return option;
      }));
      ui.context.value = options.some(([id]) => id === previous) ? previous : '';
      ui.context.dataset.options = signature;
    }
    // Factory scope remains stable when the board changes. An explicitly
    // selected project also remains stable until the user changes context.
    ui.context.title = current ? tr('Вся фабрика или отдельный проект') : tr('Контекст диалога');
    const organization = currentUser.organization?.id || currentUser.id || '';
    const context = selectedContext();
    if (state.context !== context || state.organization !== organization) {
      state.context = context;
      state.organization = organization;
      state.epoch += 1;
      state.signature = '';
      state.busy = false;
      state.active = null;
      state.activeStatus = null;
      state.thread = '';
      try { state.thread = localStorage.getItem(threadKey()) || ''; } catch {}
      ui.threads.replaceChildren(new Option(tr('Общий чат'), ''));
      ui.threads.dataset.options = '';
      ui.messages.replaceChildren();
      ui.empty.hidden = false;
      ui.error.hidden = true;
    }
  }

  const threadKey = () => 'df-director-thread:' + state.organization + ':' + state.context;

  function selectThread(identifier) {
    state.thread = identifier;
    state.epoch += 1;
    state.signature = '';
    ui.messages.replaceChildren();
    ui.empty.hidden = false;
    ui.error.hidden = true;
    try { localStorage.setItem(threadKey(), identifier); } catch {}
  }

  // The Director's decisions on stopped tasks; the open ones are answered right here in the chat.
  async function showJournal() {
    const project = selectedContext();
    if (!project) { toast(tr('Выбери проект в контексте диалога')); return; }
    const rows = await api(`/projects/${project}/director-journal`);
    const kinds = {supervisor: tr('Решил'), supervisor_escalated: tr('Ждёт тебя'), folded: tr('Передал цель')};
    const items = rows.length ? rows.map(row => `<div class="event ${row.open ? 'warn' : ''}"><small>${new Date(row.at * 1000).toLocaleString(LOCALE, {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'})} · ${esc(row.open ? tr('Ждёт тебя') : kinds[row.kind] || row.kind)} · <a href="?task=${esc(row.task_id)}" data-journal-task="${esc(row.task_id)}">${esc(row.task_title)}</a></small><p>${esc(row.message.length > 600 ? row.message.slice(0, 600) + '…' : row.message)}</p></div>`).join('')
      : '<p class="muted small">' + tr('Директор ещё не разбирал остановки в этом проекте.') + '</p>';
    modal(tr('Решения директора'), `<p class="small">${tr('Чтобы ответить на «Ждёт тебя», напиши директору в чат, например: «по задаче … продолжай, используй …».')}</p><div class="timeline">${items}</div>`, async () => {});
    $('#submit-modal').hidden = true;
    $('#cancel-modal').textContent = tr('Закрыть');
    document.querySelectorAll('[data-journal-task]').forEach(link => link.onclick = event => { event.preventDefault(); $('#modal').close(); showDetail(link.dataset.journalTask).catch(error => toast(error.message)); });
  }

  async function newChat() {
    if (!maySend() || state.sending || state.changing) return;
    const epoch = ++state.epoch;
    const context = state.context;
    state.changing = true;
    controls();
    try {
      const result = await api('/config-chat/threads', 'POST', {project_id:context || null});
      if (epoch !== state.epoch || !authenticated()) return;
      selectThread(result.id);
      ui.input.value = '';
      ui.input.style.height = '';
      await refresh();
    } catch (error) {
      if (epoch !== state.epoch || !authenticated()) return;
      ui.error.textContent = error.message || tr('Не удалось создать чат');
      ui.error.hidden = false;
    } finally {
      state.changing = false;
      if (authenticated()) controls();
    }
  }

  async function cancelMessage() {
    if (!maySend() || !state.active || state.changing) return;
    const epoch = ++state.epoch;
    state.changing = true;
    controls();
    try {
      const result = await api('/config-chat/messages/' + encodeURIComponent(state.active) + '/cancel', 'POST');
      if (epoch !== state.epoch || !authenticated()) return;
      state.activeStatus = result.status;
      state.busy = !['cancelled', 'succeeded', 'failed'].includes(result.status);
      await refresh();
    } catch (error) {
      if (epoch !== state.epoch || !authenticated()) return;
      ui.error.textContent = error.message || tr('Не удалось отменить сообщение');
      ui.error.hidden = false;
    } finally {
      state.changing = false;
      if (authenticated()) controls();
    }
  }

  function renderMessages(messages) {
    const rows = Array.isArray(messages) ? messages.filter(message => ['user', 'assistant'].includes(message.role)) : [];
    const signature = JSON.stringify(rows);
    if (signature === state.signature) return;
    const nearBottom = ui.history.scrollHeight - ui.history.scrollTop - ui.history.clientHeight < 80;
    const previousTop = ui.history.scrollTop;
    ui.messages.replaceChildren(...rows.map(message => {
      const item = element('article', `df-chat-message df-chat-${message.role}`);
      const heading = element('div', 'df-chat-message-author', message.role === 'user' ? tr('Ты') : tr('Директор фабрики'));
      item.append(heading);
      if (message.content) item.append(element('div', 'df-chat-message-content', String(message.content)));
      if (Array.isArray(message.images) && message.images.length) {
        const gallery = element('div', 'df-chat-message-images');
        message.images.forEach(path => {
          const link = element('a');
          link.target = '_blank';
          link.rel = 'noopener';
          const img = element('img');
          img.alt = tr('Приложенная картинка');
          imageSource(String(path)).then(url => { img.src = url; link.href = url; }).catch(() => { img.alt = tr('Картинка недоступна'); });
          link.append(img);
          gallery.append(link);
        });
        item.append(gallery);
      }
      const labels = {queued:tr('В очереди'), waiting_pool:tr('Ждёт подписку'), running:tr('В работе'), recovering:tr('Восстановление'), cancelling:tr('Останавливается'), failed:tr('Не удалось выполнить запрос'), cancelled:tr('Отменено')};
      if (labels[message.status]) item.append(element('small', 'df-chat-message-state', labels[message.status]));
      if (message.role === 'assistant' && message.organization_access_available === true && message.id) {
        const openOrganization = element('button', 'df-chat-organization', tr('Открыть организацию'));
        openOrganization.type = 'button';
        openOrganization.onclick = async () => {
          openOrganization.disabled = true;
          try {
            const result = await api('/config-chat/messages/' + encodeURIComponent(message.id) + '/organization-access', 'POST');
            if (typeof result.token !== 'string' || !result.token) throw new Error(tr('Не удалось открыть организацию'));
            sessionStorage.setItem('df-token', result.token);
            location.reload();
          } catch (error) {
            ui.error.textContent = error.message || tr('Не удалось открыть организацию');
            ui.error.hidden = false;
            openOrganization.disabled = false;
          }
        };
        item.append(openOrganization);
      }
      return item;
    }));
    ui.empty.hidden = rows.length > 0;
    if (nearBottom || !state.signature) ui.history.scrollTop = ui.history.scrollHeight;
    else ui.history.scrollTop = previousTop;
    state.signature = signature;
  }

  async function refresh() {
    if (state.polling || !authenticated() || !state.open) return;
    syncContext();
    const epoch = state.epoch;
    const context = state.context;
    const thread = state.thread;
    state.polling = true;
    try {
      const params = new URLSearchParams();
      if (context) params.set('project_id', context);
      const threads = await api('/config-chat/threads?' + params);
      if (epoch !== state.epoch || !authenticated()) return;
      if (thread && !threads.some(item => item.id === thread)) { selectThread(''); return; }
      const choices = [new Option(tr('Общий чат'), ''), ...threads.map(item => new Option(item.title, item.id))];
      const optionsSignature = JSON.stringify(threads);
      if (ui.threads.dataset.options !== optionsSignature) {
        ui.threads.replaceChildren(...choices);
        ui.threads.dataset.options = optionsSignature;
      }
      ui.threads.value = thread;
      if (thread) params.set('thread_id', thread);
      const result = await api('/config-chat?' + params);
      if (epoch !== state.epoch || !authenticated()) return;
      const before = state.signature;
      renderMessages(result.messages);
      state.busy = result.busy === true;
      // A pending reply is watched every 2 s; a quiet conversation backs off to 30 s.
      state.delay = state.busy || state.signature !== before ? 2000 : Math.min(state.delay * 2, 30000);
      state.due = Date.now() + state.delay;
      state.active = result.active_message_id;
      state.activeStatus = result.active_status;
      if (state.busy && (result.active_thread_id !== (thread || null) || result.active_project_id !== (context || null))) {
        ui.stop.title = tr('Отменить сообщение, которое выполняется в другом чате');
      } else ui.stop.title = tr('Уже начатые действия могут завершиться. Журнал сохранится.');
      ui.error.hidden = true;
    } catch (error) {
      if (epoch !== state.epoch || !authenticated()) return;
      ui.error.textContent = error.message || tr('Не удалось загрузить диалог');
      ui.error.hidden = false;
    } finally {
      state.polling = false;
      if (authenticated()) controls();
    }
  }

  async function send(event) {
    event.preventDefault();
    const message = ui.input.value.trim();
    const images = state.images.map(({media_type, data}) => ({media_type, data}));
    if ((!message && !images.length) || !maySend() || state.sending || state.changing || state.busy) return;
    syncContext();
    // A history request started before this submission must not clear the
    // queued/busy indicator with an older server snapshot.
    const epoch = ++state.epoch;
    const context = state.context;
    state.sending = true;
    ui.error.hidden = true;
    controls();
    try {
      const result = await api('/config-chat', 'POST', {message, images, thread_id:state.thread || null, ...(context ? {project_id:context} : {})});
      if (epoch !== state.epoch || !authenticated()) return;
      ui.input.value = '';
      ui.input.style.height = '';
      state.images = [];
      renderAttachments();
      state.busy = true;
      state.active = result.id;
      state.activeStatus = result.status;
      state.delay = 2000;
      await refresh();
    } catch (error) {
      if (epoch !== state.epoch || !authenticated()) return;
      ui.error.textContent = error.message || tr('Не удалось отправить сообщение');
      ui.error.hidden = false;
    } finally {
      state.sending = false;
      if (authenticated()) {
        controls();
        if (!mobile.matches) ui.input.focus();
      }
    }
  }

  function mount() {
    if (state.mounted || !authenticated()) return;
    state.mounted = true;
    ui.panel = element('section', 'df-chat');
    ui.panel.id = 'factory-chat';
    ui.panel.setAttribute('aria-labelledby', 'factory-chat-title');
    const header = element('div', 'df-chat-header');
    const heading = element('div', 'df-chat-heading');
    heading.append(element('span', 'df-chat-symbol', '✦'));
    const title = element('h2', '', tr('Директор фабрики'));
    title.id = 'factory-chat-title';
    heading.append(title);
    const close = element('button', 'df-chat-close', '✕');
    close.type = 'button';
    close.title = tr('Свернуть чат');
    close.setAttribute('aria-label', tr('Свернуть чат'));
    close.onclick = () => setOpen(false, true);
    header.append(heading, close);

    const contextLabel = element('label', 'df-chat-context', tr('Контекст'));
    ui.context = element('select');
    ui.context.setAttribute('aria-label', tr('Контекст диалога'));
    ui.context.onchange = () => { try { localStorage.setItem('df-director-context', selectedContext()); } catch {} syncContext(); controls(); refresh(); };
    contextLabel.append(ui.context);
    const conversations = element('div', 'df-chat-conversations');
    ui.threads = element('select');
    ui.threads.setAttribute('aria-label', tr('Чаты с директором'));
    ui.threads.onchange = () => { selectThread(ui.threads.value); controls(); refresh(); };
    ui.newChat = element('button', 'df-chat-new', '+ ' + tr('Новый чат'));
    ui.newChat.type = 'button';
    ui.newChat.onclick = newChat;
    ui.journal = element('button', 'df-chat-new', tr('Решения'));
    ui.journal.type = 'button';
    ui.journal.title = tr('Как директор разобрал остановленные задачи');
    ui.journal.onclick = () => showJournal().catch(error => toast(error.message));
    conversations.append(ui.threads, ui.newChat, ui.journal);
    ui.history = element('div', 'df-chat-history');
    ui.empty = element('div', 'df-chat-empty');
    ui.empty.append(element('span', 'df-chat-empty-symbol', '✦'), element('h3', '', tr('Что нужно фабрике?')),
      element('p', '', tr('Обсуди проекты, команду, подписки и задачи. Директор учтёт твои права и контекст.')));
    ui.messages = element('div', 'df-chat-messages');
    ui.messages.setAttribute('role', 'log');
    ui.messages.setAttribute('aria-live', 'polite');
    ui.messages.setAttribute('aria-relevant', 'additions text');
    ui.messages.setAttribute('aria-label', tr('История диалога'));
    ui.history.append(ui.empty, ui.messages);

    const composer = element('div', 'df-chat-composer');
    ui.presets = element('div', 'df-chat-presets');
    ui.presets.setAttribute('aria-label', tr('Начать разговор'));
    presets.forEach(([label, prompt]) => {
      const button = element('button', 'df-chat-preset', label);
      button.type = 'button';
      button.title = prompt;
      button.onclick = () => { ui.input.value = prompt; ui.input.dispatchEvent(new Event('input')); ui.input.focus(); };
      ui.presets.append(button);
    });
    ui.status = element('div', 'df-chat-status');
    ui.status.setAttribute('role', 'status');
    ui.status.hidden = true;
    ui.stop = element('button', 'df-chat-stop', tr('Отменить сообщение'));
    ui.stop.type = 'button';
    ui.stop.hidden = true;
    ui.stop.onclick = cancelMessage;
    ui.error = element('div', 'df-chat-error');
    ui.error.setAttribute('role', 'alert');
    ui.error.hidden = true;
    const form = element('form', 'df-chat-form');
    ui.input = element('textarea', 'df-chat-input');
    ui.input.rows = 3;
    ui.input.maxLength = 12000;
    ui.input.placeholder = tr('Напиши, что нужно сделать…');
    ui.input.setAttribute('aria-label', tr('Сообщение директору фабрики'));
    ui.input.addEventListener('input', () => {
      ui.input.style.height = 'auto';
      ui.input.style.height = Math.min(160, ui.input.scrollHeight) + 'px';
      controls();
    });
    ui.input.addEventListener('keydown', event => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(event); }
    });
    ui.input.addEventListener('paste', event => {
      const files = [...(event.clipboardData?.files || [])].filter(file => imageTypes.includes(file.type));
      if (files.length) { event.preventDefault(); attach(files); }
    });
    form.addEventListener('dragover', event => { if ([...event.dataTransfer.types].includes('Files')) event.preventDefault(); });
    form.addEventListener('drop', event => { event.preventDefault(); attach(event.dataTransfer.files); });
    ui.attachments = element('div', 'df-chat-attachments');
    ui.attachments.hidden = true;
    ui.file = element('input');
    ui.file.type = 'file';
    ui.file.accept = imageTypes.join(',');
    ui.file.multiple = true;
    ui.file.hidden = true;
    ui.file.onchange = () => { attach(ui.file.files); ui.file.value = ''; };
    ui.attach = element('button', 'df-chat-attach', '📎');
    ui.attach.type = 'button';
    ui.attach.title = tr('Приложить картинку (или вставь Ctrl/Cmd+V)');
    ui.attach.setAttribute('aria-label', tr('Приложить картинку'));
    ui.attach.onclick = () => ui.file.click();
    const actions = element('div', 'df-chat-form-actions');
    actions.append(ui.attach, ui.file, element('small', '', tr('Enter — отправить · Shift + Enter — строка')));
    ui.send = element('button', 'df-chat-send', tr('Отправить'));
    ui.send.type = 'submit';
    actions.append(ui.send);
    form.append(ui.attachments, ui.input, actions);
    form.onsubmit = send;
    composer.append(ui.presets, ui.status, ui.stop, ui.error, form,
      element('p', 'df-chat-secret-note', tr('Пароли и токены добавляй через подключения.')));
    ui.panel.append(header, contextLabel, conversations, ui.history, composer);
    ui.toggle = element('button', 'df-chat-toggle', '✦ ' + tr('Директор фабрики'));
    ui.toggle.type = 'button';
    ui.toggle.setAttribute('aria-controls', 'factory-chat');
    ui.toggle.onclick = () => setOpen(true, true);
    ui.backdrop = element('button', 'df-chat-backdrop');
    ui.backdrop.type = 'button';
    ui.backdrop.tabIndex = -1;
    ui.backdrop.setAttribute('aria-label', tr('Закрыть чат директора'));
    ui.backdrop.onclick = () => setOpen(false, true);
    shell.append(ui.backdrop, ui.panel, ui.toggle);
    syncContext();
    controls();
    let open = !mobile.matches;
    try { const saved = localStorage.getItem('df-chat-open'); if (saved !== null) open = saved === '1'; } catch {}
    setOpen(open);
    state.timer = setInterval(tick, 2000);
  }

  function tick() {
    if (!authenticated()) return;
    if (!state.mounted) { mount(); return; }
    syncContext();
    controls();
    if (!document.hidden && state.open && Date.now() >= state.due) refresh();
  }

  document.addEventListener('keydown', event => {
    if (!authenticated() || !state.open) return;
    if (event.key === 'Escape') { setOpen(false, true); return; }
    if (event.key !== 'Tab' || !mobile.matches || !ui.panel.contains(document.activeElement)) return;
    const focusable = [...ui.panel.querySelectorAll('button,select,textarea')].filter(node => !node.disabled && !node.hidden);
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  });
  mobile.addEventListener('change', () => { if (state.mounted) setOpen(state.open); });
  document.addEventListener('visibilitychange', () => { state.due = 0; tick(); });
  new MutationObserver(() => {
    if (authenticated()) tick();
    else if (state.mounted) {
      state.epoch += 1;
      state.signature = '';
      ui.messages.replaceChildren();
      ui.input.value = '';
      ui.error.hidden = true;
    }
  }).observe(shell, {attributes:true, attributeFilter:['hidden']});
  window.DarkfactoryChat = {open:() => { mount(); if (state.mounted) setOpen(true, true); }, refresh:tick};
  mount();
})();
