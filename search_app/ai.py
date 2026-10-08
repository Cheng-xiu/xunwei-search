"""Small dependency-free client for a user-selected Chat Completions API."""
from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .providers import PublicFetchError, _HTTPSHandler, _PublicHTTPSConnection, _resolve_public, canonical_url

_PUBLIC_AI_DNS_GATE = threading.BoundedSemaphore(4)


class AIError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _PublicAIDeadline:
    """Close the actual socket on cancellation or a total request deadline."""
    def __init__(self, timeout, cancel=None):
        self.deadline = time.monotonic() + max(0.001, float(timeout))
        self.cancel = cancel
        self.lock = threading.RLock()
        self.done = threading.Event()
        self.connection = None
        self.expired = False
        self.stopped = False
        self.watcher = threading.Thread(target=self._watch, daemon=True)
        self.watcher.start()

    def _abort(self, stopped):
        with self.lock:
            self.stopped = self.stopped or stopped
            self.expired = self.expired or not stopped
            connection = self.connection
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    connection.close()
                except OSError:
                    pass

    def _watch(self):
        while not self.done.wait(0.02):
            stopped = self.cancel is not None and self.cancel.is_set()
            if stopped or time.monotonic() >= self.deadline:
                self._abort(stopped)
                return

    def check(self):
        stopped = self.cancel is not None and self.cancel.is_set()
        if stopped or time.monotonic() >= self.deadline:
            self._abort(stopped)
        if self.stopped:
            raise AIError('已停止本轮 AI 请求。')
        if self.expired:
            raise AIError('AI 服务响应超时，本轮使用关键词检索结果。')

    def remaining(self):
        self.check()
        return max(0.001, self.deadline - time.monotonic())

    def attach(self, connection):
        with self.lock:
            self.connection = connection
            self.check()

    def finish(self):
        self.done.set()
        self.watcher.join(timeout=0.1)
        with self.lock:
            self.connection = None


class _PublicAIHTTPSConnection(_PublicHTTPSConnection):
    def __init__(self, *args, deadline, **kwargs):
        super().__init__(*args, **kwargs)
        self.request_deadline = deadline

    def connect(self):
        if self._tunnel_host:
            raise PublicFetchError('不支持代理隧道')
        raw = _public_ai_socket(self.host, self.port, self.request_deadline)
        try:
            self.request_deadline.attach(raw)
            # Retain a closeable SSL socket while the handshake is in progress.
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
            self.request_deadline.attach(self.sock)
            self.sock.settimeout(self.request_deadline.remaining())
            self.sock.do_handshake()
            self.request_deadline.check()
        except Exception:
            if self.sock is not None:
                self.sock.close()
            raw.close()
            raise


def _public_ai_socket(host, port, deadline):
    # OS DNS resolution cannot be cancelled. Keep at most four resolver workers,
    # and never make the AI request wait beyond its own deadline/cancellation.
    gate = _PUBLIC_AI_DNS_GATE
    if not gate.acquire(blocking=False):
        raise PublicFetchError('模型域名解析请求繁忙')
    ready = threading.Event()
    outcome = []
    def resolve():
        try:
            outcome.append((True, _resolve_public(host, port)))
        except Exception:
            outcome.append((False, None))
        finally:
            gate.release()
            ready.set()
    threading.Thread(target=resolve, daemon=True).start()
    while not ready.wait(min(0.02, deadline.remaining())):
        deadline.check()
    deadline.check()
    if not outcome[0][0]:
        raise PublicFetchError('域名解析失败或不是公网地址')
    for family, socktype, proto, _, sockaddr in outcome[0][1]:
        deadline.check()
        connection = socket.socket(family, socktype, proto)
        try:
            deadline.attach(connection)
            connection.settimeout(deadline.remaining())
            connection.connect(sockaddr)
            deadline.check()
            return connection
        except OSError:
            connection.close()
            deadline.check()
    raise PublicFetchError('公网连接失败')


class _PublicAIHTTPSHandler(_HTTPSHandler):
    def __init__(self, deadline):
        super().__init__()
        self.request_deadline = deadline

    def https_open(self, req):
        def connection(*args, **kwargs):
            return _PublicAIHTTPSConnection(*args, deadline=self.request_deadline, **kwargs)
        return self.do_open(connection, req, context=self._context)


def validate_base_url(value: str) -> str:
    value = value.strip().rstrip('/')
    try:
        p = urllib.parse.urlsplit(value)
        _ = p.port
    except ValueError:
        raise ValueError('API 地址格式不正确。') from None
    if not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError('API 地址须为不含账号、查询参数或片段的服务根地址。')
    if p.scheme != 'https' and not (p.scheme == 'http' and p.hostname in ('localhost', '127.0.0.1', '::1')):
        raise ValueError('远程 AI 服务须使用 HTTPS；本机服务可使用 HTTP。')
    return value


def parse_json_response(content: str):
    content = content.strip()
    if content.startswith('```'):
        content = re.sub(r'^```(?:json)?\s*', '', content, flags=re.I)
        content = re.sub(r'\s*```$', '', content)
    try:
        return json.loads(content)
    except (ValueError, TypeError):
        decoder = json.JSONDecoder()
        for m in re.finditer(r'[\[{]', content):
            try:
                result, _ = decoder.raw_decode(content[m.start():])
                return result
            except ValueError:
                continue
    raise AIError('模型没有返回可解析的 JSON；本轮使用关键词检索结果。')


def chat(config: dict, system: str, user: str, max_tokens: int = 1800, timeout: int = 55) -> str:
    cancel = config.get('_cancel_event')
    if cancel is None:
        return _chat_request(config, system, user, max_tokens, timeout)
    if cancel.is_set():
        raise AIError('已停止本轮 AI 请求。')
    completed = threading.Event()
    outcome = []

    def invoke():
        try:
            if cancel.is_set():
                raise AIError('已停止本轮 AI 请求。')
            outcome.append((True, _chat_request(config, system, user, max_tokens, timeout)))
        except Exception as error:
            outcome.append((False, error))
        finally:
            completed.set()

    # urllib cannot interrupt a request waiting for remote headers. Detach that
    # already-issued call on stop; its late reply is never applied to the job.
    threading.Thread(target=invoke, daemon=True).start()
    while not completed.wait(0.1):
        if cancel.is_set():
            raise AIError('已停止本轮 AI 请求；已发出的远程请求可能仍在结束中。')
    if cancel.is_set():
        raise AIError('已停止本轮 AI 请求。')
    succeeded, result = outcome[0]
    if succeeded:
        return result
    raise result


def _chat_request(config: dict, system: str, user: str, max_tokens: int = 1800, timeout: int = 55) -> str:
    key = str(config.get('api_key', '')).strip()
    if not key:
        raise AIError('尚未设置 AI API 密钥，已使用关键词检索。')
    base = validate_base_url(str(config.get('base_url', 'https://api.a6api.com/v1')))
    endpoint = base if base.endswith('/chat/completions') else base + '/chat/completions'
    public_network = config.get('_public_network') is True
    if public_network and (urllib.parse.urlsplit(endpoint).scheme != 'https' or not canonical_url(endpoint)):
        raise AIError('公开访客模型服务必须使用标准端口的公网 HTTPS 地址。')
    payload = {'model': config.get('model', 'deepseek-v4.1-flash'),
               'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
               'max_tokens': max_tokens, 'stream': False}
    # This gateway's DeepSeek model spends small budgets on hidden reasoning by
    # default. Its tested extension enables bounded structured-output requests.
    if str(payload['model']).lower().startswith('deepseek'):
        payload['thinking'] = {'type': 'disabled'}
    req = urllib.request.Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                                 headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                                          'Accept': 'application/json', 'User-Agent': 'XunweiLocal/1.0'}, method='POST')
    deadline = _PublicAIDeadline(timeout, config.get('_cancel_event')) if public_network else None
    try:
        # Never forward credentials to a redirect destination.
        if config.get('_cancel_event') is not None and config['_cancel_event'].is_set():
            raise AIError('已停止本轮 AI 请求。')
        # Public visitors choose their own endpoint. Resolve once, reject every
        # private address, and connect to that checked IP with original TLS SNI.
        # The existing local mode retains local-model/proxy compatibility.
        opener = (urllib.request.build_opener(urllib.request.ProxyHandler({}), _PublicAIHTTPSHandler(deadline), NoRedirect())
                  if public_network else urllib.request.build_opener(NoRedirect()))
        with opener.open(req, timeout=timeout) as response:
            if deadline is None:
                raw = response.read(2_000_001)
            else:
                raw = bytearray()
                while len(raw) <= 2_000_000:
                    deadline.check()
                    chunk = response.read1(min(65536, 2_000_001 - len(raw)))
                    deadline.check()
                    if not chunk:
                        break
                    raw.extend(chunk)
                raw = bytes(raw)
        if len(raw) > 2_000_000:
            raise AIError('AI 响应过大。')
        data = json.loads(raw)
        choices = data.get('choices') if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise AIError('AI 响应格式不兼容 Chat Completions。')
        choice = choices[0]
        if choice.get('finish_reason') == 'length':
            raise AIError('AI 输出达到本轮长度上限，未采用截断内容。')
        message = choice.get('message')
        if not isinstance(message, dict):
            raise AIError('AI 响应格式不兼容 Chat Completions。')
        content = message.get('content')
        if not isinstance(content, str) or not content.strip():
            raise AIError('AI 服务返回了空内容。')
        return content
    except urllib.error.HTTPError as e:
        messages = {401: '密钥无效或已过期', 403: '服务拒绝访问', 404: '地址或模型不存在',
                    429: '请求限流或余额不足', 500: '服务内部错误', 502: '上游服务异常', 503: '服务暂不可用'}
        raise AIError(f'AI 服务 HTTP {e.code}：{messages.get(e.code, "请求未成功")}。') from None
    except (TimeoutError, socket.timeout):
        raise AIError('AI 服务响应超时，本轮使用关键词检索结果。') from None
    except PublicFetchError:
        raise AIError('AI 地址未通过公网连接校验，或公网连接失败。') from None
    except (urllib.error.URLError, OSError):
        raise AIError('无法连接 AI 服务，请检查地址与网络。') from None
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise AIError('AI 响应格式不兼容 Chat Completions。') from None
    finally:
        if deadline is not None:
            deadline.finish()


def test_connection(config: dict) -> dict:
    try:
        reply = chat(config, 'This is a connectivity test. Reply with OK only.', 'Reply OK.', max_tokens=256, timeout=35)
        return {'ok': True, 'message': 'API 连接成功，模型已返回内容。', 'model': config.get('model')}
    except (AIError, ValueError) as e:
        return {'ok': False, 'message': str(e)}
