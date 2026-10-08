(() => {
  'use strict';
  const deployment = window.XUNWEI_DEPLOYMENT || {};
  const mode = deployment.mode === 'pages' ? 'pages' : 'local';
  const storageKey = `xunwei.backend.v1:${new URL('./', document.baseURI).pathname}`;
  const listeners = new Set();
  const controllers = new Set();
  let apiBase = '', accessToken = '', connected = false, revision = 0, message = '';
  let publicMode = false, visitorSession = false, sharedAvailable = false, sessionMode = '', expiresAt = '', publicLimits = {};
  let catalogPromise;
  const freeEngineIds = ['baidu', 'bing', 'google', 'yandex', 'duckduckgo'];
  const engineDefaults = [
    { id: 'baidu', label: '百度', access: 'public_html', search_url: 'https://www.baidu.com/s?wd={query}' },
    { id: 'bing', label: '必应 Bing', access: 'public_rss', search_url: 'https://www.bing.com/search?q={query}' },
    { id: 'google', label: 'Google', access: 'public_html', search_url: 'https://www.google.com/search?q={query}' },
    { id: 'yandex', label: 'Yandex', access: 'public_html', search_url: 'https://yandex.com/search/?text={query}' },
    { id: 'duckduckgo', label: 'DuckDuckGo', access: 'public_html', search_url: 'https://duckduckgo.com/?q={query}' },
    { id: 'tavily', label: 'Tavily', access: 'api', search_url: '' },
    { id: 'brave', label: 'Brave Search', access: 'api', search_url: 'https://search.brave.com/search?q={query}' },
    { id: 'searxng', label: 'SearXNG', access: 'api', search_url: '' }
  ];

  function fallbackEngines() {
    return { items: engineDefaults.map(item => ({ ...item, requires_key: item.id === 'tavily' || item.id === 'brave', configured: freeEngineIds.includes(item.id), available: freeEngineIds.includes(item.id), description: freeEngineIds.includes(item.id) ? '免费公开网页索引；自动访问可能受限制，浏览器入口可手动打开。' : '需配置自己的搜索 API 或实例后使用。' })), selection_supported: false, offline: !connected };
  }

  async function searchEngines() {
    if (!connected) return fallbackEngines();
    try {
      const data = await request('/api/search-engines');
      if (!Array.isArray(data.items) || !data.items.length) return fallbackEngines();
      const known = new Set(engineDefaults.map(item => item.id));
      const items = data.items.filter(item => item && known.has(item.id) && typeof item.label === 'string');
      if (!items.length) return fallbackEngines();
      return { ...data, items, selection_supported: true };
    } catch (error) {
      if (error.code === 'BACKEND_CHANGED' || error.status === 401 || error.status === 403) throw error;
      return fallbackEngines();
    }
  }

  function engineLinks({ query, engines, platforms, selectedPlatforms, customSites = [] }) {
    const text = String(query || '').trim();
    if (!text) return [];
    const scopes = new Map();
    const selected = new Set(selectedPlatforms || []);
    const addDomain = (value, label) => {
      const domain = String(value || '').trim().toLowerCase();
      if (!/^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/.test(domain)) return;
      if (!scopes.has(domain)) scopes.set(domain, { domain, scope: label || domain, query: `${text} site:${domain}` });
    };
    if (selected.has('web')) scopes.set('*', { domain: '', scope: '公开全网', query: text });
    (platforms || []).forEach(platform => {
      if (!selected.has(platform.id) || platform.id === 'web') return;
      (platform.domains || []).forEach(domain => addDomain(domain, `${platform.label || platform.id} · ${domain}`));
    });
    customSites.forEach(site => addDomain(site.domain, site.name ? `${site.name} · ${site.domain}` : site.domain));
    const links = [];
    (engines || []).forEach(engine => {
      if (typeof engine.search_url !== 'string' || engine.search_url.split('{query}').length !== 2) return;
      scopes.forEach(scope => {
        try {
          const url = new URL(engine.search_url.replace('{query}', encodeURIComponent(scope.query)));
          if (url.protocol !== 'https:' || url.username || url.password) return;
          links.push({ engine: engine.id, label: engine.label, url: url.href, ...scope });
        } catch (_) { /* Invalid templates are not rendered. */ }
      });
    });
    return links;
  }

  function failure(text, code, status) {
    const error = new Error(text);
    error.code = code;
    if (status) error.status = status;
    return error;
  }

  function normalizeBase(value, allowLocal = false) {
    const raw = String(value || '').trim();
    if (!raw && allowLocal && mode === 'local') return '';
    if (!/^https?:\/\//i.test(raw) || /[\s\\?#]/.test(raw)) throw failure('请输入完整的 HTTPS 服务地址；本机服务可使用 HTTP localhost 或 127.0.0.1。地址不能含查询参数或片段。', 'INVALID_BACKEND');
    let url;
    try { url = new URL(raw); } catch (_) { throw failure('搜索服务地址格式不正确。', 'INVALID_BACKEND'); }
    const loopback = url.hostname === 'localhost' || url.hostname === '127.0.0.1';
    if (url.username || url.password || url.search || url.hash || (url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback))) throw failure('远程服务须使用 HTTPS；本机 HTTP 请使用 localhost 或 127.0.0.1，地址中不能包含账号或密码。', 'INVALID_BACKEND');
    return url.href.replace(/\/+$/, '');
  }

  function snapshot() { return Object.freeze({ mode, apiBase, connected, hasToken: Boolean(accessToken), revision, message, publicMode, visitorSession, sharedAvailable, sessionMode, expiresAt, publicLimits: { ...publicLimits } }); }
  function emit(reason) { listeners.forEach(listener => listener(snapshot(), reason)); }
  function forgetStored() { try { sessionStorage.removeItem(storageKey); } catch (_) { /* Storage may be disabled. */ } }
  function remember() {
    try { sessionStorage.setItem(storageKey, JSON.stringify({ apiBase, accessToken, visitorSession, sessionMode })); } catch (_) { /* The live connection remains usable. */ }
  }
  function invalidate(reason) {
    revision += 1;
    connected = false;
    controllers.forEach(controller => controller.abort());
    controllers.clear();
    emit(reason);
  }

  async function fetchJSON(context, path, options = {}) {
    if (!/^\/api(?:\/|$)/.test(path) || path.includes('..') || path.includes('\\')) throw failure('无效的服务请求路径。', 'INVALID_PATH');
    if (context.revision !== revision) throw failure('搜索服务连接已切换，旧请求已忽略。', 'BACKEND_CHANGED');
    const controller = new AbortController();
    controllers.add(controller);
    const timeout = setTimeout(() => controller.abort(), options.timeout || 45000);
    const headers = { Accept: 'application/json' };
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    if (context.token) headers.Authorization = `Bearer ${context.token}`;
    try {
      const response = await fetch(`${context.base}${path}`, {
        method: options.method || 'GET', headers,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        signal: controller.signal, cache: 'no-store', credentials: 'omit', redirect: 'error'
      });
      const raw = await response.text();
      if (context.revision !== revision) throw failure('搜索服务连接已切换，旧请求已忽略。', 'BACKEND_CHANGED');
      let data;
      try { data = raw ? JSON.parse(raw) : {}; } catch (_) { throw failure('服务没有返回有效 JSON，请检查地址是否指向寻微后端。', 'INVALID_RESPONSE'); }
      if (!response.ok) {
        const detail = data.detail || data.error || data.message;
        throw failure(response.status === 401 || response.status === 403 ? '后端拒绝访问，请检查搜索服务访问令牌及允许的网页来源。' : typeof detail === 'string' ? detail : `请求失败（${response.status}）`, 'BACKEND_ERROR', response.status);
      }
      return data;
    } catch (error) {
      if (context.revision !== revision) throw failure('搜索服务连接已切换，旧请求已忽略。', 'BACKEND_CHANGED');
      if (error.name === 'AbortError') throw failure('连接请求超时，请检查服务是否启动。', 'TIMEOUT');
      if (error instanceof TypeError) throw failure('无法连接搜索服务。请检查地址、网络及后端允许的网页来源；本机连接也可能需要浏览器授权。', 'NETWORK');
      throw error;
    } finally { clearTimeout(timeout); controllers.delete(controller); }
  }

  async function catalog() {
    if (!catalogPromise) catalogPromise = fetch('./platforms.json', { credentials: 'omit', cache: 'no-store' }).then(response => {
      if (!response.ok) throw new Error('平台目录未加载');
      return response.json();
    }).catch(() => ({ items: [] }));
    return catalogPromise;
  }

  async function request(path, options = {}) {
    if (!connected) {
      if ((options.method || 'GET') === 'GET') {
        if (path === '/api/platforms') return catalog();
        if (path === '/api/config') return { base_url: '', model: '', searxng_url: '', custom_sites: [], search_engines: [], has_api_key: false, has_tavily_key: false, has_brave_key: false, shared_available: false, offline: true };
        if (path === '/api/search-engines') return fallbackEngines();
        if (path === '/api/history' || path === '/api/library') return { items: [], offline: true };
      }
      throw failure('请先连接搜索服务，再使用智能搜索、历史记录或资料库。', 'BACKEND_REQUIRED');
    }
    const current = { base: apiBase, token: accessToken, revision };
    try { return await fetchJSON(current, path, options); }
    catch (error) {
      if (error.status === 401 && visitorSession && current.revision === revision) {
        accessToken = '';
        forgetStored();
        message = '访客会话已失效，请重新连接。新会话不会继承旧会话的历史或 API 配置。';
        invalidate('session-expired');
      }
      throw error;
    }
  }

  function sessionInfo(value) {
    if (!value || !['shared','custom'].includes(value.mode)) throw failure('访客会话信息无效，请重新连接。', 'INVALID_SESSION');
    sessionMode = value.mode === 'shared' ? 'shared' : 'custom';
    sharedAvailable = value.shared_available === true;
    expiresAt = typeof value.expires_at === 'string' ? value.expires_at : '';
    if (value.public_limits && typeof value.public_limits === 'object') publicLimits = { ...value.public_limits };
  }

  async function createSession(current, preferredMode) {
    const body = ['shared','custom'].includes(preferredMode) ? { mode: preferredMode } : {};
    const session = await fetchJSON({ ...current, token: '' }, '/api/session', { method: 'POST', body, timeout: 15000 });
    if (typeof session.access_token !== 'string' || !session.access_token || /[^\x21-\x7e]/.test(session.access_token) || !['shared','custom'].includes(session.mode)) throw failure('访客会话响应无效，请稍后重试。', 'INVALID_SESSION');
    accessToken = session.access_token;
    current.token = accessToken;
    visitorSession = true;
    sessionInfo(session);
  }

  async function connect(value, token = '', keepToken = false, options = {}) {
    const nextBase = normalizeBase(value, true);
    const enteredToken = String(token || '').trim();
    if (enteredToken.length > 4096 || /[^\x21-\x7e]/.test(enteredToken)) throw failure('访问令牌须为不含空格或换行的文本。', 'INVALID_TOKEN');
    const nextToken = !enteredToken && keepToken && nextBase === apiBase ? accessToken : enteredToken;
    const restoreSession = Boolean(options.restoreSession || (!enteredToken && keepToken && nextBase === apiBase && visitorSession));
    apiBase = nextBase;
    accessToken = nextToken;
    publicMode = false;
    visitorSession = restoreSession && Boolean(nextToken);
    sharedAvailable = false;
    sessionMode = '';
    expiresAt = '';
    publicLimits = {};
    message = '正在验证搜索服务…';
    forgetStored();
    invalidate('changing');
    const current = { base: apiBase, token: accessToken, revision };
    try {
      const health = await fetchJSON(current, '/api/health', { timeout: 15000 });
      if (health.ok !== true) throw failure('该地址未通过寻微服务健康检查。', 'INVALID_BACKEND');
      publicMode = health.public_mode === true;
      sharedAvailable = health.shared_available === true;
      publicLimits = health.public_limits && typeof health.public_limits === 'object' ? { ...health.public_limits } : {};
      let replaced = false;
      if (publicMode && !current.token) await createSession(current, options.mode);
      let platforms;
      try { platforms = await fetchJSON(current, '/api/platforms', { timeout: 15000 }); }
      catch (error) {
        if (!(publicMode && restoreSession && error.status === 401)) throw error;
        await createSession(current, options.mode);
        platforms = await fetchJSON(current, '/api/platforms', { timeout: 15000 });
        replaced = true;
      }
      if (!Array.isArray(platforms.items) || !platforms.items.length || platforms.items.some(item => typeof item.id !== 'string')) throw failure('该服务没有返回可用的平台目录，请检查地址及访问权限。', 'INVALID_BACKEND');
      if (publicMode && visitorSession) sessionInfo(await fetchJSON(current, '/api/session', { timeout: 15000 }));
      if (current.revision !== revision) throw failure('连接已切换。', 'BACKEND_CHANGED');
      connected = true;
      message = replaced ? '原访客会话已失效，已创建新会话。旧历史和 API 配置不会迁移。' : visitorSession ? '已连接独立访客会话' : '搜索服务已连接';
      remember();
      emit('connected');
      return snapshot();
    } catch (error) {
      if (current.revision === revision) { connected = false; message = error.message; emit('failed'); }
      throw error;
    }
  }

  function disconnect() {
    apiBase = '';
    accessToken = '';
    publicMode = false;
    visitorSession = false;
    sharedAvailable = false;
    sessionMode = '';
    expiresAt = '';
    publicLimits = {};
    message = '未连接搜索服务';
    forgetStored();
    invalidate('disconnected');
  }

  async function initialize() {
    let stored;
    try { stored = JSON.parse(sessionStorage.getItem(storageKey) || 'null'); } catch (_) { forgetStored(); }
    const base = stored && typeof stored.apiBase === 'string' ? stored.apiBase : deployment.apiBase || '';
    const token = stored && typeof stored.accessToken === 'string' ? stored.accessToken : '';
    if (!base && mode === 'pages') { message = '未连接搜索服务'; emit('disconnected'); return snapshot(); }
    try { return await connect(base, token, false, { restoreSession: stored?.visitorSession === true, mode: stored?.sessionMode }); } catch (_) { return snapshot(); }
  }

  async function setModelMode(value) {
    if (!connected || !visitorSession) throw failure('当前连接没有独立访客会话。', 'SESSION_REQUIRED');
    if (!['shared','custom'].includes(value)) throw failure('请选择站主 API 或自己配置 API。', 'INVALID_MODE');
    const info = await request('/api/session', { method: 'PUT', body: { mode: value } });
    sessionInfo(info);
    remember();
    emit('model-mode');
    return snapshot();
  }

  window.XunweiConnection = Object.freeze({ mode, snapshot, normalizeBase, request, catalog, searchEngines, engineLinks, connect, disconnect, initialize, setModelMode,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); }
  });
})();
