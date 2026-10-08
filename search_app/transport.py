"""Explicit deployment boundaries, independent of saved AI/model settings."""
from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import ipaddress
import os
import re
from urllib.parse import urlsplit


CORS_METHODS = ('GET', 'POST', 'PUT', 'DELETE')
CORS_HEADERS = frozenset(('authorization', 'content-type'))


class PublicAccessError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def bearer_token(headers):
    values = headers.get_all('Authorization', [])
    parts = values[0].split(' ') if len(values) == 1 else []
    return parts[1] if len(parts) == 2 and parts[0].lower() == 'bearer' else ''


def is_loopback(host):
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_bind_host(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError('监听地址须为 localhost 或明确的 IP 地址。')
    if value == 'localhost':
        return value
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        raise ValueError('监听地址须为 localhost 或明确的 IP 地址。') from None


def validate_port(value):
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]{1,5}', str(value)):
        raise ValueError('端口须为 1–65535 的整数。')
    number = int(value)
    if not 1 <= number <= 65535:
        raise ValueError('端口须为 1–65535 的整数。')
    return number


def normalize_authority(value):
    """Validate a Host authority; neither URL syntax nor wildcard matching."""
    if not isinstance(value, str) or not value or re.search(r'[\s/@?#\\%*]', value):
        raise ValueError('PUBLIC_HOSTS 须为明确的主机名或主机名:端口，不能包含 URL、路径或通配符。')
    if ('[' in value or ']' in value) and not re.fullmatch(r'\[[0-9a-fA-F:.]+\](?::[0-9]+)?', value):
        raise ValueError('PUBLIC_HOSTS 含无效的 IPv6 主机名。')
    try:
        parts = urlsplit('//' + value)
        host, port = parts.hostname, parts.port
        if not host or parts.path or parts.username is not None or parts.password is not None:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
            if address.is_unspecified:
                raise ValueError
            host = str(address)
            authority = '[' + host + ']' if address.version == 6 else host
        except ValueError:
            if ':' in host or host in ('0.0.0.0', '::') or len(host) > 253 or not all(
                re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', label)
                for label in host.split('.')
            ):
                raise ValueError
            authority = host.lower()
        if value.endswith(':'):
            raise ValueError
        if port is not None:
            validate_port(port)
            authority += ':' + str(port)
        return authority
    except (ValueError, TypeError):
        raise ValueError('PUBLIC_HOSTS 含无效的主机名或端口。') from None


def normalize_origin(value):
    if not isinstance(value, str) or not value or value != value.strip() or any(char in value for char in ('?', '#', '\\', '*')):
        raise ValueError('ALLOWED_ORIGINS 须为明确的 HTTPS 来源，不能含路径、查询参数或通配符。')
    try:
        parts = urlsplit(value)
        if parts.scheme not in ('http', 'https') or not parts.netloc or parts.path or parts.query or parts.fragment:
            raise ValueError
        authority = normalize_authority(parts.netloc)
        if parts.scheme == 'http' and not is_loopback(parts.hostname):
            raise ValueError
        # Browser Origin serialization elides default ports.
        if (parts.scheme == 'https' and parts.port == 443) or (parts.scheme == 'http' and parts.port == 80):
            authority = authority.rsplit(':', 1)[0]
        return parts.scheme + '://' + authority
    except (ValueError, TypeError):
        raise ValueError('ALLOWED_ORIGINS 须为无路径的 HTTPS 来源；仅 localhost/回环 IP 可用 HTTP。') from None


def _entries(value, name, normalize):
    if not value:
        return frozenset()
    if not isinstance(value, str):
        raise ValueError(name + ' 须为逗号分隔的明确列表。')
    parts = value.split(',')
    if any(not item.strip() for item in parts):
        raise ValueError(name + ' 不能包含空项目。')
    return frozenset(normalize(item.strip()) for item in parts)


@dataclass(frozen=True)
class TransportPolicy:
    bind_host: str = '127.0.0.1'
    remote: bool = False
    access_token: str = field(default='', repr=False)
    public_hosts: frozenset = field(default_factory=frozenset)
    allowed_origins: frozenset = field(default_factory=frozenset)
    public_mode: bool = False

    @classmethod
    def from_environment(cls, bind_host='127.0.0.1', environ=None):
        env = os.environ if environ is None else environ
        bind_host = validate_bind_host(bind_host)
        token = env.get('XUNWEI_ACCESS_TOKEN', '')
        public_value = env.get('XUNWEI_PUBLIC_MODE', '0')
        if public_value not in ('0', '1'):
            raise ValueError('XUNWEI_PUBLIC_MODE 只能为 0 或 1。')
        public_mode = public_value == '1'
        hosts = _entries(env.get('XUNWEI_PUBLIC_HOSTS', ''), 'XUNWEI_PUBLIC_HOSTS', normalize_authority)
        origins = _entries(env.get('XUNWEI_ALLOWED_ORIGINS', ''), 'XUNWEI_ALLOWED_ORIGINS', normalize_origin)
        remote = bool(public_mode or token or hosts or origins or not is_loopback(bind_host))
        if remote:
            if not token or not hosts or not origins:
                raise ValueError('公开/跨来源模式须同时明确设置 XUNWEI_ACCESS_TOKEN、XUNWEI_PUBLIC_HOSTS、XUNWEI_ALLOWED_ORIGINS。')
            if not isinstance(token, str) or len(token) > 4096 or any(not 33 <= ord(char) <= 126 for char in token):
                raise ValueError('XUNWEI_ACCESS_TOKEN 须为不含空白的 ASCII 令牌，最长4096字符。')
        return cls(bind_host, remote, token, hosts, origins, public_mode)

    def hosts_for(self, port):
        if self.remote:
            return self.public_hosts
        hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if is_loopback(self.bind_host) and self.bind_host != 'localhost':
            address = '[' + self.bind_host + ']' if ':' in self.bind_host else self.bind_host
            hosts.add(f'{address}:{port}')
        return hosts

    def origins_for(self, port):
        return self.allowed_origins if self.remote else {'http://' + host for host in self.hosts_for(port)}

    def cors_origin(self, headers, port):
        origins = headers.get_all('Origin', [])
        if len(origins) != 1:
            return None
        origin = origins[0]
        # Echo only the exact browser origin, never arbitrary or multiple values.
        return origin if origin in self.origins_for(port) else None

    def is_admin(self, headers):
        return bool(self.access_token) and hmac.compare_digest(bearer_token(headers).encode('utf-8'), self.access_token.encode('ascii'))

    def check(self, headers, port, method, path, *, defer_auth=False):
        hosts = headers.get_all('Host', [])
        try:
            valid_host = len(hosts) == 1 and normalize_authority(hosts[0]) in self.hosts_for(port)
        except ValueError:
            valid_host = False
        if not valid_host:
            return 403, '请求 Host 不在允许列表。'
        origins = headers.get_all('Origin', [])
        if origins and (len(origins) != 1 or self.cors_origin(headers, port) is None):
            return 403, '拒绝未授权来源的跨站请求。'
        is_api = path == '/api' or path.startswith('/api/')
        # A link from another page is a normal top-level document navigation.
        # Only static reads get this exception; Host/Origin were checked above.
        document_navigation = (method in ('GET', 'HEAD') and not is_api
                               and headers.get('Sec-Fetch-Mode') == 'navigate'
                               and headers.get('Sec-Fetch-Dest') == 'document')
        if headers.get('Sec-Fetch-Site') == 'cross-site' and (not self.remote or not origins) and not document_navigation:
            return 403, '拒绝跨站请求。'
        exempt = method == 'OPTIONS' or (method == 'GET' and path == '/api/health')
        if self.remote and is_api and not exempt and not defer_auth:
            if not self.is_admin(headers):
                return 401, '访问令牌缺失或无效，请配置后端访问令牌。'
        return None
