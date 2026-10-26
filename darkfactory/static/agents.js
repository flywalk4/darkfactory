/* Project-scoped agent workspace. Catalog definitions stay project-owned after import. */
const agentStages = [['planner', tr('Планирование')], ['writer', tr('Разработка')], ['reviewer', tr('Независимое ревью')], ['director', tr('Директор: остановки и чат')]];
const agentKinds = {
  profiles: {title: tr('Роли'), singular: tr('роль'), icon: '◈', empty: tr('Собери команду из готовых ролей'), hint: tr('Менеджер разбивает работу, аналитик уточняет решение, разработчик пишет код, ревьюер проверяет результат.')},
  skills: {title: tr('Скиллы'), singular: tr('скилл'), icon: '✦', empty: tr('Добавь знания и рабочие приёмы'), hint: tr('Скилл содержит инструкции. Подключи его к одной или нескольким ролям.')},
  mcp_servers: {title: 'MCP', singular: 'MCP', icon: '⌘', empty: tr('Дай агентам нужные инструменты'), hint: tr('Готовые подключения работают в контейнере задачи. Выбери MCP, затем подключи его к роли.')},
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
function agentProviderLabel(provider) { return provider === 'project' ? tr('Из настроек проекта') : provider; }
function agentTitle(kind, item) { return agentCatalogEntry(kind, item.name)?.title || item.name; }
function agentDescription(kind, item) {
  const entry = agentCatalogEntry(kind, item.name);
  return entry?.description || item.description || (kind === 'profiles' ? item.instructions : '') || tr('Своя настройка проекта');
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
    host.innerHTML = '<div class="aw-empty"><span>◈</span><h2>' + tr('Сначала добавь проект') + '</h2><p>' + tr('Команда агентов, скиллы и инструменты настраиваются отдельно для каждого проекта.') + '</p></div>';
    return;
  }
  if (force !== true && agentWorkspace.project === project.id && agentWorkspace.identity === identity && agentWorkspace.settings) return;
  const request = ++agentWorkspace.request;
  agentWorkspace.project = project.id; agentWorkspace.identity = identity; agentWorkspace.settings = null;
  host.innerHTML = '<p class="aw-loading" role="status">' + tr('Загружаем команду и каталог…') + '</p>';
  try {
    const [settings, catalog] = await Promise.all([api(`/projects/${project.id}/agents`), api(`/projects/${project.id}/agent-catalog`)]);
    if (request !== agentWorkspace.request) return;
    agentWorkspace.settings = settings; agentWorkspace.catalog = catalog;
    agentRender();
  } catch (error) {
    if (request !== agentWorkspace.request) return;
    host.innerHTML = '<div class="aw-empty"><h2>' + tr('Не удалось загрузить агентов') + '</h2><p class="aw-load-error"></p><button type="button" data-agent-retry>' + tr('Повторить') + '</button></div>';
    host.querySelector('.aw-load-error').textContent = error.message;
    host.querySelector('[data-agent-retry]').onclick = () => agentsPage(true);
    toast(error.message);
  }
}
function agentRender() {
  const host = $('#agent-workspace'), settings = agentWorkspace.settings, owner = isOwner();
  if (!host || !settings) return;
  const kind = agentWorkspace.tab, info = agentKinds[kind];
  host.innerHTML = `<div class="aw-heading"><div><div class="aw-eyebrow">${tr('КОМАНДА ПРОЕКТА')}</div><h1>${tr('Агенты')}</h1><p>${tr('Выбери роли, знания и инструменты. Фабрика сама передаст работу между агентами.')}</p></div><label class="aw-project">${tr('Проект')}<select id="agent-project">${projects.map(project => `<option value="${esc(project.id)}" ${project.id === agentWorkspace.project ? 'selected' : ''}>${esc(project.name)}</option>`).join('')}</select></label></div>
    ${!owner ? '<div class="aw-note">' + tr('Режим просмотра. Настройки команды меняет владелец проекта.') + '</div>' : ''}
    <section class="aw-flow" aria-label="${tr('Роли в процессе')}"><div class="aw-section-heading"><div><h2>${tr('Кто выполняет задачу')}</h2><p>${tr('Каждая роль работает в отдельной сессии. Подзадачи используют общий бюджет.')}</p></div>${owner ? '<button type="button" class="secondary" data-agent-routing>' + tr('Назначения и лимиты') + '</button>' : ''}</div><div class="aw-stages">${agentStages.map(([key, label], index) => {
      const item = settings.profiles.find(profile => profile.name === settings.assignments[key]);
      return `<div class="aw-stage"><span class="aw-step">0${index + 1}</span><div><small>${label}</small><strong>${item ? esc(agentTitle('profiles', item)) : tr('Исполнитель проекта')}</strong><span>${item ? esc(agentProviderLabel(item.provider)) + (item.model ? ' · ' + esc(item.model) : '') : tr('Из настроек проекта и пула')}</span><button type="button" class="quiet aw-prompt" data-agent-prompt="${key}">${tr('Показать промпт')}</button></div></div>`;
    }).join('')}</div><div class="aw-flow-footer"><span>${tr('Глубина передачи')}: <b>${settings.max_delegation_depth}</b></span><span>${tr('Подзадач в дереве')}: <b>${settings.max_delegated_tasks}</b></span><span>${tr('Слияние после проверок и ревью → preprod')}</span></div></section>
    <div class="aw-toolbar"><div class="aw-tabs" role="tablist" aria-label="${tr('Настройки агентов')}">${Object.entries(agentKinds).map(([key, value]) => `<button type="button" role="tab" aria-selected="${key === kind}" class="${key === kind ? 'active' : ''}" data-agent-tab="${key}">${value.title}<span>${settings[key].length}</span></button>`).join('')}</div><label class="aw-search"><span class="aw-sr">${tr('Поиск в ролях, скиллах или MCP')}</span><input type="search" id="agent-search" placeholder="${kind === 'profiles' ? tr('Поиск в ролях…') : kind === 'skills' ? tr('Поиск в скиллах…') : tr('Поиск в MCP…')}" value="${esc(agentWorkspace.query)}"></label></div>
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
  const query = agentWorkspace.query.trim().toLocaleLowerCase(LOCALE);
  const matches = (title, description, name) => `${title} ${description} ${name}`.toLocaleLowerCase(LOCALE).includes(query);
  const configured = settings[kind].filter(item => matches(agentTitle(kind, item), agentDescription(kind, item), item.name));
  const catalog = (agentWorkspace.catalog[kind] || []).filter(entry => matches(entry.title, entry.description, entry.definition.name));
  const usages = name => settings.profiles.filter(profile => (profile[kind] || []).includes(name)).map(profile => agentTitle('profiles', profile));
  host.innerHTML = `<section><div class="aw-section-heading"><div><h2>${kind === 'profiles' ? tr('Твоя команда') : kind === 'skills' ? tr('Скиллы проекта') : tr('Подключено к проекту')}</h2><p>${esc(info.hint)}</p></div></div>
    ${configured.length ? `<div class="aw-grid">${configured.map(item => {
      let meta = '';
      if (kind === 'profiles') meta = `<span>${esc(agentProviderLabel(item.provider))}</span><span>${tr('{n} скилл.', {n: item.skills.length})}</span><span>${item.mcp_servers.length} MCP</span>${item.can_delegate ? '<span>' + tr('Может делегировать') + '</span>' : ''}`;
      else if (kind === 'mcp_servers') meta = `<span>${item.read_only ? tr('Только чтение') : tr('Есть запись')}</span><span>${tr('{n} инструмент.', {n: item.enabled_tools.length})}</span><span>${esc(item.transport)}</span>`;
      else meta = `<span>${usages(item.name).length ? tr('Роли: {roles}', {roles: esc(usages(item.name).join(', '))}) : tr('Ещё не подключён к роли')}</span>`;
      return `<article class="aw-card"><div class="aw-card-top"><span class="aw-card-icon">${info.icon}</span><span class="aw-badge">${tr('В проекте')}</span></div><h3>${esc(agentTitle(kind, item))}</h3><p>${esc(agentDescription(kind, item))}</p><div class="aw-tags">${meta}</div><div class="aw-card-actions"><button type="button" class="secondary" data-agent-edit="${esc(item.name)}">${owner ? tr('Настроить') : tr('Посмотреть')}</button>${owner ? `<button type="button" class="quiet" aria-label="${tr('Удалить {name}', {name: esc(agentTitle(kind, item))})}" data-agent-remove="${esc(item.name)}">${tr('Удалить')}</button>` : ''}</div></article>`;
    }).join('')}</div>` : `<div class="aw-empty aw-empty-inline"><h3>${query ? tr('Ничего не найдено') : esc(info.empty)}</h3><p>${query ? tr('Попробуй другое слово.') : tr('Ниже — готовые варианты. Добавление сохранит их в этом проекте.')}</p></div>`}</section>
    <section class="aw-catalog"><div class="aw-section-heading"><div><div class="aw-eyebrow">${tr('ГОТОВО К РАБОТЕ')}</div><h2>${kind === 'profiles' ? tr('Каталог ролей') : kind === 'skills' ? tr('Каталог скиллов') : tr('Каталог MCP')}</h2><p>${tr('Готовые настройки можно менять после добавления.')}</p></div>${owner && kind === 'profiles' ? '<button type="button" data-agent-team>' + tr('Добавить команду') + '</button>' : ''}</div>
    <div class="aw-grid">${catalog.map(entry => {
      const exists = settings[kind].some(item => item.name === entry.definition.name);
      return `<article class="aw-card aw-catalog-card"><div class="aw-card-top"><span class="aw-card-icon">${info.icon}</span><span class="aw-badge aw-catalog-badge">${kind === 'mcp_servers' ? (entry.definition.read_only ? tr('Чтение') : tr('Чтение и запись')) : tr('Из каталога')}</span></div><h3>${esc(entry.title)}</h3><p>${esc(entry.description)}</p><div class="aw-card-actions">${owner ? `<button type="button" class="${exists ? 'secondary' : ''}" data-agent-install="${esc(entry.id)}" ${exists ? 'disabled' : ''}>${exists ? '✓ ' + tr('Добавлено') : '+ ' + tr('Добавить')}</button>` : '<span class="aw-readonly">' + tr('Добавляет владелец проекта') + '</span>'}</div></article>`;
    }).join('')}</div>${!catalog.length ? '<p class="aw-no-results">' + tr('В каталоге ничего не найдено.') + '</p>' : ''}</section>
    ${owner ? `<details class="aw-advanced"><summary>${tr('Свой {kind}: расширенная настройка', {kind: info.singular})}</summary><p>${tr('Для собственного профиля, инструкций или внешнего сервера.')}</p><button type="button" class="secondary" data-agent-custom>${tr('Создать {kind}', {kind: info.singular})}</button></details>` : ''}`;
  host.querySelectorAll('[data-agent-edit]').forEach(button => button.onclick = () => agentEditor(kind, button.dataset.agentEdit));
  host.querySelectorAll('[data-agent-remove]').forEach(button => button.onclick = () => agentRemove(kind, button.dataset.agentRemove));
  host.querySelectorAll('[data-agent-install]').forEach(button => button.onclick = async () => {
    if (!isOwner()) return;
    button.disabled = true;
    try {
      const entry = agentWorkspace.catalog[kind].find(item => item.id === button.dataset.agentInstall);
      const settings = agentCopy(agentWorkspace.settings);
      agentImport(settings, kind, entry);
      await agentSave(settings, tr('Добавлено в проект'));
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
      await agentSave(settings, tr('Команда добавлена. Назначения готовы'));
    } catch (error) { toast(error.message); agentRenderCards(); }
  });
}
function agentImport(settings, kind, entry) {
  if (!entry?.definition) throw new Error(tr('Элемент каталога недоступен. Обнови страницу.'));
  if (settings[kind].some(item => item.name === entry.definition.name)) return;
  if (kind === 'profiles') {
    for (const dependencyKind of ['skills', 'mcp_servers']) {
      for (const name of entry.definition[dependencyKind] || []) {
        if (settings[dependencyKind].some(item => item.name === name)) continue;
        const dependency = agentCatalogEntry(dependencyKind, name);
        if (!dependency) throw new Error(tr('В каталоге не найдено подключение: {name}', {name: name}));
        agentImport(settings, dependencyKind, dependency);
      }
    }
  }
  settings[kind].push(agentCopy(entry.definition));
}
async function agentSave(settings, message) {
  if (!isOwner()) throw new Error(tr('Для изменения команды нужны права владельца.'));
  if (agentWorkspace.saving) throw new Error(tr('Дождись сохранения предыдущего изменения.'));
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
  const choices = [['', tr('Исполнитель проекта')], ...settings.profiles.map(item => [item.name, agentTitle('profiles', item)])];
  const fields = agentStages.map(([key, label]) => field(key, label, {type: 'select', choices, value: settings.assignments[key] || '', required: false})).join('') +
    '<p class="aw-form-note">' + tr('Планирование и ревью запускаются в отдельных сессиях и используют только инструменты чтения.') + '</p>' +
    field('max_delegation_depth', tr('Глубина передачи задач'), {type: 'number', value: settings.max_delegation_depth, min: 0, max: 4}) +
    field('max_delegated_tasks', tr('Максимум подзадач во всём дереве'), {type: 'number', value: settings.max_delegated_tasks, min: 0, max: 64}) +
    '<p class="aw-form-note">' + tr('0 выключает передачу задач. Активное время, расходы и циклы исправлений общие для дерева.') + '</p>';
  agentModal(tr('Назначения и лимиты'), fields, async values => {
    settings.assignments = Object.fromEntries(agentStages.filter(([key]) => values[key]).map(([key]) => [key, values[key]]));
    settings.max_delegation_depth = Number(values.max_delegation_depth);
    settings.max_delegated_tasks = Number(values.max_delegated_tasks);
    await agentSave(settings, tr('Назначения сохранены'));
  });
}
function agentCheckboxes(name, label, items, selected, kind) {
  return `<fieldset class="aw-checkboxes"><legend>${label}</legend>${items.length ? items.map(item => `<label><input type="checkbox" name="${name}" value="${esc(item.name)}" ${selected.includes(item.name) ? 'checked' : ''}><span><strong>${esc(agentTitle(kind, item))}</strong><small>${kind === 'mcp_servers' ? (item.read_only ? tr('Только чтение') : tr('Есть инструменты записи')) : esc(item.description || item.name)}</small></span></label>`).join('') : `<p>${kind === 'skills' ? tr('Сначала добавь скилл из каталога на странице агентов.') : tr('Сначала добавь MCP из каталога на странице агентов.')}</p>`}</fieldset>`;
}
function agentSelected(name) {
  return Array.from($('#modal-fields').querySelectorAll(`input[name="${name}"]:checked`), item => item.value);
}
// What the stage's agent is actually told: the factory's fixed rules and the role's own part.
async function agentPrompt(stage) {
  const preview = await api(`/projects/${agentWorkspace.project}/agents/preview?stage=${stage}`);
  const label = (agentStages.find(([key]) => key === stage) || [stage, stage])[1];
  modal(tr('Промпт: {stage}', {stage: label}), preview.sections.map(section => `<h3>${esc(section.title)}</h3><pre>${esc(section.text || '—')}</pre>`).join('') + '<p class="small">' + tr('К этому добавляются текст задачи, история и diff конкретного запуска.') + '</p>', async () => {});
  $('#submit-modal').hidden = true;
  $('#cancel-modal').textContent = tr('Закрыть');
}
function agentModal(title, fields, callback) {
  modal(title, '<div class="aw-form">' + fields + '</div>', async values => {
    try { await callback(values); } catch (error) { toast(error.message); throw error; }
  });
  if (!isOwner()) {
    $('#modal-fields').querySelectorAll('input,select,textarea,button').forEach(control => control.disabled = true);
    $('#submit-modal').hidden = true;
    $('#cancel-modal').textContent = tr('Закрыть');
  }
}
function agentEditor(kind, name) {
  const settings = agentCopy(agentWorkspace.settings), item = settings[kind].find(value => value.name === name);
  let fields = item ? `<p class="aw-form-note">${tr('Идентификатор')}: <code>${esc(item.name)}</code></p>` : field('name', tr('Идентификатор'), {placeholder: 'my-agent'});
  if (kind === 'profiles') {
    fields += field('provider', tr('Исполнитель'), {type: 'select', choices: [['project', tr('Из настроек проекта')], ...providers], value: item?.provider || 'codex'}) +
      field('model', tr('Модель'), {type: 'select', choices: agentModelChoices(item?.provider || 'codex', item?.model), value: item?.model || '', required: false}) +
      `<div id="agent-custom-model" ${!item?.model || agentWorkspace.catalog.models.some(model => model.value === item.model && model.provider === item.provider) ? 'hidden' : ''}>${field('custom_model', tr('Название своей модели'), {value: item?.model || '', required: false})}</div>` +
      field('use_when', tr('Когда назначать (планировщик выбирает роль по этой строке)'), {value: item?.use_when || '', required: false, placeholder: 'Dockerfile, CI, compose, Helm'}) +
      field('instructions', tr('Задача роли и инструкции'), {type: 'textarea', value: item?.instructions || '', required: false}) +
      agentCheckboxes('skills', tr('Скиллы роли'), settings.skills, item?.skills || [], 'skills') +
      agentCheckboxes('mcp_servers', tr('MCP роли'), settings.mcp_servers, item?.mcp_servers || [], 'mcp_servers') +
      field('can_delegate', tr('Может создавать подзадачи'), {type: 'select', choices: boolChoices, value: String(item?.can_delegate ?? true)}) +
      '<p class="aw-form-note">' + tr('Агент выбирает роли из команды проекта. Зависимые подзадачи получают уже принятые результаты из preprod.') + '</p>';
  } else if (kind === 'skills') {
    fields += field('description', tr('Когда применять'), {value: item?.description || '', required: false}) + field('content', tr('Инструкции скилла'), {type: 'textarea', value: item?.content || ''});
  } else {
    const ready = item && agentCatalogEntry(kind, item.name);
    // A catalog server that reaches an outside system (Jira) still needs its address and token from the owner.
    const setup = ready && 'hosts' in ready.definition;
    fields += `<p class="aw-form-note">${setup ? tr('Укажи адрес сервиса и секретное окружение в параметрах подключения.') : ready ? tr('Готовое подключение работает внутри контейнера задачи. Дополнительная авторизация не нужна.') : tr('Подключи свой сервер. Команда stdio должна быть установлена в образе исполнителя.')}</p>` +
      field('read_only', tr('Доступ к данным'), {type: 'select', choices: [['true', tr('Только чтение')], ['false', tr('Чтение и запись')]], value: String(item?.read_only ?? true)}) +
      `<details class="aw-advanced" ${!item || (setup && !item.configured_env?.length) ? 'open' : ''}><summary>${tr('Параметры подключения')}</summary>` +
      field('transport', tr('Тип подключения'), {type: 'select', choices: [['http', tr('HTTPS сервер')], ['stdio', tr('Команда в контейнере')]], value: item?.transport || 'http'}) +
      `<div id="agent-http-fields">${field('url', tr('Адрес HTTPS сервера'), {value: item?.url || '', required: false, placeholder: 'https://mcp.company.ru/mcp'})}${field('headers', tr('Секретные заголовки: Имя: значение, каждый с новой строки'), {type: 'textarea', required: false, placeholder: item?.configured_headers?.length ? tr('Сохранены: {names}. Пусто — сохранить.', {names: item.configured_headers.join(', ')}) : 'Authorization: Bearer …'})}</div>` +
      `<div id="agent-stdio-fields">${field('command', tr('Команда в контейнере'), {value: item?.command || '', required: false})}${field('args', tr('Аргументы, каждый с новой строки'), {type: 'textarea', value: (item?.args || []).join('\n'), required: false})}${field('env', tr('Секретное окружение: ИМЯ=значение, каждое с новой строки'), {type: 'textarea', required: false, placeholder: item?.configured_env?.length ? tr('Сохранены: {names}. Пусто — сохранить.', {names: item.configured_env.join(', ')}) : 'SERVICE_TOKEN=…'})}${field('hosts', tr('Адреса, к которым нужен доступ: хост или хост:порт, каждый с новой строки'), {type: 'textarea', value: (item?.hosts || []).join('\n'), required: false, placeholder: 'jira.company.ru:443'})}</div>` +
      field('enabled_tools', tr('Разрешённые инструменты, каждый с новой строки'), {type: 'textarea', value: (item?.enabled_tools || []).join('\n')}) + '</details>';
  }
  agentModal(item ? (isOwner() ? tr('Настроить: {name}', {name: agentTitle(kind, item)}) : tr('Просмотр: {name}', {name: agentTitle(kind, item)})) : tr('Создать: {name}', {name: agentKinds[kind].singular}), fields, async values => {
    if (!isOwner()) return;
    const value = {name: name || values.name};
    if (kind === 'profiles') {
      Object.assign(value, {provider: values.provider, model: values.model === '__custom__' ? values.custom_model.trim() || null : values.model || null, instructions: values.instructions, use_when: values.use_when.trim(), can_delegate: values.can_delegate === 'true', skills: agentSelected('skills'), mcp_servers: agentSelected('mcp_servers')});
    } else if (kind === 'skills') Object.assign(value, {description: values.description, content: values.content});
    else Object.assign(value, {transport: values.transport, url: values.transport === 'http' ? values.url.trim() || null : null, command: values.transport === 'stdio' ? values.command.trim() || null : null, args: values.transport === 'stdio' ? agentLines(values.args) : [], hosts: values.transport === 'stdio' ? agentLines(values.hosts) : [], enabled_tools: agentLines(values.enabled_tools), read_only: values.read_only === 'true', headers: values.transport === 'http' ? agentSecrets(values.headers, ':') : null, env: values.transport === 'stdio' ? agentSecrets(values.env, '=') : null});
    settings[kind] = item ? settings[kind].map(existing => existing.name === name ? value : existing) : settings[kind].concat(value);
    await agentSave(settings, tr('Настройки сохранены'));
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
  const choices = [['', tr('Модель подключения')], ...(agentWorkspace.catalog.models || []).filter(model => model.provider === provider).map(model => [model.value, model.label]), ['__custom__', tr('Другая модель…')]];
  if (current && !choices.some(([value]) => value === current)) choices.push([current, current]);
  return choices;
}
const agentLines = value => value.split('\n').map(line => line.trim()).filter(Boolean);
function agentSecrets(value, separator) {
  if (!value.trim()) return null;
  return Object.fromEntries(agentLines(value).map(line => {
    const index = line.indexOf(separator);
    if (index < 1 || !line.slice(index + 1).trim()) throw new Error(tr('Укажи имя и значение каждого секрета.'));
    return [line.slice(0, index).trim(), line.slice(index + 1).trim()];
  }));
}
function agentRemove(kind, name) {
  const settings = agentCopy(agentWorkspace.settings), item = settings[kind].find(value => value.name === name);
  const used = kind === 'profiles' ? agentStages.filter(([key]) => settings.assignments[key] === name).map(([, title]) => title) : settings.profiles.filter(profile => profile[kind].includes(name)).map(profile => agentTitle('profiles', profile));
  if (used.length) { toast(tr('Сначала убери назначение: {roles}', {roles: used.join(', ')})); return; }
  agentModal(tr('Удалить: {name}', {name: agentTitle(kind, item)}), '<p>' + tr('Настройка будет удалена из этого проекта. Каталог останется доступен.') + '</p>', async () => {
    settings[kind] = settings[kind].filter(value => value.name !== name);
    await agentSave(settings, tr('Удалено из проекта'));
  });
  $('#submit-modal').textContent = tr('Удалить');
}
