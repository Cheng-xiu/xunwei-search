"""Short-lived visitor workspaces; owner credentials never enter custom mode."""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import contextmanager
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import os
import secrets
import tempfile
import threading
import time
import uuid

from .ai import validate_base_url
from .providers import canonical_url
from .server import App, DEFAULTS, round_budget
from .storage import Storage, _safe
from .transport import PublicAccessError


LIMITS = {'max_rounds': 3, 'max_concurrent_jobs': 4, 'session_ttl_seconds': 3600,
          'max_sessions': 20, 'requests_per_minute': 240, 'expensive_requests_per_minute': 6,
          'max_documents': 20, 'max_document_bytes': 2_000_000}
AI_FIELDS = frozenset(('api_key', 'base_url', 'model'))


class SessionStorage(Storage):
    """Revocation waits for open SQLite calls, then forbids reopening the file."""
    def __init__(self, path):
        self._session_gate = threading.RLock()
        self._revoked = False
        super().__init__(path)

    @contextmanager
    def _connect(self):
        with self._session_gate:
            if self._revoked:
                raise PublicAccessError(401, '访客会话已结束，请重新连接。')
            with super()._connect() as connection:
                yield connection

    def revoke(self):
        with self._session_gate:
            self._revoked = True

    def add_document(self, title, url, text, platform):
        # Measure the same sanitized UTF-8 fields that Storage will persist.
        # The gate spans quota checking AND insertion, including concurrent HTTP imports.
        title = _safe(str(title or '未命名导入内容').strip()) or '未命名导入内容'
        url = _safe(str(url or '').strip())
        text = _safe(str(text or ''))
        incoming = sum(len(value.encode('utf-8')) for value in (title, url, text))
        with self._session_gate:
            with self._connect() as connection:
                count, used = connection.execute('''
                    SELECT COUNT(*), COALESCE(SUM(length(CAST(title AS BLOB))
                        + length(CAST(url AS BLOB)) + length(CAST(text AS BLOB))), 0)
                    FROM library
                ''').fetchone()
            if count >= LIMITS['max_documents']:
                raise PublicAccessError(400, '访客临时资料库最多20条，请先删除部分资料再导入。')
            if used + incoming > LIMITS['max_document_bytes']:
                raise PublicAccessError(400, '访客临时资料库的标题、链接和正文合计最多2 MB，请先删除资料或减少内容。')
            return super().add_document(title, url, text, platform)


class VisitorApp(App):
    def __init__(self, directory, owner_ai, mode):
        self.mode = mode
        self._owner_ai = dict(owner_ai)
        self._revoked = False
        self.lifetime_cancel = threading.Event()
        super().__init__(directory, environment=False, persist_settings=False, storage_factory=SessionStorage)
        self._custom_config = copy.deepcopy(self.config)
        self.config = self._config_for(mode)

    def _ensure_active(self):
        if self._revoked:
            raise PublicAccessError(401, '访客会话已结束，请重新连接。')

    def _config_for(self, mode):
        if mode not in ('shared', 'custom'):
            raise ValueError('模型模式须为 shared 或 custom。')
        if mode == 'shared' and not self._owner_ai.get('api_key'):
            raise PublicAccessError(409, '站主尚未配置共享 AI；请选择自配 API。')
        config = copy.deepcopy(self._custom_config)
        if mode == 'shared':
            config.update(self._owner_ai)
        config['_public_network'] = True
        return config

    def public_config(self):
        with self.lock:
            self._ensure_active()
            return {**super().public_config(), 'api_mode': self.mode,
                    'shared_available': bool(self._owner_ai.get('api_key')),
                    'ai_config_readonly': self.mode == 'shared', 'public_limits': dict(LIMITS)}

    def set_mode(self, mode):
        with self.lock:
            self._ensure_active()
            if self.summary_jobs or any(job['state'] in ('queued', 'running') for job in self.jobs.values()):
                raise PublicAccessError(409, '搜索或总结运行时不能切换模型模式，请先停止任务。')
            config = self._config_for(mode)
            self.mode, self.config = mode, config

    def save_config(self, data):
        with self.lock:
            self._ensure_active()
            if 'api_mode' in data or 'mode' in data:
                raise ValueError('请使用会话模式接口切换共享或自配 API。')
            clearing = data.get('clear_secrets', [])
            if self.mode == 'shared' and (AI_FIELDS.intersection(data) or
                    isinstance(clearing, list) and 'api_key' in clearing):
                raise PublicAccessError(403, '站主模型配置不可修改；请切换为自配 API 后设置。')
            if self.mode == 'custom' and 'base_url' in data:
                base = data['base_url']
                if not isinstance(base, str) or not base.strip().startswith('https://') or not canonical_url(base.strip()):
                    raise ValueError('公开访客 AI 地址须为标准 HTTPS 公网地址，不得指向本机、内网或非标准端口。')
            result = super().save_config(data)
            self.config['_public_network'] = True
            if self.mode == 'custom':
                self._custom_config = copy.deepcopy(self.config)
            else:
                self._custom_config.update({key: copy.deepcopy(value) for key, value in self.config.items() if key not in AI_FIELDS})
            return result

    @staticmethod
    def _budget(data, default=3):
        budget = round_budget(data.get('max_rounds', default))
        if not 1 <= budget <= LIMITS['max_rounds']:
            raise ValueError('公开访客每次最多3轮，不能使用持续搜索；完成后可按额度继续。')

    def create_job(self, data):
        with self.lock:
            self._ensure_active()
            self._budget(data)
            return super().create_job(data)

    def continue_job(self, job_id, data):
        with self.lock:
            self._ensure_active()
            job = self.get_job(job_id)
            self._budget(data, (job or {}).get('max_rounds', 3))
            return super().continue_job(job_id, data)

    def create_summary(self, job_id):
        with self.lock:
            self._ensure_active()
            return super().create_summary(job_id)

    def revoke(self):
        with self.lock:
            if self._revoked:
                return
            for key, event in self.controls.items():
                event.set()
                self.epochs[key] = self.epochs.get(key, 0) + 1
            for job in self.jobs.values():
                if job['state'] in ('queued', 'running'):
                    self._interrupt_round(job)
                    job.update(state='stopped', stage='stopped', stop_reason='session_ended', message='访客会话已结束。')
            self.summary_jobs.clear()
            self._revoked = True
            self.lifetime_cancel.set()
            self.storage.revoke()
            self.config = copy.deepcopy(DEFAULTS)
            self._custom_config = copy.deepcopy(DEFAULTS)
            self._owner_ai = {}


@dataclass
class Session:
    id: str
    digest: str = field(repr=False)
    expires_at: float
    app: VisitorApp = field(repr=False)
    temporary: object = field(repr=False)
    requests: deque = field(default_factory=deque, repr=False)
    expensive: deque = field(default_factory=deque, repr=False)


class PublicSessions:
    def __init__(self, environ=None, *, clock=time.time, limits=None, start_janitor=True):
        env = os.environ if environ is None else environ
        self.owner_ai = {'api_key': env.get('AI_API_KEY', ''),
                         'base_url': validate_base_url(env.get('AI_BASE_URL') or DEFAULTS['base_url']),
                         'model': env.get('AI_MODEL') or DEFAULTS['model']}
        if not isinstance(self.owner_ai['model'], str) or not 1 <= len(self.owner_ai['model']) <= 150 or any(c in self.owner_ai['model'] for c in '\r\n\x00'):
            raise ValueError('站主 AI_MODEL 配置无效。')
        self.limits = dict(LIMITS, **(limits or {}))
        self.clock = clock
        self.lock = threading.RLock()
        self.sessions = {}
        self.created_by_peer = defaultdict(deque)
        self.inflight = 0
        self._closed = False
        self._stop = threading.Event()
        self._janitor = None
        if start_janitor:
            self._janitor = threading.Thread(target=self._maintain, daemon=True)
            self._janitor.start()

    def capabilities(self):
        return {'public_mode': True, 'session_required': True,
                'shared_available': bool(self.owner_ai.get('api_key')), 'public_limits': dict(self.limits)}

    @staticmethod
    def _rate(queue, now, limit):
        while queue and queue[0] <= now - 60:
            queue.popleft()
        if len(queue) >= limit:
            raise PublicAccessError(429, '请求过于频繁，请一分钟后重试。')
        queue.append(now)

    def _discard(self, session):
        self.sessions.pop(session.digest, None)
        session.app.revoke()
        try:
            session.temporary.cleanup()
        except OSError:
            # Revoked storage cannot be reopened, even if OS cleanup is delayed.
            pass

    def _sweep(self):
        now = self.clock()
        for session in list(self.sessions.values()):
            if session.expires_at <= now:
                self._discard(session)
        for peer, queue in list(self.created_by_peer.items()):
            while queue and queue[0] <= now - 60:
                queue.popleft()
            if not queue:
                self.created_by_peer.pop(peer, None)

    def _maintain(self):
        while not self._stop.wait(10):
            with self.lock:
                self._sweep()

    def describe(self, session):
        return {'session_id': session.id,
                'expires_at': datetime.fromtimestamp(session.expires_at, timezone.utc).isoformat(),
                'mode': session.app.mode, **self.capabilities()}

    def create(self, data, peer):
        with self.lock:
            if self._closed:
                raise PublicAccessError(503, '服务正在关闭，请稍后重连。')
            self._sweep()
            self._rate(self.created_by_peer[str(peer)], self.clock(), 10)
            if len(self.sessions) >= self.limits['max_sessions']:
                raise PublicAccessError(429, '临时访客会话已满，请稍后重试。')
            mode = data.get('mode', 'shared' if self.owner_ai.get('api_key') else 'custom')
            if mode not in ('shared', 'custom'):
                raise ValueError('模型模式须为 shared 或 custom。')
            if mode == 'shared' and not self.owner_ai.get('api_key'):
                raise PublicAccessError(409, '站主尚未配置共享 AI；请选择自配 API。')
            temporary = tempfile.TemporaryDirectory(prefix='xunwei-visitor-')
            try:
                app = VisitorApp(temporary.name, self.owner_ai, mode)
            except Exception:
                temporary.cleanup()
                raise
            token = secrets.token_urlsafe(32)
            digest = hashlib.sha256(token.encode()).hexdigest()
            session = Session(uuid.uuid4().hex, digest, self.clock() + self.limits['session_ttl_seconds'], app, temporary)
            self.sessions[digest] = session
            return {**self.describe(session), 'access_token': token}

    def authenticate(self, token):
        with self.lock:
            self._sweep()
            digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
            session = self.sessions.get(digest)
            if not session or not hmac.compare_digest(digest, session.digest):
                raise PublicAccessError(401, '访客会话无效或已过期，请重新连接；旧会话数据不会迁移。')
            self._rate(session.requests, self.clock(), self.limits['requests_per_minute'])
            return session

    def active(self, session):
        if self._closed or self.sessions.get(session.digest) is not session or session.expires_at <= self.clock():
            raise PublicAccessError(401, '访客会话已结束，请重新连接。')

    @contextmanager
    def work_slot(self, session):
        with self.lock:
            self.active(session)
            self._rate(session.expensive, self.clock(), self.limits['expensive_requests_per_minute'])
            busy = self.inflight
            for item in self.sessions.values():
                with item.app.lock:
                    busy += len(item.app.summary_jobs) + sum(job['state'] in ('queued', 'running') for job in item.app.jobs.values())
            if busy >= self.limits['max_concurrent_jobs']:
                raise PublicAccessError(429, '公开服务的并行任务已满，请稍后重试。')
            self.inflight += 1
        try:
            yield
        finally:
            with self.lock:
                self.inflight -= 1

    def switch(self, session, mode):
        with self.lock:
            self.active(session)
            session.app.set_mode(mode)
            return self.describe(session)

    def revoke(self, session):
        with self.lock:
            self._discard(session)

    def close(self):
        self._stop.set()
        with self.lock:
            self._closed = True
            for session in list(self.sessions.values()):
                self._discard(session)
            self.owner_ai.clear()
        if self._janitor and self._janitor is not threading.current_thread():
            self._janitor.join(1)
