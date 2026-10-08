import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../web/connection.js', import.meta.url), 'utf8');
const platforms = JSON.parse(await readFile(new URL('../web/platforms.json', import.meta.url), 'utf8'));
const json = (value, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(value), json: async () => value });

function setup({ mode = 'pages', apiBase = '', handler, store = new Map(), page = 'https://example.github.io/repo/' } = {}) {
  const calls = [];
  const window = { XUNWEI_DEPLOYMENT: { mode, apiBase } };
  const context = {
    window, document: { baseURI: page }, URL, AbortController, setTimeout, clearTimeout,
    sessionStorage: { getItem: key => store.get(key) || null, setItem: (key, value) => store.set(key, value), removeItem: key => store.delete(key) },
    fetch: async (url, options = {}) => {
      calls.push({ url, options });
      if (handler) return handler(url, options);
      if (url === './platforms.json' || url.endsWith('/api/platforms')) return json(platforms);
      if (url.endsWith('/api/health')) return json({ ok: true, version: 'test' });
      return json({ items: [] });
    }
  };
  Object.defineProperty(context, 'localStorage', { get() { throw new Error('Credentials must not use localStorage'); } });
  vm.runInNewContext(source, context, { filename: 'connection.js' });
  return { connection: window.XunweiConnection, calls, store };
}

test('Pages without a backend reads only the relative static catalog and rejects actions', async () => {
  const { connection, calls } = setup();
  await connection.initialize();
  assert.equal(connection.snapshot().connected, false);
  assert.equal((await connection.request('/api/platforms')).items.length, 16);
  assert.equal((await connection.request('/api/config')).offline, true);
  assert.equal((await connection.request('/api/history')).items.length, 0);
  assert.equal((await connection.request('/api/library')).items.length, 0);
  const engines = await connection.searchEngines();
  assert.equal(engines.items.length, 8);
  assert.equal(engines.selection_supported, false);
  assert.deepEqual(Array.from(engines.items.filter(item => item.available).map(item => item.id)), ['baidu', 'bing', 'google', 'yandex', 'duckduckgo']);
  await assert.rejects(connection.request('/api/search', { method: 'POST', body: { query: 'offline' } }), { code: 'BACKEND_REQUIRED' });
  assert.deepEqual(calls.map(call => call.url), ['./platforms.json']);
});

test('Older backends without an engine catalog remain connected with compatible defaults', async () => {
  const { connection, calls } = setup({ handler: url => {
    if (url.endsWith('/api/health')) return json({ ok: true });
    if (url.endsWith('/api/platforms')) return json(platforms);
    return json({ error: 'Unknown route' }, 404);
  } });
  await connection.connect('https://older.example');
  const catalog = await connection.searchEngines();
  assert.equal(catalog.items.length, 8);
  assert.equal(catalog.selection_supported, false);
  assert.equal(connection.snapshot().connected, true);
  assert(calls.every(call => (call.options.method || 'GET') === 'GET'));
});

test('Engine catalog availability is reported independently of connection authentication', async () => {
  const service = publicService();
  const { connection } = setup({ handler: (url, options) => url.endsWith('/api/search-engines') ? json({ items: [{ id: 'google', label: 'Google', access: 'public_html', available: true, configured: true, description: 'May be blocked', search_url: 'https://www.google.com/search?q={query}' }, { id: 'unknown-script', label: 'Unrecognized' }] }) : service.handler(url, options) });
  await connection.connect('https://public.example');
  const catalog = await connection.searchEngines();
  assert.equal(catalog.selection_supported, true);
  assert.equal(catalog.items.length, 1);
  assert.equal(catalog.items[0].id, 'google');
  assert.equal(connection.snapshot().sessionMode, 'shared');
  assert.equal(service.count(), 1);
});

test('All free manual engines honor selected platform domains without an implicit whole-web scope', async () => {
  const { connection, calls } = setup();
  const engines = (await connection.searchEngines()).items.filter(item => item.available);
  const links = connection.engineLinks({ query: '冷门菜品 & 评价', engines, platforms: platforms.items, selectedPlatforms: ['zhihu', 'bilibili'] });
  assert.equal(links.length, 15);
  assert.equal(new Set(links.map(link => link.engine)).size, 5);
  for (const link of links) {
    assert(['zhihu.com', 'bilibili.com', 'b23.tv'].includes(link.domain));
    assert.equal(link.query, `冷门菜品 & 评价 site:${link.domain}`);
    const parsed = new URL(link.url);
    assert.equal(parsed.protocol, 'https:');
    assert([...parsed.searchParams.values()].includes(link.query));
  }
  assert.equal(calls.length, 0);
});

test('Custom-only manual search stays scoped and only explicit web selection creates a broad link', async () => {
  const { connection } = setup();
  const engines = (await connection.searchEngines()).items.filter(item => item.id === 'bing');
  const options = { query: '校园 食堂', engines, platforms: platforms.items, selectedPlatforms: [], customSites: [{ name: '校园论坛', domain: 'bbs.example.org' }, { domain: 'bbs.example.org' }, { domain: 'bad.example/path' }] };
  const restricted = connection.engineLinks(options);
  assert.equal(restricted.length, 1);
  assert.equal(restricted[0].query, '校园 食堂 site:bbs.example.org');
  const broad = connection.engineLinks({ ...options, selectedPlatforms: ['web'] });
  assert.equal(broad.length, 2);
  assert.equal(broad.filter(link => !link.domain).length, 1);
  assert.equal(broad.find(link => !link.domain).scope, '公开全网');
  assert.equal(connection.engineLinks({ ...options, customSites: [] }).length, 0);
});

test('Manual search ignores credentialed, unsafe, or non-template engine URLs', async () => {
  const { connection } = setup();
  const links = connection.engineLinks({ query: '<script> & #', platforms: platforms.items, selectedPlatforms: ['web'], engines: [
    { id: 'bad-js', search_url: 'javascript:alert({query})' },
    { id: 'bad-http', search_url: 'http://search.example?q={query}' },
    { id: 'bad-user', search_url: 'https://user:password@search.example?q={query}' },
    { id: 'bad-template', search_url: 'https://search.example?q={query}&again={query}' },
    { id: 'no-template', search_url: '' },
    { id: 'valid', label: 'Search', search_url: 'https://search.example?q={query}' }
  ] });
  assert.equal(links.length, 1);
  assert.equal(new URL(links[0].url).searchParams.get('q'), '<script> & #');
});

test('Local default retains same-origin API paths after health and catalog verification', async () => {
  const { connection, calls } = setup({ mode: 'local', page: 'http://127.0.0.1:8877/' });
  await connection.initialize();
  assert.equal(connection.snapshot().connected, true);
  await connection.request('/api/config');
  assert.deepEqual(calls.map(call => call.url), ['/api/health', '/api/platforms', '/api/config']);
  assert(calls.every(call => !call.options.headers.Authorization));
});

test('Concurrent-search capabilities and limits are read from health and cleared for legacy connections', async () => {
  const { connection } = setup({ handler: url => url.endsWith('/api/health') ? json(url.startsWith('https://modern.example') ? { ok: true, features: { concurrent_search: true, disabled_feature: false }, search_limits: { max_active_jobs: 4, max_search_concurrency: 12 } } : { ok: true }) : json(platforms) });
  await connection.connect('https://modern.example');
  assert.deepEqual(Array.from(connection.snapshot().features), ['concurrent_search']);
  assert.equal(connection.snapshot().searchLimits.max_active_jobs, 4);
  const snapshot = connection.snapshot(); snapshot.features.push('modified'); snapshot.searchLimits.max_active_jobs = 99;
  assert.equal(connection.snapshot().features.length, 1);
  assert.equal(connection.snapshot().searchLimits.max_active_jobs, 4);
  await connection.connect('https://legacy.example');
  assert.equal(connection.snapshot().features.length, 0);
  assert.equal(Object.keys(connection.snapshot().searchLimits).length, 0);
});

test('Disconnected task discovery stays offline and concurrent task reads all abort on backend switch', async () => {
  const pending = [];
  const { connection, calls } = setup({ handler: url => {
    if (url.endsWith('/api/health')) return json({ ok: true, features: ['concurrent_search'] });
    if (url.endsWith('/api/platforms')) return json(platforms);
    return new Promise(resolve => pending.push(resolve));
  } });
  assert.equal((await connection.request('/api/jobs')).items.length, 0);
  assert.equal(calls.length, 0);
  await connection.connect('https://first.example');
  const reads = [connection.request('/api/jobs/a'), connection.request('/api/jobs/b')];
  const jobCalls = calls.slice(-2);
  await connection.connect('https://second.example');
  assert(jobCalls.every(call => call.options.signal.aborted));
  pending.forEach(resolve => resolve(json({ state: 'running' })));
  for (const read of reads) await assert.rejects(read, { code: 'BACKEND_CHANGED' });
  assert(!calls.some(call => call.url.startsWith('https://second.example/api/jobs/')));
});

test('Remote prefix and Bearer token are applied only after explicit configuration', async () => {
  const { connection, calls, store } = setup();
  await connection.connect('https://backend.example/prefix/', 'test-access-token');
  await connection.request('/api/jobs/abc/continue', { method: 'POST', body: { depth: 'research' } });
  assert.deepEqual(calls.map(call => call.url), ['https://backend.example/prefix/api/health', 'https://backend.example/prefix/api/platforms', 'https://backend.example/prefix/api/jobs/abc/continue']);
  assert(calls.every(call => call.options.headers.Authorization === 'Bearer test-access-token'));
  assert(calls.every(call => call.options.credentials === 'omit' && call.options.redirect === 'error'));
  assert.equal(calls.at(-1).options.body, '{"depth":"research"}');
  assert.equal(calls[0].options.headers['Content-Type'], undefined);
  assert.equal(calls.at(-1).options.headers['Content-Type'], 'application/json');
  assert.equal(store.size, 1);
  assert(!JSON.stringify(connection.snapshot()).includes('test-access-token'));
  connection.disconnect();
  assert.equal(store.size, 0);
  assert.equal(connection.snapshot().hasToken, false);
});

test('Public health success is insufficient when authenticated platform access fails', async () => {
  const { connection, calls, store } = setup({ handler: url => url.endsWith('/api/health') ? json({ ok: true }) : json({ error: 'denied' }, 401) });
  await assert.rejects(connection.connect('https://backend.example', 'wrong-test-token'), { status: 401 });
  assert.equal(connection.snapshot().connected, false);
  assert.equal(store.size, 0);
  await assert.rejects(connection.request('/api/search', { method: 'POST' }), { code: 'BACKEND_REQUIRED' });
  assert.equal(calls.length, 2);
});

test('Invalid backend addresses and multiline tokens cause no network or connection mutation', async () => {
  const { connection, calls } = setup();
  await connection.connect('https://backend.example', 'good-test-token');
  const revision = connection.snapshot().revision;
  for (const url of ['', './api', '//backend.example', 'http://remote.example', 'https://user:pass@backend.example', 'https://backend.example?token=bad', 'https://backend.example#part', 'https://backend.example/?', 'https://backend.example\\other', 'ftp://backend.example', 'https://back\nend.example']) {
    await assert.rejects(connection.connect(url), { code: 'INVALID_BACKEND' });
  }
  await assert.rejects(connection.connect('https://backend.example', 'line\nbreak'), { code: 'INVALID_TOKEN' });
  assert.equal(connection.snapshot().revision, revision);
  assert.equal(connection.snapshot().connected, true);
  assert.equal(calls.length, 2);
  assert.equal(connection.normalizeBase('http://localhost:8877/'), 'http://localhost:8877');
  assert.equal(connection.normalizeBase('http://127.0.0.1:8877'), 'http://127.0.0.1:8877');
  assert.throws(() => connection.normalizeBase('http://[::1]:8877'), { code: 'INVALID_BACKEND' });
  assert.throws(() => connection.normalizeBase('http://127.1.2.3:8877'), { code: 'INVALID_BACKEND' });
});

test('Switching backends aborts in-flight reads and cannot route an old job to the new host', async () => {
  let finishOld;
  const { connection, calls } = setup({ handler: url => {
    if (url.endsWith('/api/health')) return json({ ok: true });
    if (url.endsWith('/api/platforms')) return json(platforms);
    return new Promise(resolve => { finishOld = () => resolve(json({ id: 'old-job' })); });
  } });
  await connection.connect('https://old.example', 'old-test-token');
  const oldRead = connection.request('/api/jobs/old-job');
  const oldCall = calls.at(-1);
  await connection.connect('https://new.example', '', true);
  assert.equal(oldCall.options.signal.aborted, true);
  finishOld();
  await assert.rejects(oldRead, { code: 'BACKEND_CHANGED' });
  assert.equal(connection.snapshot().hasToken, false);
  assert(calls.filter(call => call.url.startsWith('https://new.example')).every(call => !call.options.headers.Authorization));
  assert(!calls.some(call => call.url === 'https://new.example/api/jobs/old-job'));
});

test('Tokens restore only from the current session and repository path, with same-address blank preservation', async () => {
  const original = setup();
  await original.connection.connect('https://backend.example', 'session-test-token');
  await original.connection.connect('https://backend.example/', '', true);
  assert.equal(original.calls.at(-1).options.headers.Authorization, 'Bearer session-test-token');
  const restored = setup({ store: original.store });
  await restored.connection.initialize();
  assert.equal(restored.calls[0].options.headers.Authorization, 'Bearer session-test-token');
  const anotherRepo = setup({ store: original.store, page: 'https://example.github.io/another-repo/' });
  await anotherRepo.connection.initialize();
  assert.equal(anotherRepo.calls.length, 0);
  assert.equal(anotherRepo.connection.snapshot().connected, false);
});

test('Malformed platform response fails verification even when health is valid', async () => {
  const { connection } = setup({ handler: url => url.endsWith('/api/health') ? json({ ok: true }) : json({ items: [] }) });
  await assert.rejects(connection.connect('https://backend.example'), { code: 'INVALID_BACKEND' });
  assert.equal(connection.snapshot().connected, false);
});

function publicService({ shared = true } = {}) {
  const sessions = new Map();
  let created = 0;
  let busy = false;
  const limits = { max_rounds: 3, session_ttl_seconds: 3600 };
  const info = session => ({ session_id: session.id, mode: session.mode, expires_at: '2030-01-01T01:00:00Z', shared_available: shared, public_limits: limits });
  const handler = (url, options = {}) => {
    if (url.endsWith('/api/health')) return json({ ok: true, public_mode: true, session_required: true, shared_available: shared, public_limits: limits });
    const method = options.method || 'GET';
    if (url.endsWith('/api/session') && method === 'POST') {
      const body = JSON.parse(options.body);
      if (body.mode === 'shared' && !shared) return json({ error: '站主 API 尚未开放' }, 409);
      const id = `visitor-${++created}`;
      const session = { id, mode: body.mode || (shared ? 'shared' : 'custom'), config: {} };
      sessions.set(id, session);
      return json({ ...info(session), access_token: id });
    }
    const token = options.headers?.Authorization?.replace(/^Bearer /, '');
    if (token === 'private-admin-test-token') return json(url.endsWith('/api/platforms') ? platforms : {});
    const session = sessions.get(token);
    if (!session) return json({ error: 'Expired visitor session' }, 401);
    if (url.endsWith('/api/platforms')) return json(platforms);
    if (url.endsWith('/api/session')) {
      if (method === 'PUT') {
        if (busy) return json({ error: '搜索运行中不能切换模型模式' }, 409);
        const mode = JSON.parse(options.body).mode;
        if (mode === 'shared' && !shared) return json({ error: '站主 API 尚未开放' }, 409);
        session.mode = mode;
      }
      return json(info(session));
    }
    if (url.endsWith('/api/config')) {
      if (method === 'PUT') session.config = { ...session.config, ...JSON.parse(options.body) };
      return json({ api_mode: session.mode, shared_available: shared, ai_config_readonly: session.mode === 'shared', has_api_key: Boolean(session.config.api_key) });
    }
    return json({ items: [] });
  };
  return { handler, sessions, count: () => created, setBusy: value => { busy = value; } };
}

test('Public backend creates a visitor session without a site token and uses its Bearer', async () => {
  const service = publicService();
  const { connection, calls, store } = setup({ handler: service.handler });
  await connection.connect('https://public.example');
  assert.equal(connection.snapshot().visitorSession, true);
  assert.equal(connection.snapshot().publicMode, true);
  assert.equal(connection.snapshot().sessionMode, 'shared');
  assert.equal(connection.snapshot().sharedAvailable, true);
  assert.equal(connection.snapshot().publicLimits.max_rounds, 3);
  const creation = calls.find(call => call.options.method === 'POST');
  assert.equal(creation.url, 'https://public.example/api/session');
  assert.equal(creation.options.body, '{}');
  assert.equal(creation.options.headers.Authorization, undefined);
  assert.equal(calls.find(call => call.url.endsWith('/api/platforms')).options.headers.Authorization, 'Bearer visitor-1');
  const stored = JSON.parse([...store.values()][0]);
  assert.equal(stored.visitorSession, true);
  assert.equal(stored.accessToken, 'visitor-1');
  assert.equal(stored.sessionMode, 'shared');
  assert(!JSON.stringify(connection.snapshot()).includes('visitor-1'));
});

test('Public visitors can use custom configuration without putting AI keys in browser storage', async () => {
  const service = publicService();
  const { connection, store, calls } = setup({ handler: service.handler });
  await connection.connect('https://public.example', '', false, { mode: 'custom' });
  assert.equal(connection.snapshot().sessionMode, 'custom');
  await connection.request('/api/config', { method: 'PUT', body: { base_url: 'https://model.example/v1', model: 'test-model', api_key: 'synthetic-custom-ai-key' } });
  assert.equal(service.sessions.get('visitor-1').config.api_key, 'synthetic-custom-ai-key');
  assert(!JSON.stringify([...store.values()]).includes('synthetic-custom-ai-key'));
  assert.equal(calls.at(-1).options.headers.Authorization, 'Bearer visitor-1');
  assert.equal(service.sessions.size, 1);
});

test('Manual private/admin tokens on a public backend do not create visitor sessions', async () => {
  const service = publicService();
  const { connection, calls } = setup({ handler: service.handler });
  await connection.connect('https://public.example', 'private-admin-test-token');
  assert.equal(connection.snapshot().connected, true);
  assert.equal(connection.snapshot().visitorSession, false);
  assert.equal(service.count(), 0);
  assert(!calls.some(call => call.options.method === 'POST'));
});

test('Restored visitor sessions are validated and reused, expired sessions are replaced explicitly', async () => {
  const service = publicService();
  const first = setup({ handler: service.handler });
  await first.connection.connect('https://public.example');
  const restored = setup({ handler: service.handler, store: first.store });
  await restored.connection.initialize();
  assert.equal(service.count(), 1);
  assert.equal(restored.connection.snapshot().visitorSession, true);
  assert(!restored.calls.some(call => call.options.method === 'POST'));
  service.sessions.delete('visitor-1');
  const expired = setup({ handler: service.handler, store: first.store });
  await expired.connection.initialize();
  assert.equal(service.count(), 2);
  assert.equal(expired.connection.snapshot().connected, true);
  assert(expired.connection.snapshot().message.includes('旧历史和 API 配置不会迁移'));
  const creation = expired.calls.find(call => call.options.method === 'POST');
  assert.equal(creation.options.headers.Authorization, undefined);
  assert.equal(expired.calls.at(-1).options.headers.Authorization, 'Bearer visitor-2');
});

test('Session expiry during a write disconnects without creating a session or replaying the write', async () => {
  const service = publicService();
  const { connection, calls, store } = setup({ handler: service.handler });
  await connection.connect('https://public.example');
  service.sessions.delete('visitor-1');
  await assert.rejects(connection.request('/api/search', { method: 'POST', body: { query: 'offline regression' } }), { status: 401 });
  assert.equal(connection.snapshot().connected, false);
  assert.equal(connection.snapshot().hasToken, false);
  assert.equal(store.size, 0);
  assert.equal(service.count(), 1);
  assert.equal(calls.filter(call => call.url.endsWith('/api/search')).length, 1);
});

test('Model switching uses the session route, persists mode, and honors a busy refusal', async () => {
  const service = publicService();
  const { connection, calls, store } = setup({ handler: service.handler });
  await connection.connect('https://public.example');
  await connection.setModelMode('custom');
  assert.equal(connection.snapshot().sessionMode, 'custom');
  assert.equal(calls.at(-1).url, 'https://public.example/api/session');
  assert.equal(calls.at(-1).options.method, 'PUT');
  assert.equal(calls.at(-1).options.body, '{"mode":"custom"}');
  assert.equal(JSON.parse([...store.values()][0]).sessionMode, 'custom');
  service.setBusy(true);
  await assert.rejects(connection.setModelMode('shared'), { status: 409 });
  assert.equal(connection.snapshot().sessionMode, 'custom');
  assert.equal(connection.snapshot().connected, true);
  assert(!calls.some(call => call.url.endsWith('/api/config')));
});

test('Unavailable owner API is not advertised or silently substituted after an explicit shared choice', async () => {
  const service = publicService({ shared: false });
  const explicit = setup({ handler: service.handler });
  await assert.rejects(explicit.connection.connect('https://public.example', '', false, { mode: 'shared' }), { status: 409 });
  assert.equal(explicit.connection.snapshot().connected, false);
  assert.equal(service.count(), 0);
  const automatic = setup({ handler: service.handler, apiBase: 'https://public.example' });
  await automatic.connection.initialize();
  assert.equal(automatic.connection.snapshot().sessionMode, 'custom');
  assert.equal(automatic.connection.snapshot().sharedAvailable, false);
  assert.equal(automatic.connection.snapshot().connected, true);
});
