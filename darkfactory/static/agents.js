/* Project-scoped agent workspace. Catalog definitions stay project-owned after import. */
const agentStages = [['planner', 'Планирование'], ['writer', 'Разработка'], ['reviewer', 'Независимое ревью'], ['director', 'Директор: остановки и чат']];
const agentKinds = {
  profiles: {title: 'Роли', singular: 'роль', icon: '◈', empty: 'Собери команду из готовых ролей', hint: 'Менеджер разбивает работу, аналитик уточняет решение, разработчик пишет код, ревьюер проверяет результат.'},
  skills: {title: 'Скиллы', singular: 'скилл', icon: '✦', empty: 'Добавь знания и рабочие приёмы', hint: 'Скилл содержит инструкции. Подключи его к одной или нескольким ролям.'},
  mcp_servers: {title: 'MCP', singular: 'MCP', icon: '⌘', empty: 'Дай агентам нужные инструменты', hint: 'Готовые подключения работают в контейнере задачи. Выбери MCP, затем подключи его к роли.'},
};
const agentWorkspace = {project: '', identity: '', settings: null, catalog: null, tab: 'profiles', query: '', request: 0};
function agentPayload(settings) {
  return {...settings, mcp_servers: settings.mcp_servers.map(server => {
    const {configured_headers, configured_env, ...value} = server;
    return value;
  })};
}
const agentCopy = value => JSON.parse(JSON.stringify(value));
function agentCatalogEntry(kind, name) {
  return (agentWorkspace.catalog?.[kind] || []).find(entry => entry.definition.name === name);
}
function agentProviderLabel(provider) { return provider === 'project' ? 'Из настроек проекта' : provider; }
function agentTitle(kind, item) { return agentCatalogEntry(kind, item.name)?.title || item.name; }
function agentDescription(kind, item) {
  const entry = agentCatalogEntry(kind, item.name);
  return entry?.description || item.description || (kind === 'profiles' ? item.instructions : '') || 'Своя настройка проекта';
}
async function agentSettings(project) {
  projectId = project.id;
  const navigation = document.querySelector('[data-view="agents"]');
  if (navigation) navigation.click();
  else {
    document.querySelectorAll('main > section').forEach(section => section.hidden = section.id !== 'agents-view');
    await agentsPage();
  }
}
async function agentsPage(force = false) {
  const host = $('#agent-workspace');
  if (!host) return;
  const identity = currentUser?.id + ':' + currentUser?.role + ':' + currentUser?.token_role;
  const project = projects.find(item => item.id === projectId) || projects[0];
  if (!project) {
    agentWorkspace.project = ''; agentWorkspace.settings = null;
    host.innerHTML = '<div class="aw-empty"><span>◈</span><h2>Сначала добавь проект</h2><p>Команда агентов, скиллы и инструменты настраиваются отдельно для каждого проекта.</p></div>';
    return;
  }
  if (force !== true && agentWorkspace.project === project.id && agentWorkspace.identity === identity && agentWorkspace.settings) return;
  const request = ++agentWorkspace.request;
  agentWorkspace.project = project.id; agentWorkspace.identity = identity; agentWorkspace.settings = null;
  host.innerHTML = '<p class="aw-loading" role="status">Загружаем команду и каталог…</p>';
  try {
    const [settings, catalog] = await Promise.all([api(`/projects/${project.id}/agents`), api(`/projects/${project.id}/agent-catalog`)]);
    if (request !== agentWorkspace.request) return;
    agentWorkspace.settings = settings; agentWorkspace.catalog = catalog;
    agentRender();
  } catch (error) {
    if (request !== agentWorkspace.request) return;
    host.innerHTML = '<div class="aw-empty"><h2>Не удалось загрузить агентов</h2><p class="aw-load-error"></p><button type="button" data-agent-retry>Повторить</button></div>';
    host.querySelector('.aw-load-error').textContent = error.message;
    host.querySelector('[data-agent-retry]').onclick = () => agentsPage(true);
    toast(error.message);
  }
}
function agentRender() {
  const host = $('#agent-workspace'), settings = agentWorkspace.settings, owner = isOwner();
  if (!host || !settings) return;
  const kind = agentWorkspace.tab, info = agentKinds[kind];
  host.innerHTML = `<div class="aw-heading"><div><div class="aw-eyebrow">КОМАНДА ПРОЕКТА</div><h1>Агенты</h1><p>Выбери роли, знания и инструменты. Фабрика сама передаст работу между агентами.</p></div><label class="aw-project">Проект<select id="agent-project">${projects.map(project => `<option value="${esc(project.id)}" ${project.id === agentWorkspace.project ? 'selected' : ''}>${esc(project.name)}</option>`).join('')}</select></label></div>
    ${!owner ? '<div class="aw-note">Режим просмотра. Настройки команды меняет владелец проекта.</div>' : ''}
    <section class="aw-flow" aria-label="Роли в процессе"><div class="aw-section-heading"><div><h2>Кто выполняет задачу</h2><p>Каждая роль работает в отдельной сессии. Подзадачи используют общий бюджет.</p></div>${owner ? '<button type="button" class="secondary" data-agent-routing>Назначения и лимиты</button>' : ''}</div><div class="aw-stages">${agentStages.map(([key, label], index) => {
      const item = settings.profiles.find(profile => profile.name === settings.assignments[key]);
      return `<div class="aw-stage"><span class="aw-step">0${index + 1}</span><div><small>${label}</small><strong>${item ? esc(agentTitle('profiles', item)) : 'Исполнитель проекта'}</strong><span>${item ? esc(agentProviderLabel(item.provider)) + (item.model ? ' · ' + esc(item.model) : '') : 'Из настроек проекта и пула'}</span><button type="button" class="quiet aw-prompt" data-agent-prompt="${key}">Показать промпт</button></div></div>`;
    }).join('')}</div><div class="aw-flow-footer"><span>Глубина передачи: <b>${settings.max_delegation_depth}</b></span><span>Подзадач в дереве: <b>${settings.max_delegated_tasks}</b></span><span>Слияние после проверок и ревью → preprod</span></div></section>
    <div class="aw-toolbar"><div class="aw-tabs" role="tablist" aria-label="Настройки агентов">${Object.entries(agentKinds).map(([key, value]) => `<button type="button" role="tab" aria-selected="${key === kind}" class="${key === kind ? 'active' : ''}" data-agent-tab="${key}">${value.title}<span>${settings[key].length}</span></button>`).join('')}</div><label class="aw-search"><span class="aw-sr">Поиск в ролях, скиллах или MCP</span><input type="search" id="agent-search" placeholder="Поиск в ${kind === 'profiles' ? 'ролях' : kind === 'skills' ? 'скиллах' : 'MCP'}…" value="${esc(agentWorkspace.query)}"></label></div>
    <div class="aw-content" id="agent-content" role="tabpanel" aria-label="${info.title}"></div>`;
  host.querySelector('#agent-project').onchange = event => { projectId = event.target.value; agentsPage(true); };
  host.querySelector('#agent-search').oninput = event => { agentWorkspace.query = event.target.value; agentRenderCards(); };
  host.querySelectorAll('[data-agent-tab]').forEach(button => button.onclick = () => { agentWorkspace.tab = button.dataset.agentTab; agentWorkspace.query = ''; agentRender(); });
  host.querySelector('[data-agent-routing]')?.addEventListener('click', agentRouting);
  host.querySelectorAll('[data-agent-prompt]').forEach(button => button.onclick = () => agentPrompt(button.dataset.agentPrompt).catch(error => toast(error.message)));
  agentRenderCards();
}
function agentRenderCards() {
  const host = $('#agent-content');
  if (!host) return;
  const kind = agentWorkspace.tab, info = agentKinds[kind], settings = agentWorkspace.settings, owner = isOwner();
  const query = agentWorkspace.query.trim().toLocaleLowerCase('ru');
  const matches = (title, description, name) => `${title} ${description} ${name}`.toLocaleLowerCase('ru').includes(query);
  const configured = settings[kind].filter(item => matches(agentTitle(kind, item), agentDescription(kind, item), item.name));
  const catalog = (agentWorkspace.catalog[kind] || []).filter(entry => matches(entry.title, entry.description, entry.definition.name));
  const usages = name => settings.profiles.filter(profile => (profile[kind] || []).includes(name)).map(profile => agentTitle('profiles', profile));
  host.innerHTML = `<section><div class="aw-section-heading"><div><h2>${kind === 'profiles' ? 'Твоя команда' : kind === 'skills' ? 'Скиллы проекта' : 'Подключено к проекту'}</h2><p>${esc(info.hint)}</p></div></div>
    ${configured.length ? `<div class="aw-grid">${configured.map(item => {
      let meta = '';
      if (kind === 'profiles') meta = `<span>${esc(agentProviderLabel(item.provider))}</span><span>${item.skills.length} скилл.</span><span>${item.mcp_servers.length} MCP</span>${item.can_delegate ? '<span>Может делегировать</span>' : ''}`;
      else if (kind === 'mcp_servers') meta = `<span>${item.read_only ? 'Только чтение' : 'Есть запись'}</span><span>${item.enabled_tools.length} инструмент.</span><span>${esc(item.transport)}</span>`;
      else meta = `<span>${usages(item.name).length ? 'Роли: ' + esc(usages(item.name).join(', ')) : 'Ещё не подключён к роли'}</span>`;
      return `<article class="aw-card"><div class="aw-card-top"><span class="aw-card-icon">${info.icon}</span><span class="aw-badge">В проекте</span></div><h3>${esc(agentTitle(kind, item))}</h3><p>${esc(agentDescription(kind, item))}</p><div class="aw-tags">${meta}</div><div class="aw-card-actions"><button type="button" class="secondary" data-agent-edit="${esc(item.name)}">${owner ? 'Настроить' : 'Посмотреть'}</button>${owner ? `<button type="button" class="quiet" aria-label="Удалить ${esc(agentTitle(kind, item))}" data-agent-remove="${esc(item.name)}">Удалить</button>` : ''}</div></article>`;
    }).join('')}</div>` : `<div class="aw-empty aw-empty-inline"><h3>${query ? 'Ничего не найдено' : esc(info.empty)}</h3><p>${query ? 'Попробуй другое слово.' : 'Ниже — готовые варианты. Добавление сохранит их в этом проекте.'}</p></div>`}</section>
    <section class="aw-catalog"><div class="aw-section-heading"><div><div class="aw-eyebrow">ГОТОВО К РАБОТЕ</div><h2>Каталог ${kind === 'profiles' ? 'ролей' : kind === 'skills' ? 'скиллов' : 'MCP'}</h2><p>Готовые настройки можно менять после добавления.</p></div>${owner && kind === 'profiles' ? '<button type="button" data-agent-team>Добавить команду</button>' : ''}</div>
    <div class="aw-grid">${catalog.map(entry => {
      const exists = settings[kind].some(item => item.name === entry.definition.name);
      return `<article class="aw-card aw-catalog-card"><div class="aw-card-top"><span class="aw-card-icon">${info.icon}</span><span class="aw-badge aw-catalog-badge">${kind === 'mcp_servers' ? (entry.definition.read_only ? 'Чтение' : 'Чтение и запись') : 'Из каталога'}</span></div><h3>${esc(entry.title)}</h3><p>${esc(entry.description)}</p><div class="aw-card-actions">${owner ? `<button type="button" class="${exists ? 'secondary' : ''}" data-agent-install="${esc(entry.id)}" ${exists ? 'disabled' : ''}>${exists ? '✓ Добавлено' : '+ Добавить'}</button>` : '<span class="aw-readonly">Добавляет владелец проекта</span>'}</div></article>`;
    }).join('')}</div>${!catalog.length ? '<p class="aw-no-results">В каталоге ничего не найдено.</p>' : ''}</section>
    ${owner ? `<details class="aw-advanced"><summary>Свой ${info.singular}: расширенная настройка</summary><p>Для собственного профиля, инструкций или внешнего сервера.</p><button type="button" class="secondary" data-agent-custom>Создать ${info.singular}</button></details>` : ''}`;
  host.querySelectorAll('[data-agent-edit]').forEach(button => button.onclick = () => agentEditor(kind, button.dataset.agentEdit));
  host.querySelectorAll('[data-agent-remove]').forEach(button => button.onclick = () => agentRemove(kind, button.dataset.agentRemove));
  host.querySelectorAll('[data-agent-install]').forEach(button => button.onclick = async () => {
    if (!isOwner()) return;
    button.disabled = true;
    try {
      const entry = agentWorkspace.catalog[kind].find(item => item.id === button.dataset.agentInstall);
      const settings = agentCopy(agentWorkspace.settings);
      agentImport(settings, kind, entry);
      await agentSave(settings, 'Добавлено в проект');
    } catch (error) { toast(error.message); button.disabled = false; }
  });
  host.querySelector('[data-agent-custom]')?.addEventListener('click', () => agentEditor(kind));
  host.querySelector('[data-agent-team]')?.addEventListener('click', async event => {
    if (!isOwner()) return;
    event.currentTarget.disabled = true;
    try {
      const settings = agentCopy(agentWorkspace.settings);
      for (const entry of agentWorkspace.catalog.profiles) agentImport(settings, 'profiles', entry);
      for (const [key, name] of [['planner', 'manager'], ['writer', 'developer'], ['reviewer', 'reviewer'], ['director', 'director']]) {
        if (!settings.assignments[key] && settings.profiles.some(profile => profile.name === name)) settings.assignments[key] = name;
      }
      await agentSave(settings, 'Команда добавлена. Назначения готовы');
    } catch (error) { toast(error.message); agentRenderCards(); }
  });
}
function agentImport(settings, kind, entry) {
  if (!entry?.definition) throw new Error('Элемент каталога недоступен. Обнови страницу.');
  if (settings[kind].some(item => item.name === entry.definition.name)) return;
  if (kind === 'profiles') {
    for (const dependencyKind of ['skills', 'mcp_servers']) {
      for (const name of entry.definition[dependencyKind] || []) {
        if (settings[dependencyKind].some(item => item.name === name)) continue;
        const dependency = agentCatalogEntry(dependencyKind, name);
        if (!dependency) throw new Error('В каталоге не найдено подключение: ' + name);
        agentImport(settings, dependencyKind, dependency);
      }
    }
  }
  settings[kind].push(agentCopy(entry.definition));
}
async function agentSave(settings, message) {
  if (!isOwner()) throw new Error('Для изменения команды нужны права владельца.');
  if (agentWorkspace.saving) throw new Error('Дождись сохранения предыдущего изменения.');
  const project = agentWorkspace.project;
  agentWorkspace.saving = true;
  $('#agent-workspace').querySelectorAll('button').forEach(button => button.disabled = true);
  try {
    await api(`/projects/${project}/agents`, 'PUT', agentPayload(settings));
    const updated = await api(`/projects/${project}/agents`);
    if (project === agentWorkspace.project) { agentWorkspace.settings = updated; agentRender(); }
    toast(message);
  } finally {
    agentWorkspace.saving = false;
    if (project === agentWorkspace.project) agentRender();
  }
}
function agentRouting() {
  const settings = agentCopy(agentWorkspace.settings);
  const choices = [['', 'Исполнитель проекта'], ...settings.profiles.map(item => [item.name, agentTitle('profiles', item)])];
  const fields = agentStages.map(([key, label]) => field(key, label, {type: 'select', choices, value: settings.assignments[key] || '', required: false})).join('') +
    '<p class="aw-form-note">Планирование и ревью запускаются в отдельных сессиях и используют только инструменты чтения.</p>' +
    field('max_delegation_depth', 'Глубина передачи задач', {type: 'number', value: settings.max_delegation_depth, min: 0, max: 4}) +
    field('max_delegated_tasks', 'Максимум подзадач во всём дереве', {type: 'number', value: settings.max_delegated_tasks, min: 0, max: 64}) +
    '<p class="aw-form-note">0 выключает передачу задач. Активное время, расходы и циклы исправлений общие для дерева.</p>';
  agentModal('Назначения и лимиты', fields, async values => {
    settings.assignments = Object.fromEntries(agentStages.filter(([key]) => values[key]).map(([key]) => [key, values[key]]));
    settings.max_delegation_depth = Number(values.max_delegation_depth);
    settings.max_delegated_tasks = Number(values.max_delegated_tasks);
    await agentSave(settings, 'Назначения сохранены');
  });
}
function agentCheckboxes(name, label, items, selected, kind) {
  return `<fieldset class="aw-checkboxes"><legend>${label}</legend>${items.length ? items.map(item => `<label><input type="checkbox" name="${name}" value="${esc(item.name)}" ${selected.includes(item.name) ? 'checked' : ''}><span><strong>${esc(agentTitle(kind, item))}</strong><small>${kind === 'mcp_servers' ? (item.read_only ? 'Только чтение' : 'Есть инструменты записи') : esc(item.description || item.name)}</small></span></label>`).join('') : `<p>Сначала добавь ${kind === 'skills' ? 'скилл' : 'MCP'} из каталога на странице агентов.</p>`}</fieldset>`;
}
function agentSelected(name) {
  return Array.from($('#modal-fields').querySelectorAll(`input[name="${name}"]:checked`), item => item.value);
}
// What the stage's agent is actually told: the factory's fixed rules and the role's own part.
async function agentPrompt(stage) {
  const preview = await api(`/projects/${agentWorkspace.project}/agents/preview?stage=${stage}`);
  const label = (agentStages.find(([key]) => key === stage) || [stage, stage])[1];
  modal('Промпт: ' + label, preview.sections.map(section => `<h3>${esc(section.title)}</h3><pre>${esc(section.text || '—')}</pre>`).join('') + '<p class="small">К этому добавляются текст задачи, история и diff конкретного запуска.</p>', async () => {});
  $('#submit-modal').hidden = true;
  $('#cancel-modal').textContent = 'Закрыть';
}
function agentModal(title, fields, callback) {
  modal(title, '<div class="aw-form">' + fields + '</div>', async values => {
    try { await callback(values); } catch (error) { toast(error.message); throw error; }
  });
  if (!isOwner()) {
    $('#modal-fields').querySelectorAll('input,select,textarea,button').forEach(control => control.disabled = true);
    $('#submit-modal').hidden = true;
    $('#cancel-modal').textContent = 'Закрыть';
  }
}
function agentEditor(kind, name) {
  const settings = agentCopy(agentWorkspace.settings), item = settings[kind].find(value => value.name === name);
  let fields = item ? `<p class="aw-form-note">Идентификатор: <code>${esc(item.name)}</code></p>` : field('name', 'Идентификатор', {placeholder: 'my-agent'});
  if (kind === 'profiles') {
    fields += field('provider', 'Исполнитель', {type: 'select', choices: [['project', 'Из настроек проекта'], ...providers], value: item?.provider || 'codex'}) +
      field('model', 'Модель', {type: 'select', choices: agentModelChoices(item?.provider || 'codex', item?.model), value: item?.model || '', required: false}) +
      `<div id="agent-custom-model" ${!item?.model || agentWorkspace.catalog.models.some(model => model.value === item.model && model.provider === item.provider) ? 'hidden' : ''}>${field('custom_model', 'Название своей модели', {value: item?.model || '', required: false})}</div>` +
      field('use_when', 'Когда назначать (планировщик выбирает роль по этой строке)', {value: item?.use_when || '', required: false, placeholder: 'Dockerfile, CI, compose, Helm'}) +
      field('instructions', 'Задача роли и инструкции', {type: 'textarea', value: item?.instructions || '', required: false}) +
      agentCheckboxes('skills', 'Скиллы роли', settings.skills, item?.skills || [], 'skills') +
      agentCheckboxes('mcp_servers', 'MCP роли', settings.mcp_servers, item?.mcp_servers || [], 'mcp_servers') +
      field('can_delegate', 'Может создавать подзадачи', {type: 'select', choices: boolChoices, value: String(item?.can_delegate ?? true)}) +
      '<p class="aw-form-note">Агент выбирает роли из команды проекта. Зависимые подзадачи получают уже принятые результаты из preprod.</p>';
  } else if (kind === 'skills') {
    fields += field('description', 'Когда применять', {value: item?.description || '', required: false}) + field('content', 'Инструкции скилла', {type: 'textarea', value: item?.content || ''});
  } else {
    const ready = item && agentCatalogEntry(kind, item.name);
    // A catalog server that reaches an outside system (Jira) still needs its address and token from the owner.
    const setup = ready && 'hosts' in ready.definition;
    fields += `<p class="aw-form-note">${setup ? 'Укажи адрес сервиса и секретное окружение в параметрах подключения.' : ready ? 'Готовое подключение работает внутри контейнера задачи. Дополнительная авторизация не нужна.' : 'Подключи свой сервер. Команда stdio должна быть установлена в образе исполнителя.'}</p>` +
      field('read_only', 'Доступ к данным', {type: 'select', choices: [['true', 'Только чтение'], ['false', 'Чтение и запись']], value: String(item?.read_only ?? true)}) +
      `<details class="aw-advanced" ${!item || (setup && !item.configured_env?.length) ? 'open' : ''}><summary>Параметры подключения</summary>` +
      field('transport', 'Тип подключения', {type: 'select', choices: [['http', 'HTTPS сервер'], ['stdio', 'Команда в контейнере']], value: item?.transport || 'http'}) +
      `<div id="agent-http-fields">${field('url', 'Адрес HTTPS сервера', {value: item?.url || '', required: false, placeholder: 'https://mcp.company.ru/mcp'})}${field('headers', 'Секретные заголовки: Имя: значение, каждый с новой строки', {type: 'textarea', required: false, placeholder: item?.configured_headers?.length ? 'Сохранены: ' + item.configured_headers.join(', ') + '. Пусто — сохранить.' : 'Authorization: Bearer …'})}</div>` +
      `<div id="agent-stdio-fields">${field('command', 'Команда в контейнере', {value: item?.command || '', required: false})}${field('args', 'Аргументы, каждый с новой строки', {type: 'textarea', value: (item?.args || []).join('\n'), required: false})}${field('env', 'Секретное окружение: ИМЯ=значение, каждое с новой строки', {type: 'textarea', required: false, placeholder: item?.configured_env?.length ? 'Сохранены: ' + item.configured_env.join(', ') + '. Пусто — сохранить.' : 'SERVICE_TOKEN=…'})}${field('hosts', 'Адреса, к которым нужен доступ: хост или хост:порт, каждый с новой строки', {type: 'textarea', value: (item?.hosts || []).join('\n'), required: false, placeholder: 'jira.company.ru:443'})}</div>` +
      field('enabled_tools', 'Разрешённые инструменты, каждый с новой строки', {type: 'textarea', value: (item?.enabled_tools || []).join('\n')}) + '</details>';
  }
  agentModal((item ? (isOwner() ? 'Настроить: ' : 'Просмотр: ') : 'Создать: ') + (item ? agentTitle(kind, item) : agentKinds[kind].singular), fields, async values => {
    if (!isOwner()) return;
    const value = {name: name || values.name};
    if (kind === 'profiles') {
      Object.assign(value, {provider: values.provider, model: values.model === '__custom__' ? values.custom_model.trim() || null : values.model || null, instructions: values.instructions, use_when: values.use_when.trim(), can_delegate: values.can_delegate === 'true', skills: agentSelected('skills'), mcp_servers: agentSelected('mcp_servers')});
    } else if (kind === 'skills') Object.assign(value, {description: values.description, content: values.content});
    else Object.assign(value, {transport: values.transport, url: values.transport === 'http' ? values.url.trim() || null : null, command: values.transport === 'stdio' ? values.command.trim() || null : null, args: values.transport === 'stdio' ? agentLines(values.args) : [], hosts: values.transport === 'stdio' ? agentLines(values.hosts) : [], enabled_tools: agentLines(values.enabled_tools), read_only: values.read_only === 'true', headers: values.transport === 'http' ? agentSecrets(values.headers, ':') : null, env: values.transport === 'stdio' ? agentSecrets(values.env, '=') : null});
    settings[kind] = item ? settings[kind].map(existing => existing.name === name ? value : existing) : settings[kind].concat(value);
    await agentSave(settings, 'Настройки сохранены');
  });
  if (kind === 'profiles') {
    const provider = $('#modal-fields [name="provider"]'), model = $('#modal-fields [name="model"]');
    const custom = () => {
      $('#agent-custom-model').hidden = model.value !== '__custom__';
      $('#modal-fields [name="custom_model"]').required = model.value === '__custom__';
    };
    if (item?.model && !agentWorkspace.catalog.models.some(model => model.value === item.model && model.provider === item.provider)) model.value = '__custom__';
    custom(); model.onchange = custom;
    provider.onchange = () => { model.innerHTML = agentModelChoices(provider.value).map(([value, title]) => `<option value="${esc(value)}">${esc(title)}</option>`).join(''); custom(); };
  }
  if (kind === 'mcp_servers') {
    const transport = $('#modal-fields [name="transport"]');
    const toggle = () => { $('#agent-http-fields').hidden = transport.value !== 'http'; $('#agent-stdio-fields').hidden = transport.value !== 'stdio'; };
    toggle(); transport.onchange = toggle;
  }
}
function agentModelChoices(provider, current) {
  const choices = [['', 'Модель подключения'], ...(agentWorkspace.catalog.models || []).filter(model => model.provider === provider).map(model => [model.value, model.label]), ['__custom__', 'Другая модель…']];
  if (current && !choices.some(([value]) => value === current)) choices.push([current, current]);
  return choices;
}
const agentLines = value => value.split('\n').map(line => line.trim()).filter(Boolean);
function agentSecrets(value, separator) {
  if (!value.trim()) return null;
  return Object.fromEntries(agentLines(value).map(line => {
    const index = line.indexOf(separator);
    if (index < 1 || !line.slice(index + 1).trim()) throw new Error('Укажи имя и значение каждого секрета.');
    return [line.slice(0, index).trim(), line.slice(index + 1).trim()];
  }));
}
function agentRemove(kind, name) {
  const settings = agentCopy(agentWorkspace.settings), item = settings[kind].find(value => value.name === name);
  const used = kind === 'profiles' ? agentStages.filter(([key]) => settings.assignments[key] === name).map(([, title]) => title) : settings.profiles.filter(profile => profile[kind].includes(name)).map(profile => agentTitle('profiles', profile));
  if (used.length) { toast('Сначала убери назначение: ' + used.join(', ')); return; }
  agentModal('Удалить: ' + agentTitle(kind, item), '<p>Настройка будет удалена из этого проекта. Каталог останется доступен.</p>', async () => {
    settings[kind] = settings[kind].filter(value => value.name !== name);
    await agentSave(settings, 'Удалено из проекта');
  });
  $('#submit-modal').textContent = 'Удалить';
}
