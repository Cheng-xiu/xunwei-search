(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const connection = window.XunweiConnection;
  const ownerAI = (() => {
    const value = window.XUNWEI_DEPLOYMENT?.ownerAI;
    if (!value || typeof value.baseURL !== 'string' || typeof value.model !== 'string' || typeof value.apiKey !== 'string') return null;
    try {
      const url = new URL(value.baseURL);
      if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || !value.model.trim() || !value.apiKey.trim()) return null;
      return Object.freeze({ base_url: url.href.replace(/\/+$/, ''), model: value.model.trim(), api_key: value.apiKey.trim() });
    } catch (_) { return null; }
  })();
  let ownerChoice = 'shared';
  let ownerAppliedRevision = -1;
  let ownerCheckedRevision = -1;
  let ownerNeedsPersonalKey = false;
  let ownerApplyOnConnect = false;
  let modelOperation = 0;
  function ownerChoiceKey() { return `xunwei.model-choice.v1:${new URL('./', document.baseURI).pathname}:${connection.snapshot().apiBase}`; }
  function savedOwnerChoice() { try { return sessionStorage.getItem(ownerChoiceKey()) || ''; } catch (_) { return ''; } }
  function rememberOwnerChoice(value) { if (ownerAI) try { sessionStorage.setItem(ownerChoiceKey(), value); } catch (_) { /* Only a non-secret choice is retained. */ } }
  function beginModelOperation() { modelModeBusy = true; const token = ++modelOperation; updateModelModes(); updateSubmitButton(); return token; }
  function endModelOperation(token) { if (token === modelOperation) { modelModeBusy = false; updateConnection(); } }
  function presetConfigurationPending() {
    if (!ownerAI) return false;
    const backend = connection.snapshot();
    return ownerChoice === 'shared' ? ownerAppliedRevision !== backend.revision : ownerNeedsPersonalKey || backend.visitorSession && backend.sessionMode !== 'custom';
  }
  const platformNames = { bilibili: 'B 站', xiaohongshu: '小红书', zhihu: '知乎', wechat: '微信公众号', meituan: '美团', dianping: '大众点评', douyin: '抖音', tieba: '贴吧', douban: '豆瓣', github: 'GitHub', stackoverflow: 'Stack Overflow', v2ex: 'V2EX', csdn: 'CSDN', cnblogs: '博客园', reddit: 'Reddit', web: '公开网页', local: '本地资料' };
  const platformIcons = { bilibili: ['bili', 'B'], xiaohongshu: ['red', '小'], zhihu: ['blue', '知'], wechat: ['green', '微'], meituan: ['gold', '美'], dianping: ['orange', '评'], douyin: ['gray', '抖'], tieba: ['blue', '贴'], douban: ['green', '豆'], github: ['gray', 'G'], stackoverflow: ['orange', 'S'], v2ex: ['gray', 'V'], csdn: ['orange', 'C'], cnblogs: ['blue', '博'], reddit: ['red', 'R'], web: ['gray', '◎'], local: ['gray', '▤'] };
  const stageNames = { queued: '等待搜索', planning: '正在拆解问题', searching: '正在跨平台检索', fetching: '正在读取原文', reading: '正在读取原文', ranking: '正在核对匹配与证据', analyzing: 'AI 正在核对证据', evaluating: 'AI 正在核对证据', reporting: 'AI 正在汇报本轮进展', adapting: 'AI 正在调整下一轮搜索方向', waiting: '等待继续搜索', stopped: '搜索已停止', summarizing: 'AI 正在整理有据可查的总结', done: '搜索完成', error: '搜索未完成' };
  const terminalStates = new Set(['done', 'error', 'awaiting_user', 'stopped']);
  const resumableStates = new Set(['done', 'awaiting_user', 'stopped']);
  const state = { config: null, job: null, filter: 'all', views: 'all', busy: false, busyMode: 'search', activeJobId: '', stopRequested: false, sitesDirty: false, selectedPlatforms: new Set($$('.platform-chip.selected').map(button => button.dataset.platform)), platformSelectionEdited: false, pollToken: 0, lastResults: '', lastAISummary: '', lastRounds: '', lastProgressReport: '', displayedReport: null, previousReadyReport: null, reportJobId: '', reportClockKey: '', reportStartedAt: 0, summaryPending: false, summaryError: '', history: [], library: [] };
  let toastTimer;
  let initialized = false;
  let offlinePlatforms = [];
  let requestedModelMode;
  let modelModeBusy = false;
  let searchEngineItems = [];
  let engineSelectionSupported = false;
  let engineDraftIds = null;
  const engineNames = { baidu: '百度', bing: '必应 Bing', google: 'Google', yandex: 'Yandex', duckduckgo: 'DuckDuckGo', tavily: 'Tavily', brave: 'Brave Search', searxng: 'SearXNG' };
  const actionNames = { search: '搜索公开线索', search_site: '搜索指定网站', search_videos: '搜索视频', inspect: '读取入口', read_replies: '读取公开回复' };
  const tasks = new Map();
  let taskEpoch = 0;
  let submissionBusy = false;
  let serverTaskLimit = 0;
  let taskListing = false;
  let composerVersion = 0;

  function node(tag, className, content) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (content !== undefined && content !== null) element.textContent = String(content);
    return element;
  }

  function safeURL(value) {
    try {
      const url = new URL(String(value));
      if (url.protocol === 'https:' || url.protocol === 'http:') return url.href;
    } catch (_) { /* An absent or unsafe URL is not rendered as a link. */ }
    return null;
  }

  function sourceLink(text, url, className) {
    const safe = safeURL(url);
    const element = node(safe ? 'a' : 'span', className, text);
    if (safe) {
      element.href = safe;
      element.target = '_blank';
      element.rel = 'noopener noreferrer';
    }
    return element;
  }

  function toggle(element, visible) { element.classList.toggle('hidden', !visible); }

  function notice(selector, message, success = false) {
    const element = $(selector);
    element.textContent = message || '';
    element.classList.toggle('success', success);
    toggle(element, Boolean(message));
  }

  function toast(message) {
    clearTimeout(toastTimer);
    $('#toast').textContent = message;
    toggle($('#toast'), true);
    toastTimer = setTimeout(() => toggle($('#toast'), false), 4000);
  }

  async function api(path, options = {}) {
    return connection.request(path, options);
  }

  function formatDate(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(date);
  }

  function empty(title, description) {
    const element = node('div', 'empty-state');
    const orbit = node('div', 'empty-orbit');
    orbit.setAttribute('aria-hidden', 'true');
    orbit.append(node('span', '', '⌕'), node('i'), node('b'));
    element.append(orbit, node('h3', '', title), node('p', '', description));
    return element;
  }

  function showView(name) {
    const names = { search: '探索搜索', library: localBackend() ? '本地资料库' : '资料库', history: '搜索记录' };
    if (!names[name]) return;
    $$('.view').forEach(element => toggle(element, element.id === `${name}-view`));
    $$('[data-view]').forEach(button => {
      button.classList.toggle('active', button.dataset.view === name);
      if (button.dataset.view === name) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    $('#breadcrumb-current').textContent = names[name];
    if (name === 'library') loadLibrary();
    if (name === 'history') loadHistory();
  }

  function localBackend() {
    const backend = connection.snapshot();
    return backend.mode === 'local' && (!backend.apiBase || new URL(backend.apiBase).origin === location.origin);
  }

  function updateConnection() {
    const backend = connection.snapshot();
    const local = localBackend();
    const configured = backend.connected && Boolean(state.config?.has_api_key);
    $('#connection-pill').classList.toggle('connected', configured);
    $('#connection-pill span').textContent = !backend.connected ? '未连接' : ownerAI && ownerAppliedRevision === backend.revision ? '站主 API' : backend.visitorSession && (state.config?.api_mode || backend.sessionMode) === 'shared' ? '站主 API' : configured ? 'AI 已配置' : '配置 AI';
    $('#connection-pill').title = !backend.connected ? '先连接搜索服务，再配置模型。' : configured ? `当前模型：${state.config.model || '未设置'}。点击查看设置。` : '点击配置 AI 服务';
    $('#open-backend').textContent = backend.connected ? '服务已连接' : '连接服务';
    $('#open-backend').title = backend.connected ? backend.apiBase || location.origin : '连接你自己的寻微搜索服务';
    $('#deployment-tag').textContent = backend.mode === 'pages' ? 'PAGES / 01' : 'LOCAL / 01';
    $('#deployment-note').replaceChildren(document.createTextNode(backend.connected ? local ? '在本机运行' : '已连接搜索服务' : '尚未连接搜索服务'), node('small', '', backend.connected ? local ? 'AI 密钥保存在本机' : 'AI 密钥保存在搜索服务上' : '可先手动打开平台搜索'));
    $('[data-view="library"]').childNodes.forEach(child => { if (child.nodeType === 3 && child.textContent.trim()) child.textContent = local ? '本地资料库' : '资料库'; });
    $('#library-view h1').replaceChildren(document.createTextNode(local ? '本地' : ''), node('em', '', '资料库'));
    $('#library-view .page-intro p').textContent = `保存你已获得的帖子原文，在之后的搜索中一起查找。资料保存在${local ? '本机' : '所连接的搜索服务上'}。`;
    $('#settings-dialog .modal-description').textContent = backend.visitorSession ? '自配 API 密钥会发送至连接的搜索服务，仅用于你的独立访客会话，不会修改站主或其他访客的配置。会话到期后需重新连接；AI 会接收搜索问题及相关原文。' : `接入兼容 OpenAI 格式的 AI 服务。密钥保存在${local ? '本机' : '所连接的搜索服务上'}，留空可保留已有密钥。开启 AI 理解会将搜索问题、相关检索结果及导入原文发送至配置的 AI 服务。`;
    $('#import-dialog .modal-footer .subtle').textContent = local ? '内容保存在本机资料库' : '内容保存在连接的搜索服务';
    if (!$('#library-view').classList.contains('hidden')) $('#breadcrumb-current').textContent = local ? '本地资料库' : '资料库';
    platformNames.local = local ? '本地资料' : '导入资料';
    toggle($('#backend-banner'), !backend.connected);
    $('#backend-banner-copy').textContent = ownerAI && backend.mode === 'pages' ? '站主模型配置已提供，选择“使用站主 API”可查看自动填写的设置。智能搜索仍需连接搜索服务；当前只能手动打开搜索入口。' : backend.mode === 'pages' ? '这是 GitHub Pages 静态界面。站主 API 与自配 API 都需要连接搜索服务；当前无法确认站主是否提供额度。可先手动打开平台搜索。' : '搜索服务尚未连接。请确认本机程序已启动，或填写你自己的服务地址；也可以先手动打开平台搜索入口。';
    if (!state.busy) $('#search-button span').textContent = backend.connected ? '开始搜索' : '连接后搜索';
    renderOfflineLinks();
    updateModelModes();
    updateAdaptiveControls();
    updateConcurrencyControl();
    renderTasks();
  }

  function updateModelModes() {
    const backend = connection.snapshot();
    const mode = ownerAI ? ownerChoice : state.config?.api_mode || backend.sessionMode || 'custom';
    const available = Boolean(ownerAI) || backend.connected && backend.visitorSession && (state.config?.shared_available ?? backend.sharedAvailable);
    toggle($('#model-mode-panel'), Boolean(ownerAI) || backend.mode === 'pages' || backend.publicMode);
    $$('[data-model-mode]').forEach(button => {
      const selected = (Boolean(ownerAI) || backend.connected) && button.dataset.modelMode === mode;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
      button.disabled = anyTaskActive() || modelModeBusy || (backend.connected && button.dataset.modelMode === 'shared' && !available);
      button.title = backend.connected && button.dataset.modelMode === 'shared' && !available ? '该服务尚未向此连接开放站主 API。' : '';
    });
    $('#shared-mode-description').textContent = ownerAI ? ownerAppliedRevision === backend.revision && backend.connected ? '站主预置已保存至当前搜索服务' : '自动填入站主公开提供的模型配置' : !backend.connected ? '需要连接服务；尚未确认站主是否开放' : available ? '站主已开放，无需填写模型密钥' : '此服务尚未开放站主 API';
    $('#custom-mode-description').textContent = ownerAI && mode === 'custom' && backend.visitorSession && backend.sessionMode === 'shared' ? '当前仍为服务端站主模式，点击切换' : !backend.connected ? '连接服务后，填写自己的模型与密钥' : backend.visitorSession ? mode === 'custom' && state.config?.has_api_key && !ownerNeedsPersonalKey ? '当前会话已配置个人模型' : '密钥与设置只属于你的访客会话' : '使用当前服务上的个人 API 配置';
    $('#visitor-session-status').textContent = backend.connected && backend.visitorSession ? `独立访客会话${backend.expiresAt ? ` · 至 ${formatDate(backend.expiresAt)}` : ''}` : '';
    $('#model-mode-note').textContent = ownerAI ? !backend.connected ? '站主预置可先查看；两种方式都需要连接搜索服务后才能智能搜索。' : ownerNeedsPersonalKey ? '服务仍保留上次站主配置。填写并保存个人密钥后才会改用个人 API；旧个人密钥无法从服务读回。' : ownerAppliedRevision === backend.revision ? '已应用站主预置。切换个人 API 后请填写自己的密钥；不会自动恢复之前的密钥。' : '当前服务已有的模型配置会保留；明确点击“使用站主 API”或保存站主设置才会替换。' : !backend.connected ? '两种方式均需要连接搜索服务。静态页面本身不提供 AI 额度。' : backend.visitorSession ? '你的历史、资料与 API 配置按访客会话隔离。API 密钥由连接的服务代为调用，请使用你信任的服务。' : '当前是私人服务连接；只有提供独立访客会话的公开服务才可选择站主 API。';
    toggle($('#settings-model-control'), Boolean(ownerAI) || backend.visitorSession);
    $('#settings-model-mode').value = mode;
    $('#settings-model-mode').disabled = anyTaskActive() || modelModeBusy;
    $('#settings-model-mode option[value="shared"]').disabled = !available;
    const readonly = state.config?.ai_config_readonly === true || (backend.visitorSession && backend.sessionMode === 'shared');
    const presetSelected = Boolean(ownerAI) && mode === 'shared';
    toggle($('#custom-ai-settings'), !readonly || presetSelected);
    $$('#custom-ai-settings input').forEach(input => { input.disabled = readonly && !presetSelected; input.readOnly = presetSelected; });
    toggle($('#clear-api').closest('label'), !readonly && !presetSelected);
    $('#clear-api').disabled = readonly || presetSelected;
    toggle($('#test-ai'), !readonly);
    $('#test-ai').disabled = modelModeBusy || Boolean(ownerAI && !backend.connected);
    $('#save-settings').disabled = modelModeBusy || Boolean(ownerAI && anyTaskActive());
    $('#save-settings').textContent = ownerAI && !backend.connected ? '连接后保存' : '保存设置';
    toggle($('#shared-model-info'), readonly || presetSelected || ownerNeedsPersonalKey);
    $('#shared-model-info').textContent = presetSelected ? !backend.connected ? '已自动填入站主公开预置；尚未连接搜索服务，也未保存或测试。' : ownerAppliedRevision === backend.revision ? '已将站主预置保存到当前服务。公开访客使用自己的隔离配置，不修改服务器的站主设置。' : '已填入站主预置。当前服务已有配置时，保存会替换其 AI 地址、模型与密钥。' : ownerNeedsPersonalKey ? '已清空表单中的站主密钥。请输入并保存自己的密钥后再搜索；留空不会自动恢复旧个人密钥。' : readonly ? `当前使用站主提供的模型${state.config?.model ? `：${state.config.model}` : ''}。站主密钥不会显示或提供修改入口；切换到“自己配置 API”可使用个人模型。` : '';
  }

  async function changeModelMode(mode, showSettings = false) {
    if (!['shared','custom'].includes(mode) || modelModeBusy || anyTaskActive()) return;
    if (ownerAI) { await changePresetMode(mode, showSettings); return; }
    const backend = connection.snapshot();
    if (!backend.connected) { requestedModelMode = mode; openBackend(); return; }
    if (!backend.visitorSession) { if (mode === 'custom') openSettings(); return; }
    const revision = backend.revision;
    modelModeBusy = true;
    notice('#model-mode-feedback', '正在切换模型 API…');
    updateModelModes();
    try {
      await connection.setModelMode(mode);
      await loadConfig();
      if (connection.snapshot().revision !== revision) return;
      notice('#model-mode-feedback', mode === 'shared' ? '已使用站主 API。' : '已切换至个人 API，仅影响你的访客会话。', true);
      if ($('#settings-dialog').open) fillSettings(state.config || {});
      else if (showSettings && mode === 'custom') await openSettings();
    } catch (error) {
      if (error.code !== 'BACKEND_CHANGED') { notice('#model-mode-feedback', error.message); if ($('#settings-dialog').open) notice('#settings-feedback', error.message); }
    } finally { modelModeBusy = false; updateModelModes(); }
  }

  async function applyOwnerPreset(explicit = false) {
    const backend = connection.snapshot();
    if (!ownerAI || ownerChoice !== 'shared' || !backend.connected || modelModeBusy || anyTaskActive()) return false;
    if (!explicit && state.config?.has_api_key !== false) return false;
    const revision = backend.revision;
    const operation = beginModelOperation();
    try {
      if (backend.visitorSession && backend.sessionMode !== 'custom') await connection.setModelMode('custom');
      if (connection.snapshot().revision !== revision || !connection.snapshot().connected) return false;
      if (anyTaskActive()) throw new Error('当前有搜索或总结任务，请结束后再应用站主配置。');
      const config = await api('/api/config', { method: 'PUT', body: { ...ownerAI } });
      if (connection.snapshot().revision !== revision) return false;
      state.config = config; ownerAppliedRevision = revision; ownerNeedsPersonalKey = false;
      rememberOwnerChoice('shared');
      notice('#model-mode-feedback', '站主预置已保存至当前搜索服务；尚未发起 AI 调用。', true);
      return true;
    } catch (error) {
      if (connection.snapshot().revision === revision && error.code !== 'BACKEND_CHANGED') { notice('#model-mode-feedback', error.message); notice('#settings-feedback', error.message); }
      return false;
    } finally { endModelOperation(operation); }
  }

  async function changePresetMode(mode, showSettings) {
    const backend = connection.snapshot();
    const revision = backend.revision;
    const previousChoice = ownerChoice;
    ownerChoice = mode;
    if (mode === 'shared') {
      if (backend.connected) await applyOwnerPreset(true);
      else rememberOwnerChoice('shared');
    } else {
      ownerApplyOnConnect = false;
      ownerNeedsPersonalKey ||= backend.connected && ownerAppliedRevision === revision;
      if (backend.connected && backend.visitorSession && backend.sessionMode !== 'custom') {
        const operation = beginModelOperation();
        try { await connection.setModelMode('custom'); state.config = await api('/api/config'); }
        catch (error) { if (connection.snapshot().revision === revision) { ownerChoice = previousChoice; if (error.code !== 'BACKEND_CHANGED') notice('#model-mode-feedback', error.message); } return; }
        finally { endModelOperation(operation); }
      }
      rememberOwnerChoice(ownerNeedsPersonalKey ? 'custom_pending' : 'custom');
    }
    if (connection.snapshot().revision !== revision) return;
    updateConnection();
    if ($('#settings-dialog').open) fillSettings(state.config || {});
    else if (showSettings) await openSettings();
  }

  function openBackend() {
    const backend = connection.snapshot();
    $('#backend-url').value = backend.apiBase || (backend.mode === 'local' ? location.origin : '');
    $('#backend-token').value = '';
    $('#clear-backend-token').checked = false;
    $('#backend-token-state').textContent = backend.visitorSession ? '访客会话自动管理，无需填写' : backend.hasToken ? '当前会话已保存' : '私人服务需要时填写';
    notice('#backend-feedback', backend.connected ? '服务已通过健康检查和访问验证。' : backend.message && backend.message !== '未连接搜索服务' ? backend.message : '', backend.connected);
    $('#backend-dialog').showModal();
  }

  function ensureBackend() {
    if (connection.snapshot().connected) return true;
    openBackend();
    return false;
  }

  async function saveBackend(event) {
    event.preventDefault();
    if (!$('#backend-form').reportValidity()) return;
    $('#save-backend').disabled = true;
    notice('#backend-feedback', '正在验证健康状态、访问权限与平台目录…');
    try {
      await connection.connect($('#backend-url').value, $('#clear-backend-token').checked ? '' : $('#backend-token').value, !$('#clear-backend-token').checked, { mode: ownerAI ? 'custom' : requestedModelMode });
      requestedModelMode = undefined;
      $('#backend-token').value = '';
      $('#backend-dialog').close();
      toast(connection.snapshot().message);
    } catch (error) { if (error.code !== 'BACKEND_CHANGED') notice('#backend-feedback', error.message); }
    finally { $('#save-backend').disabled = false; }
  }

  function resetBackendView() {
    modelOperation += 1; modelModeBusy = false;
    ownerAppliedRevision = -1; ownerNeedsPersonalKey = false;
    taskEpoch += 1;
    tasks.forEach(entry => { entry.generation += 1; });
    tasks.clear();
    submissionBusy = false;
    serverTaskLimit = 0;
    taskListing = false;
    state.pollToken += 1;
    Object.assign(state, { config: null, job: null, activeJobId: '', stopRequested: false, lastResults: '', lastAISummary: '', lastRounds: '', lastNavigation: '', summaryPending: false, summaryError: '', history: [], library: [], sitesDirty: false, filter: 'all', views: 'all' });
    resetProgressReport();
    renderSiteRows([]);
    setBusy(false);
    ['settings-dialog','import-dialog'].forEach(id => { if ($(`#${id}`).open) $(`#${id}`).close(); });
    $('#settings-form').reset();
    $('#import-form').reset();
    ['api-key','tavily-key','brave-key'].forEach(id => { $(`#${id}`).value = ''; });
    notice('#model-mode-feedback', '');
    engineSelectionSupported = false;
    engineDraftIds = null;
    searchEngineItems = [];
    $('#views-filter').value = 'all';
    $$('[data-filter]').forEach(button => { button.classList.toggle('active', button.dataset.filter === 'all'); button.setAttribute('aria-pressed', String(button.dataset.filter === 'all')); });
    ['export-results','result-tools','answer-summary','ai-summary-card','rounds-card','job-status-panel','progress-panel'].forEach(id => toggle($(`#${id}`), false));
    $('#rounds-timeline').replaceChildren();
    $('#navigation-leads').replaceChildren();
    $('#navigation-details').open = false;
    toggle($('#navigation-card'), false);
    $('#search-warnings').replaceChildren();
    $('#result-count').textContent = '0';
    $('#library-count').textContent = '0';
    $('#recent-history').replaceChildren(node('p', 'subtle', '连接服务后读取搜索记录'));
    $('#history-list').replaceChildren(empty('搜索记录来自连接的服务', '连接后可读取该服务保存的搜索记录。'));
    $('#library-list').replaceChildren(empty('资料保存在连接的服务上', '连接后可读取或导入资料。'));
    $('#results').replaceChildren(empty('从一个具体的问题开始', '连接搜索服务进行智能检索，也可以先输入关键词，手动打开平台搜索。'));
    $('#provider-status').replaceChildren(node('p', 'subtle', '尚未连接；未执行检索'));
    $('#search-plan').replaceChildren(node('p', 'plan-intent', '搜索后会展示问题拆解、跨平台检索方向和可核对的证据。'));
    notice('#search-notice', '');
  }

  async function refreshBackendData() {
    const revision = connection.snapshot().revision;
    await Promise.allSettled([loadConfig(), loadHistory(), loadLibrary(), loadPlatforms(), loadSearchEngines(), loadTasks()]);
    if (revision !== connection.snapshot().revision) return;
    if (ownerAI && connection.snapshot().connected && ownerCheckedRevision !== revision) {
      ownerCheckedRevision = revision;
      let explicit = ownerApplyOnConnect; ownerApplyOnConnect = false;
      const saved = savedOwnerChoice();
      if (!explicit) {
        if (saved === 'custom' || saved === 'custom_pending') ownerChoice = 'custom';
        ownerNeedsPersonalKey = saved === 'custom_pending';
        if (saved === 'shared') { ownerChoice = 'shared'; explicit = true; }
        else if (state.config?.has_api_key && connection.snapshot().sessionMode !== 'shared') { ownerChoice = 'custom'; rememberOwnerChoice(ownerNeedsPersonalKey ? 'custom_pending' : 'custom'); }
        else if (ownerChoice === 'shared' && connection.snapshot().visitorSession && connection.snapshot().sessionMode === 'shared') explicit = true;
      }
      await applyOwnerPreset(explicit);
    }
    updateConnection();
  }

  function renderOfflineLinks() {
    renderEngineLinks();
    if (connection.snapshot().connected || state.job) return;
    const query = $('#query').value.trim();
    if (!query) { renderNativeLinks([]); return; }
    let sites = [];
    try { sites = collectSites(); } catch (_) { /* Incomplete site fields do not create links. */ }
    renderNativeLinks([...offlinePlatforms.filter(item => state.selectedPlatforms.has(item.id) && item.id !== 'web' && item.search_url).map(item => ({
      platform: item.id, label: item.search_label || `${item.label}搜索`, query,
      url: item.search_url.replace('{query}', encodeURIComponent(query))
    })), ...sites.filter(site => site.search_url).map(site => ({ label: `${site.name || site.domain}站内搜索`, query, url: site.search_url.replace('{query}', encodeURIComponent(query)) }))]);
  }

  async function loadConfig() {
    try { state.config = await api('/api/config'); updateConnection(); if (!state.sitesDirty) renderSiteRows(state.config.custom_sites || []); renderEngineChoices(); renderEngineLinks(); }
    catch (error) { if (error.code === 'BACKEND_CHANGED') return; $('#connection-pill span').textContent = '服务未连接'; notice('#search-notice', error.message); }
  }

  function selectedEngineIds(configured = state.config?.search_engines) {
    if (Array.isArray(configured) && configured.length) return new Set(configured.filter(id => searchEngineItems.some(item => item.id === id)));
    return new Set(searchEngineItems.filter(item => item.available === true || (!engineSelectionSupported && ((item.id === 'tavily' && state.config?.has_tavily_key) || (item.id === 'brave' && state.config?.has_brave_key) || (item.id === 'searxng' && state.config?.searxng_url)))).map(item => item.id));
  }

  async function loadSearchEngines() {
    try {
      const data = await connection.searchEngines();
      searchEngineItems = Array.isArray(data.items) ? data.items : [];
      engineSelectionSupported = data.selection_supported === true;
      searchEngineItems.forEach(item => { engineNames[item.id] = item.label || engineNames[item.id]; });
      renderEngineChoices();
      renderEngineLinks();
    } catch (error) { if (error.code !== 'BACKEND_CHANGED' && error.status !== 401) notice('#search-notice', error.message); }
  }

  function renderEngineChoices() {
    const container = $('#search-engine-options');
    const selected = engineDraftIds || selectedEngineIds();
    container.replaceChildren();
    searchEngineItems.forEach(item => {
      const label = node('label', 'engine-option');
      const input = node('input');
      input.type = 'checkbox';
      input.value = item.id;
      input.name = 'search_engine';
      input.checked = selected.has(item.id);
      input.disabled = !engineSelectionSupported;
      input.dataset.engine = item.id;
      const copy = node('span');
      const free = item.access !== 'api';
      const configured = item.configured || (item.id === 'tavily' && state.config?.has_tavily_key) || (item.id === 'brave' && state.config?.has_brave_key) || (item.id === 'searxng' && state.config?.searxng_url);
      copy.append(node('strong', '', item.label), node('small', '', free ? '免费网页检索 · 可能受限' : configured ? '自配服务 · 已有配置' : '自配服务 · 需填写下方配置'));
      label.title = item.description || '';
      label.append(input, copy);
      container.append(label);
    });
    $('#search-engine-hint').textContent = !connection.snapshot().connected ? '连接搜索服务后可保存引擎选择。未连接时可使用下方的手动搜索入口。' : !engineSelectionSupported ? '当前服务未提供搜索引擎目录，使用兼容默认值；升级服务后可保存选择。其他设置仍可使用。' : '默认启用五个免费引擎及已配置的搜索 API。勾选只代表尝试使用；实际可用性以每轮检索状态为准。';
  }

  function renderEngineLinks() {
    const query = state.job?.query || $('#query').value.trim();
    let customSites = state.job?.custom_sites || [];
    if (!state.job) { try { customSites = collectSites(); } catch (_) { customSites = []; } }
    const ids = selectedEngineIds(state.job?.search_engines || state.config?.search_engines);
    const engines = searchEngineItems.filter(item => ids.has(item.id));
    const links = connection.engineLinks({ query, engines, platforms: offlinePlatforms, selectedPlatforms: state.job?.platforms || Array.from(state.selectedPlatforms), customSites });
    const container = $('#engine-links');
    const expanded = new Set($$('details[open]', container).map(item => item.dataset.engine));
    container.replaceChildren();
    engines.forEach(engine => {
      const items = links.filter(link => link.engine === engine.id);
      if (!items.length) return;
      const group = node('details', 'manual-engine-group');
      group.dataset.engine = engine.id;
      group.open = expanded.has(engine.id);
      const summary = node('summary');
      summary.append(node('strong', '', engine.label), node('span', '', `${items.length} 个搜索范围`));
      const list = node('div', 'native-links');
      items.forEach(link => { const element = sourceLink(`${link.scope} ↗`, link.url, 'native-link'); element.title = link.query; list.append(element); });
      group.append(summary, list);
      container.append(group);
    });
    toggle($('#engine-links-section'), container.childElementCount > 0);
  }

  async function loadPlatforms() {
    try {
      const data = await api('/api/platforms');
      if (!Array.isArray(data.items)) return;
      const container=$('.platform-options');
      const imports=$('#import-platform');
      const previousImport=imports.value;
      data.items.forEach(item => {
        if (typeof item.id !== 'string' || !/^[a-z][a-z0-9_-]{0,63}$/.test(item.id)) return;
        if (item.label) platformNames[item.id] = item.label;
        let chip = $$('.platform-chip',container).find(button => button.dataset.platform === item.id);
        if (!chip) {
          chip=node('button','platform-chip');chip.type='button';chip.dataset.platform=item.id;
          const [className,symbol]=platformIcons[item.id] || ['gray','◎'];
          chip.append(node('b',`platform-dot ${className}`,symbol),node('span','platform-name',item.label || platformNames[item.id] || item.id),node('span','platform-check','✓'));
          container.append(chip);
          if (!state.platformSelectionEdited) state.selectedPlatforms.add(item.id);
        }
        chip.title = `${item.description || ''}${item.access === 'public_index' ? ' · 通过公开网页索引检索' : item.access === 'public_api' ? ' · 公开接口与网页索引' : ''}`;
        if (!Array.from(imports.options).some(option => option.value === item.id)) {const option=node('option','',item.label || platformNames[item.id] || item.id);option.value=item.id;imports.append(option);}
      });
      if (Array.from(imports.options).some(option => option.value === previousImport)) imports.value=previousImport;
      syncPlatformSelection();
      if (state.job) renderResults();
      renderOfflineLinks();
    } catch (_) { /* Static platform labels remain available with older local servers. */ }
  }

  function syncPlatformSelection() {
    $$('.platform-chip').forEach(button => {const selected=state.selectedPlatforms.has(button.dataset.platform);button.classList.toggle('selected',selected);button.setAttribute('aria-pressed',String(selected));});
  }

  function selectPlatform(button) {
    if (!button?.dataset.platform) return;
    const id=button.dataset.platform;
    composerVersion += 1;
    state.platformSelectionEdited=true;
    if (state.selectedPlatforms.has(id)) state.selectedPlatforms.delete(id);
    else state.selectedPlatforms.add(id);
    syncPlatformSelection();
    renderOfflineLinks();
  }

  function searchDepth() {const value=$('#search-depth').value;return ['quick','deep','research'].includes(value) ? value : 'deep';}

  function updateDepthDescription() {
    const descriptions={quick:'快速：用较少的检索请求寻找直接线索。',deep:'深入：扩大每轮的关键词和来源覆盖。',research:'穷尽线索：尽量追踪更多公开来源与稀有表达。'};
    $('#depth-description').textContent=`${descriptions[searchDepth()]}档位控制每轮覆盖，轮数与人工停止独立。`;
  }

  function siteRow(site = {}) {
    const row = node('div', 'custom-site-row');
    const fields = [['name', '网站名称（可选）', '例如：学校论坛', 80], ['domain', '网站域名', 'example.org', 253], ['search_url', '站内搜索模板（可选）', 'https://example.org/search?q={query}', 2000]];
    fields.forEach(([key, label, placeholder, maxLength]) => {
      const field = node('label', `site-field site-field-${key}`, label);
      const input = node('input');
      input.type = 'text';
      input.dataset.field = key;
      input.placeholder = placeholder;
      input.maxLength = maxLength;
      input.value = typeof site[key] === 'string' ? site[key] : '';
      input.autocomplete = 'off';
      input.addEventListener('input', () => { state.sitesDirty = true; notice('#custom-sites-feedback', ''); renderOfflineLinks(); });
      field.append(input);
      row.append(field);
    });
    const remove = node('button', 'remove-site', '移除');
    remove.type = 'button';
    remove.setAttribute('aria-label', '移除这个指定网站');
    remove.addEventListener('click', () => { row.remove(); state.sitesDirty = true; updateSiteCount(); renderOfflineLinks(); });
    row.append(remove);
    return row;
  }

  function updateSiteCount() {
    const count = $$('.custom-site-row').length;
    $('#custom-sites-count').textContent = `${count} / 6`;
    $('#add-site').disabled = count >= 6;
  }

  function renderSiteRows(sites) {
    $('#custom-sites-list').replaceChildren();
    if (Array.isArray(sites)) sites.slice(0, 6).forEach(site => $('#custom-sites-list').append(siteRow(site)));
    updateSiteCount();
  }

  function collectSites() {
    const sites = [];
    const seen = new Set();
    $$('.custom-site-row').forEach((row, index) => {
      const value = key => $(`[data-field="${key}"]`, row).value.trim();
      const name = value('name');
      let domain = value('domain').toLowerCase().replace(/\.$/, '');
      const searchURL = value('search_url');
      if (!name && !domain && !searchURL) return;
      if (!domain) throw new Error(`请填写第 ${index + 1} 个网站的域名。`);
      try {
        const parsed = new URL(domain.includes('://') ? domain : `https://${domain}`);
        if (parsed.pathname !== '/' || parsed.search || parsed.hash || parsed.port || parsed.username || parsed.password) throw new Error();
        domain = parsed.hostname;
      } catch (_) { throw new Error(`第 ${index + 1} 个网站请只填写域名，例如 example.org。`); }
      if (!/^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]*$/i.test(domain) || /(?:^|\.)(?:localhost|local|internal|test|invalid)$/i.test(domain)) throw new Error(`第 ${index + 1} 个网站需要有效的公开域名，不支持内网地址或 IP。`);
      if (seen.has(domain)) throw new Error(`网站 ${domain} 已在列表中，请合并重复项。`);
      if (searchURL) {
        if ((searchURL.match(/\{query\}/g) || []).length !== 1) throw new Error(`第 ${index + 1} 个站内搜索模板需包含一次 {query}，搜索时会替换为关键词。`);
        try {
          const parsed = new URL(searchURL.replace('{query}', 'search'));
          if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.port || (parsed.hostname !== domain && !parsed.hostname.endsWith(`.${domain}`))) throw new Error();
        } catch (_) { throw new Error(`第 ${index + 1} 个搜索模板须使用该网站的 HTTPS 地址。`); }
      }
      seen.add(domain);
      sites.push({ name, domain, search_url: searchURL });
    });
    return sites;
  }

  async function saveSites() {
    if (!ensureBackend()) return;
    $('#save-sites').disabled = true;
    try {
      const custom_sites = collectSites();
      state.config = await api('/api/config', { method: 'PUT', body: { custom_sites } });
      state.sitesDirty = false;
      updateConnection();
      notice('#custom-sites-feedback', '网站列表已保存到连接的搜索服务。', true);
    } catch (error) { notice('#custom-sites-feedback', error.message); }
    finally { $('#save-sites').disabled = false; }
  }

  function updateAdaptiveControls() {
    const useAI = $('#use-ai').checked;
    const adaptive = useAI && $('#adaptive-search').checked;
    const backend = connection.snapshot();
    const publicLimit = backend.connected && backend.visitorSession ? Number(backend.publicLimits.max_rounds) : 0;
    Array.from($('#max-rounds').options).forEach(option => { option.disabled = publicLimit > 0 && (Number(option.value) === 0 || Number(option.value) > publicLimit); });
    if ($('#max-rounds').selectedOptions[0]?.disabled) $('#max-rounds').value = Array.from($('#max-rounds').options).find(option => !option.disabled)?.value || '3';
    $('#adaptive-search').disabled = !useAI;
    $('#max-rounds').disabled = !adaptive && !resumableStates.has(state.job?.state);
    const agentic = backend.features.includes('agentic_search');
    $('#adaptive-hint').textContent = !useAI ? '开启 AI 理解后可使用 AI 递进搜索' : !adaptive ? '本次使用一轮检索' : agentic ? `AI 决定搜索工具与执行顺序${$('#max-rounds').value === '0' ? '，可随时停止' : '，达到轮数后等你继续'}` : $('#max-rounds').value === '0' ? '持续探索；可随时停止，无新线索或服务不可用时也会暂停' : '依据实际线索调整下一步，达到轮数后等你继续';
    if (publicLimit > 0) $('#adaptive-hint').textContent += ` · 公开服务每段最多 ${publicLimit} 轮`;
    $('#report-mode-hint').textContent = useAI ? '开启 AI 理解后，每轮会额外生成一份进展报告。' : '已关闭 AI 理解，本次不生成每轮 AI 报告。';
  }

  function roundBudget() { const value = Number($('#max-rounds').value); return [0, 3, 6, 12].includes(value) ? value : 3; }

  async function openSettings() {
    if (modelModeBusy) return;
    if (ownerAI && !connection.snapshot().connected) {
      fillSettings(state.config || {}); notice('#settings-feedback', '当前只预览模型配置。连接搜索服务后才能保存、测试或搜索。'); $('#settings-dialog').showModal(); return;
    }
    if (!ensureBackend()) return;
    notice('#settings-feedback', '');
    try { state.config = await api('/api/config'); updateConnection(); }
    catch (error) { if (error.code === 'BACKEND_CHANGED') return; notice('#settings-feedback', error.message); }
    fillSettings(state.config || {});
    $('#settings-dialog').showModal();
  }

  function fillSettings(config) {
    engineDraftIds = null;
    $('#base-url').value = config.base_url || '';
    $('#model').value = config.model || '';
    $('#searxng-url').value = config.searxng_url || '';
    ['api-key', 'tavily-key', 'brave-key'].forEach(id => { $(`#${id}`).value = ''; });
    if (ownerAI && ownerChoice === 'shared') {
      $('#base-url').value = ownerAI.base_url; $('#model').value = ownerAI.model; $('#api-key').value = ownerAI.api_key;
    }
    ['clear-api', 'clear-tavily', 'clear-brave'].forEach(id => { $(`#${id}`).checked = false; });
    updateSecretLabels(config);
    updateModelModes();
    renderEngineChoices();
    updateConcurrencyControl(config.search_concurrency);
  }

  function updateSecretLabels(config) {
    $('#ai-key-state').textContent = ownerAI && ownerChoice === 'shared' ? '站主公开预置 · 已填入' : ownerNeedsPersonalKey ? '需要填写个人密钥' : config.has_api_key ? '已保存 · 留空不修改' : '尚未设置';
    $('#tavily-key-state').textContent = config.has_tavily_key ? '已保存 · 留空不修改' : '尚未设置';
    $('#brave-key-state').textContent = config.has_brave_key ? '已保存 · 留空不修改' : '尚未设置';
  }

  function settingsPayload() {
    const readonly = ownerAI && ownerChoice === 'shared' || state.config?.ai_config_readonly === true || (connection.snapshot().visitorSession && (state.config?.api_mode || connection.snapshot().sessionMode) === 'shared');
    if (ownerNeedsPersonalKey && !$('#api-key').value.trim() && !$('#clear-api').checked) throw new Error('请输入自己的 API 密钥后保存；留空不会把站主密钥变成个人配置。');
    const payload = { searxng_url: $('#searxng-url').value.trim(), clear_secrets: [] };
    if (supportsConcurrentSearch()) {
      const concurrency = Number($('#search-concurrency').value);
      if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > concurrencyLimit()) throw new Error(`来源检索并发数需要是 1–${concurrencyLimit()} 的整数。`);
      payload.search_concurrency = concurrency;
    }
    if (engineSelectionSupported) {
      payload.search_engines = $$('#search-engine-options input:checked').map(input => input.value);
      if (!payload.search_engines.length) throw new Error('请至少选择一个搜索引擎。');
    }
    if (!readonly) {
      payload.base_url = $('#base-url').value.trim();
      payload.model = $('#model').value.trim();
      if (!safeURL(payload.base_url)) throw new Error('API 地址需要以 https:// 或 http:// 开头。');
    }
    if (payload.searxng_url && !safeURL(payload.searxng_url)) throw new Error('SearXNG 地址需要以 https:// 或 http:// 开头。');
    [['api_key', 'api-key', 'clear-api'], ['tavily_key', 'tavily-key', 'clear-tavily'], ['brave_key', 'brave-key', 'clear-brave']].forEach(([key, inputId, clearId]) => {
      if (key === 'api_key' && readonly) return;
      if ($(`#${clearId}`).checked) payload.clear_secrets.push(key);
      else if ($(`#${inputId}`).value.trim()) payload[key] = $(`#${inputId}`).value.trim();
    });
    return payload;
  }

  async function saveSettings(testConnection) {
    if (modelModeBusy) return;
    if (ownerAI && !connection.snapshot().connected) {
      ownerApplyOnConnect = ownerChoice === 'shared';
      $('#settings-dialog').close(); openBackend(); return;
    }
    if (ownerAI && anyTaskActive()) { notice('#settings-feedback', '请先结束当前搜索或总结任务，再保存模型设置。'); return; }
    if (!ensureBackend()) return;
    if (!$('#settings-form').reportValidity()) return;
    if (ownerAI && ownerChoice === 'shared' && !await applyOwnerPreset(true)) return;
    const revision = connection.snapshot().revision;
    const operation = beginModelOperation();
    const current = () => connection.snapshot().revision === revision && operation === modelOperation;
    notice('#settings-feedback', testConnection ? '正在保存设置并测试 AI 连接…' : '正在保存…');
    try {
      if (ownerAI && ownerChoice === 'custom' && connection.snapshot().visitorSession && connection.snapshot().sessionMode !== 'custom') throw new Error('请先点击“自己配置 API”完成访客模式切换，再保存个人配置。');
      const config = await api('/api/config', { method: 'PUT', body: settingsPayload() });
      if (!current()) return;
      state.config = config;
      if (ownerAI && ownerChoice === 'custom') { ownerNeedsPersonalKey = false; ownerAppliedRevision = -1; rememberOwnerChoice('custom'); }
      engineDraftIds = null;
      await loadSearchEngines();
      if (!current()) return;
      updateConnection();
      updateSecretLabels(state.config);
      ['api-key', 'tavily-key', 'brave-key'].forEach(id => { $(`#${id}`).value = ''; });
      if (ownerAI && ownerChoice === 'shared') $('#api-key').value = ownerAI.api_key;
      ['clear-api', 'clear-tavily', 'clear-brave'].forEach(id => { $(`#${id}`).checked = false; });
      if (testConnection) {
        const result = await api('/api/ai/test', { method: 'POST', body: {}, timeout: 120000 });
        if (!current()) return;
        notice('#settings-feedback', result.message || (result.ok ? '连接成功，模型可以正常响应。' : '设置已保存，但连接测试失败。'), Boolean(result.ok));
      } else {
        $('#settings-dialog').close();
        toast('设置已保存');
      }
    } catch (error) { if (current() && error.code !== 'BACKEND_CHANGED') notice('#settings-feedback', error.message); }
    finally { endModelOperation(operation); }
  }

  function supportsConcurrentSearch() { return connection.snapshot().features.includes('concurrent_search'); }
  function concurrencyLimit() {
    const backend = connection.snapshot();
    return Math.max(1, Math.min(12, Number(backend.visitorSession ? backend.publicLimits.search_concurrency || 4 : backend.searchLimits.max_search_concurrency || 12)));
  }
  function updateConcurrencyControl(value) {
    const input = $('#search-concurrency');
    const supported = supportsConcurrentSearch();
    input.disabled = !supported;
    input.max = String(concurrencyLimit());
    if (value !== undefined || !$('#settings-dialog').open) input.value = String(Math.max(1, Math.min(concurrencyLimit(), Number.isInteger(value) ? value : Number.isInteger(state.config?.search_concurrency) ? state.config.search_concurrency : 4)));
    $('#search-concurrency-hint').textContent = !supported ? '兼容默认：当前服务未提供并发设置，使用服务默认值 4。' : `每个问题同时查询 ${1}–${concurrencyLimit()} 个来源，默认 4；与同时搜索的问题数量独立。`;
  }
  function taskLimit() {
    const backend = connection.snapshot();
    if (!supportsConcurrentSearch()) return 1;
    return serverTaskLimit || Number(backend.visitorSession ? backend.publicLimits.max_session_jobs || 2 : backend.searchLimits.max_active_jobs || 4);
  }
  function taskActive(entry) { return Boolean(entry && (entry.operation || entry.stopPending || ['queued', 'running'].includes(entry.job.state) || entry.job.ai_summary?.state === 'running' || (!entry.full && entry.job.active))); }
  function activeTaskCount() { return Array.from(tasks.values()).filter(taskActive).length; }
  function anyTaskActive() { return submissionBusy || activeTaskCount() > 0; }
  function hasTaskCapacity() { return activeTaskCount() + Number(submissionBusy) < taskLimit(); }
  function currentTask() { return tasks.get(state.activeJobId); }

  function updateSubmitButton() {
    const button = $('#search-button');
    const connected = connection.snapshot().connected;
    button.disabled = modelModeBusy || submissionBusy || (connected && !hasTaskCapacity());
    button.querySelector('span').textContent = modelModeBusy ? '正在配置模型…' : submissionBusy ? '创建任务…' : !connected ? '连接后搜索' : !hasTaskCapacity() ? '运行任务已满' : state.activeJobId ? '作为新任务搜索' : '开始搜索';
    button.title = connected && !hasTaskCapacity() ? `最多同时运行 ${taskLimit()} 个搜索或总结任务。可先停止一个任务。` : '';
  }

  function renderTasks() {
    const container = $('#search-tasks');
    const focusedId = document.activeElement?.dataset.taskId;
    container.replaceChildren();
    const statuses = { queued: '等待中', running: '搜索中', done: '已完成', awaiting_user: '等待继续', stopped: '已停止', error: '失败' };
    tasks.forEach(entry => {
      const job = entry.job;
      const button = node('button', 'search-task');
      button.type = 'button';
      button.dataset.taskId = entry.id;
      const selected = state.activeJobId === entry.id;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
      const active = taskActive(entry);
      const label = entry.stopRequested ? '停止中' : entry.error ? '连接中断' : entry.operation === 'summary' || job.ai_summary?.state === 'running' ? '总结中' : statuses[job.state] || '读取中';
      button.append(node('strong', '', job.query || '正在读取问题…'), node('span', active ? 'task-state active' : 'task-state', `${label}${active && Number.isFinite(Number(job.progress)) ? ` · ${Math.round(Number(job.progress))}%` : ''}`));
      button.title = `${job.query || '搜索任务'} · ${entry.error || job.message || label}`;
      button.addEventListener('click', () => selectTask(entry.id));
      container.append(button);
      if (focusedId === entry.id) button.focus({ preventScroll: true });
    });
    const backend = connection.snapshot();
    $('#task-capacity-note').textContent = !backend.connected ? '连接服务后可同时搜索多个问题。' : `${activeTaskCount()} / ${taskLimit()} 个任务运行中${supportsConcurrentSearch() ? ' · 切换任务不会中断搜索' : ' · 当前服务使用单任务兼容模式'}`;
    $('#selected-task-note').textContent = state.activeJobId ? `当前查看：${currentTask()?.job.query || state.job?.query || '正在读取'}。停止与继续仅作用于此任务。` : '新问题 · 其他任务会继续搜索';
    updateSubmitButton();
  }

  function acceptTask(job, full = true) {
    const id = String(job.id || '');
    if (!id) return null;
    let entry = tasks.get(id);
    if (!entry) { entry = { id, job: {}, full: false, generation: 0, polling: false, operation: '', stopRequested: false, stopPending: false, error: '', summaryError: '', view: null }; tasks.set(id, entry); }
    entry.job = full ? job : { ...entry.job, ...job };
    if (!full && typeof job.ai_summary_state === 'string') entry.job.ai_summary = { ...(entry.job.ai_summary || {}), state: job.ai_summary_state };
    entry.full ||= full;
    if (full) { entry.error = ''; if (!taskActive(entry)) entry.stopRequested = false; }
    return entry;
  }

  function rememberTaskView() {
    const entry = currentTask();
    if (!entry) return;
    entry.view = { filter: state.filter, views: state.views, previousReadyReport: state.previousReadyReport, displayedReport: state.displayedReport, reportJobId: state.reportJobId, reportClockKey: state.reportClockKey, reportStartedAt: state.reportStartedAt };
  }

  function clearTaskView() {
    state.job = null; state.activeJobId = ''; state.stopRequested = false;
    state.filter = 'all'; state.views = 'all'; state.lastResults = ''; state.lastAISummary = ''; state.lastRounds = ''; state.lastNavigation = '';
    state.summaryPending = false; state.summaryError = ''; resetProgressReport();
    $('#views-filter').value = 'all';
    $$('[data-filter]').forEach(button => { button.classList.toggle('active', button.dataset.filter === 'all'); button.setAttribute('aria-pressed', String(button.dataset.filter === 'all')); });
    ['export-results','result-tools','answer-summary','ai-summary-card','native-links-section','engine-links-section','rounds-card','navigation-card','job-status-panel','progress-panel'].forEach(id => toggle($(`#${id}`), false));
    $('#navigation-leads').replaceChildren(); $('#navigation-details').open = false;
    $('#rounds-timeline').replaceChildren(); $('#search-warnings').replaceChildren(); $('#result-count').textContent = '0';
    $('#results').replaceChildren(empty('为新问题寻找线索', '其他任务会继续运行，可通过上方任务列表随时切换。'));
    $('#search-plan').replaceChildren(node('p', 'plan-intent', '新任务会独立拆解问题、检索并核对证据。'));
    $('#provider-status').replaceChildren(node('p', 'subtle', '新任务尚未开始'));
    notice('#search-notice', ''); setBusy(false);
  }

  function newSearch() {
    rememberTaskView(); state.pollToken += 1; composerVersion += 1; clearTaskView();
    $('#query').value = ''; showView('search'); renderTasks(); $('#query').focus();
  }

  function restoreTaskForm(job) {
    $('#query').value = job.query || '';
    if (typeof job.use_ai === 'boolean') $('#use-ai').checked = job.use_ai;
    if (typeof job.adaptive === 'boolean') $('#adaptive-search').checked = job.adaptive;
    if ([0,3,6,12].includes(job.max_rounds)) $('#max-rounds').value = String(job.max_rounds);
    $('#search-depth').value = ['quick','deep','research'].includes(job.depth) ? job.depth : 'deep'; updateDepthDescription();
    if (typeof job.fetch_pages === 'boolean') $('#fetch-pages').checked = job.fetch_pages;
    if (Array.isArray(job.platforms)) { state.selectedPlatforms = new Set(job.platforms); state.platformSelectionEdited = true; syncPlatformSelection(); }
    if (Array.isArray(job.custom_sites)) { renderSiteRows(job.custom_sites); state.sitesDirty = true; }
  }

  function displayTask(entry) {
    if (state.activeJobId !== entry.id) return;
    state.job = entry.job; state.stopRequested = entry.stopRequested;
    state.summaryPending = entry.operation === 'summary'; state.summaryError = entry.summaryError;
    setBusy(taskActive(entry), entry.operation === 'summary' || entry.job.ai_summary?.state === 'running' ? 'summary' : 'search');
    renderJob(entry.job);
    notice('#search-notice', entry.error || (entry.job.state === 'error' ? entry.job.error || entry.job.message || '搜索未能完成。' : ''));
  }

  function selectTask(id, query = '') {
    rememberTaskView(); state.pollToken += 1; clearTaskView();
    const entry = tasks.get(id) || acceptTask({ id, query }, false);
    state.activeJobId = id;
    entry.restoreVersion = ++composerVersion;
    if (entry.view) { Object.assign(state, entry.view); $('#views-filter').value = state.views; $$('[data-filter]').forEach(button => { const selected = button.dataset.filter === state.filter; button.classList.toggle('active', selected); button.setAttribute('aria-pressed', String(selected)); }); }
    restoreTaskForm(entry.job); showView('search'); displayTask(entry); renderTasks();
    startTaskPolling(entry);
  }

  async function loadTasks() {
    if (!connection.snapshot().connected || !supportsConcurrentSearch() || taskListing) return;
    const epoch = taskEpoch; taskListing = true;
    try {
      const data = await api('/api/jobs');
      if (epoch !== taskEpoch) return;
      if (Number.isInteger(data.max_active_jobs) && data.max_active_jobs > 0) serverTaskLimit = data.max_active_jobs;
      (Array.isArray(data.items) ? data.items : []).forEach(job => {
        const existing = tasks.get(String(job.id || ''));
        const entry = existing?.full ? existing : acceptTask(job, false);
        if (entry && (taskActive(entry) || job.active || ['queued','running'].includes(job.state) || job.ai_summary_state === 'running' || state.activeJobId === entry.id)) startTaskPolling(entry);
      });
      renderTasks(); updateModelModes();
    } catch (error) { if (error.code !== 'BACKEND_CHANGED' && error.status !== 404 && error.status !== 401) $('#task-capacity-note').textContent = `任务列表暂时不可用：${error.message}`; }
    finally { if (epoch === taskEpoch) taskListing = false; }
  }

  function setBusy(busy, mode = 'search') {
    state.busy = busy; state.busyMode = mode;
    $('#search-form').setAttribute('aria-busy', String(submissionBusy));
    renderAISummary(state.job); renderJobControls(); updateModelModes(); renderTasks();
  }

  async function startSearch(event) {
    if (event) event.preventDefault();
    if (!ensureBackend() || submissionBusy || modelModeBusy) return;
    if ($('#use-ai').checked && presetConfigurationPending()) { toast('请先应用站主预置，或填写并保存个人 API 配置。'); openSettings(); return; }
    if (!hasTaskCapacity()) { notice('#search-notice', `当前已达到 ${taskLimit()} 个并行任务，请等待或停止一个任务后再创建。`); loadTasks(); return; }
    if (connection.snapshot().visitorSession && $('#use-ai').checked && state.config?.api_mode === 'custom' && !state.config?.has_api_key) { toast('请先为当前访客会话配置自己的模型 API。'); openSettings(); return; }
    const query = $('#query').value.trim(); const platforms = Array.from(state.selectedPlatforms); let customSites;
    try { customSites = collectSites(); } catch (error) { $('#custom-sites-panel').open = true; notice('#custom-sites-feedback', error.message); return; }
    if (query.length < 2 || query.length > 500) { notice('#search-notice', '请用 2–500 个字符描述你想搜索的内容。'); return; }
    if (!platforms.length && !customSites.length) { notice('#search-notice', '请至少选择一个搜索平台，或添加一个指定网站。'); return; }
    const body = { query, platforms, depth: searchDepth(), use_ai: $('#use-ai').checked, adaptive: $('#adaptive-search').checked && $('#use-ai').checked, max_rounds: roundBudget(), custom_sites: customSites, fetch_pages: $('#fetch-pages').checked, only_verified: false };
    rememberTaskView(); clearTaskView(); const viewToken = ++state.pollToken; const epoch = taskEpoch;
    state.platformSelectionEdited = true; submissionBusy = true; renderTasks(); updateModelModes();
    showProgress({ stage: 'planning', progress: 0, message: '正在创建独立搜索任务…' });
    try {
      const response = await api('/api/search', { method: 'POST', body });
      if (epoch !== taskEpoch) return;
      if (!response.job_id) throw new Error('服务没有返回搜索任务编号，请重试。');
      const entry = acceptTask({ ...body, id: response.job_id, state: 'queued', stage: 'queued', progress: 0, results: [], search_engines: state.config?.search_engines || [] });
      submissionBusy = false;
      if (viewToken === state.pollToken) selectTask(entry.id); else startTaskPolling(entry);
    } catch (error) {
      if (epoch !== taskEpoch) return;
      if (viewToken === state.pollToken) { toggle($('#progress-panel'), false); notice('#search-notice', error.message); }
      else toast(error.message);
      loadTasks();
    } finally { if (epoch === taskEpoch) { submissionBusy = false; renderTasks(); updateModelModes(); } }
  }

  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

  function startTaskPolling(entry, restart = false) {
    if (entry.polling && !restart) return;
    const generation = ++entry.generation; const epoch = taskEpoch; entry.polling = true;
    const valid = () => epoch === taskEpoch && entry.generation === generation && tasks.get(entry.id) === entry;
    (async () => {
      let failures = 0;
      try {
        while (valid()) {
          let job;
          try { job = await api(`/api/jobs/${encodeURIComponent(entry.id)}`); failures = 0; }
          catch (error) { if (!valid()) return; if (++failures >= 4 || error.status === 404 || error.status === 401) throw error; await delay(1000 * failures); continue; }
          if (!valid()) return;
          const firstFull = !entry.full;
          acceptTask(job); entry.summaryError = '';
          if (firstFull && state.activeJobId === entry.id && entry.restoreVersion === composerVersion) restoreTaskForm(job);
          if (!taskActive(entry)) entry.stopRequested = false;
          displayTask(entry); renderTasks(); updateModelModes();
          if (terminalStates.has(job.state) && !taskActive(entry)) { loadHistory(); return; }
          await delay(900);
        }
      } catch (error) {
        if (!valid()) return;
        entry.error = error.message;
        if (entry.job.ai_summary?.state === 'running') entry.summaryError = error.message;
        displayTask(entry); renderTasks();
      } finally { if (valid()) entry.polling = false; }
    })();
  }

  function showProgress(job) {
    toggle($('#progress-panel'), true);
    const raw = Number(job.progress);
    const progress = Number.isFinite(raw) ? Math.max(0, Math.min(100, raw)) : 0;
    $('#progress-stage').textContent = state.stopRequested ? '正在停止搜索' : stageNames[job.stage] || job.stage || '正在搜索';
    $('#progress-message').textContent = state.stopRequested ? '停止请求已发送，正在保留已找到的线索。' : job.message || '正在整理搜索结果…';
    $('#progress-percent').textContent = `${Math.round(progress)}%`;
    $('#progress-bar').value = progress;
    $('#progress-round-label').textContent = [Number(job.round) > 0 ? `第 ${Number(job.round)} 轮` : '', Number.isFinite(job.searches_count) ? `已执行 ${job.searches_count} 次检索` : ''].filter(Boolean).join(' · ');
  }

  function renderJob(job) {
    if (job.state === 'running' || job.state === 'queued') showProgress(job);
    else toggle($('#progress-panel'), false);
    renderPlan(job.plan);
    renderProviders(job.provider_status);
    renderWarnings(job.warnings);
    renderNativeLinks(job.native_links);
    renderAISummary(job);
    renderProgressReport(job);
    renderNavigation(job);
    renderRounds(job);
    renderJobControls();
    $('#answer-summary').textContent = job.summary || '';
    toggle($('#answer-summary'), Boolean(job.summary));
    toggle($('#export-results'), terminalStates.has(job.state) || (Array.isArray(job.results) && job.results.length > 0));
    const resultKey = JSON.stringify([job.state, job.results || []]);
    if (resultKey !== state.lastResults) { state.lastResults = resultKey; renderResults(); }
  }

  function renderJobControls() {
    const job = state.job;
    const stop = $('#stop-search');
    const active = Boolean(state.activeJobId || job?.id) && (state.busy || ['queued', 'running'].includes(job?.state) || job?.ai_summary?.state === 'running');
    toggle(stop, active);
    stop.disabled = state.stopRequested;
    stop.textContent = state.stopRequested ? '停止中…' : state.busyMode === 'summary' ? '停止总结' : '停止搜索';
    const resumable = job && resumableStates.has(job.state) && !state.busy;
    toggle($('#job-status-panel'), Boolean(resumable));
    $('#continue-search').disabled = state.busy || !hasTaskCapacity();
    $('#continue-search').title = !hasTaskCapacity() ? `最多同时运行 ${taskLimit()} 个任务，请先等待或停止其他任务。` : '只继续当前选中的任务';
    if (resumable) {
      $('#job-status-panel').dataset.state = job.state;
      const titles = { awaiting_user: '搜索已暂停，等待你决定下一步', stopped: '已停止搜索，现有线索已保留', done: '本次搜索已完成，还可以继续深挖' };
      const reasons = { round_limit: '已达到本次设置的轮数。', budget_reached: '已达到本次设置的轮数。', round_budget: '已达到本次设置的轮数。', user: '已按你的要求停止后续检索。', user_stopped: '已按你的要求停止后续检索。', manual: '已按你的要求停止后续检索。', no_new_results: '最近几轮没有发现新线索。', sources_unavailable: '检索服务暂时无法继续。', exhausted: '当前搜索方向暂时没有更多新线索。', failures: '检索服务暂时无法继续。', interrupted: '搜索已中断，可以保留现有结果继续。' };
      $('#job-status-title').textContent = titles[job.state];
      const reason = typeof job.stop_reason === 'string' ? job.stop_reason : '';
      $('#job-status-reason').textContent = job.message || reasons[reason] || reason || '';
    }
    updateAdaptiveControls();
  }

  async function stopSearch() {
    const entry = currentTask();
    if (!entry || entry.stopRequested || !taskActive(entry)) return;
    const epoch = taskEpoch; entry.stopRequested = true; entry.stopPending = true; displayTask(entry); renderTasks();
    try {
      await api(`/api/jobs/${encodeURIComponent(entry.id)}/stop`, { method: 'POST', body: {} });
      if (epoch !== taskEpoch) return;
      entry.stopPending = false;
      if (state.activeJobId === entry.id) toast('已请求停止当前任务。其他任务继续运行。');
      startTaskPolling(entry, true);
    } catch (error) {
      if (epoch !== taskEpoch) return;
      entry.stopRequested = false; entry.stopPending = false; entry.error = error.message; displayTask(entry); renderTasks(); loadTasks();
    }
  }

  async function continueSearch() {
    const entry = currentTask();
    if (modelModeBusy || (entry?.job.use_ai !== false && presetConfigurationPending())) { toast('请先完成模型配置。'); return; }
    if (!entry || taskActive(entry) || !resumableStates.has(entry.job.state)) return;
    if (!hasTaskCapacity()) { notice('#search-notice', `已有 ${taskLimit()} 个任务运行，请等待或停止其中一个后继续。`); loadTasks(); return; }
    const epoch = taskEpoch; const body = { max_rounds: roundBudget(), depth: searchDepth() };
    entry.operation = 'continue'; entry.stopRequested = false; entry.error = ''; entry.summaryError = '';
    displayTask(entry); renderTasks();
    try {
      await api(`/api/jobs/${encodeURIComponent(entry.id)}/continue`, { method: 'POST', body });
      if (epoch !== taskEpoch) return;
      entry.operation = ''; entry.job = { ...entry.job, state: 'queued', stage: 'adapting', depth: body.depth, max_rounds: body.max_rounds };
      if (entry.stopRequested) await api(`/api/jobs/${encodeURIComponent(entry.id)}/stop`, { method: 'POST', body: {} });
      if (epoch !== taskEpoch) return;
      displayTask(entry); startTaskPolling(entry, true);
    } catch (error) {
      if (epoch !== taskEpoch) return;
      entry.operation = ''; entry.error = error.message; displayTask(entry); renderTasks(); loadTasks();
    }
  }

  function renderRounds(job) {
    const rounds = Array.isArray(job.rounds) ? job.rounds : [];
    toggle($('#rounds-card'), rounds.length > 0);
    if (!rounds.length) return;
    $('#round-count').textContent = `${Number(job.round) || rounds.length} 轮`;
    const latest = rounds[rounds.length - 1];
    const platforms = [...new Set((latest.queries || []).map(query => typeof query === 'object' ? query.platform : '').filter(Boolean))];
    const actions = (latest.queries || []).filter(query => query && typeof query === 'object' && query.action);
    $('#round-focus').textContent = actions.length ? `最近一轮：${actions.slice(0, 6).map(action => actionNames[action.action] || action.action).join(' → ')}${actions.length > 6 ? ` · 共 ${actions.length} 个动作` : ''}` : platforms.length ? `最近一轮涉及：${platforms.map(platform => platformNames[platform] || platform).join('、')}` : Number.isFinite(job.searches_count) ? `累计执行 ${job.searches_count} 次检索` : '依据各轮结果调整搜索方向';
    const leads = Array.isArray(job.navigation_leads) ? job.navigation_leads.filter(lead => lead && typeof lead === 'object') : [];
    const key = JSON.stringify([rounds, leads.map(lead => [lead.id, lead.title, lead.url])]);
    if (key === state.lastRounds) return;
    state.lastRounds = key;
    const timeline = $('#rounds-timeline');
    const expanded = new Map($$('.search-round', timeline).map(element => [element.dataset.round, element.open]));
    const reportsExpanded = new Map($$('.round-progress-report', timeline).map(element => [element.dataset.round, element.open]));
    timeline.replaceChildren();
    rounds.slice().reverse().forEach((round, reverseIndex) => {
      const number = round.number ?? rounds.length - reverseIndex;
      const item = node('details', 'search-round');
      item.dataset.round = String(number);
      item.open = expanded.has(String(number)) ? expanded.get(String(number)) : reverseIndex === 0;
      const heading = node('summary', 'search-round-heading');
      heading.append(node('strong', '', `第 ${number} 轮`));
      const counts = [];
      if (Number.isFinite(round.new_results)) counts.push(`新增 ${round.new_results}`);
      if (Number.isFinite(round.updated_results) && round.updated_results > 0) counts.push(`补充证据 ${round.updated_results}`);
      if (Number.isFinite(round.total_results)) counts.push(`累计 ${round.total_results}`);
      heading.append(node('span', '', counts.join(' · ') || (round.phase ? stageNames[round.phase] || round.phase : '检索中')));
      item.append(heading);
      if (round.planner) item.append(node('span', `round-planner ${round.planner === 'ai_actions' ? 'agentic' : 'fallback'}`, round.planner === 'ai_actions' ? 'AI 决定工具与顺序' : round.planner === 'fallback' ? '备用检索 · 非 AI 动作规划' : round.planner === 'mixed' ? 'AI 动作与备用路径' : String(round.planner)));
      if (round.reason) item.append(node('p', 'round-reason', round.reason));
      const rationale = typeof round.ai_rationale === 'string' ? round.ai_rationale : Array.isArray(round.ai_rationale) ? round.ai_rationale.filter(value => typeof value === 'string').join('\n') : '';
      if (rationale) { const decision = node('div', 'round-decision'); decision.append(node('strong', '', 'AI 决定依据'), node('p', '', rationale)); item.append(decision); }
      if (round.report && typeof round.report === 'object') {
        const report = node('details', 'round-progress-report');
        report.dataset.round = String(number);
        report.open = reportsExpanded.get(String(number)) || false;
        const likelihood = likelihoodLabel(round.report.assessment?.likelihood);
        report.append(node('summary', '', round.report.state === 'ready' ? `本轮进展报告 · 成功可能性${likelihood}` : `本轮进展报告 · ${{running:'整理中',error:'暂未完成',disabled:'未启用',stopped:'已停止'}[round.report.state] || '待确认'}`));
        report.append(reportBody(round.report, true));
        item.append(report);
      }
      if (Array.isArray(round.queries) && round.queries.length) {
        const queries = node('ul', 'round-queries');
        round.queries.forEach((query, index) => {
          if (query && typeof query === 'object' && query.action) { queries.append(actionRow(query, index, job)); return; }
          const row = node('li');
          row.append(node('span', '', typeof query === 'string' ? query : query.query || ''));
          if (typeof query === 'object') row.append(node('small', '', [platformNames[query.platform] || query.platform, engineNames[query.provider] || query.provider].filter(Boolean).join(' · ')));
          queries.append(row);
        });
        item.append(queries);
      }
      if (Array.isArray(round.platform_stats) && round.platform_stats.length) {
        const feedback = node('div', 'round-platform-stats');
        feedback.append(node('span', 'round-stats-label', '平台反馈'));
        round.platform_stats.forEach(stat => {
          const row = node('div', 'round-stat-row');
          row.append(node('span', '', stat.label || platformNames[stat.platform] || stat.platform || '公开网页'));
          row.append(node('span', '', [Number.isFinite(stat.results) ? `${stat.results} 条` : '', Number.isFinite(stat.relevant) ? `${stat.relevant} 相关` : '', Number.isFinite(stat.score) ? `${Math.round(stat.score)} 分` : ''].filter(Boolean).join(' · ')));
          feedback.append(row);
        });
        item.append(feedback);
      }
      timeline.append(item);
    });
  }

  function actionRow(action, index, job) {
    const row = node('li', 'agent-action');
    row.dataset.action = String(action.action);
    if (action.id) row.dataset.actionId = String(action.id);
    const status = String(action.status || 'queued');
    row.dataset.status = status;
    const labels = { queued:'等待执行', pending:'等待执行', running:'执行中', completed:'已完成', done:'已完成', success:'已完成', error:'未完成', failed:'未完成', skipped:'已跳过', cancelled:'已取消', canceled:'已取消', stopped:'已停止', blocked:'访问受限' };
    const top = node('div', 'action-heading');
    top.append(node('strong', '', `${index + 1}. ${actionNames[action.action] || action.action}`), node('span', 'action-state', action.ok === false && status === 'completed' ? '未成功' : labels[status] || status));
    row.append(top);
    if (action.query) row.append(node('p', 'action-query', action.query));
    if (action.purpose) row.append(node('p', 'action-purpose', `目的：${action.purpose}`));
    if (action.planner) row.append(node('small', 'action-planner', action.planner === 'ai_actions' ? '由 AI 选择' : '程序备用路径'));
    const lead = Array.isArray(job.navigation_leads) ? job.navigation_leads.find(item => item && item.id === action.lead_id) : null;
    const targetURL = action.target_url || lead?.url;
    if (targetURL && safeURL(targetURL)) row.append(sourceLink(`目标：${lead?.title || action.domain || targetURL} ↗`, targetURL, 'action-target'));
    else if (action.lead_id) row.append(node('p', 'action-target', `目标入口：${lead?.title || action.lead_id}`));
    if (action.domain) row.append(node('small', '', `限定网站：${action.domain}`));
    const source = [platformNames[action.platform] || action.platform, engineNames[action.provider] || action.provider].filter(Boolean).join(' · ');
    if (source) row.append(node('small', 'action-source', source));
    const dependencies = Array.isArray(action.depends_on) ? action.depends_on.filter(value => ['string','number'].includes(typeof value)).join('、') : typeof action.depends_on === 'string' ? action.depends_on : '';
    if (dependencies) row.append(node('small', 'action-dependency', `依赖动作：${dependencies}`));
    const counts = [];
    if (Number.isFinite(action.discovered_count)) counts.push(`${action.discovered_count} 个导航入口`);
    if (Number.isFinite(action.result_count)) counts.push(`${action.result_count} 条候选`);
    if (counts.length) row.append(node('p', 'action-counts', counts.join(' · ')));
    if (action.error) row.append(node('p', 'action-error', action.error));
    if (action.coverage) row.append(node('p', 'action-coverage', action.coverage));
    return row;
  }

  function renderNavigation(job) {
    const leads = Array.isArray(job.navigation_leads) ? job.navigation_leads.filter(lead => lead && typeof lead === 'object') : [];
    toggle($('#navigation-card'), leads.length > 0);
    if (!leads.length) return;
    $('#navigation-count').textContent = `${leads.length} 个入口`;
    const key = JSON.stringify([job.id, leads]);
    if (key === state.lastNavigation) return;
    state.lastNavigation = key;
    const container = $('#navigation-leads');
    const expanded = new Set($$('details[open]', container).map(item => item.dataset.leadId));
    container.replaceChildren();
    const kinds = { website:'网站入口', channel:'账号 / 频道', video:'视频入口', page:'网页入口' };
    leads.forEach(lead => {
      const item = node('article', 'navigation-lead');
      item.dataset.leadId = String(lead.id || '');
      const top = node('div', 'navigation-lead-heading');
      top.append(node('span', '', kinds[lead.kind] || '导航入口'), node('span', 'navigation-unverified', '尚未核验'));
      const heading = node('h4');
      heading.append(sourceLink(lead.title || lead.url || '未命名入口', lead.url));
      item.append(top, heading);
      if (lead.snippet) item.append(node('p', 'navigation-snippet', lead.snippet));
      if (lead.purpose) item.append(node('p', 'navigation-purpose', `下一步用途：${lead.purpose}`));
      const meta = [lead.error ? '读取遇到限制' : lead.inspected ? '已读取入口' : '尚未读取', platformNames[lead.platform] || lead.platform, Number.isFinite(lead.round) ? `第 ${lead.round} 轮发现` : '', lead.from_action ? `来自动作 ${lead.from_action}` : ''].filter(Boolean);
      item.append(node('p', 'navigation-meta', meta.join(' · ')));
      if (lead.error) item.append(node('p', 'action-error', lead.error));
      if (lead.coverage) item.append(node('p', 'navigation-coverage', lead.coverage));
      const links = Array.isArray(lead.links) ? lead.links.filter(link => link && typeof link === 'object' && safeURL(link.url)) : [];
      if (links.length) {
        const details = node('details', 'navigation-child-links');
        details.dataset.leadId = String(lead.id || ''); details.open = expanded.has(details.dataset.leadId);
        details.append(node('summary', '', `入口中发现的链接 · ${links.length}`));
        const list = node('ul');
        links.slice(0, 12).forEach(link => { const row = node('li'); row.append(sourceLink(link.title || link.url, link.url)); list.append(row); });
        details.append(list);
        if (links.length > 12) details.append(node('p', 'subtle', '此处展示前 12 个入口，完整记录保存在导出的 JSON 中。'));
        item.append(details);
      }
      container.append(item);
    });
  }

  function resetProgressReport() {
    state.lastProgressReport = '';
    state.displayedReport = null;
    state.previousReadyReport = null;
    state.reportJobId = '';
    state.reportClockKey = '';
    state.reportStartedAt = 0;
    toggle($('#progress-report-card'), false);
    $('#progress-report-content').replaceChildren();
    $('#progress-report-meta').replaceChildren();
  }

  function likelihoodLabel(value) {
    return { high: '高', medium: '中', low: '低', unknown: '信息不足' }[value] || '信息不足';
  }

  function sourceTypeLabel(citation) {
    const type = citation.source_type || citation.content_level;
    return { snippet:'搜索摘要', search_snippet:'搜索摘要', page:'已读取原文', public_page:'公开网页原文', full_text:'已读取原文', local:'本地导入原文', local_import:'本地导入原文' }[type] || (type ? `来源类型：${type}` : '来源类型未标明');
  }

  function reportBody(report, compact = false) {
    const body = node('div', `report-body${compact ? ' compact-report' : ''}`);
    const status = report.state || 'ready';
    if (status !== 'ready' && !(status === 'running' && !compact)) {
      const defaults = { running:'正在整理本轮发现和下一步方向…', error:'AI 进展报告暂时未能完成，已获取的线索仍会保留。', disabled:'AI 理解未开启，本轮展示实际检索统计。', stopped:'本轮报告已停止生成，已获取的线索仍会保留。' };
      const message = node('div', `report-state ${status === 'error' ? 'error' : ''}`);
      if (status === 'running') { const spinner=node('span','spinner');spinner.setAttribute('aria-hidden','true');message.append(spinner); }
      message.append(node('span','',report.message || defaults[status] || '报告暂不可用。'));
      body.append(message);
    }
    if (typeof report.progress === 'string' && report.progress.trim() && report.progress !== report.message) body.append(node('p', 'report-progress-copy', report.progress));
    const stats = report.stats || {};
    const metrics = node('div', 'report-metrics');
    [['new_results','新增线索'],['updated_results','补充证据'],['total_results','累计线索'],['requests','检索请求'],['failed_requests','未成功请求']].forEach(([key,label]) => {
      if (!Number.isFinite(stats[key]) || stats[key] < 0) return;
      const metric=node('div','report-metric');
      metric.append(node('strong','',stats[key].toLocaleString('zh-CN')),node('span','',label));
      metrics.append(metric);
    });
    if (metrics.childElementCount) body.append(metrics);
    const findings = Array.isArray(report.findings) ? report.findings.filter(item => item && typeof item.text === 'string' && item.text.trim()) : [];
    const numbers = new Map();
    ['supported','inference'].forEach(kind => {
      const items=findings.filter(item => kind === 'supported' ? item.kind === 'supported' : item.kind !== 'supported');
      if (!items.length) return;
      const group=node('section',`report-findings ${kind}`);
      const heading=node('h4','report-findings-heading');
      heading.append(node('span','report-kind-tag',kind === 'supported' ? '来源支持' : '待验证推断'),node('span','report-findings-count',`${items.length} 项`));
      group.append(heading);
      items.forEach(finding => {
        const item=node('article','report-finding');
        item.append(node('p','report-finding-text',finding.text));
        if (finding.caveat) item.append(node('p','report-finding-caveat',finding.caveat));
        const citations=Array.isArray(finding.citations) ? finding.citations.filter(citation => citation && typeof citation === 'object') : [];
        if (citations.length) {
          const links=node('div','summary-citations');
          const details=node('details','summary-quotes report-quotes');
          details.append(node('summary','',`核对 ${citations.length} 条原文依据`));
          const visibleSources=new Set();
          citations.forEach(citation => {
            const key=citation.result_id || citation.url || citation.title || `source-${numbers.size}`;
            if (!numbers.has(key)) numbers.set(key,numbers.size+1);
            const number=numbers.get(key);
            if (!visibleSources.has(key)) {visibleSources.add(key);links.append(citationLink(citation,number));}
            const quote=node('div','summary-quote');
            quote.append(citationLink(citation,number,false),node('span','summary-source-level',sourceTypeLabel(citation)),node('blockquote','',citation.quote || '该来源未提供可展示的原文摘录。'));
            details.append(quote);
          });
          item.append(links,details);
        }
        group.append(item);
      });
      body.append(group);
    });
    if (status === 'ready' && !findings.length) body.append(node('p','report-no-findings','本轮尚无可引用的发现或明确的待验证推断。'));
    const assessment=report.assessment;
    if (assessment && typeof assessment === 'object') {
      const likelihood=['high','medium','low','unknown'].includes(assessment.likelihood) ? assessment.likelihood : 'unknown';
      const panel=node('section',`report-assessment ${likelihood}`);
      const title=node('div','report-assessment-heading');
      title.append(node('h4','','成功可能性'),node('span',`likelihood-badge ${likelihood}`,likelihoodLabel(likelihood)));
      panel.append(title);
      panel.append(node('p','report-assessment-reason',assessment.reason || '当前可见证据不足以进一步判断。'));
      const outlook=node('div','report-outlook');
      [['blockers','目前的障碍'],['next_steps','下一步建议']].forEach(([key,label]) => {
        const items=Array.isArray(assessment[key]) ? assessment[key].filter(item => typeof item === 'string' && item.trim()) : [];
        if (!items.length) return;
        const column=node('div','report-outlook-column');
        column.append(node('h5','',label));
        const list=node('ul');
        items.forEach(text => list.append(node('li','',text)));
        column.append(list);
        outlook.append(column);
      });
      if (outlook.childElementCount) panel.append(outlook);
      if (compact) panel.append(node('p','report-compact-note','AI 基于当前证据的定性判断，不是统计概率。'));
      body.append(panel);
    }
    return body;
  }

  function renderProgressReport(job) {
    if (!job) { resetProgressReport(); return; }
    if (state.reportJobId !== job.id) { resetProgressReport();state.reportJobId=job.id; }
    let incoming=job.progress_report;
    if (incoming?.state === 'running' && ['stopped','error'].includes(job.state)) incoming={...incoming,state:job.state,message:job.state === 'stopped' ? '搜索已停止，本轮进展报告未完成。' : '搜索中断，本轮进展报告未完成。'};
    if (incoming && typeof incoming === 'object') {
      state.displayedReport=incoming;
      if (incoming.state === 'ready') state.previousReadyReport=incoming;
    }
    const report=state.displayedReport;
    toggle($('#progress-report-card'),Boolean(report));
    if (!report) return;
    const rounds=Array.isArray(job.rounds) ? job.rounds : [];
    const pendingRecord=rounds.slice().reverse().find(round=>round.report?.state === 'running');
    const reporting=job.stage === 'reporting' || report.state === 'running';
    const pendingRound=reporting ? Number(pendingRecord?.number || job.round || report.round) || 1 : 0;
    const previous=state.previousReadyReport && state.previousReadyReport !== report ? state.previousReadyReport : null;
    const display=report.state === 'running' && previous ? previous : report;
    const roundLabel=Number(display.round)>0 ? `第 ${Number(display.round)} 轮` : '本轮';
    const latestRecord=rounds[rounds.length-1];
    const laterReportStopped=latestRecord?.report?.state === 'stopped' && Number(latestRecord.number)>Number(display.round);
    $('#progress-report-round').textContent=`${roundLabel}${reporting && display.state === 'ready' ? ' · 上一份报告' : Number(job.round)>Number(display.round) ? ' · 最近已完成' : ''}`;
    $('#progress-report-card').setAttribute('aria-busy',String(reporting));
    if (reporting) {
      const clockKey=`${job.id}:${pendingRound}`;
      if (clockKey !== state.reportClockKey) {state.reportClockKey=clockKey;state.reportStartedAt=Date.now();}
    } else {state.reportClockKey='';state.reportStartedAt=0;}
    const key=JSON.stringify([job.id,report,display,reporting,pendingRound,previous,laterReportStopped]);
    const container=$('#progress-report-content');
    if (key !== state.lastProgressReport) {
      state.lastProgressReport=key;
      container.replaceChildren();
      if (reporting) {
        const loading=node('div','report-updating');
        const spinner=node('span','spinner');spinner.setAttribute('aria-hidden','true');
        const message=display.state === 'ready' ? `正在生成第 ${pendingRound} 轮报告，以下保留${roundLabel}的发现。` : `正在生成第 ${pendingRound} 轮进展报告…`;
        loading.append(spinner,node('span','',message),node('span','report-elapsed',''));
        container.append(loading);
      }
      if (!reporting && laterReportStopped) container.append(node('p','report-update-paused',`第 ${latestRecord.number} 轮报告已停止生成，以下保留${roundLabel}的报告。`));
      container.append(reportBody(display));
      if (!reporting && previous && report.state !== 'ready' && previous.round !== report.round) {
        const retained=node('details','report-retained');
        retained.append(node('summary','',`回看第 ${previous.round || '上一'} 轮已完成报告`),reportBody(previous,true));
        container.append(retained);
      }
      const meta=$('#progress-report-meta');meta.replaceChildren();
      if (display.model) meta.append(node('span','',display.model));
      if (display.generated_at) meta.append(node('span','',formatDate(display.generated_at)));
    }
    const elapsed=$('.report-elapsed',container);
    if (elapsed && reporting) elapsed.textContent=`已等待 ${Math.max(0,Math.floor((Date.now()-state.reportStartedAt)/1000))} 秒`;
  }

  function citationLink(citation, number, compact = true) {
    const title = citation.title || '查看来源';
    const label = compact ? `[${number}] ${title}` : `${number}. ${title}`;
    if (safeURL(citation.url)) return sourceLink(`${label} ↗`, citation.url, compact ? 'summary-citation' : 'summary-source-link');
    const result = state.job?.results?.find(item => String(item.id) === String(citation.result_id));
    if (!result) return node('span', compact ? 'summary-citation' : 'summary-source-link', label);
    const button = node('button', compact ? 'summary-citation' : 'summary-source-link', `${label} · ${platformNames.local}`);
    button.type = 'button';
    button.addEventListener('click', () => {
      state.filter = 'all';
      state.views = 'all';
      $('#views-filter').value = 'all';
      $$('[data-filter]').forEach(item => { item.classList.toggle('active', item.dataset.filter === 'all'); item.setAttribute('aria-pressed', String(item.dataset.filter === 'all')); });
      renderResults();
      const card = $$('.result-card').find(item => item.dataset.resultId === String(citation.result_id));
      if (card) { card.scrollIntoView({ behavior: 'auto', block: 'center' }); card.focus({ preventScroll: true }); }
    });
    return button;
  }

  function renderAISummary(job) {
    const card = $('#ai-summary-card');
    if (!job) { toggle(card, false); return; }
    const summary = job.ai_summary || {};
    const hasResults = Array.isArray(job.results) && job.results.length > 0;
    const terminal = terminalStates.has(job.state);
    const canGenerate = resumableStates.has(job.state);
    const visible = hasResults || Boolean(summary.state) || terminal;
    toggle(card, visible);
    if (!visible) return;
    const status = state.summaryPending ? 'running' : state.summaryError ? 'error' : summary.state || 'missing';
    const canResumePolling = Boolean(state.summaryError && summary.state === 'running' && job.state !== 'error');
    const button = $('#generate-summary');
    button.disabled = (!canResumePolling && (state.busy || status === 'running' || !hasTaskCapacity())) || !hasResults || (!canGenerate && !canResumePolling);
    button.textContent = status === 'running' ? '正在总结…' : state.summaryError || status === 'error' ? '重试 AI 总结 ↗' : status === 'ready' ? '重新总结 ↗' : '生成 AI 总结 ↗';
    button.title = job.state === 'error' ? '搜索尚未成功完成，请重新搜索后生成 AI 总结' : !hasResults ? '获取搜索结果后即可生成总结' : !canGenerate && !canResumePolling ? '搜索成功完成后才能生成 AI 总结，请先重新完成搜索' : '将本次搜索中相关的已获取内容发送至配置的 AI 服务，生成附有来源的总结';
    card.setAttribute('aria-busy', String(status === 'running'));
    const key = JSON.stringify([job.id, summary, status, state.summaryError, hasResults, terminal]);
    if (key === state.lastAISummary) return;
    state.lastAISummary = key;
    const container = $('#ai-summary-content');
    container.replaceChildren();
    const meta = $('#ai-summary-meta');
    meta.replaceChildren();
    if (status === 'running') {
      const loading = node('div', 'summary-state running');
      const spinner = node('span', 'spinner');
      spinner.setAttribute('aria-hidden', 'true');
      loading.append(spinner, node('span', '', (!state.summaryPending && summary.message) || '正在阅读相关结果、提炼要点并核对引用…'));
      container.append(loading);
    } else if (status === 'ready' && Array.isArray(summary.points) && summary.points.length) {
      const points = node('ol', 'summary-points');
      const numbers = new Map();
      summary.points.forEach((point, index) => {
        const item = node('li', 'summary-point');
        const number = node('span', 'summary-point-number', String(index + 1).padStart(2, '0'));
        number.setAttribute('aria-hidden', 'true');
        const copy = node('div', 'summary-point-copy');
        copy.append(node('p', 'summary-point-text', point.text || ''));
        const citations = Array.isArray(point.citations) ? point.citations : [];
        if (citations.length) {
          const links = node('div', 'summary-citations');
          const visibleSources = new Set();
          const details = node('details', 'summary-quotes');
          details.append(node('summary', '', `核对 ${citations.length} 条原文依据`));
          citations.forEach(citation => {
            const sourceId = citation.result_id || citation.url || citation.title || `${index}-${numbers.size}`;
            if (!numbers.has(sourceId)) numbers.set(sourceId, numbers.size + 1);
            const sourceNumber = numbers.get(sourceId);
            if (!visibleSources.has(sourceId)) {
              visibleSources.add(sourceId);
              links.append(citationLink(citation, sourceNumber));
            }
            const source = node('div', 'summary-quote');
            source.append(citationLink(citation, sourceNumber, false));
            source.append(node('span', 'summary-source-level', { snippet: '搜索摘要', page: '已读取原文', local: '本地导入原文' }[citation.content_level] || '内容层级未知'));
            source.append(node('blockquote', '', citation.quote || '该来源未提供可展示的原文摘录。'));
            details.append(source);
          });
          copy.append(links, details);
        }
        item.append(number, copy);
        points.append(item);
      });
      container.append(points);
    } else {
      const defaults = {
        empty: '现有结果中还没有足够的原文依据可供总结。可以补充资料，或调整搜索关键词。',
        error: 'AI 总结暂时未能生成。你可以重试，现有搜索结果仍可继续查看。',
        disabled: '这次搜索未自动生成 AI 总结。可点击上方按钮，根据已有结果生成。',
        missing: hasResults ? '根据这些线索提炼要点，并为每条结论附上来源和原文依据。' : '找到可用的搜索结果后，AI 会在这里整理主要发现。',
        ready: '这次总结没有返回可展示的要点，可尝试重新生成。'
      };
      container.append(node('p', `summary-state ${status === 'error' ? 'error' : ''}`, (state.summaryError ? defaults.error : summary.message) || defaults[status] || defaults.missing));
    }
    if (state.summaryError) container.append(node('p', 'summary-inline-error', state.summaryError));
    if (summary.stale) container.append(node('p', 'summary-stale', summary.stale_message || '搜索结果已更新，当前总结对应较早的线索。可按需重新总结。'));
    if (status !== 'running' && Array.isArray(summary.limitations) && summary.limitations.length) {
      const limitations = node('details', 'summary-limitations');
      limitations.append(node('summary', '', `依据与待确认事项 · ${summary.limitations.length}`));
      const list = node('ul');
      summary.limitations.forEach(item => list.append(node('li', '', typeof item === 'string' ? item : item.message || '')));
      limitations.append(list);
      container.append(limitations);
    }
    if (status === 'ready') {
      if (Number.isFinite(summary.source_count)) meta.append(node('span', '', `引用 ${summary.source_count} 个来源`));
      if (Number.isFinite(summary.considered_count)) meta.append(node('span', '', `参考 ${summary.considered_count} 条结果`));
      if (summary.model) meta.append(node('span', '', summary.model));
      if (summary.generated_at) meta.append(node('span', '', formatDate(summary.generated_at)));
    }
  }

  async function generateSummary() {
    if (modelModeBusy || presetConfigurationPending()) { toast('请先完成模型配置。'); return; }
    const entry = currentTask(); const job = entry?.job;
    const resumePolling = Boolean(entry?.summaryError && job?.ai_summary?.state === 'running');
    if (!job || (taskActive(entry) && !resumePolling) || !resumableStates.has(job.state) || !Array.isArray(job.results) || !job.results.length) return;
    if (resumePolling) { entry.error = ''; entry.summaryError = ''; startTaskPolling(entry, true); displayTask(entry); return; }
    if (!hasTaskCapacity()) { notice('#search-notice', `已有 ${taskLimit()} 个任务运行，请等待或停止其中一个后生成总结。`); loadTasks(); return; }
    const epoch = taskEpoch;
    entry.operation = 'summary'; entry.summaryError = ''; entry.error = ''; entry.stopRequested = false;
    displayTask(entry); renderTasks();
    try {
      await api(`/api/jobs/${encodeURIComponent(entry.id)}/summarize`, { method: 'POST', body: {} });
      if (epoch !== taskEpoch) return;
      entry.operation = ''; entry.job.ai_summary = { ...(entry.job.ai_summary || {}), state: 'running' };
      if (entry.stopRequested) await api(`/api/jobs/${encodeURIComponent(entry.id)}/stop`, { method: 'POST', body: {} });
      if (epoch !== taskEpoch) return;
      displayTask(entry); startTaskPolling(entry, true);
    } catch (error) {
      if (epoch !== taskEpoch) return;
      entry.operation = ''; entry.summaryError = error.message; displayTask(entry); renderTasks(); loadTasks();
    }
  }

  function renderPlan(plan) {
    if (!plan) return;
    const container = $('#search-plan');
    container.replaceChildren();
    if (plan.intent) container.append(node('p', 'plan-intent', plan.intent));
    if (Array.isArray(plan.must_have) && plan.must_have.length) {
      container.append(node('div', 'plan-label', '关键条件'));
      const tags = node('div', 'plan-tags');
      plan.must_have.forEach(item => tags.append(node('span', '', typeof item === 'string' ? item : item.condition || JSON.stringify(item))));
      container.append(tags);
    }
    if (Array.isArray(plan.queries) && plan.queries.length) {
      container.append(node('div', 'plan-label', `检索表达 · ${plan.queries.length}`));
      const list = node('ul', 'plan-queries');
      plan.queries.forEach(query => {
        const item = node('li', '', typeof query === 'string' ? query : query.query);
        if (query.reason) item.append(node('small', '', query.reason));
        list.append(item);
      });
      container.append(list);
    }
    if (Array.isArray(plan.uncertain) && plan.uncertain.length) {
      container.append(node('div', 'plan-label', '需要保留的不确定性'));
      const list = node('ul', 'plan-uncertain');
      plan.uncertain.forEach(item => list.append(node('li', '', typeof item === 'string' ? item : item.condition || JSON.stringify(item))));
      container.append(list);
    }
  }

  function renderProviders(providers) {
    if (!providers) return;
    const list = Array.isArray(providers) ? providers : Object.entries(providers).map(([name, detail]) => ({ name, ...(typeof detail === 'object' ? detail : { status: detail }) }));
    if (!list.length) return;
    const container = $('#provider-status');
    const expanded = new Set($$('.provider-group[open]', container).map(item => item.dataset.provider));
    container.replaceChildren();
    const names = { ...engineNames, local: localBackend() ? '本地资料库' : '服务资料库', bilibili: 'B 站公开搜索', github: 'GitHub 公开 API', stackoverflow: 'Stack Overflow 公开 API' };
    const groups = new Map();
    list.forEach(provider => {
      const key = provider.provider || provider.name || provider.label || '检索服务';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(provider);
    });
    groups.forEach((items, key) => {
      const group = node('details', 'provider-group');
      group.dataset.provider = key;
      group.open = expanded.has(key);
      let success = 0, failed = 0, count = 0;
      items.forEach(provider => {
        const status = String(provider.status || provider.state || '').toLowerCase();
        if (['ok', 'success', 'available', 'done', 'ready'].includes(status) || provider.ok === true) success += 1;
        else if (['error', 'failed', 'blocked', 'unavailable', 'disabled'].includes(status) || provider.ok === false) failed += 1;
        if (typeof provider.count === 'number') count += provider.count;
      });
      const label = names[key] || items[0].label || key;
      const badgeText = success && failed ? '部分响应' : success ? `${count} 条候选` : failed ? '不可用' : '检索中';
      const badge = node('span', `provider-state ${failed ? 'bad' : success ? 'good' : 'neutral'}`, badgeText);
      badge.title = '各次检索返回的候选数量，结果列表会进一步去重和筛选。';
      const summary = node('summary');
      const row = node('div', 'provider-row');
      row.append(node('span', '', label), badge);
      summary.append(row);
      summary.setAttribute('aria-label', `${label}：${badgeText}，展开查看各次检索详情`);
      group.append(summary);
      items.forEach(provider => {
        const detail = node('p', 'provider-query-status');
        if (provider.query) detail.append(node('strong', '', provider.query));
        const message = provider.error || provider.message || (provider.ok === true ? `返回 ${Number(provider.count) || 0} 条候选` : '等待服务响应');
        detail.append(node('span', '', message));
        if (provider.coverage) detail.append(node('span', '', provider.coverage));
        group.append(detail);
      });
      container.append(group);
    });
  }

  function renderWarnings(warnings) {
    $('#search-warnings').replaceChildren();
    if (!Array.isArray(warnings)) return;
    warnings.forEach(warning => $('#search-warnings').append(node('p', '', typeof warning === 'string' ? warning : warning.message || JSON.stringify(warning))));
  }

  function renderNativeLinks(links) {
    const container = $('#native-links');
    container.replaceChildren();
    if (Array.isArray(links)) links.forEach(link => {
      if (link.engine || link.platform === 'web') return;
      if (!safeURL(link.url)) return;
      const text = link.label || link.title || `${platformNames[link.platform] || '平台'}内搜索`;
      const element = sourceLink(`${text} ↗`, link.url, 'native-link');
      if (link.query) element.title = link.query;
      container.append(element);
    });
    toggle($('#native-links-section'), container.childElementCount > 0);
    renderEngineLinks();
  }

  function renderResults() {
    const job = state.job;
    if (!job) return;
    const results = Array.isArray(job.results) ? job.results : [];
    const filtered = results.filter(result => {
      if (state.filter === 'strong' && result.match !== 'strong') return false;
      if (state.filter === 'partial' && result.match !== 'partial') return false;
      if (state.filter === 'verified' && !['page', 'local'].includes(result.content_level)) return false;
      const knownViews = typeof result.views === 'number' && Number.isFinite(result.views) && result.views >= 0;
      if (state.views === 'unknown' && knownViews) return false;
      if (['100', '1000'].includes(state.views) && (!knownViews || result.views > Number(state.views))) return false;
      return true;
    });
    $('#result-count').textContent = filtered.length === results.length ? String(results.length) : `${filtered.length} / ${results.length}`;
    toggle($('#result-tools'), results.length > 0);
    const container = $('#results');
    container.replaceChildren();
    if (!filtered.length) {
      if (results.length) container.append(empty('没有符合筛选条件的线索', state.views !== 'all' ? '低浏览量筛选只包含明确公开了浏览量的结果，未知浏览量不会被当作低浏览量。' : '试着切换证据筛选，查看其他候选结果。'));
      else if (job.state === 'running' || job.state === 'queued') container.append(empty('正在寻找相关线索', '检索服务正在查找公开内容，结果整理完成后会显示在这里。'));
      else if (job.state === 'error') container.append(empty('这次搜索未能完成', '请根据上方提示检查网络和服务配置，或稍后重试。'));
      else container.append(empty('暂时没有找到可靠的线索', '可以换一种说法、缩短关键词，或打开平台内搜索。你已获得的原文也可以导入资料库。'));
      return;
    }
    filtered.forEach((result, index) => container.append(resultCard(result, index)));
  }

  function resultCard(result, index) {
    const card = node('article', 'result-card');
    card.dataset.resultId = String(result.id ?? index);
    card.tabIndex = -1;
    const top = node('div', 'result-topline');
    const platform = platformNames[result.platform] || result.platform || '公开网页';
    const badge = node('span', 'source-badge');
    const [className, symbol] = platformIcons[result.platform] || ['gray', '◎'];
    badge.append(node('b', `platform-dot ${className}`, symbol), node('span', '', platform));
    top.append(badge);
    const discoveredBy = engineNames[result.engine] || engineNames[result.source];
    if (discoveredBy) top.append(node('span', 'result-discovery', `发现来源：${discoveredBy}`));
    else if (result.source) top.append(node('span', '', result.source));
    const matchName = { strong: '匹配充分', partial: '部分匹配', unverified: '待核实' };
    const match = ['strong', 'partial'].includes(result.match) ? result.match : 'unverified';
    const score = typeof result.score === 'number' && Number.isFinite(result.score) ? ` · ${Math.max(0, Math.min(100, Math.round(result.score)))}` : '';
    const matchBadge = node('span', `match-badge ${match}`, `${matchName[match]}${score}`);
    matchBadge.title = '分数表示与搜索问题的相关度，不代表事实正确的概率。';
    top.append(matchBadge);
    const heading = node('h3');
    heading.append(sourceLink(result.title || `搜索线索 ${index + 1}`, result.url));
    card.append(top, heading);
    if (result.snippet) card.append(node('p', 'result-snippet', result.snippet));
    if (Array.isArray(result.evidence) && result.evidence.length) {
      const evidence = node('div', 'evidence-list');
      result.evidence.forEach(item => {
        const status = ['supported', 'contradicted'].includes(item.status) ? item.status : 'unknown';
        const row = node('div', `evidence-item ${status}`);
        const labels = { supported: '有依据', unknown: '未确认', contradicted: '不符合' };
        const icon = node('span', 'evidence-icon', { supported: '✓', unknown: '?', contradicted: '×' }[status]);
        icon.setAttribute('aria-label', labels[status]);
        const copy = node('div');
        copy.append(node('div', 'evidence-condition', `${item.condition || '匹配条件'} · ${labels[status]}`));
        if (item.quote) copy.append(node('p', 'evidence-quote', `“${item.quote}”`));
        row.append(icon, copy);
        evidence.append(row);
      });
      card.append(evidence);
    }
    if (result.reason) card.append(node('p', 'result-reason', result.reason));
    if (typeof result.details_coverage === 'string' && result.details_coverage.trim()) {
      const coverage = node('div', 'result-coverage');
      const hasReplies = typeof result.details_text === 'string' && result.details_text.trim();
      const replyLabel = result.platform === 'github' && result.content_kind === 'issue' ? '公开评论' : result.platform === 'stackoverflow' && result.content_kind === 'question' ? '公开回答' : '';
      coverage.append(node('strong', '', hasReplies && replyLabel ? `首楼 + ${replyLabel}` : '补充内容读取范围'), node('span', '', result.details_coverage));
      card.append(coverage);
    }
    if (typeof result.details_error === 'string' && result.details_error.trim()) card.append(node('p', 'result-details-error', result.details_error));
    if (result.body) {
      const details = node('details', 'result-body');
      details.append(node('summary', '', '查看已读取的正文'), node('pre', '', result.body));
      card.append(details);
    }
    const footer = node('div', 'result-footer');
    const meta = node('div', 'result-footer-meta');
    meta.append(node('span', '', { snippet: '仅搜索摘要', page: '已读取原文', local: '本地导入原文' }[result.content_level] || '内容层级未知'));
    const knownViews = typeof result.views === 'number' && Number.isFinite(result.views) && result.views >= 0;
    const views = node('span', '', `浏览量 ${knownViews ? result.views.toLocaleString('zh-CN') : '未知'}`);
    views.title = knownViews ? '平台公开记录的浏览次数，不代表独立用户人数。' : '来源未公开浏览量，不作推测。';
    meta.append(views);
    if (result.published) meta.append(node('span', '', formatDate(result.published)));
    footer.append(meta);
    if (safeURL(result.url)) footer.append(sourceLink('查看来源 ↗', result.url));
    card.append(footer);
    return card;
  }

  async function loadHistory() {
    try {
      const response = await api('/api/history');
      if (response.offline) { state.history = []; $('#recent-history').replaceChildren(node('p','subtle','连接服务后读取搜索记录')); $('#history-list').replaceChildren(empty('搜索记录来自连接的服务','连接搜索服务后，可查看并继续该服务保存的任务。')); return; }
      state.history = Array.isArray(response.items) ? response.items : [];
      const recent = $('#recent-history');
      recent.replaceChildren();
      state.history.slice(0, 5).forEach(item => {
        const button = node('button', 'recent-item', item.query || '未命名搜索');
        button.type = 'button';
        button.title = item.query || '';
        button.addEventListener('click', () => openHistory(item.id));
        recent.append(button);
      });
      if (!state.history.length) recent.append(node('p', 'subtle', '还没有搜索记录'));
      renderHistory();
    } catch (error) {
      if (error.code === 'BACKEND_CHANGED') return;
      $('#history-list').replaceChildren(empty('搜索记录暂时无法读取', error.message));
    }
  }

  function renderHistory() {
    const container = $('#history-list');
    container.replaceChildren();
    if (!state.history.length) { container.append(empty('还没有搜索记录', '完成一次搜索后，可以在这里回到当时的线索和结果。')); return; }
    state.history.forEach(item => {
      const button = node('button', 'history-item');
      button.type = 'button';
      const main = node('div', 'list-item-main');
      main.append(node('h3', '', item.query || '未命名搜索'), node('p', '', `${formatDate(item.created_at)} · ${Number(item.count) || 0} 条线索`));
      button.append(main, node('span', '', '打开 ↗'));
      button.addEventListener('click', () => openHistory(item.id));
      container.append(button);
    });
  }

  async function openHistory(id) {
    if (!ensureBackend()) return;
    const item = state.history.find(value => value.id === id);
    selectTask(id, item?.query || '');
  }

  async function loadLibrary() {
    try {
      const response = await api('/api/library');
      if (response.offline) { state.library=[]; $('#library-count').textContent='0'; $('#library-list').replaceChildren(empty('连接服务后使用资料库','导入内容将保存在你连接的服务上，本机服务则保存在本机。')); return; }
      state.library = Array.isArray(response.items) ? response.items : [];
      $('#library-count').textContent = String(state.library.length);
      const container = $('#library-list');
      container.replaceChildren();
      if (!state.library.length) { container.append(empty('把已有的好资料收进来', '导入帖子的正文与来源链接，搜索时就能一并发现这些线索。')); return; }
      state.library.forEach(item => {
        const row = node('article', 'library-item');
        const main = node('div', 'list-item-main');
        main.append(node('h3', '', item.title || '未命名资料'), node('p', '', `${platformNames[item.platform] || item.platform || '本地资料'} · ${formatDate(item.created_at)}`));
        const actions = node('div', 'list-item-actions');
        if (safeURL(item.url)) actions.append(sourceLink('原文 ↗', item.url));
        const remove = node('button', 'delete-button', '删除');
        remove.type = 'button';
        remove.setAttribute('aria-label', `删除资料：${item.title || '未命名资料'}`);
        remove.addEventListener('click', async () => {
          remove.disabled = true;
          try { await api(`/api/library/${encodeURIComponent(item.id)}`, { method: 'DELETE' }); toast('资料已删除'); await loadLibrary(); }
          catch (error) { toast(error.message); remove.disabled = false; }
        });
        actions.append(remove);
        row.append(main, actions);
        container.append(row);
      });
    } catch (error) { if (error.code !== 'BACKEND_CHANGED') $('#library-list').replaceChildren(empty('资料库暂时无法读取', error.message)); }
  }

  async function importItem(event) {
    event.preventDefault();
    if (!ensureBackend()) return;
    const url = $('#import-url').value.trim();
    const title = $('#import-title-input').value.trim();
    const text = $('#import-text').value.trim();
    if (!title || !text) { notice('#import-feedback', '请填写资料标题和正文。'); return; }
    if (text.length < 10) { notice('#import-feedback', '请粘贴至少 10 个字符的正文，让搜索有足够的上下文。'); return; }
    if (url && !safeURL(url)) { notice('#import-feedback', '原始链接需要以 https:// 或 http:// 开头。'); return; }
    $('#import-submit').disabled = true;
    notice('#import-feedback', '正在导入资料…');
    try {
      const response = await api('/api/import', { method: 'POST', body: { title, url, text, platform: $('#import-platform').value } });
      if (response.ok === false) throw new Error(response.message || '导入失败，请稍后重试。');
      $('#import-dialog').close();
      $('#import-form').reset();
      notice('#import-feedback', '');
      await loadLibrary();
      toast('资料已保存，之后的搜索将一起检索');
    } catch (error) { notice('#import-feedback', error.message); }
    finally { $('#import-submit').disabled = false; }
  }

  function exportResults() {
    if (!state.job) return;
    const blob = new Blob([JSON.stringify(state.job, null, 2)], { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = node('a');
    anchor.href = url;
    anchor.download = `xunwei-search-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  $$('[data-view]').forEach(button => button.addEventListener('click', () => showView(button.dataset.view)));
  ['#open-settings', '#provider-settings', '#connection-pill'].forEach(selector => $(selector).addEventListener('click', openSettings));
  $$('.close-modal').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
  $$('dialog').forEach(dialog => dialog.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
  }));
  $('#settings-form').addEventListener('submit', event => { event.preventDefault(); saveSettings(false); });
  $$('[data-model-mode]').forEach(button => button.addEventListener('click', () => changeModelMode(button.dataset.modelMode, true)));
  $('#settings-model-mode').addEventListener('change', () => changeModelMode($('#settings-model-mode').value));
  $('#search-engine-options').addEventListener('change', event => {
    if (!event.target.matches('input[data-engine]')) return;
    engineDraftIds = new Set($$('#search-engine-options input:checked').map(input => input.value));
    notice('#settings-feedback', '');
  });
  $('#test-ai').addEventListener('click', () => saveSettings(true));
  $('#search-form').addEventListener('submit', startSearch);
  ['input','change'].forEach(event => $('#search-form').addEventListener(event, () => { composerVersion += 1; }));
  $('#new-search').addEventListener('click', newSearch);
  $('#query').addEventListener('keydown', event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); startSearch(); } });
  $('.platform-options').addEventListener('click', event => {const button=event.target.closest('.platform-chip');if(button && $('.platform-options').contains(button))selectPlatform(button);});
  $('#search-depth').addEventListener('change', updateDepthDescription);
  $$('.suggestion').forEach(button => button.addEventListener('click', () => { $('#query').value = button.dataset.query; $('#query').focus(); renderOfflineLinks(); }));
  $$('[data-filter]').forEach(button => button.addEventListener('click', () => {
    state.filter = button.dataset.filter;
    $$('[data-filter]').forEach(item => { item.classList.toggle('active', item === button); item.setAttribute('aria-pressed', String(item === button)); });
    renderResults();
  }));
  $('#views-filter').addEventListener('change', () => { state.views = $('#views-filter').value; renderResults(); });
  $('#export-results').addEventListener('click', exportResults);
  $('#generate-summary').addEventListener('click', generateSummary);
  $('#stop-search').addEventListener('click', stopSearch);
  $('#continue-search').addEventListener('click', continueSearch);
  $('#use-ai').addEventListener('change', updateAdaptiveControls);
  $('#adaptive-search').addEventListener('change', updateAdaptiveControls);
  $('#max-rounds').addEventListener('change', updateAdaptiveControls);
  $('#add-site').addEventListener('click', () => {
    if ($$('.custom-site-row').length >= 6) return;
    const row = siteRow();
    $('#custom-sites-list').append(row);
    state.sitesDirty = true;
    updateSiteCount();
    $('[data-field="domain"]', row).focus();
  });
  $('#save-sites').addEventListener('click', saveSites);
  $('#open-import').addEventListener('click', () => { if (!ensureBackend()) return; notice('#import-feedback', ''); $('#import-dialog').showModal(); });
  $('#import-form').addEventListener('submit', importItem);
  ['#open-backend','#connect-backend'].forEach(selector => $(selector).addEventListener('click', openBackend));
  $('#backend-form').addEventListener('submit', saveBackend);
  $('#disconnect-backend').addEventListener('click', () => { ownerApplyOnConnect = false; connection.disconnect(); $('#backend-token').value=''; $('#backend-dialog').close(); });
  $('#search-button').addEventListener('click', event => { if (!connection.snapshot().connected) { event.preventDefault(); openBackend(); } });
  $('#query').addEventListener('input', renderOfflineLinks);
  connection.subscribe((backend, reason) => {
    if (reason === 'changing' || reason === 'disconnected' || reason === 'session-expired') resetBackendView();
    if (ownerAI && reason === 'connected') {
      const choice = savedOwnerChoice();
      if (choice === 'custom' || choice === 'custom_pending') ownerChoice = 'custom';
      ownerNeedsPersonalKey = choice === 'custom_pending';
    }
    updateConnection();
    if (reason === 'failed' || reason === 'session-expired') notice('#search-notice', backend.message);
    if (initialized && (reason === 'connected' || reason === 'disconnected')) refreshBackendData();
  });
  if (ownerAI) { const choice = savedOwnerChoice(); if (choice === 'custom' || choice === 'custom_pending') ownerChoice = 'custom'; ownerNeedsPersonalKey = choice === 'custom_pending'; }
  updateAdaptiveControls();
  updateDepthDescription();
  connection.catalog().then(data => { offlinePlatforms = Array.isArray(data.items) ? data.items : []; renderOfflineLinks(); });
  connection.initialize().then(() => { initialized = true; refreshBackendData(); });
  setInterval(() => { if (connection.snapshot().connected) loadTasks(); }, 6000);
})();
