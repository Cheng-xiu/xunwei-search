"""Local HTTP server with explicit, authenticated remote deployment support."""
from __future__ import annotations

import argparse
import copy
import json
import mimetypes
import os
import re
import socket
import threading
import uuid
import webbrowser
import urllib.request
from contextlib import nullcontext
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, unquote

from .ai import test_connection, validate_base_url
from .engine import run_search
from .providers import (canonical_url, platform_of, platform_catalog, normalize_custom_sites,
                        PLATFORM_LABELS, SEARCH_ENGINE_IDS, search_engine_catalog, available_providers)
from .safety import check_query
from .storage import Storage
from .summarizer import summarize_results
from .transport import CORS_HEADERS, CORS_METHODS, PublicAccessError, TransportPolicy, bearer_token, validate_bind_host, validate_port

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {'base_url': 'https://api.a6api.com/v1', 'model': 'deepseek-v4.1-flash',
            'api_key': '', 'tavily_key': '', 'brave_key': '', 'searxng_url': '',
            'custom_sites': [], 'search_engines': [], 'search_concurrency': 4}
SECRET_KEYS = ('api_key', 'tavily_key', 'brave_key')
FINISHED_STATES = ('done', 'stopped', 'awaiting_user', 'error')
PUBLIC_READ_TIMEOUT = 10.0
PUBLIC_MAX_CONNECTIONS = 64
MAX_ACTIVE_JOBS = 4
MAX_SEARCH_CONCURRENCY = 12


def round_budget(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 12:
        raise ValueError('轮数须为 1–12；0 表示持续搜索至手动停止或无可继续方向。')
    return value


def normalize_search_engines(value):
    """An empty list means automatic selection; IDs never contain secrets."""
    if not isinstance(value, list) or len(value) > len(SEARCH_ENGINE_IDS) or any(
            not isinstance(item, str) or item not in SEARCH_ENGINE_IDS for item in value):
        raise ValueError('搜索引擎须为支持的来源编号数组。')
    return list(dict.fromkeys(value))


def validate_search_concurrency(value, max_concurrency=MAX_SEARCH_CONCURRENCY):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= max_concurrency:
        raise ValueError(f'并发检索数须为 1–{max_concurrency} 的整数。')
    return value


class App:
    def __init__(self, data_dir=None, *, environment=True, persist_settings=True, storage_factory=Storage):
        self.data_dir = Path(data_dir or os.environ.get('XUNWEI_DATA_DIR') or ROOT / '.local')
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / 'settings.json'
        self.storage = storage_factory(str(self.data_dir / 'library.sqlite3'))
        self.persist_settings = persist_settings
        self.lock = threading.RLock()
        self.jobs = {}
        self.summary_jobs = set()
        self.controls = {}
        self.epochs = {}
        self.previous_summaries = {}
        self.max_active_jobs = MAX_ACTIVE_JOBS
        self.max_search_concurrency = MAX_SEARCH_CONCURRENCY
        self.config = copy.deepcopy(DEFAULTS)
        if persist_settings and self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding='utf-8-sig'))
                self.config.update({k: v for k, v in data.items()
                                    if k in DEFAULTS and isinstance(DEFAULTS[k], str) and isinstance(v, str)})
                self.config['custom_sites'] = normalize_custom_sites(data.get('custom_sites', []))
                self.config['search_engines'] = normalize_search_engines(data.get('search_engines', []))
                self.config['search_concurrency'] = validate_search_concurrency(data.get('search_concurrency', 4))
            except (ValueError, OSError):
                print('本地配置无法读取，已使用默认配置。请在设置中重新保存。')
        for setting, env in [('api_key', 'AI_API_KEY'), ('base_url', 'AI_BASE_URL'), ('model', 'AI_MODEL'),
                             ('tavily_key', 'TAVILY_API_KEY'), ('brave_key', 'BRAVE_API_KEY'), ('searxng_url', 'SEARXNG_URL')]:
            if environment and os.environ.get(env):
                self.config[setting] = os.environ[env]

    def public_config(self):
        with self.lock:
            return {**{k: copy.deepcopy(v) for k, v in self.config.items() if k in DEFAULTS and k not in SECRET_KEYS},
                    **{'has_' + k: bool(self.config.get(k)) for k in SECRET_KEYS}}

    def save_config(self, data):
        with self.lock:
            config = {key: copy.deepcopy(value) for key, value in self.config.items() if key in DEFAULTS}
            for key in DEFAULTS:
                if key not in data:
                    continue
                value = data[key]
                if key == 'custom_sites':
                    config[key] = normalize_custom_sites(value)
                    continue
                if key == 'search_engines':
                    config[key] = normalize_search_engines(value)
                    continue
                if key == 'search_concurrency':
                    config[key] = validate_search_concurrency(value, self.max_search_concurrency)
                    continue
                if not isinstance(value, str) or len(value) > 4096 or any(c in value for c in '\r\n\x00'):
                    raise ValueError('配置字段须为单行文本。')
                if key in SECRET_KEYS and not value.strip():
                    continue
                config[key] = value.strip()
            for key in data.get('clear_secrets', []) if isinstance(data.get('clear_secrets', []), list) else []:
                if key in SECRET_KEYS:
                    config[key] = ''
            config['base_url'] = validate_base_url(config['base_url'])
            if not config['model'] or len(config['model']) > 150:
                raise ValueError('请输入有效模型名称。')
            if config['searxng_url']:
                config['searxng_url'] = validate_base_url(config['searxng_url'])
            if self.persist_settings:
                temporary = self.config_path.with_suffix('.tmp')
                temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
                try:
                    temporary.chmod(0o600)
                except OSError:
                    pass
                temporary.replace(self.config_path)
            self.config = config
        return self.public_config()

    def create_job(self, data):
        query = str(data.get('query', '')).strip()
        if not 2 <= len(query) <= 500:
            raise ValueError('搜索问题须为 2–500 个字符。')
        if re.search(r'\bsk-[A-Za-z0-9_-]{16,}', query):
            raise ValueError('请勿在搜索框填写 API 密钥；请在设置中配置。')
        safety = check_query(query)
        if not safety['allowed']:
            raise ValueError(safety['message'])
        platforms = data.get('platforms', ['bilibili', 'xiaohongshu', 'zhihu', 'web'])
        sites = normalize_custom_sites(data.get('custom_sites', self.config.get('custom_sites', [])))
        if not isinstance(platforms, list) or (not platforms and not sites) or any(p not in PLATFORM_LABELS for p in platforms):
            raise ValueError('请至少选择一个有效搜索平台。')
        max_rounds = round_budget(data.get('max_rounds', 3))
        depth = data.get('depth', 'quick')
        if depth not in ('quick', 'deep', 'research'):
            raise ValueError('检索深度须为 quick、deep 或 research。')
        with self.lock:
            concurrency = validate_search_concurrency(data.get('search_concurrency', self.config['search_concurrency']), self.max_search_concurrency)
            self._check_job_capacity()
            if len(self.jobs) > 100:
                for key in list(self.jobs):
                    if self.jobs[key]['state'] in FINISHED_STATES and key not in self.summary_jobs:
                        del self.jobs[key]
                        self.controls.pop(key, None)
                        self.epochs.pop(key, None)
                        self.previous_summaries.pop(key, None)
                        if len(self.jobs) <= 80:
                            break
            job_id = uuid.uuid4().hex
            job = {'id': job_id, 'query': query, 'platforms': list(dict.fromkeys(platforms)), 'depth': depth,
                   'search_engines': [item for item in available_providers(self.config) if item in SEARCH_ENGINE_IDS],
                   'search_concurrency': concurrency,
                   'custom_sites': sites, 'adaptive': data.get('adaptive', True) is True,
                   'max_rounds': max_rounds, 'round': 0, 'rounds': [], 'searches_count': 0,
                   'use_ai': data.get('use_ai', True) is True, 'fetch_pages': data.get('fetch_pages', True) is True,
                   'public_post_only': safety.get('public_post_only', False),
                   'state': 'queued', 'stage': 'queued', 'progress': 0, 'message': '等待开始',
                   'results': [], 'provider_status': [], 'warnings': safety.get('warnings', []),
                   'native_links': [], 'created_at': datetime.now(timezone.utc).isoformat()}
            self.jobs[job_id] = job
            config = copy.deepcopy(self.config)
            self.controls[job_id] = threading.Event()
            self.epochs[job_id] = 1
            config.update(_cancel_event=self.controls[job_id], _run_token=1)
        threading.Thread(target=self._run, args=(job_id, config), daemon=True).start()
        return {'job_id': job_id}

    def _run(self, job_id, config):
        token = config.get('_run_token')
        def current():
            return token is None or self.epochs.get(job_id) == token
        def update(**fields):
            with self.lock:
                if current():
                    incoming_summary = fields.get('ai_summary', {})
                    previous_summary = self.jobs[job_id].get('ai_summary', {})
                    if incoming_summary.get('state') == 'running' and previous_summary.get('state') == 'ready':
                        self.previous_summaries[job_id] = copy.deepcopy(previous_summary)
                    elif incoming_summary.get('state') in ('ready', 'empty', 'error'):
                        self.previous_summaries.pop(job_id, None)
                    self.jobs[job_id].update(fields)
                    # Keep completed rounds recoverable if the app is closed.
                    if self.jobs[job_id].get('rounds'):
                        checkpoint = copy.deepcopy(self.jobs[job_id])
                        checkpoint.update(state='awaiting_user', stage='waiting', stop_reason='interrupted',
                                          message='本轮结果已保存；可继续搜索。')
                        self._interrupt_round(checkpoint)
                        self.storage.save_job(checkpoint)
        try:
            with self.lock:
                if not current():
                    return
                job = copy.deepcopy(self.jobs[job_id])
            run_search(job, config, self.storage, update)
        except Exception:
            # Do not return upstream errors or stack frames containing request secrets.
            update(state='error', stage='error', progress=100, error='搜索过程中出现异常。请重试或减少搜索范围。', message='搜索未完成')
        finally:
            with self.lock:
                if not current():
                    return
                snapshot = copy.deepcopy(self.jobs[job_id])
                # A manual summary can start immediately after the search marks
                # itself done. Persist a recoverable state, never a running one.
                if snapshot.get('ai_summary', {}).get('state') == 'running':
                    snapshot['ai_summary'] = {'state': 'error', 'points': [], 'limitations': [],
                                               'source_count': 0, 'considered_count': 0,
                                               'message': '总结未保存完成，可重新生成；搜索结果已保留。'}
                self._interrupt_round(snapshot)
                self.storage.save_job(snapshot)

    def get_job(self, job_id):
        if not re.fullmatch(r'[a-f0-9]{32}', job_id):
            return None
        with self.lock:
            job = self.jobs.get(job_id)
            if job:
                return copy.deepcopy(job)
        return self.storage.get_job(job_id)

    def _active_job_count(self):
        return sum(j['state'] in ('queued', 'running') for j in self.jobs.values()) + len(self.summary_jobs)

    def _check_job_capacity(self):
        # The caller holds self.lock, so simultaneous HTTP requests cannot
        # both reserve the same remaining job slot.
        if self._active_job_count() >= self.max_active_jobs:
            raise ValueError(f'已有 {self.max_active_jobs} 个搜索或总结任务正在运行，请等待或停止部分任务。')

    def list_jobs(self):
        with self.lock:
            fields = ('id', 'query', 'state', 'stage', 'progress', 'message', 'round', 'created_at',
                      'resumed_at', 'depth', 'max_rounds', 'search_concurrency')
            items = []
            for job in self.jobs.values():
                item = {key: copy.deepcopy(job[key]) for key in fields if key in job}
                item.update(ai_summary_state=(job.get('ai_summary') or {}).get('state', 'empty'),
                            active=job['state'] in ('queued', 'running') or job['id'] in self.summary_jobs)
                items.append(item)
            items.sort(key=lambda item: item.get('created_at', ''), reverse=True)
            return {'items': items, 'active_jobs': self._active_job_count(),
                    'max_active_jobs': self.max_active_jobs, 'max_search_concurrency': self.max_search_concurrency}

    def create_summary(self, job_id):
        with self.lock:
            job = self.get_job(job_id)
            if not job:
                raise LookupError('搜索记录不存在。')
            if job['state'] not in ('done', 'awaiting_user', 'stopped'):
                raise ValueError('请等待本轮搜索完成后再生成总结。')
            if job_id in self.summary_jobs:
                return {'job_id': job_id}
            if not job.get('results'):
                raise ValueError('这次搜索没有可供总结的结果，请先搜索或导入资料。')
            safety = check_query(job.get('query', ''))
            if not safety['allowed']:
                raise ValueError(safety['message'])
            if not self.config.get('api_key'):
                raise ValueError('请先在设置中填写 AI API 密钥，再生成总结。')
            self._check_job_capacity()
            # Persist the completed retrieval before changing the epoch: an
            # immediate manual action must not race away its final save.
            self.storage.save_job(copy.deepcopy(job))
            self.summary_jobs.add(job_id)
            self.previous_summaries[job_id] = copy.deepcopy(job.get('ai_summary'))
            job['ai_summary'] = {'state': 'running', 'points': [], 'limitations': [], 'source_count': 0,
                                 'considered_count': 0, 'message': '正在根据这次搜索的已有结果生成 AI 总结…'}
            self.jobs[job_id] = job
            snapshot = copy.deepcopy(job)
            config = copy.deepcopy(self.config)
            self.controls[job_id] = threading.Event()
            self.epochs[job_id] = self.epochs.get(job_id, 0) + 1
            config.update(_cancel_event=self.controls[job_id], _run_token=self.epochs[job_id])
        threading.Thread(target=self._summarize, args=(snapshot, config), daemon=True).start()
        return {'job_id': job_id}

    def _summarize(self, job, config):
        try:
            summary = summarize_results(job['query'], job['results'], config, job.get('plan'), job.get('warnings'))
        except Exception:
            summary = {'state': 'error', 'points': [], 'limitations': [], 'source_count': 0,
                       'considered_count': 0, 'message': 'AI 总结暂未完成，搜索结果已保留，可稍后重试。'}
        with self.lock:
            if config.get('_run_token') is not None and self.epochs.get(job['id']) != config['_run_token']:
                return
            job['ai_summary'] = summary
            self.jobs[job['id']] = job
            try:
                self.storage.save_job(copy.deepcopy(job))
            finally:
                self.summary_jobs.discard(job['id'])
                self.previous_summaries.pop(job['id'], None)

    @staticmethod
    def _interrupt_round(job):
        if (job.get('progress_report') or {}).get('state') == 'running':
            job['progress_report'].update(state='stopped', message='本轮进展报告已中断，已有线索已保留。')
        for record in job.get('rounds', []):
            if (record.get('report') or {}).get('state') == 'running':
                record['report'].update(state='stopped', message='本轮进展报告已中断，已有线索与此前报告已保留。')
            if record.get('state') == 'running':
                record['state'] = 'stopped'
                for task in record.get('queries', []):
                    if task.get('status') in ('queued', 'running'):
                        task['status'] = 'cancelled'

    def stop_job(self, job_id):
        with self.lock:
            job = self.get_job(job_id)
            if not job:
                raise LookupError('搜索记录不存在。')
            summary_only = job_id in self.summary_jobs
            if job['state'] not in ('queued', 'running') and not summary_only:
                return {'job_id': job_id}
            cancel = self.controls.get(job_id)
            if cancel:
                cancel.set()
            self.epochs[job_id] = self.epochs.get(job_id, 0) + 1
            self.summary_jobs.discard(job_id)
            if job.get('ai_summary', {}).get('state') == 'running':
                previous = self.previous_summaries.pop(job_id, None)
                if previous and not summary_only:
                    previous['stale'] = True
                job['ai_summary'] = previous or {'state': 'disabled', 'points': [], 'limitations': [],
                                                 'source_count': 0, 'considered_count': 0,
                                                 'message': '已停止 AI 总结，已有搜索结果已保留。'}
            if summary_only:
                job['message'] = '已停止本次总结；已有结果已保留。'
            else:
                self._interrupt_round(job)
                job.update(state='stopped', stage='stopped', progress=100, stop_reason='user',
                           message='已停止搜索，已有结果已保留；不会安排新请求，在途请求可能稍后结束。',
                           completed_at=datetime.now(timezone.utc).isoformat())
            self.jobs[job_id] = job
            self.storage.save_job(copy.deepcopy(job))
            return {'job_id': job_id}

    def continue_job(self, job_id, data):
        with self.lock:
            job = self.get_job(job_id)
            if not job:
                raise LookupError('搜索记录不存在。')
            if job['state'] not in ('done', 'awaiting_user', 'stopped') or job_id in self.summary_jobs:
                raise ValueError('请等待当前搜索或总结结束后再继续。')
            self._check_job_capacity()
            safety = check_query(job.get('query', ''))
            if not safety['allowed']:
                raise ValueError(safety['message'])
            budget = round_budget(data.get('max_rounds', job.get('max_rounds', 3)))
            depth = data.get('depth', job.get('depth', 'quick'))
            if depth not in ('quick', 'deep', 'research'):
                raise ValueError('检索深度须为 quick、deep 或 research。')
            concurrency = validate_search_concurrency(data.get('search_concurrency', self.config['search_concurrency']), self.max_search_concurrency)
            self.storage.save_job(copy.deepcopy(job))
            job.update(state='queued', stage='adapting', progress=0, adaptive=True, max_rounds=budget, depth=depth,
                       search_engines=[item for item in available_providers(self.config) if item in SEARCH_ENGINE_IDS],
                       search_concurrency=concurrency,
                       message='准备根据已有线索继续深挖。', resumed_at=datetime.now(timezone.utc).isoformat())
            job.pop('stop_reason', None)
            job.pop('completed_at', None)
            job.pop('error', None)
            job.setdefault('custom_sites', [])
            self.jobs[job_id] = job
            self.controls[job_id] = threading.Event()
            self.epochs[job_id] = self.epochs.get(job_id, 0) + 1
            config = copy.deepcopy(self.config)
            config.update(_cancel_event=self.controls[job_id], _run_token=self.epochs[job_id])
        threading.Thread(target=self._run, args=(job_id, config), daemon=True).start()
        return {'job_id': job_id}


class LocalHTTPServer(ThreadingHTTPServer):
    # Windows permits two active listeners with SO_REUSEADDR; reserve the port
    # exclusively so clicking the launcher twice cannot create a hidden server.
    allow_reuse_address = False

    def __init__(self, server_address, handler, bind_and_activate=True, *, transport=None):
        self.transport = transport or TransportPolicy.from_environment(server_address[0])
        self._public_connections = threading.BoundedSemaphore(PUBLIC_MAX_CONNECTIONS) if self.transport.public_mode else None
        super().__init__(server_address, handler, bind_and_activate)

    def process_request(self, request, client_address):
        slots = self._public_connections
        if slots is not None and not slots.acquire(blocking=False):
            try:
                request.settimeout(0.2)
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\nCache-Control: no-store\r\nVary: Origin\r\n\r\n')
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            if slots is not None:
                slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            if self._public_connections is not None:
                self._public_connections.release()

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class IPv6HTTPServer(LocalHTTPServer):
    address_family = socket.AF_INET6


class Handler(BaseHTTPRequestHandler):
    server_version = 'Xunwei/1.4'

    def setup(self):
        super().setup()
        self._read_deadline_lock = threading.Lock()
        self._read_deadline = None
        self._read_expired = False
        if self.transport.public_mode:
            self.connection.settimeout(PUBLIC_READ_TIMEOUT)
            self._start_read_deadline()

    def _start_read_deadline(self):
        if not self.transport.public_mode:
            return
        self._cancel_read_deadline()
        self._read_expired = False
        marker = object()
        def expire():
            with self._read_deadline_lock:
                if self._read_deadline is None or self._read_deadline[0] is not marker:
                    return
                self._read_deadline = None
                self._read_expired = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        timer = threading.Timer(PUBLIC_READ_TIMEOUT, expire)
        timer.daemon = True
        with self._read_deadline_lock:
            self._read_deadline = (marker, timer)
        timer.start()

    def _cancel_read_deadline(self):
        with self._read_deadline_lock:
            deadline = self._read_deadline
            self._read_deadline = None
            if deadline is not None:
                deadline[1].cancel()

    def finish(self):
        self._cancel_read_deadline()
        super().finish()

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except OSError:
            if not self.transport.public_mode:
                raise
            # Deadline shutdown can surface as EOF, reset, or WinError 10058.
            self.close_connection = True

    @property
    def app(self):
        visitor = getattr(self, '_visitor', None)
        return visitor.app if visitor is not None else self.server.app

    @property
    def public_sessions(self):
        return getattr(self.server, 'public_sessions', None) if self.transport.public_mode else None

    def log_message(self, format, *args):
        pass

    @property
    def transport(self):
        # Existing embedded/test servers retain the original loopback policy.
        return getattr(self.server, 'transport', TransportPolicy())

    def parse_request(self):
        self._visitor = None
        try:
            parsed = super().parse_request()
        except OSError:
            if not self.transport.public_mode:
                raise
            self.close_connection = True
            return False
        finally:
            self._cancel_read_deadline()
        if not parsed or self._read_expired:
            self.close_connection = True
            return False
        return self.allowed()

    def allowed(self):
        try:
            path = unquote(urlsplit(self.path).path)
        except ValueError:
            self.respond(400, {'error': '请求路径无效。'})
            return False
        sessions = self.public_sessions
        error = self.transport.check(self.headers, self.server.server_port, self.command, path, defer_auth=sessions is not None)
        if error:
            self.respond(error[0], {'error': error[1]})
            return False
        is_api = path == '/api' or path.startswith('/api/')
        anonymous = self.command == 'OPTIONS' or (self.command == 'GET' and path == '/api/health') or (self.command == 'POST' and path == '/api/session')
        if sessions is not None and is_api and not anonymous and not self.transport.is_admin(self.headers):
            try:
                self._visitor = sessions.authenticate(bearer_token(self.headers))
            except PublicAccessError as error:
                self.respond(error.status, {'error': str(error)})
                return False
        return True

    def run_expensive(self, callback):
        visitor = getattr(self, '_visitor', None)
        slot = self.public_sessions.work_slot(visitor) if visitor is not None else nullcontext()
        with slot:
            return callback()

    def send_error(self, code, message=None, explain=None):
        self.respond(code, {'error': '请求格式或方法不受支持。'})

    def respond(self, status, value, content_type='application/json; charset=utf-8', extra_headers=None):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8') if isinstance(value, (dict, list)) else value
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Vary', 'Origin')
        origin = self.transport.cors_origin(self.headers, self.server.server_port) if hasattr(self, 'headers') else None
        if origin:
            self.send_header('Access-Control-Allow-Origin', origin)
        if status == 401:
            self.send_header('WWW-Authenticate', 'Bearer realm="Xunwei"')
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' https: http://localhost:* http://127.0.0.1:*; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        try:
            self.end_headers()
            if getattr(self, 'command', '') != 'HEAD':
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def read_json(self):
        if self.headers.get_content_type() != 'application/json':
            raise ValueError('请求须为 application/json。')
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise ValueError('请求长度无效。') from None
        if not 0 < length <= 600_000:
            raise ValueError('请求为空或过大。')
        try:
            self._start_read_deadline()
            try:
                body = self.rfile.read(length)
            finally:
                self._cancel_read_deadline()
            if self.transport.public_mode and (self._read_expired or len(body) != length):
                raise ValueError('请求读取超时或正文不完整。')
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise ValueError('请求不是有效 JSON。') from None
        if not isinstance(data, dict):
            raise ValueError('请求须为 JSON 对象。')
        return data

    def do_GET(self):
        try:
            self._get()
        except PublicAccessError as error:
            self.respond(error.status, {'error': str(error)})
        except Exception:
            self.respond(500, {'error': '请求未完成，请稍后重试。'})

    def _get(self):
        path = unquote(urlsplit(self.path).path)
        if path == '/api/config':
            return self.respond(200, self.app.public_config())
        if path == '/api/health':
            capabilities = self.public_sessions.capabilities() if self.public_sessions else {'public_mode': False, 'session_required': False, 'shared_available': False}
            public_limits = capabilities.get('public_limits', {})
            search_limits = {'max_active_jobs': public_limits.get('max_session_jobs', self.app.max_active_jobs),
                             'max_search_concurrency': public_limits.get('search_concurrency', self.app.max_search_concurrency)}
            return self.respond(200, {'ok': True, 'version': '1.4.0', 'features': ['ai_summary', 'adaptive_search', 'stop_resume', 'custom_sites', 'round_progress_reports', 'direct_longtail_sources', 'research_depth', 'multi_engine_search', 'concurrent_search'],
                                     'search_limits': search_limits, **capabilities})
        if path == '/api/session' and self.public_sessions:
            if self._visitor is None:
                raise PublicAccessError(403, '管理令牌不属于访客会话，请创建独立访客会话。')
            return self.respond(200, self.public_sessions.describe(self._visitor))
        if path == '/api/platforms':
            return self.respond(200, {'items': platform_catalog(), 'custom_sites': self.app.public_config()['custom_sites']})
        if path == '/api/search-engines':
            with self.app.lock:
                catalog = search_engine_catalog(self.app.config)
            return self.respond(200, {'items': catalog})
        if path == '/api/library':
            # List metadata only; text is sent to AI only as a relevant search candidate.
            docs = [{k: v for k, v in d.items() if k not in ('text', 'body')} for d in self.app.storage.list_documents()]
            return self.respond(200, {'items': docs})
        if path == '/api/history':
            return self.respond(200, {'items': self.app.storage.list_history()})
        if path == '/api/jobs':
            return self.respond(200, self.app.list_jobs())
        if path.startswith('/api/jobs/'):
            job = self.app.get_job(path.rsplit('/', 1)[-1])
            return self.respond(200, job) if job else self.respond(404, {'error': '搜索记录不存在。'})
        if path.startswith('/api/'):
            return self.respond(404, {'error': '接口不存在。'})
        if path == '/':
            path = '/index.html'
        target = (ROOT / 'web' / path.lstrip('/')).resolve()
        root = (ROOT / 'web').resolve()
        public_catalog = path == '/platforms.json' and target == root / 'platforms.json'
        if not target.is_relative_to(root) or not target.is_file() or (not public_catalog and target.suffix not in ('.html', '.js', '.css', '.svg', '.ico')):
            return self.respond(404, {'error': '文件不存在。'})
        content_type = {'.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml', '.json': 'application/json'}.get(target.suffix, 'application/octet-stream')
        self.respond(200, target.read_bytes(), content_type + '; charset=utf-8')

    def do_OPTIONS(self):
        path = unquote(urlsplit(self.path).path)
        if path != '/api' and not path.startswith('/api/'):
            return self.respond(404, {'error': '接口不存在。'})
        methods = self.headers.get_all('Access-Control-Request-Method', [])
        requested_headers = self.headers.get_all('Access-Control-Request-Headers', [])
        if not self.transport.cors_origin(self.headers, self.server.server_port) or len(methods) != 1 or methods[0] not in CORS_METHODS:
            return self.respond(403, {'error': '预检来源或方法不受支持。'})
        if len(requested_headers) > 1:
            return self.respond(403, {'error': '预检请求头不受支持。'})
        names = [name.strip().lower() for name in requested_headers[0].split(',')] if requested_headers else []
        if any(name not in CORS_HEADERS for name in names):
            return self.respond(403, {'error': '预检请求头不受支持。'})
        return self.respond(204, b'', extra_headers={
            'Access-Control-Allow-Methods': ', '.join(CORS_METHODS),
            'Access-Control-Allow-Headers': 'Authorization, Content-Type',
            'Access-Control-Max-Age': '600',
        })

    def do_POST(self):
        self.mutate('POST')

    def do_PUT(self):
        self.mutate('PUT')

    def do_DELETE(self):
        self.mutate('DELETE')

    def mutate(self, method):
        path = unquote(urlsplit(self.path).path)
        try:
            data = {} if method == 'DELETE' else self.read_json()
            if path == '/api/session' and self.public_sessions:
                if method == 'POST':
                    return self.respond(201, self.public_sessions.create(data, self.client_address[0]))
                if self._visitor is None:
                    raise PublicAccessError(403, '管理令牌不能修改访客会话；请使用该访客的会话令牌。')
                if method == 'PUT':
                    return self.respond(200, self.public_sessions.switch(self._visitor, data.get('mode')))
                if method == 'DELETE':
                    self.public_sessions.revoke(self._visitor)
                    return self.respond(200, {'ok': True, 'message': '访客会话已撤销，临时数据已删除。'})
            if method == 'PUT' and path == '/api/config':
                return self.respond(200, self.app.save_config(data))
            if method == 'POST' and path == '/api/ai/test':
                def run_test():
                    with self.app.lock:
                        if self._visitor:
                            self.app._ensure_active()
                        config = dict(self.app.config)
                        if self._visitor:
                            config['_cancel_event'] = self.app.lifetime_cancel
                    return test_connection(config)
                return self.respond(200, self.run_expensive(run_test))
            if method == 'POST' and path == '/api/search':
                return self.respond(202, self.run_expensive(lambda: self.app.create_job(data)))
            if method == 'POST' and re.fullmatch(r'/api/jobs/[a-f0-9]{32}/summarize', path):
                return self.respond(202, self.run_expensive(lambda: self.app.create_summary(path.split('/')[3])))
            if method == 'POST' and re.fullmatch(r'/api/jobs/[a-f0-9]{32}/stop', path):
                return self.respond(202, self.app.stop_job(path.split('/')[3]))
            if method == 'POST' and re.fullmatch(r'/api/jobs/[a-f0-9]{32}/continue', path):
                return self.respond(202, self.run_expensive(lambda: self.app.continue_job(path.split('/')[3], data)))
            if method == 'POST' and path == '/api/import':
                title, text, url = str(data.get('title', '')).strip(), str(data.get('text', '')).strip(), str(data.get('url', '')).strip()
                if not 1 <= len(title) <= 500 or not 10 <= len(text) <= 150000:
                    raise ValueError('标题须为 1–500 字，正文须为 10–150000 字。')
                if re.search(r'\bsk-[A-Za-z0-9_-]{16,}', text):
                    raise ValueError('导入内容含疑似 API 密钥，请先移除。')
                if url:
                    url = canonical_url(url)
                    if not url:
                        raise ValueError('来源链接须为有效的公开 HTTP(S) 地址。')
                if url:
                    platform = platform_of(url)
                else:
                    platform = data.get('platform', 'web')
                    if platform not in PLATFORM_LABELS and platform != 'website':
                        raise ValueError('请选择有效的导入资料平台。')
                item = self.app.storage.add_document(title, url, text, platform)
                return self.respond(201, {'ok': True, 'id': item['id']})
            if method == 'DELETE' and path.startswith('/api/library/'):
                deleted = self.app.storage.delete_document(unquote(path.rsplit('/', 1)[-1]))
                return self.respond(200 if deleted else 404, {'ok': deleted})
            self.respond(404, {'error': '接口不存在。'})
        except PublicAccessError as error:
            self.respond(error.status, {'error': str(error)})
        except LookupError as e:
            self.respond(404, {'error': str(e)})
        except ValueError as e:
            self.respond(400, {'error': str(e)})
        except Exception:
            self.respond(500, {'error': '本地请求未完成，请重试。'})


def main(argv=None):
    parser = argparse.ArgumentParser(description='寻微 · 多平台公开信息搜索')
    parser.add_argument('--host', default=os.environ.get('XUNWEI_HOST', '127.0.0.1'))
    parser.add_argument('--port', default=os.environ.get('XUNWEI_PORT') or os.environ.get('PORT') or '8877')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--data-dir')
    args = parser.parse_args(argv)
    try:
        args.host, args.port = validate_bind_host(args.host), validate_port(args.port)
        transport = TransportPolicy.from_environment(args.host)
        sessions = None
        if transport.public_mode:
            from .public_sessions import PublicSessions
            sessions = PublicSessions()
    except ValueError as error:
        parser.error(str(error))
    address = '[' + args.host + ']' if ':' in args.host else args.host
    browser_url = f'http://{address}:{args.port}'
    try:
        server_type = IPv6HTTPServer if ':' in args.host else LocalHTTPServer
        server = server_type((args.host, args.port), Handler, transport=transport)
    except OSError:
        if sessions:
            sessions.close()
        if not transport.remote:
            try:
                with urllib.request.urlopen(browser_url + '/api/health', timeout=2) as response:
                    existing = json.load(response)
                if existing.get('ok') and str(existing.get('version', '')).startswith('1.'):
                    print(f'寻微已在运行：{browser_url}')
                    if not args.no_browser:
                        webbrowser.open(browser_url)
                    return 0
            except Exception:
                pass
        print(f'端口 {args.port} 已被占用。请打开已有页面，或使用 --port 8878 启动。')
        return 1
    app = App(args.data_dir)
    server.app = app
    server.public_sessions = sessions
    print(f'寻微已启动，监听 {args.host}:{args.port}；公开 HTTPS 请通过已配置的反向代理访问。' if transport.remote else f'寻微已启动：{browser_url}')
    print(f'关闭此窗口或按 Ctrl+C 停止。数据目录：{app.data_dir.resolve()}')
    if not args.no_browser and not transport.remote:
        threading.Timer(0.6, lambda: webbrowser.open(browser_url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if sessions:
            sessions.close()
        server.server_close()
    return 0
