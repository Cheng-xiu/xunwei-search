(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const connection = window.XunweiConnection;
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
    $('#connection-pill span').textContent = !backend.connected ? '未连接' : backend.visitorSession && (state.config?.api_mode || backend.sessionMode) === 'shared' ? '站主 API' : configured ? 'AI 已配置' : '配置 AI';
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
    $('#backend-banner-copy').textContent = backend.mode === 'pages' ? '这是 GitHub Pages 静态界面。站主 API 与自配 API 都需要连接搜索服务；当前无法确认站主是否提供额度。可先手动打开平台搜索。' : '搜索服务尚未连接。请确认本机程序已启动，或填写你自己的服务地址；也可以先手动打开平台搜索入口。';
    if (!state.busy) $('#search-button span').textContent = backend.connected ? '开始搜索' : '连接后搜索';
    renderOfflineLinks();
    updateModelModes();
    updateAdaptiveControls();
  }

  function updateModelModes() {
    const backend = connection.snapshot();
    const mode = state.config?.api_mode || backend.sessionMode || 'custom';
    const available = backend.connected && backend.visitorSession && (state.config?.shared_available ?? backend.sharedAvailable);
    toggle($('#model-mode-panel'), backend.mode === 'pages' || backend.publicMode);
    $$('[data-model-mode]').forEach(button => {
      const selected = backend.connected && button.dataset.modelMode === mode;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
      button.disabled = state.busy || modelModeBusy || (backend.connected && button.dataset.modelMode === 'shared' && !available);
      button.title = backend.connected && button.dataset.modelMode === 'shared' && !available ? '该服务尚未向此连接开放站主 API。' : '';
    });
    $('#shared-mode-description').textContent = !backend.connected ? '需要连接服务；尚未确认站主是否开放' : available ? '站主已开放，无需填写模型密钥' : '此服务尚未开放站主 API';
    $('#custom-mode-description').textContent = !backend.connected ? '连接服务后，填写自己的模型与密钥' : backend.visitorSession ? mode === 'custom' && state.config?.has_api_key ? '当前会话已配置个人模型' : '密钥与设置只属于你的访客会话' : '使用当前服务上的个人 API 配置';
    $('#visitor-session-status').textContent = backend.connected && backend.visitorSession ? `独立访客会话${backend.expiresAt ? ` · 至 ${formatDate(backend.expiresAt)}` : ''}` : '';
    $('#model-mode-note').textContent = !backend.connected ? '两种方式均需要连接搜索服务。静态页面本身不提供 AI 额度。' : backend.visitorSession ? '你的历史、资料与 API 配置按访客会话隔离。API 密钥由连接的服务代为调用，请使用你信任的服务。' : '当前是私人服务连接；只有提供独立访客会话的公开服务才可选择站主 API。';
    toggle($('#settings-model-control'), backend.visitorSession);
    $('#settings-model-mode').value = mode;
    $('#settings-model-mode').disabled = state.busy || modelModeBusy;
    $('#settings-model-mode option[value="shared"]').disabled = !available;
    const readonly = state.config?.ai_config_readonly === true || (backend.visitorSession && mode === 'shared');
    toggle($('#custom-ai-settings'), !readonly);
    $$('#custom-ai-settings input').forEach(input => { input.disabled = readonly; });
    toggle($('#clear-api').closest('label'), !readonly);
    $('#clear-api').disabled = readonly;
    toggle($('#test-ai'), !readonly);
    toggle($('#shared-model-info'), readonly);
    $('#shared-model-info').textContent = readonly ? `当前使用站主提供的模型${state.config?.model ? `：${state.config.model}` : ''}。站主密钥不会显示或提供修改入口；切换到“自己配置 API”可使用个人模型。` : '';
  }

  async function changeModelMode(mode, showSettings = false) {
    if (!['shared','custom'].includes(mode) || modelModeBusy || state.busy) return;
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
      await connection.connect($('#backend-url').value, $('#clear-backend-token').checked ? '' : $('#backend-token').value, !$('#clear-backend-token').checked, { mode: requestedModelMode });
      requestedModelMode = undefined;
      $('#backend-token').value = '';
      $('#backend-dialog').close();
      toast(connection.snapshot().message);
    } catch (error) { if (error.code !== 'BACKEND_CHANGED') notice('#backend-feedback', error.message); }
    finally { $('#save-backend').disabled = false; }
  }

  function resetBackendView() {
    state.pollToken += 1;
    Object.assign(state, { config: null, job: null, activeJobId: '', stopRequested: false, lastResults: '', lastAISummary: '', lastRounds: '', summaryPending: false, summaryError: '', history: [], library: [], sitesDirty: false, filter: 'all', views: 'all' });
    resetProgressReport();
    renderSiteRows([]);
    setBusy(false);
    ['settings-dialog','import-dialog'].forEach(id => { if ($(`#${id}`).open) $(`#${id}`).close(); });
    $('#settings-form').reset();
    $('#import-form').reset();
    ['api-key','tavily-key','brave-key'].forEach(id => { $(`#${id}`).value = ''; });
    notice('#model-mode-feedback', '');
    $('#views-filter').value = 'all';
    $$('[data-filter]').forEach(button => { button.classList.toggle('active', button.dataset.filter === 'all'); button.setAttribute('aria-pressed', String(button.dataset.filter === 'all')); });
    ['export-results','result-tools','answer-summary','ai-summary-card','rounds-card','job-status-panel','progress-panel'].forEach(id => toggle($(`#${id}`), false));
    $('#rounds-timeline').replaceChildren();
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
    await Promise.allSettled([loadConfig(), loadHistory(), loadLibrary(), loadPlatforms()]);
    updateConnection();
  }

  function renderOfflineLinks() {
    if (connection.snapshot().connected || state.job) return;
    const query = $('#query').value.trim();
    if (!query) { renderNativeLinks([]); return; }
    renderNativeLinks(offlinePlatforms.filter(item => state.selectedPlatforms.has(item.id) && item.search_url).map(item => ({
      platform: item.id, label: item.search_label || `${item.label}搜索`, query,
      url: item.search_url.replace('{query}', encodeURIComponent(query))
    })));
  }

  async function loadConfig() {
    try { state.config = await api('/api/config'); updateConnection(); if (!state.sitesDirty) renderSiteRows(state.config.custom_sites || []); }
    catch (error) { if (error.code === 'BACKEND_CHANGED') return; $('#connection-pill span').textContent = '服务未连接'; notice('#search-notice', error.message); }
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
      input.addEventListener('input', () => { state.sitesDirty = true; notice('#custom-sites-feedback', ''); });
      field.append(input);
      row.append(field);
    });
    const remove = node('button', 'remove-site', '移除');
    remove.type = 'button';
    remove.setAttribute('aria-label', '移除这个指定网站');
    remove.addEventListener('click', () => { row.remove(); state.sitesDirty = true; updateSiteCount(); });
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
    $('#adaptive-hint').textContent = !useAI ? '开启 AI 理解后可使用 AI 递进搜索' : !adaptive ? '本次使用一轮检索' : $('#max-rounds').value === '0' ? '持续探索；可随时停止，无新线索或服务不可用时也会暂停' : '依据结果调整关键词与平台，达到轮数后等你继续';
    if (publicLimit > 0) $('#adaptive-hint').textContent += ` · 公开服务每段最多 ${publicLimit} 轮`;
    $('#report-mode-hint').textContent = useAI ? '开启 AI 理解后，每轮会额外生成一份进展报告。' : '已关闭 AI 理解，本次不生成每轮 AI 报告。';
  }

  function roundBudget() { const value = Number($('#max-rounds').value); return [0, 3, 6, 12].includes(value) ? value : 3; }

  async function openSettings() {
    if (!ensureBackend()) return;
    notice('#settings-feedback', '');
    try { state.config = await api('/api/config'); updateConnection(); }
    catch (error) { if (error.code === 'BACKEND_CHANGED') return; notice('#settings-feedback', error.message); }
    fillSettings(state.config || {});
    $('#settings-dialog').showModal();
  }

  function fillSettings(config) {
    $('#base-url').value = config.base_url || '';
    $('#model').value = config.model || '';
    $('#searxng-url').value = config.searxng_url || '';
    ['api-key', 'tavily-key', 'brave-key'].forEach(id => { $(`#${id}`).value = ''; });
    ['clear-api', 'clear-tavily', 'clear-brave'].forEach(id => { $(`#${id}`).checked = false; });
    updateSecretLabels(config);
    updateModelModes();
  }

  function updateSecretLabels(config) {
    $('#ai-key-state').textContent = config.has_api_key ? '已保存 · 留空不修改' : '尚未设置';
    $('#tavily-key-state').textContent = config.has_tavily_key ? '已保存 · 留空不修改' : '尚未设置';
    $('#brave-key-state').textContent = config.has_brave_key ? '已保存 · 留空不修改' : '尚未设置';
  }

  function settingsPayload() {
    const readonly = state.config?.ai_config_readonly === true || (connection.snapshot().visitorSession && (state.config?.api_mode || connection.snapshot().sessionMode) === 'shared');
    const payload = { searxng_url: $('#searxng-url').value.trim(), clear_secrets: [] };
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
    if (!ensureBackend()) return;
    if (!$('#settings-form').reportValidity()) return;
    $('#save-settings').disabled = true;
    $('#test-ai').disabled = true;
    notice('#settings-feedback', testConnection ? '正在保存设置并测试 AI 连接…' : '正在保存…');
    try {
      state.config = await api('/api/config', { method: 'PUT', body: settingsPayload() });
      updateConnection();
      updateSecretLabels(state.config);
      ['api-key', 'tavily-key', 'brave-key'].forEach(id => { $(`#${id}`).value = ''; });
      ['clear-api', 'clear-tavily', 'clear-brave'].forEach(id => { $(`#${id}`).checked = false; });
      if (testConnection) {
        const result = await api('/api/ai/test', { method: 'POST', body: {}, timeout: 120000 });
        notice('#settings-feedback', result.message || (result.ok ? '连接成功，模型可以正常响应。' : '设置已保存，但连接测试失败。'), Boolean(result.ok));
      } else {
        $('#settings-dialog').close();
        toast('设置已保存');
      }
    } catch (error) { notice('#settings-feedback', error.message); }
    finally { $('#save-settings').disabled = false; $('#test-ai').disabled = false; }
  }

  function setBusy(busy, mode = 'search') {
    state.busy = busy;
    state.busyMode = mode;
    $('#search-button').disabled = busy;
    $('#search-button span').textContent = busy ? (mode === 'summary' ? '总结中…' : '搜索中…') : connection.snapshot().connected ? '开始搜索' : '连接后搜索';
    $('#search-form').setAttribute('aria-busy', String(busy));
    renderAISummary(state.job);
    renderJobControls();
    updateModelModes();
  }

  async function startSearch(event) {
    if (event) event.preventDefault();
    if (!ensureBackend()) return;
    if (state.busy) return;
    if (connection.snapshot().visitorSession && $('#use-ai').checked && state.config?.api_mode === 'custom' && !state.config?.has_api_key) { toast('请先为当前访客会话配置自己的模型 API。'); openSettings(); return; }
    const query = $('#query').value.trim();
    const platforms = Array.from(state.selectedPlatforms);
    let customSites;
    try { customSites = collectSites(); }
    catch (error) { $('#custom-sites-panel').open = true; notice('#custom-sites-feedback', error.message); return; }
    if (!query) { $('#query').focus(); return; }
    if (query.length < 2 || query.length > 500) { notice('#search-notice', '请用 2–500 个字符描述你想搜索的内容。'); return; }
    if (!platforms.length && !customSites.length) { notice('#search-notice', '请至少选择一个搜索平台，或添加一个指定网站。'); return; }
    state.platformSelectionEdited = true;
    const token = ++state.pollToken;
    state.job = null;
    state.activeJobId = '';
    state.stopRequested = false;
    state.filter = 'all';
    state.views = 'all';
    state.lastResults = '';
    state.lastAISummary = '';
    state.lastRounds = '';
    resetProgressReport();
    state.summaryPending = false;
    state.summaryError = '';
    $('#views-filter').value = 'all';
    $$('[data-filter]').forEach(button => { button.classList.toggle('active', button.dataset.filter === 'all'); button.setAttribute('aria-pressed', String(button.dataset.filter === 'all')); });
    notice('#search-notice', '');
    $('#search-warnings').replaceChildren();
    $('#result-count').textContent = '0';
    toggle($('#export-results'), false);
    toggle($('#result-tools'), false);
    toggle($('#answer-summary'), false);
    toggle($('#ai-summary-card'), false);
    toggle($('#native-links-section'), false);
    toggle($('#rounds-card'), false);
    toggle($('#job-status-panel'), false);
    $('#rounds-timeline').replaceChildren();
    $('#results').replaceChildren(empty('正在寻找相关线索', '搜索需要一点时间。可在右侧查看问题拆解与检索服务的状态。'));
    $('#search-plan').replaceChildren(node('p', 'plan-intent', '正在根据你的问题生成搜索思路…'));
    $('#provider-status').replaceChildren(node('p', 'subtle', '正在准备检索服务…'));
    setBusy(true);
    showProgress({ stage: 'planning', progress: 0, message: '正在创建搜索任务…' });
    try {
      const result = await api('/api/search', { method: 'POST', body: { query, platforms, depth: searchDepth(), use_ai: $('#use-ai').checked, adaptive: $('#adaptive-search').checked && $('#use-ai').checked, max_rounds: roundBudget(), custom_sites: customSites, fetch_pages: $('#fetch-pages').checked, only_verified: false } });
      if (!result.job_id) throw new Error('服务没有返回搜索任务编号，请重试。');
      if (token !== state.pollToken) return;
      state.activeJobId = result.job_id;
      renderJobControls();
      await pollJob(result.job_id, token);
    } catch (error) {
      if (token !== state.pollToken) return;
      if (state.job?.ai_summary?.state === 'running') state.summaryError = error.message;
      setBusy(false);
      toggle($('#progress-panel'), false);
      notice('#search-notice', error.message);
      if (!state.job || !(state.job.results || []).length) $('#results').replaceChildren(empty('这次搜索未能完成', '请根据上方提示检查连接或修改条件，然后重新搜索。'));
    }
  }

  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

  async function pollJob(id, token) {
    let failures = 0;
    while (token === state.pollToken) {
      let job;
      try { job = await api(`/api/jobs/${encodeURIComponent(id)}`); failures = 0; }
      catch (error) {
        failures += 1;
        if (failures >= 4 || error.status === 404) throw error;
        $('#progress-message').textContent = '连接暂时中断，正在重新获取进度…';
        await delay(1500 * failures);
        continue;
      }
      if (token !== state.pollToken) return;
      state.job = job;
      state.activeJobId = job.id || id;
      state.summaryPending = false;
      renderJob(job);
      if (terminalStates.has(job.state) && job.ai_summary?.state !== 'running') {
        state.stopRequested = false;
        setBusy(false);
        toggle($('#progress-panel'), false);
        if (job.state === 'error') notice('#search-notice', job.error || job.message || '搜索未能完成，请检查服务配置后重试。');
        loadHistory();
        return;
      }
      await delay(900);
    }
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
    $('#continue-search').disabled = state.busy;
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
    const id = state.activeJobId || state.job?.id;
    if (!id || state.stopRequested || (!state.busy && !['queued', 'running'].includes(state.job?.state) && state.job?.ai_summary?.state !== 'running')) return;
    const resumePolling = !state.busy;
    const token = resumePolling ? ++state.pollToken : state.pollToken;
    state.stopRequested = true;
    if (resumePolling) setBusy(true, state.job?.ai_summary?.state === 'running' ? 'summary' : 'search');
    renderJobControls();
    if (state.job && ['running', 'queued'].includes(state.job.state)) showProgress(state.job);
    try {
      await api(`/api/jobs/${encodeURIComponent(id)}/stop`, { method: 'POST', body: {} });
      if (token !== state.pollToken) return;
      if (state.busy) toast('已请求停止。已发出的外部请求可能仍会完成，后续检索将停止。');
      if (resumePolling) await pollJob(id, token);
    } catch (error) {
      if (token !== state.pollToken) return;
      state.stopRequested = false;
      if (resumePolling) setBusy(false);
      renderJobControls();
      notice('#search-notice', error.message);
    }
  }

  async function continueSearch() {
    const job = state.job;
    if (!job || state.busy || !resumableStates.has(job.state)) return;
    const token = ++state.pollToken;
    const id = job.id;
    state.activeJobId = id;
    state.stopRequested = false;
    state.summaryError = '';
    state.summaryPending = false;
    notice('#search-notice', '');
    setBusy(true);
    showProgress({ stage: 'adapting', progress: 0, round: job.round, searches_count: job.searches_count, message: '正在保留已有线索并准备继续搜索…' });
    try {
      const response = await api(`/api/jobs/${encodeURIComponent(id)}/continue`, { method: 'POST', body: { max_rounds: roundBudget(), depth: searchDepth() } });
      if (token !== state.pollToken) return;
      if (state.stopRequested) await api(`/api/jobs/${encodeURIComponent(id)}/stop`, { method: 'POST', body: {} });
      await pollJob(response.job_id || id, token);
    } catch (error) {
      if (token !== state.pollToken) return;
      setBusy(false);
      toggle($('#progress-panel'), false);
      notice('#search-notice', error.message);
    }
  }

  function renderRounds(job) {
    const rounds = Array.isArray(job.rounds) ? job.rounds : [];
    toggle($('#rounds-card'), rounds.length > 0);
    if (!rounds.length) return;
    $('#round-count').textContent = `${Number(job.round) || rounds.length} 轮`;
    const latest = rounds[rounds.length - 1];
    const platforms = [...new Set((latest.queries || []).map(query => typeof query === 'object' ? query.platform : '').filter(Boolean))];
    $('#round-focus').textContent = platforms.length ? `最近一轮涉及：${platforms.map(platform => platformNames[platform] || platform).join('、')}` : Number.isFinite(job.searches_count) ? `累计执行 ${job.searches_count} 次检索` : '依据各轮结果调整搜索方向';
    const key = JSON.stringify(rounds);
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
      if (round.reason) item.append(node('p', 'round-reason', round.reason));
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
        round.queries.forEach(query => {
          const row = node('li');
          row.append(node('span', '', typeof query === 'string' ? query : query.query || ''));
          if (typeof query === 'object') row.append(node('small', '', [platformNames[query.platform] || query.platform, query.provider].filter(Boolean).join(' · ')));
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
    button.disabled = state.busy || status === 'running' || !hasResults || (!canGenerate && !canResumePolling);
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
    const job = state.job;
    if (!job || state.busy || job.state === 'error' || !Array.isArray(job.results) || !job.results.length) return;
    const jobId = job.id;
    const resumePolling = Boolean(state.summaryError && job.ai_summary?.state === 'running');
    if (!resumableStates.has(job.state) && !resumePolling) return;
    const token = ++state.pollToken;
    state.summaryPending = true;
    state.summaryError = '';
    state.stopRequested = false;
    state.activeJobId = jobId;
    setBusy(true, 'summary');
    try {
      if (resumePolling) { await pollJob(jobId, token); return; }
      const response = await api(`/api/jobs/${encodeURIComponent(jobId)}/summarize`, { method: 'POST', body: {} });
      if (token !== state.pollToken || state.job?.id !== jobId) return;
      if (state.stopRequested) await api(`/api/jobs/${encodeURIComponent(jobId)}/stop`, { method: 'POST', body: {} });
      await pollJob(response.job_id || jobId, token);
    } catch (error) {
      if (token !== state.pollToken || state.job?.id !== jobId) return;
      state.summaryPending = false;
      state.summaryError = error.message;
      setBusy(false);
      renderAISummary(state.job);
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
    const names = { local: localBackend() ? '本地资料库' : '服务资料库', bing: 'Bing 网页索引', duckduckgo: 'DuckDuckGo', bilibili: 'B 站公开搜索', tavily: 'Tavily', brave: 'Brave Search', searxng: 'SearXNG' };
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
      const label = key === 'local' ? names.local : items[0].label || names[key] || key;
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
      if (!safeURL(link.url)) return;
      const text = link.label || link.title || `${platformNames[link.platform] || '平台'}内搜索`;
      const element = sourceLink(`${text} ↗`, link.url, 'native-link');
      if (link.query) element.title = link.query;
      container.append(element);
    });
    toggle($('#native-links-section'), container.childElementCount > 0);
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
    if (result.source) top.append(node('span', '', result.source));
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
    if (state.busy) { toast(state.busyMode === 'summary' ? 'AI 总结正在生成，完成后即可打开其他记录。' : '当前搜索仍在运行，请完成后再打开历史记录。'); return; }
    const token = ++state.pollToken;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
      if (token !== state.pollToken) return;
      state.job = job;
      state.activeJobId = job.id || id;
      state.stopRequested = false;
      state.lastResults = '';
      state.lastAISummary = '';
      state.lastRounds = '';
      resetProgressReport();
      state.summaryPending = false;
      state.summaryError = '';
      state.filter = 'all';
      state.views = 'all';
      $('#views-filter').value = 'all';
      $$('[data-filter]').forEach(button => { button.classList.toggle('active', button.dataset.filter === 'all'); button.setAttribute('aria-pressed', String(button.dataset.filter === 'all')); });
      const historyItem = state.history.find(item => item.id === id);
      $('#query').value = job.query || (historyItem && historyItem.query) || '';
      if (typeof job.use_ai === 'boolean') $('#use-ai').checked = job.use_ai;
      if (typeof job.adaptive === 'boolean') $('#adaptive-search').checked = job.adaptive;
      if ([0, 3, 6, 12].includes(job.max_rounds)) $('#max-rounds').value = String(job.max_rounds);
      $('#search-depth').value = ['quick','deep','research'].includes(job.depth) ? job.depth : 'deep';
      updateDepthDescription();
      if (typeof job.fetch_pages === 'boolean') $('#fetch-pages').checked = job.fetch_pages;
      if (Array.isArray(job.platforms)) {state.selectedPlatforms=new Set(job.platforms);state.platformSelectionEdited=true;syncPlatformSelection();}
      if (Array.isArray(job.custom_sites)) { renderSiteRows(job.custom_sites); state.sitesDirty = true; }
      notice('#search-notice', job.state === 'error' ? job.error || job.message || '这次搜索未能完成。' : '');
      showView('search');
      renderJob(job);
      if (job.state === 'running' || job.state === 'queued' || job.ai_summary?.state === 'running') { setBusy(true, job.ai_summary?.state === 'running' ? 'summary' : 'search'); await pollJob(id, token); }
    } catch (error) { if (token !== state.pollToken) return; if (state.job?.ai_summary?.state === 'running') state.summaryError = error.message; setBusy(false); notice('#search-notice', error.message); toast(error.message); }
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
  $('#test-ai').addEventListener('click', () => saveSettings(true));
  $('#search-form').addEventListener('submit', startSearch);
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
  $('#disconnect-backend').addEventListener('click', () => { connection.disconnect(); $('#backend-token').value=''; $('#backend-dialog').close(); });
  $('#search-button').addEventListener('click', event => { if (!connection.snapshot().connected) { event.preventDefault(); openBackend(); } });
  $('#query').addEventListener('input', renderOfflineLinks);
  connection.subscribe((backend, reason) => {
    if (reason === 'changing' || reason === 'disconnected' || reason === 'session-expired') resetBackendView();
    updateConnection();
    if (reason === 'failed' || reason === 'session-expired') notice('#search-notice', backend.message);
    if (initialized && (reason === 'connected' || reason === 'disconnected')) refreshBackendData();
  });
  updateAdaptiveControls();
  updateDepthDescription();
  connection.catalog().then(data => { offlinePlatforms = Array.isArray(data.items) ? data.items : []; renderOfflineLinks(); });
  connection.initialize().then(() => { initialized = true; refreshBackendData(); });
})();
