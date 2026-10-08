"""Public search connectors and bounded, DNS-pinned public page extraction.

Only upstream results become search hits. Authentication walls and bot challenges
are reported, never bypassed. This module uses the Python standard library.
"""
from __future__ import annotations

from contextlib import contextmanager
import base64
import datetime as dt
import html
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
import zlib

USER_AGENT = "SmartSearchBot/1.0 (public-source research; no login)"
TIMEOUT = 9
MAX_BYTES = 2_000_000
BILIBILI_MIN_INTERVAL = 1.0
BILIBILI_COOLDOWN_SECONDS = 60.0
_BILIBILI_GATE = threading.Semaphore(1)
_bilibili_next_request_at = 0.0
_bilibili_cooldown_until = 0.0
PLATFORM_HOSTS = {
    "bilibili": ("bilibili.com", "b23.tv"),
    "xiaohongshu": ("xiaohongshu.com", "xhslink.com"),
    "zhihu": ("zhihu.com",),
    "wechat": ("mp.weixin.qq.com",),
    "meituan": ("meituan.com",),
    "dianping": ("dianping.com",),
    "douyin": ("douyin.com", "iesdouyin.com"),
    "tieba": ("tieba.baidu.com",),
    "douban": ("douban.com",),
    "github": ("github.com",),
    "stackoverflow": ("stackoverflow.com",),
    "v2ex": ("v2ex.com",),
    "csdn": ("csdn.net",),
    "cnblogs": ("cnblogs.com",),
    "reddit": ("reddit.com",),
}
PLATFORM_LABELS = {"bilibili": "哔哩哔哩", "xiaohongshu": "小红书", "zhihu": "知乎", "wechat": "微信公众号", "meituan": "美团", "dianping": "大众点评", "douyin": "抖音", "tieba": "百度贴吧", "douban": "豆瓣", "github": "GitHub", "stackoverflow": "Stack Overflow", "v2ex": "V2EX", "csdn": "CSDN", "cnblogs": "博客园", "reddit": "Reddit", "web": "全网"}
_TRACKING_KEYS = {"spm_id_from", "vd_source", "share_source", "share_medium", "share_plat", "share_session_id", "from_source", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"}
SEARCH_ENGINE_IDS = ("baidu", "bing", "google", "yandex", "duckduckgo", "tavily", "brave", "searxng")
_ENGINE_LABELS = {"baidu": "百度", "bing": "必应", "google": "Google", "yandex": "Yandex", "duckduckgo": "DuckDuckGo", "tavily": "Tavily", "brave": "Brave Search", "searxng": "SearXNG"}
_ENGINE_SEARCH_URLS = {
    "baidu": "https://www.baidu.com/s?wd={query}",
    "bing": "https://www.bing.com/search?q={query}",
    "google": "https://www.google.com/search?q={query}",
    "yandex": "https://yandex.com/search/?text={query}",
    "duckduckgo": "https://duckduckgo.com/?q={query}",
    "brave": "https://search.brave.com/search?q={query}",
}


class PublicFetchError(Exception):
    """Safe user-facing failure; never contains headers or response secrets."""

    def __init__(self, message, *, http_status=None):
        super().__init__(message)
        self.http_status = http_status


class SearchCancelled(PublicFetchError):
    """Cooperative cancellation; not a source failure or a retry request."""


def _check_cancel_event(event):
    if event is not None and event.is_set():
        raise SearchCancelled("已取消检索，未继续请求来源")


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def platform_catalog() -> list[dict]:
    notes = {
        "bilibili": "公开视频搜索接口及公开索引；接口可能限流，不覆盖全部动态、专栏或评论。",
        "wechat": "仅检索 mp.weixin.qq.com 公开文章索引；搜狗文章入口需在浏览器打开，不接入登录后微信搜索。",
        "meituan": "检索美团公开网页索引；无已验证的通用站内搜索接口，不抓取登录后商户或评论数据。",
        "dianping": "检索大众点评公开网页索引；城市与登录限制可能影响访问，不接入认证接口。",
        "douyin": "检索抖音公开网页索引；站内入口可能依赖浏览器、登录或验证，不自动绕过。",
        "tieba": "检索百度贴吧公开网页索引；站内搜索入口可能限制自动访问。",
        "douban": "检索豆瓣公开网页索引，并提供浏览器站内搜索入口。",
        "github": "匿名公开 API 搜索 issues、PR 和仓库；可读取帖子首楼正文，不包含评论、私有仓库或需授权的代码搜索。",
        "stackoverflow": "StackExchange 公开 API 搜索 Stack Overflow 问题，含问题正文与实际浏览量；不包含未返回的回答或评论。",
        "v2ex": "通过搜索引擎检索 V2EX 公开主题索引；没有接入历史主题全文搜索 API。",
        "csdn": "通过搜索引擎检索 CSDN 公开文章索引；不接入登录或付费全文。",
        "cnblogs": "通过搜索引擎检索博客园公开文章索引；站内搜索可能要求人机验证。",
        "reddit": "通过搜索引擎检索 Reddit 公开帖子索引；未接入需要授权或拒绝匿名访问的 API。",
        "web": "通过配置的搜索来源检索公开全网索引；不能保证覆盖未收录、私密或需登录内容。",
    }
    return [{"id": platform, "label": label, "domains": list(PLATFORM_HOSTS.get(platform, ())),
             "access": "public_api" if platform in ("bilibili", "github", "stackoverflow") else "public_index",
             "description": notes.get(platform, "检索公开网页索引并提供站内入口；不使用登录后接口，不绕过访问限制。")}
            for platform, label in PLATFORM_LABELS.items()]


def _public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_global and not address.is_multicast
    except ValueError:
        return False


def canonical_url(url: str) -> str:
    """Normalize an ordinary public HTTP(S) URL; do not perform network I/O."""
    if not isinstance(url, str) or len(url) > 8192 or re.search(r"[\x00-\x20\x7f\\]", url):
        return ""
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or parts.username is not None or parts.password is not None:
            return ""
        host = (parts.hostname or "").rstrip(".").lower()
        if not host or "%" in host or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home", ".onion")):
            return ""
        if parts.port is not None and parts.port != (443 if parts.scheme.lower() == "https" else 80):
            return ""
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # libc accepts historical IP forms such as 2130706433 and 0177.0.0.1.
            try:
                socket.inet_aton(host)
                return ""
            except OSError:
                pass
            host = host.encode("idna").decode("ascii")
            if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host) or any(not p or len(p) > 63 or p.startswith("-") or p.endswith("-") for p in host.split(".")):
                return ""
        else:
            if not _public_ip(str(address)):
                return ""
            if address.version == 6:
                host = "[" + address.compressed + "]"
        pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        query = urllib.parse.urlencode([(k, v) for k, v in pairs if k.lower() not in _TRACKING_KEYS and not k.lower().startswith("utm_")])
        path = urllib.parse.quote(parts.path or "/", safe="/!$&'()*+,;=:@~-._%")
        return urllib.parse.urlunsplit((parts.scheme.lower(), host, path, query, ""))
    except (ValueError, UnicodeError, OSError):
        return ""


def platform_of(url: str) -> str:
    normalized = canonical_url(url)
    host = urllib.parse.urlsplit(normalized).hostname or ""
    for platform, domains in PLATFORM_HOSTS.items():
        if any(host == domain if platform == "wechat" else _host_matches(host, domain) for domain in domains):
            return platform
    return "web"


def _resolve_public(host: str, port: int):
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise PublicFetchError("域名解析失败") from None
    if not addresses or any(not _public_ip(entry[4][0]) for entry in addresses):
        raise PublicFetchError("已拒绝访问非公网地址")
    return addresses


def _public_socket(host: str, port: int, timeout):
    addresses = _resolve_public(host, port)
    deadline = time.monotonic() + (timeout if isinstance(timeout, (int, float)) else TIMEOUT)
    for family, socktype, proto, _, sockaddr in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        connection = socket.socket(family, socktype, proto)
        try:
            connection.settimeout(remaining)
            connection.connect(sockaddr)  # Connect to the validated IP, without resolving again.
            return connection
        except OSError:
            connection.close()
    raise PublicFetchError("网络连接失败或超时")


class _PublicHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        if self._tunnel_host:
            raise PublicFetchError("不支持代理隧道")
        self.sock = _public_socket(self.host, self.port, self.timeout)


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        if self._tunnel_host:
            raise PublicFetchError("不支持代理隧道")
        raw = _public_socket(self.host, self.port, self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PublicHTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPSConnection, req, context=self._context)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _inflate_limited(payload: bytes, wbits: int, max_bytes: int):
    decoder = zlib.decompressobj(wbits)
    # A max_length is essential: gzip.decompress/zlib.decompress can allocate
    # the entire expanded body before a later size check notices a bomb.
    decoded = decoder.decompress(payload, max_bytes + 1)
    if len(decoded) > max_bytes or decoder.unconsumed_tail:
        raise PublicFetchError("来源解压后响应过大，已停止读取")
    if not decoder.eof:
        raise PublicFetchError("来源压缩数据不完整，已停止正文解析")
    return decoded, decoder.unused_data


def _decode_content(payload: bytes, headers: dict, max_bytes=MAX_BYTES) -> bytes:
    """Decode HTTP content coding with bounds on every expansion stage."""
    if len(payload) > max_bytes:
        raise PublicFetchError("来源响应过大，已停止读取")
    coding_header = ",".join(str(value) for key, value in headers.items() if key.lower() == "content-encoding")
    codings = [value.strip().lower() for value in coding_header.split(",") if value.strip().lower() not in ("", "identity")]
    if not codings and payload.startswith(b"\x1f\x8b"):
        # Some origins compress even when identity was requested and omit the
        # encoding header. Never pass a recognizable gzip stream to a text parser.
        codings = ["gzip"]
    if len(codings) > 3 or any(coding not in ("gzip", "x-gzip", "deflate") for coding in codings):
        raise PublicFetchError("来源使用了不支持的压缩编码，已停止正文解析")
    try:
        for coding in reversed(codings):
            if coding in ("gzip", "x-gzip"):
                decoded, remaining, members = bytearray(), payload, 0
                while remaining:
                    members += 1
                    if members > 32:
                        raise PublicFetchError("来源压缩分段过多，已停止正文解析")
                    chunk, trailing = _inflate_limited(remaining, zlib.MAX_WBITS | 16, max_bytes - len(decoded))
                    decoded.extend(chunk)
                    if not trailing or not trailing.strip(b"\x00"):
                        break
                    if not trailing.startswith(b"\x1f\x8b"):
                        raise PublicFetchError("来源压缩数据包含无效尾部，已停止正文解析")
                    remaining = trailing
                if not payload:
                    raise PublicFetchError("来源压缩数据不完整，已停止正文解析")
                payload = bytes(decoded)
            else:
                # HTTP deflate is zlib-wrapped; tolerate legacy raw-deflate
                # origins, while preserving strict truncation and size checks.
                wrapped = len(payload) >= 2 and payload[0] & 15 == 8 and ((payload[0] << 8) + payload[1]) % 31 == 0
                payload, trailing = _inflate_limited(payload, zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS, max_bytes)
                if trailing:
                    raise PublicFetchError("来源压缩数据包含无效尾部，已停止正文解析")
    except zlib.error:
        raise PublicFetchError("来源压缩数据损坏或格式不兼容，已停止正文解析") from None
    return payload


def _request(url: str, *, headers=None, body=None, max_bytes=MAX_BYTES, allowed_status=(), redirect_guard=None, cancel_event=None):
    """Fetch a bounded response, revalidating every redirect and pinning DNS.

    Returns (decoded_bytes, response_headers, final_url, status). Content-Encoding
    is removed after bounded decompression. Proxies are deliberately disabled so
    a local proxy cannot bypass address checks.
    """
    _check_cancel_event(cancel_event)
    current = canonical_url(url)
    if not current:
        raise PublicFetchError("URL 无效或不是允许的公网 HTTP(S) 地址")
    request_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Accept": "*/*"}
    request_headers.update(headers or {})
    sensitive = any(k.lower() in ("authorization", "x-subscription-token", "x-api-key", "cookie") for k in request_headers) or body is not None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _HTTPHandler(), _HTTPSHandler(), _NoRedirect())
    deadline = time.monotonic() + TIMEOUT * 2
    for hop in range(4):
        _check_cancel_event(cancel_event)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PublicFetchError("请求超时")
        request = urllib.request.Request(current, data=body, headers=request_headers, method="POST" if body is not None else "GET")
        try:
            response = opener.open(request, timeout=min(TIMEOUT, remaining))
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location", "")
                exc.close()
                next_url = canonical_url(urllib.parse.urljoin(current, location))
                if not next_url or hop >= 3:
                    raise PublicFetchError("重定向无效、过多或指向非公网地址") from None
                if sensitive:
                    raise PublicFetchError("带凭据的搜索 API 不允许重定向") from None
                if redirect_guard:
                    redirect_guard(next_url)
                current = next_url
                continue
            if exc.code in allowed_status:
                response_headers, code = dict(exc.headers), exc.code
                exc.close()
                return b"", response_headers, current, code
            code = exc.code
            exc.close()
            if code in (401, 403, 412, 418, 429):
                raise PublicFetchError(f"HTTP {code}：来源拒绝访问、需要授权或触发限流；未绕过限制", http_status=code) from None
            raise PublicFetchError(f"上游返回 HTTP {code}", http_status=code) from None
        except PublicFetchError:
            raise
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
            raise PublicFetchError("网络请求失败、TLS 校验失败或超时") from None
        try:
            declared = response.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise PublicFetchError("来源响应过大，已停止读取")
            payload = bytearray()
            while len(payload) <= max_bytes:
                _check_cancel_event(cancel_event)
                if time.monotonic() > deadline:
                    raise PublicFetchError("读取来源超时")
                chunk = response.read1(min(65536, max_bytes + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > max_bytes:
                raise PublicFetchError("来源响应过大，已停止读取")
            response_headers = dict(response.headers)
            decoded = _decode_content(bytes(payload), response_headers, max_bytes=max_bytes)
            response_headers = {key: value for key, value in response_headers.items() if key.lower() not in ("content-encoding", "content-length")}
            response_headers["Content-Length"] = str(len(decoded))
            return decoded, response_headers, current, response.status
        except PublicFetchError:
            raise
        except (OSError, http.client.HTTPException):
            raise PublicFetchError("读取来源失败或超时") from None
        finally:
            response.close()
    raise PublicFetchError("重定向过多")


def _decode(payload: bytes, headers: dict) -> str:
    payload = _decode_content(payload, headers)
    content_type = next((str(v) for k, v in headers.items() if k.lower() == "content-type"), "")
    match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type, re.I)
    encoding = match.group(1) if match else "utf-8"
    try:
        return payload.decode(encoding, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


class _PlainText(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "canvas", "iframe", "nav", "footer", "form", "template"}
    BLOCKS = {"p", "div", "br", "li", "section", "article", "main", "h1", "h2", "h3", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.title_parts = []
        self.skipping = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping.append(tag)
        if tag == "title":
            self.in_title = True
        if not self.skipping and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.skipping:
            index = len(self.skipping) - 1 - self.skipping[::-1].index(tag)
            self.skipping = self.skipping[:index]
        if tag == "title":
            self.in_title = False
        if not self.skipping and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, value):
        if self.in_title:
            self.title_parts.append(value)
        elif not self.skipping:
            self.parts.append(value)

    def text(self):
        return "\n".join(line for line in (re.sub(r"\s+", " ", x).strip() for x in "".join(self.parts).splitlines()) if line)


def _clean(value, limit=3000):
    parser = _PlainText()
    parser.feed(str(value or ""))
    return parser.text()[:limit]


def _setting(config, *keys):
    for source in (config, config.get("search", {}), config.get("providers", {})):
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def search_engine_catalog(config=None) -> list[dict]:
    """Search engines are discovery sources, separate from content platforms."""
    config = config if isinstance(config, dict) else {}
    configured = {"tavily": bool(_setting(config, "tavily_key", "tavily_api_key")),
                  "brave": bool(_setting(config, "brave_key", "brave_api_key")),
                  "searxng": bool(_setting(config, "searxng_url"))}
    catalog = []
    for engine in SEARCH_ENGINE_IDS:
        template = _ENGINE_SEARCH_URLS.get(engine, "")
        if engine == "searxng":
            base = canonical_url(_setting(config, "searxng_url"))
            if base and urllib.parse.urlsplit(base).scheme == "https":
                parts = urllib.parse.urlsplit(base)
                path = parts.path.rstrip("/")
                if not path.endswith("/search"):
                    path += "/search"
                template = urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "q={query}", ""))
        access = "api" if engine in ("tavily", "brave", "searxng") else ("public_rss" if engine == "bing" else "public_html")
        description = ("需配置搜索 API；仅返回接口实际结果" if engine in ("tavily", "brave") else
                       "需配置允许访问的 SearXNG 实例并启用 JSON 输出" if engine == "searxng" else
                       "公开 RSS 搜索结果；覆盖和相关性由来源决定" if engine == "bing" else
                       "公开网页搜索，非官方 API；可能因验证码、页面变化或网络限制而无法自动读取")
        catalog.append({"id": engine, "label": _ENGINE_LABELS[engine], "access": access,
                        "requires_key": engine in ("tavily", "brave"),
                        "configured": configured.get(engine, True), "available": configured.get(engine, True), "search_url": template,
                        "description": description})
    return catalog


def search_engine_links(query: str, engines=None, config=None) -> list[dict]:
    if not isinstance(query, str) or not query.strip():
        return []
    selected = engines if isinstance(engines, list) and engines else SEARCH_ENGINE_IDS
    encoded = urllib.parse.quote(query.strip()[:1000], safe="")
    return [{"engine": item["id"], "label": item["label"] + "搜索", "url": item["search_url"].replace("{query}", encoded)}
            for item in search_engine_catalog(config) if item["id"] in selected and item["search_url"]]


def available_providers(config: dict) -> list[str]:
    config = config if isinstance(config, dict) else {}
    chosen = config.get("search_engines")
    selected = chosen if isinstance(chosen, list) and chosen else SEARCH_ENGINE_IDS
    generic = [item["id"] for item in search_engine_catalog(config) if item["id"] in selected and item["configured"]]
    return generic + ["bilibili", "github", "stackoverflow"]


def _platforms(platforms):
    return list(dict.fromkeys(p for p in (platforms or ["web"]) if p in PLATFORM_LABELS))


def _scoped_query(query, platforms):
    selected = _platforms(platforms)
    if not selected or "web" in selected or re.search(r"\bsite:", query, re.I):
        return query
    domains = [domain for p in selected if p in PLATFORM_HOSTS for domain in PLATFORM_HOSTS[p]]
    return query + " (" + " OR ".join("site:" + domain for domain in domains) + ")"


def _normalize_results(rows, platforms, provider, limit):
    selected = _platforms(platforms)
    seen = set()
    output = []
    for row in rows:
        url = _unwrap_search_url(row.get("url", "")) if provider in SEARCH_ENGINE_IDS else canonical_url(row.get("url", ""))
        if not url or url in seen:
            continue
        platform = platform_of(url)
        if "web" not in selected and platform not in selected:
            continue
        title = _clean(row.get("title", ""), 500)
        if not title:
            continue
        seen.add(url)
        item = {"title": title, "url": url, "snippet": _clean(row.get("snippet", "")), "platform": platform, "source": provider}
        if provider in SEARCH_ENGINE_IDS:
            item["engine"] = provider
        if isinstance(row.get("views"), int) and not isinstance(row.get("views"), bool) and row["views"] >= 0:
            item["views"] = row["views"]
        if row.get("published"):
            item["published"] = str(row["published"])[:80]
        # These bodies were returned by the public source API itself. Preserve
        # their evidence level, but never treat unrelated provider snippets as
        # fetched page bodies. GitHub Markdown/code is kept as text.
        if provider in ("github", "stackoverflow") and isinstance(row.get("body"), str) and row["body"].strip():
            item["body"] = row["body"].strip()[:12000]
            item["content_level"] = "page"
        if provider in ("github", "stackoverflow") and row.get("content_kind"):
            item["content_kind"] = str(row["content_kind"])[:40]
        output.append(item)
        if len(output) >= limit:
            break
    return output


_SEARCH_HOSTS = {"baidu.com", "www.baidu.com", "m.baidu.com", "wappass.baidu.com",
                 "bing.com", "www.bing.com", "cn.bing.com", "google.com", "www.google.com",
                 "google.co.uk", "www.google.co.uk", "google.com.hk", "www.google.com.hk",
                 "accounts.google.com", "consent.google.com", "yandex.com", "www.yandex.com",
                 "yandex.ru", "www.yandex.ru", "duckduckgo.com", "www.duckduckgo.com",
                 "html.duckduckgo.com", "search.brave.com"}


def _unwrap_search_url(value, base=""):
    """Unwrap known query wrappers without executing scripts or opening links."""
    if not isinstance(value, str) or len(value) > 16000:
        return ""
    value = html.unescape(value).strip()
    if base:
        value = urllib.parse.urljoin(base, value)
    for _ in range(4):
        normalized = canonical_url(value)
        if not normalized:
            return ""
        parts = urllib.parse.urlsplit(normalized)
        host = parts.hostname or ""
        if any(_host_matches(host, domain) for domain in ("googleadservices.com", "doubleclick.net", "cpro.baidu.com")):
            return ""
        if host not in _SEARCH_HOSTS:
            return normalized
        query = urllib.parse.parse_qs(parts.query)
        destination = ""
        if host in ("google.com", "www.google.com", "google.co.uk", "www.google.co.uk", "google.com.hk", "www.google.com.hk") and parts.path == "/url":
            destination = query.get("q", query.get("url", [""]))[0]
        elif host in ("bing.com", "www.bing.com", "cn.bing.com") and parts.path.rstrip("/") == "/ck/a":
            destination = query.get("u", [""])[0]
            if destination.startswith("a1"):
                try:
                    encoded = destination[2:]
                    destination = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True).decode("utf-8")
                except (ValueError, UnicodeError):
                    return ""
        elif host in ("duckduckgo.com", "www.duckduckgo.com", "html.duckduckgo.com") and parts.path in ("/l", "/l/"):
            destination = query.get("uddg", [""])[0]
        elif host in ("yandex.com", "www.yandex.com", "yandex.ru", "www.yandex.ru") and parts.path.startswith(("/clck/", "/redir")):
            destination = query.get("url", query.get("u", [""]))[0]
        elif host in ("baidu.com", "www.baidu.com") and parts.path in ("/link", "/ulink"):
            destination = query.get("url", [""])[0]
        if not destination or not destination.startswith(("https://", "http://")):
            return ""
        value = destination
    return ""


class _SearchNode:
    __slots__ = ("tag", "attrs", "parent", "children")

    def __init__(self, tag, attrs, parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []


class _SearchHTML(HTMLParser):
    """A bounded tree preserves title/link/snippet membership in result cards."""
    VOID = frozenset(("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"))

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _SearchNode("root", [])
        self.stack = [self.root]
        self.count = 0

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 40000 or len(self.stack) > 200:
            raise PublicFetchError("搜索页面结构过大或嵌套过深，已停止解析")
        node = _SearchNode(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _search_nodes(node):
    pending = [node]
    while pending:
        current = pending.pop()
        if not isinstance(current, _SearchNode):
            continue
        yield current
        pending.extend(reversed(current.children))


def _search_node_excluded(node):
    if node.tag in ("script", "style", "nav", "footer", "form", "template", "noscript"):
        return True
    if "hidden" in node.attrs or str(node.attrs.get("aria-hidden", "")).strip().lower() == "true":
        return True
    style = re.sub(r"/\*.*?\*/", "", str(node.attrs.get("style", "")), flags=re.S)
    return bool(re.search(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse)|content-visibility\s*:\s*hidden)\s*(?:!\s*important\s*)?(?:;|$)", style, re.I))


def _search_excluded_ancestor(node):
    while node is not None:
        if _search_node_excluded(node):
            return True
        node = node.parent
    return False


def _search_text(node, limit=3000):
    pending, pieces, length = [node], [], 0
    while pending and length < limit:
        current = pending.pop()
        if isinstance(current, str):
            pieces.append(current)
            length += len(current)
        elif not _search_node_excluded(current):
            pending.extend(reversed(current.children))
    return re.sub(r"\s+", " ", "".join(pieces)).strip()[:limit]


def _node_classes(node):
    return set((node.attrs.get("class") or "").split())


def _ad_node(node):
    classes = _node_classes(node)
    return (bool(classes.intersection({"ad", "ads", "advertisement", "uEierd", "ec-tuiguang", "ec_result", "c-container-ad", "serp-item_type_ad", "serp-item_type_adv"}))
            or node.attrs.get("id") in ("tads", "tadsb", "bottomads", "ads")
            or "data-text-ad" in node.attrs or "data-ad" in node.attrs)


def _search_challenge(parser, final_url):
    parts = urllib.parse.urlsplit(final_url)
    if re.search(r"/(?:sorry|showcaptcha|captcha|antispider|login|signin)(?:/|$)", parts.path, re.I) or parts.hostname in ("consent.google.com", "accounts.google.com", "wappass.baidu.com"):
        return True
    for node in _search_nodes(parser.root):
        if node.tag == "title":
            title = _search_text(node, 500).strip().lower()
            if title in ("百度安全验证", "安全验证", "人机验证", "are you not a robot?", "are you a robot?", "robot check", "just a moment...") or title.startswith(("google - sorry", "sorry - google")):
                return True
        if node.tag == "form" and ((node.attrs.get("id") or "").lower() in ("captcha-form", "challenge-form", "challenge") or re.search(r"/(?:showcaptcha|sorry|captcha)(?:/|\?|$)", node.attrs.get("action") or "", re.I)):
            return True
        if node.tag in ("div", "iframe") and ((node.attrs.get("id") or "").lower() in ("recaptcha", "captcha") or "g-recaptcha" in _node_classes(node)):
            return True
    return False


def _search_card(node, engine):
    current = node.parent
    while current is not None:
        classes = _node_classes(current)
        if engine == "baidu" and (classes.intersection(("result", "result-op")) or "c-container" in classes and str(current.attrs.get("id", "")).isdigit()):
            return current
        if engine == "google" and classes.intersection(("g", "MjjYud", "tF2Cxc", "Gx5Zad", "ezO2md")):
            return current
        if engine == "yandex" and classes.intersection(("serp-item", "Organic", "organic")):
            return current
        current = current.parent
    return None


def _search_html_rows(parser, engine, final_url):
    snippets = {"baidu": {"c-abstract", "c-span-last", "c-font-normal", "content-right"},
                "google": {"VwiC3b", "aCOpRe", "IsZvec", "st", "s3v9rd", "yXK7lf"},
                "yandex": {"OrganicTextContentSpan", "OrganicTextContent", "organic__text", "text-container"}}
    rows, seen_cards = [], set()
    for heading in _search_nodes(parser.root):
        if heading.tag not in (("h2", "h3") if engine == "yandex" else ("h3",)):
            continue
        if _search_excluded_ancestor(heading):
            continue
        card = _search_card(heading, engine)
        if card is None or id(card) in seen_cards:
            continue
        current, ad = card, False
        while current is not None:
            ad = ad or _ad_node(current)
            current = current.parent
        if ad or any(_ad_node(child) for child in _search_nodes(card)):
            continue
        link = next((child for child in _search_nodes(heading) if child.tag == "a" and child.attrs.get("href") and not _search_excluded_ancestor(child)), None)
        current = heading.parent
        while link is None and current is not None:
            if current.tag == "a" and current.attrs.get("href"):
                link = current
                break
            if current is card:
                break
            current = current.parent
        if link is None:
            continue
        title = _search_text(heading, 500)
        if not title:
            continue
        seen_cards.add(id(card))
        raw_url = urllib.parse.urljoin(final_url, link.attrs.get("href", ""))
        raw_parts = urllib.parse.urlsplit(canonical_url(raw_url))
        if any(_host_matches(raw_parts.hostname or "", domain) for domain in ("googleadservices.com", "doubleclick.net", "cpro.baidu.com")) or raw_parts.path in ("/baidu.php", "/aclick"):
            continue
        destination = _unwrap_search_url(raw_url)
        if not destination and engine == "baidu":
            # Only complete embedded target URLs count, never an abbreviated
            # display domain or a breadcrumb that would require guessing paths.
            for node in (link, card):
                candidates = [node.attrs.get(key, "") for key in ("mu", "data-landurl", "data-url")]
                for key in ("data-tools", "data-log"):
                    try:
                        embedded = json.loads(node.attrs.get(key) or "{}")
                    except (ValueError, TypeError):
                        continue
                    if isinstance(embedded, dict):
                        candidates.extend(embedded.get(key, "") for key in ("url", "mu"))
                destination = next((url for value in candidates if (url := _unwrap_search_url(value))), "")
                if destination:
                    break
        snippet = ""
        for child in _search_nodes(card):
            if _node_classes(child).intersection(snippets[engine]) and not _search_excluded_ancestor(child):
                snippet = _search_text(child)
                if snippet:
                    break
        row = {"title": title, "url": destination, "snippet": snippet}
        if not destination and engine == "baidu":
            parts = urllib.parse.urlsplit(canonical_url(raw_url))
            if parts.hostname in ("www.baidu.com", "baidu.com") and parts.path in ("/link", "/ulink"):
                row["_baidu_link"] = raw_url
        if destination or row.get("_baidu_link"):
            rows.append(row)
    return rows


class _SearchDestination(Exception):
    def __init__(self, url):
        self.url = url


def _baidu_target(url, event):
    """Stop at an external Location; never fetch result bodies to unwrap links."""
    def redirect_guard(target):
        _check_cancel_event(event)
        destination = _unwrap_search_url(target)
        if destination:
            raise _SearchDestination(destination)
        parts = urllib.parse.urlsplit(target)
        if parts.hostname not in ("baidu.com", "www.baidu.com") or parts.path not in ("/link", "/ulink"):
            raise PublicFetchError("百度结果跳转需要验证或没有可核实目标，已跳过")
        raise PublicFetchError("百度结果仍指向不透明跳转，已停止继续跳转解析")
    try:
        payload, headers, final_url, _ = _request(url, max_bytes=32000, redirect_guard=redirect_guard, cancel_event=event)
    except _SearchDestination as found:
        return found.url
    parser = _SearchHTML()
    parser.feed(_decode(payload, headers))
    if _search_challenge(parser, final_url):
        raise PublicFetchError("百度结果跳转返回人机验证，未继续请求")
    for node in _search_nodes(parser.root):
        if node.tag == "meta" and str(node.attrs.get("http-equiv", "")).lower() == "refresh":
            match = re.fullmatch(r"\s*\d+(?:\.\d+)?\s*;\s*url\s*=\s*['\"]?(https?://[^'\"\s]+)['\"]?\s*", node.attrs.get("content", ""), re.I)
            if match:
                return _unwrap_search_url(match.group(1))
    return ""


def _public_search_html(engine, query, limit, config):
    event, gate = config.get("_cancel_event"), _SEARCH_HTML_GATES[engine]
    template = _ENGINE_SEARCH_URLS[engine]
    url = template.replace("{query}", urllib.parse.quote(query, safe=""))
    with gate.slot(event):
        try:
            payload, headers, final_url, _ = _request(url, headers={"Accept": "text/html,application/xhtml+xml"}, cancel_event=event)
            _check_cancel_event(event)
            parser = _SearchHTML()
            parser.feed(_decode(payload, headers))
            if _search_challenge(parser, final_url):
                gate.cooldown(60)
                raise PublicFetchError(_ENGINE_LABELS[engine] + " 返回人机验证或登录页面，未绕过；可打开浏览器搜索入口或选择其他来源")
            allowed_hosts = {"baidu": ("baidu.com", "www.baidu.com"),
                             "google": ("google.com", "www.google.com", "google.co.uk", "www.google.co.uk", "google.com.hk", "www.google.com.hk"),
                             "yandex": ("yandex.com", "www.yandex.com", "yandex.ru", "www.yandex.ru")}
            if urllib.parse.urlsplit(final_url).hostname not in allowed_hosts[engine]:
                raise PublicFetchError(_ENGINE_LABELS[engine] + " 跳转到了其他来源，未把第三方页面当作该引擎结果")
            rows = _search_html_rows(parser, engine, final_url)
            if not rows and not re.search(r"did not match any documents|no results found|no results were found|没有找到|未找到相关|抱歉[^。]{0,40}没有|ничего не найдено|по вашему запросу ничего", _search_text(parser.root, 12000), re.I):
                raise PublicFetchError(_ENGINE_LABELS[engine] + " 未返回可识别的公开搜索结果；可能需要 JavaScript、访问受限或页面结构已改变")
            output = _ProviderRows()
            attempts, unresolved, resolving = 0, 0, True
            for row in rows:
                _check_cancel_event(event)
                if not row["url"] and row.get("_baidu_link"):
                    if resolving and attempts < 2:
                        attempts += 1
                        try:
                            row["url"] = _baidu_target(row["_baidu_link"], event)
                        except SearchCancelled:
                            raise
                        except PublicFetchError as exc:
                            if exc.http_status in (403, 412, 429) or "验证" in str(exc):
                                gate.cooldown(60)
                                resolving = False
                    if not row["url"]:
                        unresolved += 1
                        continue
                output.append({key: value for key, value in row.items() if not key.startswith("_")})
                if len(output) >= limit:
                    break
            output.coverage = _ENGINE_LABELS[engine] + " 公开网页搜索第 1 页及来源摘要（非官方 API）"
            if engine == "baidu":
                output.coverage += f"；额外核实 {attempts} 次结果跳转（最多 2 次，不读取目标正文）"
            if unresolved:
                output.warning = f"{unresolved} 条结果未能核实目标 URL，已跳过；没有用显示域名推测帖子链接"
            return output
        except PublicFetchError as exc:
            if exc.http_status in (403, 412, 429):
                gate.cooldown(60)
            raise


def _baidu(query, limit, config):
    return _public_search_html("baidu", query, limit, config)


def _google(query, limit, config):
    return _public_search_html("google", query, limit, config)


def _yandex(query, limit, config):
    return _public_search_html("yandex", query, limit, config)


def _bing(query, limit, config):
    url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "format": "rss", "count": min(limit, 50), "mkt": "zh-CN"})
    payload, _, _, _ = _request(url, cancel_event=config.get("_cancel_event"))
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        raise PublicFetchError("Bing 未返回 RSS 搜索数据，可能需要验证或服务已变更") from None
    if root.tag.lower() not in ("rss", "rdf"):
        raise PublicFetchError("Bing 未返回 RSS 搜索数据，可能需要验证或服务已变更")
    return [{"title": item.findtext("title", ""), "url": item.findtext("link", ""), "snippet": item.findtext("description", ""), "published": item.findtext("pubDate", "")} for item in root.findall(".//item")]


class _DDGResults(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.active = None
        self.capture = None
        self.capture_tag = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(attrs.get("class", "").split())
        if tag == "a" and "result__a" in classes:
            link = attrs.get("href", "")
            if link.startswith("//"):
                link = "https:" + link
            if link.startswith("/"):
                link = urllib.parse.urljoin("https://html.duckduckgo.com/", link)
            parsed = urllib.parse.urlsplit(link)
            if parsed.hostname and (parsed.hostname == "duckduckgo.com" or parsed.hostname.endswith(".duckduckgo.com")):
                link = urllib.parse.parse_qs(parsed.query).get("uddg", [link])[0]
            self.active = {"title": "", "url": link, "snippet": ""}
            self.rows.append(self.active)
            self.capture = "title"
            self.capture_tag = tag
        elif "result__snippet" in classes and self.active is not None:
            self.capture = "snippet"
            self.capture_tag = tag

    def handle_endtag(self, tag):
        if tag == self.capture_tag:
            self.capture = None
            self.capture_tag = None

    def handle_data(self, data):
        if self.active is not None and self.capture:
            self.active[self.capture] += data


def _duckduckgo(query, limit, config):
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query, "kl": "cn-zh"})
    payload, headers, _, _ = _request(url, cancel_event=config.get("_cancel_event"))
    document = _decode(payload, headers)
    parser = _DDGResults()
    parser.feed(document)
    if not parser.rows and any(token in document.lower() for token in ("anomaly.js", "challenge-form", "bots use duckduckgo", "captcha")):
        raise PublicFetchError("DuckDuckGo 返回人机验证，未绕过；可使用其他来源或配置搜索 API")
    if not parser.rows and not any(token in document.lower() for token in ("no results", "no-results", "没有找到")):
        raise PublicFetchError("DuckDuckGo 页面未包含可识别的搜索结果")
    return parser.rows


def _json_request(url, **kwargs):
    payload, headers, _, _ = _request(url, **kwargs)
    try:
        value = json.loads(_decode(payload, headers))
    except (ValueError, UnicodeError):
        raise PublicFetchError("上游未返回有效 JSON，可能需要验证或授权") from None
    if not isinstance(value, dict):
        raise PublicFetchError("上游响应格式不符合搜索接口约定")
    return value


def _parse_views(value):
    if isinstance(value, int) and value >= 0:
        return value
    text = str(value or "").replace(",", "").strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(万|亿)?", text)
    if match:
        return int(float(match.group(1)) * {None: 1, "万": 10000, "亿": 100000000}[match.group(2)])
    return None


class _ProviderRows(list):
    """List-compatible result batch with source coverage diagnostics."""
    coverage = ""
    warning = ""


class _SourceGate:
    """One in-flight request per API, with cancellable spacing and cooldown."""

    def __init__(self, label, interval):
        self.label, self.interval = label, interval
        self.lock = threading.Semaphore(1)
        self.next_request_at = self.cooldown_until = 0.0

    def cooldown(self, seconds):
        self.cooldown_until = max(self.cooldown_until, time.monotonic() + min(86400, max(0, seconds)))

    @contextmanager
    def slot(self, event):
        while True:
            _check_cancel_event(event)
            if self.lock.acquire(timeout=0.1):
                break
        try:
            _check_cancel_event(event)
            now = time.monotonic()
            if now < self.cooldown_until:
                remaining = max(1, int(self.cooldown_until - now + 0.999))
                raise PublicFetchError(f"{self.label} 来源冷却中（约 {remaining} 秒），本次未继续请求")
            delay = self.next_request_at - now
            if delay > 0:
                if event is None:
                    time.sleep(delay)
                else:
                    event.wait(delay)
                    _check_cancel_event(event)
            _check_cancel_event(event)
            self.next_request_at = time.monotonic() + self.interval
            yield
        finally:
            self.lock.release()


_GITHUB_GATE = _SourceGate("GitHub", 6.2)
_STACKOVERFLOW_GATE = _SourceGate("Stack Overflow", 1.0)
_SEARCH_HTML_GATES = {engine: _SourceGate(_ENGINE_LABELS[engine], 1.5) for engine in ("baidu", "google", "yandex")}


def _numeric(value, default=0):
    try:
        return max(0, min(10**12, int(value)))
    except (ValueError, TypeError, OverflowError):
        return default


def _public_api_json(url, gate, config, headers=None, *, allow_list=False):
    event = config.get("_cancel_event")
    with gate.slot(event):
        try:
            payload, response_headers, _, status = _request(url, headers=headers, allowed_status=(403, 429), cancel_event=event)
        except PublicFetchError as exc:
            if exc.http_status in (403, 412, 429):
                gate.cooldown(60)
            raise
        info = {key.lower(): value for key, value in response_headers.items()}
        if str(info.get("x-ratelimit-remaining", "")) == "0":
            gate.cooldown(max(60, _numeric(info.get("x-ratelimit-reset")) - time.time() + 1))
        if status in (403, 429):
            gate.cooldown(max(60, _numeric(info.get("retry-after"))))
            raise PublicFetchError(f"{gate.label} 公开 API 返回 HTTP {status}，已停止后续请求并进入冷却", http_status=status)
        try:
            value = json.loads(_decode(payload, response_headers))
        except (ValueError, UnicodeError):
            raise PublicFetchError(f"{gate.label} 未返回有效 JSON") from None
        if allow_list and isinstance(value, list):
            return value
        if not isinstance(value, dict):
            raise PublicFetchError(f"{gate.label} 响应格式已变更")
        backoff = _numeric(value.get("backoff"))
        if backoff:
            gate.cooldown(backoff)
        if value.get("quota_remaining") == 0:
            # StackExchange anonymous daily quota resets at midnight UTC.
            gate.cooldown(86400 - (time.time() % 86400) + 1)
        if value.get("error_id") is not None:
            gate.cooldown(max(60, backoff))
            raise PublicFetchError(f"{gate.label} API 返回错误（代码 {_numeric(value.get('error_id'))}），已停止继续请求")
        return value


def _direct_query(query):
    query = re.sub(r"\bsite:\S+", "", query, flags=re.I).strip(" ()")
    # A long natural-language Chinese sentence is usually an over-constrained
    # AND query in these APIs. Preserve technical identifiers without treating
    # the omitted prose as evidence or relaxing later result verification.
    if len(query) > 40 and re.search(r"[\u3400-\u9fff]", query):
        identifiers = re.findall(r"[A-Za-z][A-Za-z0-9_+.#:/-]*", query)
        identifiers = [term.rstrip("./:-") for term in identifiers if len(term) > 1]
        if len(identifiers) >= 2:
            return " ".join(dict.fromkeys(identifiers))[:256]
    return query


def _github_rows(value, kind):
    if not isinstance(value.get("items"), list):
        raise PublicFetchError("GitHub 未返回公开搜索结果集合")
    rows = []
    for item in value["items"]:
        if not isinstance(item, dict):
            continue
        url = canonical_url(item.get("html_url", ""))
        if platform_of(url) != "github":
            continue
        if kind == "issues":
            body = str(item.get("body") or "")[:12000]
            row = {"title": item.get("title"), "url": url, "snippet": body[:3000], "body": body,
                   "content_kind": "pull_request" if item.get("pull_request") else "issue", "published": item.get("created_at")}
        else:
            row = {"title": item.get("full_name") or item.get("name"), "url": url,
                   "snippet": item.get("description") or "", "content_kind": "repository", "published": item.get("created_at")}
        rows.append(row)
    return rows


def _github(query, limit, config):
    query = _direct_query(query)
    if not query or len(query) > 256:
        raise PublicFetchError("GitHub 检索请使用不超过 256 字符的关键词或技术标识符")
    depth = config.get("_search_depth")
    passes = [("issues", 1)]
    if depth in ("deep", "research") and config.get('_github_search_kind') != 'issues' and not re.search(r"\b(?:repo|is|label|author|assignee|milestone|linked|project|in):", query, re.I):
        passes.append(("repositories", 1))
    if depth == "research":
        passes.append(("issues", 2))
    output, batches, completed = _ProviderRows(), [], []
    more_issues = False
    for kind, page in passes:
        if kind == "issues" and page > 1 and not more_issues:
            continue
        url = "https://api.github.com/search/" + kind + "?" + urllib.parse.urlencode({"q": query, "per_page": min(limit, 50), "page": page})
        try:
            value = _public_api_json(url, _GITHUB_GATE, config, {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
            batches.append(_github_rows(value, kind))
            completed.append(("issues / PR" if kind == "issues" else "仓库") + f"第 {page} 页")
            if kind == "issues":
                more_issues = _numeric(value.get("total_count")) > page * min(limit, 50) and bool(value.get("items"))
            if value.get("incomplete_results"):
                output.warning = "GitHub 标记当前搜索结果不完整；仅展示接口实际返回的内容"
        except SearchCancelled:
            raise
        except PublicFetchError as exc:
            if not batches:
                raise
            output.warning = "部分搜索完成；后续请求已停止：" + str(exc)
            break
    for index in range(max((len(batch) for batch in batches), default=0)):
        for batch in batches:
            if index < len(batch):
                output.append(batch[index])
    output.coverage = "GitHub 公开 " + "、".join(completed) + "；issue / PR 首楼正文，不含评论、私有内容及代码搜索"
    return output


def _stackoverflow(query, limit, config):
    query = _direct_query(query)
    if not query:
        raise PublicFetchError("Stack Overflow 搜索关键词不能为空")
    pages = {"deep": 2, "research": 3}.get(config.get("_search_depth"), 1)
    output, batches, completed = _ProviderRows(), [], []
    for page in range(1, pages + 1):
        url = "https://api.stackexchange.com/2.3/search/advanced?" + urllib.parse.urlencode({
            "site": "stackoverflow", "q": query, "pagesize": min(limit, 50), "page": page,
            "sort": "relevance", "order": "desc", "filter": "withbody"})
        try:
            value = _public_api_json(url, _STACKOVERFLOW_GATE, config)
            if not isinstance(value.get("items"), list):
                raise PublicFetchError("Stack Overflow 未返回问题集合")
            batch = []
            for item in value["items"]:
                if not isinstance(item, dict):
                    continue
                url = canonical_url(item.get("link", ""))
                if platform_of(url) != "stackoverflow":
                    continue
                body = _clean(item.get("body", ""), 12000)
                row = {"title": item.get("title"), "url": url, "snippet": body[:3000], "body": body,
                       "content_kind": "question", "views": item.get("view_count")}
                timestamp = _numeric(item.get("creation_date"))
                if 0 < timestamp < 253402300800:
                    row["published"] = dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).isoformat()
                batch.append(row)
            batches.append(batch)
            completed.append(str(page))
            if value.get("backoff") or value.get("quota_remaining") == 0:
                output.warning = "StackExchange 要求暂停或匿名额度已用尽；保留已返回问题，后续请求已停止"
                break
            if not value.get("has_more") or not value["items"]:
                break
        except SearchCancelled:
            raise
        except PublicFetchError as exc:
            if not completed:
                raise
            output.warning = "部分搜索完成；后续分页已停止：" + str(exc)
            break
    for index in range(max((len(batch) for batch in batches), default=0)):
        for batch in batches:
            if index < len(batch):
                output.append(batch[index])
    output.coverage = "Stack Overflow 公开问题第 " + "、".join(completed) + " 页及问题正文；浏览量为接口记录，不包含回答或评论"
    return output


def _bilibili_rows(value):
    rows = []
    for item in (value.get("data") or {}).get("result") or []:
        link = item.get("arcurl") or ("https://www.bilibili.com/video/" + str(item["bvid"]) if item.get("bvid") else "")
        row = {"title": item.get("title"), "url": link, "snippet": item.get("description", ""), "views": _parse_views(item.get("play"))}
        try:
            timestamp = int(item.get("pubdate", 0))
            if timestamp > 0:
                row["published"] = dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).isoformat()
        except (ValueError, TypeError, OverflowError, OSError):
            pass
        rows.append(row)
    return rows


@contextmanager
def _bilibili_slot(event):
    if event is None:
        with _BILIBILI_GATE:
            yield
        return
    while True:
        _check_cancel_event(event)
        if _BILIBILI_GATE.acquire(timeout=0.1):
            break
    try:
        _check_cancel_event(event)
        yield
    finally:
        _BILIBILI_GATE.release()


def _bilibili_request(url, config=None):
    """Serialize this source, space starts, and stop queued work after denial."""
    global _bilibili_next_request_at, _bilibili_cooldown_until
    event = (config or {}).get("_cancel_event")
    with _bilibili_slot(event):
        _check_cancel_event(event)
        now = time.monotonic()
        if now < _bilibili_cooldown_until:
            remaining = max(1, int(_bilibili_cooldown_until - now + 0.999))
            raise PublicFetchError(f"哔哩哔哩请求暂时冷却（约 {remaining} 秒）；上一请求被拒绝，本次未向来源重试")
        delay = _bilibili_next_request_at - now
        if delay > 0:
            if event is None:
                time.sleep(delay)
            else:
                event.wait(delay)
                _check_cancel_event(event)
        _bilibili_next_request_at = time.monotonic() + BILIBILI_MIN_INTERVAL
        try:
            _check_cancel_event(event)
            value = _json_request(url, headers={"Referer": "https://search.bilibili.com/"}, cancel_event=event)
            if value.get("code") != 0:
                code = value.get("code")
                safe_code = str(code) if isinstance(code, int) else "未知"
                denied_status = abs(code) if isinstance(code, int) and abs(code) in (403, 412, 429) else None
                raise PublicFetchError(f"哔哩哔哩公开视频接口拒绝请求（代码 {safe_code}）；未绕过登录或风控", http_status=denied_status)
            return value
        except PublicFetchError as exc:
            if exc.http_status in (403, 412, 429):
                _bilibili_cooldown_until = time.monotonic() + BILIBILI_COOLDOWN_SECONDS
            raise


def _bilibili(query, limit, config):
    query = re.sub(r"\bsite:\S+", "", query, flags=re.I).strip(" ()")
    passes = [("totalrank", 1)]
    if config.get("_search_depth") in ("deep", "research"):
        passes += [("pubdate", 1), ("pubdate", 2)]
    batches = []
    completed = []
    output = _ProviderRows()
    for order, page in passes:
        url = "https://api.bilibili.com/x/web-interface/search/type?" + urllib.parse.urlencode({"search_type": "video", "keyword": query, "page": page, "page_size": min(limit, 50), "order": order})
        try:
            value = _bilibili_request(url, config)
            batches.append(_bilibili_rows(value))
            completed.append(("综合" if order == "totalrank" else "最新发布") + f"第 {page} 页")
        except SearchCancelled:
            raise
        except PublicFetchError as exc:
            if not batches:
                raise
            output.warning = "部分搜索完成；后续分页已停止：" + str(exc)
            break
    # Interleave ranks so a full first page cannot crowd out the newer results.
    for index in range(max((len(batch) for batch in batches), default=0)):
        for batch in batches:
            if index < len(batch):
                output.append(batch[index])
    output.coverage = "公开视频；已读取" + "、".join(completed) + "（不包含全部动态、专栏或评论）"
    return output


def _tavily(query, limit, config):
    key = _setting(config, "tavily_key", "tavily_api_key")
    if not key:
        raise PublicFetchError("请先配置 Tavily API Key")
    body = json.dumps({"query": query, "search_depth": "advanced", "max_results": min(limit, 20), "include_answer": False, "include_raw_content": False}).encode()
    value = _json_request("https://api.tavily.com/search", headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, body=body, cancel_event=config.get("_cancel_event"))
    if "results" not in value:
        raise PublicFetchError("Tavily 未返回结果集合，请检查额度与接口配置")
    return [{"title": item.get("title"), "url": item.get("url"), "snippet": item.get("content", ""), "published": item.get("published_date")} for item in value.get("results", [])]


def _brave(query, limit, config):
    key = _setting(config, "brave_key", "brave_api_key")
    if not key:
        raise PublicFetchError("请先配置 Brave Search API Key")
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": min(limit, 20), "search_lang": "zh-hans", "country": "ALL"})
    value = _json_request(url, headers={"X-Subscription-Token": key, "Accept": "application/json"}, cancel_event=config.get("_cancel_event"))
    if value.get("error"):
        raise PublicFetchError("Brave Search API 返回错误，请检查配置与额度")
    return [{"title": item.get("title"), "url": item.get("url"), "snippet": item.get("description", ""), "published": item.get("page_age")} for item in (value.get("web") or {}).get("results", [])]


def _searxng(query, limit, config):
    base = canonical_url(_setting(config, "searxng_url"))
    if not base:
        raise PublicFetchError("请配置公网 SearXNG HTTPS/HTTP 地址；不允许内网实例")
    parts = urllib.parse.urlsplit(base)
    path = parts.path.rstrip("/")
    if not path.endswith("/search"):
        path += "/search"
    url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, urllib.parse.urlencode({"q": query, "format": "json", "language": "zh-CN"}), ""))
    value = _json_request(url, cancel_event=config.get("_cancel_event"))
    if "results" not in value:
        raise PublicFetchError("SearXNG 未返回结果集合；实例需启用 JSON 输出")
    return [{"title": item.get("title"), "url": item.get("url"), "snippet": item.get("content", ""), "published": item.get("publishedDate")} for item in value.get("results", [])]


def search_provider(provider: str, query: str, platforms: list[str], limit: int, config: dict) -> dict:
    config = config if isinstance(config, dict) else {}
    status = {"provider": provider, "ok": False, "count": 0}
    implementations = {"baidu": _baidu, "bing": _bing, "google": _google, "yandex": _yandex, "duckduckgo": _duckduckgo, "bilibili": _bilibili, "github": _github, "stackoverflow": _stackoverflow, "tavily": _tavily, "brave": _brave, "searxng": _searxng}
    try:
        _check_cancel_event(config.get("_cancel_event"))
        if provider not in implementations:
            raise PublicFetchError("未知搜索来源")
        if not isinstance(query, str) or not query.strip():
            raise PublicFetchError("搜索词不能为空")
        selected = _platforms(platforms)
        if not selected:
            raise PublicFetchError("未选择有效平台")
        direct = provider in ("bilibili", "github", "stackoverflow")
        if direct and provider not in selected and "web" not in selected:
            return {"results": [], "status": {**status, "ok": True, "skipped": True}}
        limit = max(1, min(int(limit), 50))
        scoped = query.strip()[:1000] if direct else _scoped_query(query.strip()[:1000], selected)
        rows = implementations[provider](scoped, limit, config or {})
        _check_cancel_event(config.get("_cancel_event"))
        results = _normalize_results(rows, selected, provider, limit)
        status.update(ok=True, count=len(results))
        if getattr(rows, "coverage", ""):
            status["coverage"] = rows.coverage
        if getattr(rows, "warning", ""):
            status.update(partial=True, warning=rows.warning, message=rows.warning)
        if not results:
            status["message"] = "来源未返回符合平台范围的可用结果；不代表相关帖子不存在"
        return {"results": results, "status": status}
    except SearchCancelled as exc:
        status.update(cancelled=True, error=str(exc))
    except PublicFetchError as exc:
        status["error"] = str(exc)
    except Exception:
        status["error"] = "搜索来源响应异常或格式已变更"
    return {"results": [], "status": status}


def native_search_links(query: str, platforms: list[str]) -> list[dict]:
    encoded = urllib.parse.quote(query, safe="")
    builders = {
        "bilibili": "https://search.bilibili.com/all?" + urllib.parse.urlencode({"keyword": query}),
        "xiaohongshu": "https://www.xiaohongshu.com/search_result?" + urllib.parse.urlencode({"keyword": query, "source": "web_search_result_notes"}),
        "zhihu": "https://www.zhihu.com/search?" + urllib.parse.urlencode({"type": "content", "q": query}),
        "wechat": "https://weixin.sogou.com/weixin?" + urllib.parse.urlencode({"type": 2, "query": query}),
        "meituan": "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query + " site:meituan.com"}),
        "dianping": "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query + " site:dianping.com"}),
        "douyin": "https://www.douyin.com/search/" + encoded,
        "tieba": "https://tieba.baidu.com/f/search/res?" + urllib.parse.urlencode({"ie": "utf-8", "qw": query}),
        "douban": "https://www.douban.com/search?" + urllib.parse.urlencode({"q": query}),
        "github": "https://github.com/search?" + urllib.parse.urlencode({"q": query, "type": "issues"}),
        "stackoverflow": "https://stackoverflow.com/search?" + urllib.parse.urlencode({"q": query}),
        "v2ex": "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query + " site:v2ex.com"}),
        "csdn": "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query + " site:csdn.net"}),
        "cnblogs": "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query + " site:cnblogs.com"}),
        "reddit": "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query + " site:reddit.com"}),
        "web": "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query}),
    }
    labels = {"wechat": "公众号文章搜索（搜狗）", "meituan": "美团公开索引", "dianping": "大众点评公开索引", "v2ex": "V2EX 公开索引", "csdn": "CSDN 公开索引", "cnblogs": "博客园公开索引", "reddit": "Reddit 公开索引", "web": "Bing 全网搜索"}
    return [{"platform": p, "label": labels.get(p, PLATFORM_LABELS[p] + "站内搜索"), "url": builders[p]} for p in _platforms(platforms)]


def normalize_custom_sites(value: list[dict | str]) -> list[dict]:
    """Validate up to six public host scopes and optional HTTPS search templates."""
    if not isinstance(value, list) or len(value) > 6:
        raise ValueError("自定义网站须为列表，最多设置 6 个网站。")
    output, seen = [], set()
    for entry in value:
        if isinstance(entry, str):
            entry = {"domain": entry}
        if not isinstance(entry, dict) or not isinstance(entry.get("domain"), str):
            raise ValueError("每个自定义网站须提供域名。")
        raw = entry["domain"].strip()
        normalized = canonical_url(raw if "://" in raw else "https://" + raw)
        if not normalized:
            raise ValueError("网站域名须为有效公网地址，不允许本机、内网、账号密码或非标准端口。")
        domain = urllib.parse.urlsplit(normalized).hostname or ""
        try:
            ipaddress.ip_address(domain)
        except ValueError:
            pass
        else:
            raise ValueError("自定义网站请填写域名，不使用 IP 地址。")
        name = entry.get("name", domain)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or re.search(r"[\x00-\x1f\x7f]", name):
            raise ValueError("网站名称须为 1–80 个字符的单行文本。")
        template = entry.get("search_url", "")
        if not isinstance(template, str):
            raise ValueError("站内搜索模板须为文本。")
        template = template.strip()
        if template:
            decoded = urllib.parse.unquote(template)
            if template.count("{query}") != 1 or re.findall(r"\{[^{}]*\}", decoded) != ["{query}"] or "{" in decoded.replace("{query}", "") or "}" in decoded.replace("{query}", ""):
                raise ValueError("站内搜索模板必须且只能包含一次 {query}，不能有其他占位符。")
            marker = "smartsearchplaceholder7e263d"
            try:
                parts = urllib.parse.urlsplit(template)
            except ValueError:
                raise ValueError("站内搜索模板 URL 格式无效。") from None
            if "{query}" in parts.netloc or "{query}" in parts.fragment:
                raise ValueError("搜索词占位符只能出现在 URL 路径或查询参数中。")
            concrete = canonical_url(template.replace("{query}", marker))
            parsed = urllib.parse.urlsplit(concrete)
            if not concrete or parsed.scheme != "https" or not _host_matches(parsed.hostname or "", domain) or concrete.count(marker) != 1:
                raise ValueError("站内搜索模板须为同一域名下的公网 HTTPS URL，不能包含账号密码。")
            template = concrete.replace(marker, "{query}")
        if domain in seen:
            continue
        seen.add(domain)
        output.append({"name": name.strip(), "domain": domain, "search_url": template})
    return output


def _custom_search_url(site, query):
    if site.get("search_url"):
        return site["search_url"].replace("{query}", urllib.parse.quote(query, safe=""))
    return "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query + " site:" + site["domain"]})


def custom_search_links(query: str, sites: list) -> list[dict]:
    return [{"platform": "website", "label": site["name"] + ("站内搜索" if site["search_url"] else "公开索引"),
             "url": _custom_search_url(site, query), "site": site["name"], "domain": site["domain"]}
            for site in normalize_custom_sites(sites)]


class _InternalSearchResults(HTMLParser):
    """Extract real anchors from semantic result containers/headings.

    Generic navigation and link menus are deliberately not search results. An
    unsupported dynamic layout is reported as such instead of inventing hits.
    """
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    # Several server-rendered sites put result headings inside the search form.
    # Ignore form controls via result semantics, not the entire form subtree.
    EXCLUDE = (_PlainText.SKIP - {"form"}) | {"head", "aside"}
    RESULT_MARKER = re.compile(r"(?:^|[\s_-])(?:result|results-item|search-result|search-item|search-hit|hit|entry|post-item|item-result)(?:$|[\s_-])|searchresult", re.I)

    def __init__(self, base_url, domain):
        super().__init__(convert_charrefs=True)
        self.base_url, self.domain = base_url, domain
        self.stack, self.anchors = [], []
        self.active = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        excluded = tag in self.EXCLUDE or any(frame["excluded"] for frame in self.stack)
        marker = attrs.get("class", "") + " " + attrs.get("id", "")
        context = None
        if not excluded and (tag == "article" or tag in ("div", "li", "section", "tr") and self.RESULT_MARKER.search(marker)):
            context = {"parts": [], "size": 0}
        if tag not in self.VOID:
            self.stack.append({"tag": tag, "excluded": excluded, "context": context})
        if excluded or tag != "a":
            return
        contexts = [frame["context"] for frame in self.stack if frame["context"] is not None]
        heading = any(frame["tag"] in ("h1", "h2", "h3", "h4") for frame in self.stack)
        title_class = bool(re.search(r"(?:title|result|permalink)", marker, re.I))
        if not (contexts or heading or title_class):
            return
        if not contexts and (heading or title_class):
            # Activate unnamed result-list context after finding a title, so
            # its snippet stays local without promoting generic menu links.
            for frame in reversed(self.stack):
                if frame["tag"] == "li":
                    frame["context"] = {"parts": [], "size": 0}
                    contexts = [frame["context"]]
                    break
        href = attrs.get("href", "")
        if not href or href.startswith(("#", "javascript:", "mailto:")) or attrs.get("rel", "").lower() == "next":
            return
        url = canonical_url(urllib.parse.urljoin(self.base_url, href))
        host = urllib.parse.urlsplit(url).hostname or ""
        if not url or not _host_matches(host, self.domain):
            return
        parsed = urllib.parse.urlsplit(url)
        if re.search(r"/(?:login|signin|signup|register|logout|account)(?:/|$)", parsed.path, re.I):
            return
        self.active = {"url": url, "parts": [], "context": contexts[-1] if contexts else None,
                       "heading": heading or title_class}
        self.anchors.append(self.active)

    def handle_endtag(self, tag):
        if tag == "a":
            self.active = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                self.stack = self.stack[:index]
                break

    def handle_data(self, data):
        if any(frame["excluded"] for frame in self.stack):
            return
        if self.active is not None:
            self.active["parts"].append(data)
        for frame in self.stack:
            context = frame["context"]
            if context is not None and context["size"] < 6000:
                piece = data[:6000 - context["size"]]
                context["parts"].append(piece)
                context["size"] += len(piece)

    def rows(self):
        rows = []
        strong_contexts = {id(anchor["context"]) for anchor in self.anchors if anchor["heading"] and anchor["context"] is not None}
        for anchor in self.anchors:
            context = anchor["context"]
            if context is not None and id(context) in strong_contexts and not anchor["heading"]:
                continue
            title = re.sub(r"\s+", " ", "".join(anchor["parts"])).strip()
            if len(title) < 2 or title.lower() in ("read more", "learn more", "下一页", "上一页", "查看详情", "阅读全文", "登录注册", "登录", "注册", "首页", "更多"):
                continue
            snippet = re.sub(r"\s+", " ", " ".join(context["parts"])).strip() if context else ""
            rows.append({"title": title[:500], "url": anchor["url"], "snippet": snippet[:3000]})
        return rows


def search_custom_site(site: dict, query: str, limit: int, config: dict) -> dict:
    config = config if isinstance(config, dict) else {}
    status = {"provider": "website", "ok": False, "count": 0}
    try:
        _check_cancel_event(config.get("_cancel_event"))
        normalized = normalize_custom_sites([site])[0]
        domain, name = normalized["domain"], normalized["name"]
        status.update(site=name, domain=domain)
        if not isinstance(query, str) or not query.strip():
            raise PublicFetchError("搜索词不能为空")
        query, limit = query.strip()[:1000], max(1, min(int(limit), 50))
        if not normalized["search_url"]:
            engine = config.get("_engine", "bing")
            if engine not in SEARCH_ENGINE_IDS:
                raise PublicFetchError("自定义网站公开索引须选择通用搜索来源")
            response = search_provider(engine, query + " site:" + domain, ["web"], limit, config)
            status.update(response["status"], provider="website", engine=engine, site=name, domain=domain,
                          coverage="指定域名的公开搜索索引；不保证该站所有内容已被收录")
            rows = [row for row in response["results"] if _host_matches(urllib.parse.urlsplit(row.get("url", "")).hostname or "", domain)]
        else:
            target = _custom_search_url(normalized, query)
            event = config.get("_cancel_event")

            def guard(url):
                _check_cancel_event(event)
                if not _host_matches(urllib.parse.urlsplit(url).hostname or "", domain):
                    raise PublicFetchError("站内搜索跳转到了域名范围之外，已停止")
                _check_robots(url, cancel_event=event)

            guard(target)
            payload, headers, final_url, _ = _request(target, headers={"Accept": "text/html,application/xhtml+xml"}, redirect_guard=guard, cancel_event=event)
            _check_cancel_event(event)
            if not _host_matches(urllib.parse.urlsplit(final_url).hostname or "", domain):
                raise PublicFetchError("站内搜索返回了域名范围之外的页面")
            content_type = next((str(v).lower() for k, v in headers.items() if k.lower() == "content-type"), "")
            if not any(value in content_type for value in ("text/html", "application/xhtml+xml")):
                raise PublicFetchError("站内搜索未返回可解析的 HTML 页面")
            document = _decode(payload, headers)
            plain = _PlainText()
            plain.SKIP = plain.SKIP - {"form"}
            plain.feed(document)
            title = " ".join(plain.title_parts).lower()
            if re.search(r"/(?:login|signin|antispider|captcha)(?:/|\?|$)", final_url, re.I) or any(token in title for token in ("captcha", "just a moment", "人机验证", "安全验证", "登录", "sign in")) or "challenge-form" in document.lower():
                raise PublicFetchError("站内搜索需要登录或人机验证，未绕过限制")
            parser = _InternalSearchResults(final_url, domain)
            parser.feed(document)
            rows = parser.rows()
            if not rows and not re.search(r"no results|no matches|没有找到|未找到|暂无结果|没有相关", plain.text(), re.I):
                raise PublicFetchError("站内页面没有可识别的公开结果链接，可能依赖 JavaScript 或使用尚不支持的页面结构；请打开浏览器入口")
            status.update(ok=True, coverage="已读取公开站内 HTML 搜索结果；仅提取同一域名内的实际链接")
        _check_cancel_event(config.get("_cancel_event"))
        results = _normalize_results(rows, ["web"], "website", limit)
        for row in results:
            row.update(platform="website", site=name, domain=domain)
            if status.get("engine"):
                row["engine"] = status["engine"]
        status["count"] = len(results)
        if not results and status.get("ok"):
            status["message"] = "该网站本轮没有可用公开结果；不代表站内不存在相关内容"
        return {"results": results, "status": status}
    except SearchCancelled as exc:
        status.update(cancelled=True, error=str(exc))
    except (PublicFetchError, ValueError) as exc:
        status["error"] = str(exc)
    except Exception:
        status["error"] = "自定义网站返回格式异常，未生成推测性结果"
    return {"results": [], "status": status}


def _page_domains(allowed_domains):
    """Normalize an explicit scope without widening a discovered hostname."""
    if allowed_domains is None:
        return None
    if not isinstance(allowed_domains, (list, tuple)):
        raise PublicFetchError("页面域名范围必须是域名列表")
    domains = []
    for domain in allowed_domains:
        if not isinstance(domain, str) or any(char in domain for char in "/:?#@"):
            raise PublicFetchError("页面域名范围包含无效域名")
        normalized = canonical_url("https://" + domain)
        if not normalized:
            raise PublicFetchError("页面域名范围包含无效域名")
        domains.append(urllib.parse.urlsplit(normalized).hostname)
    return tuple(dict.fromkeys(domains))


def _require_page_scope(url, allowed_domains):
    normalized = canonical_url(url)
    if not normalized:
        raise PublicFetchError("URL 无效或不是允许的公网 HTTP(S) 地址")
    host = urllib.parse.urlsplit(normalized).hostname or ""
    if allowed_domains is not None and not any(_host_matches(host, domain) for domain in allowed_domains):
        raise PublicFetchError("页面地址超出所选域名范围，已在请求前停止")
    return normalized


def _check_robots(url: str, cancel_event=None, allowed_domains=None):
    parts = urllib.parse.urlsplit(url)
    robots_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    try:
        def guard(target):
            _check_cancel_event(cancel_event)
            _require_page_scope(target, allowed_domains)

        guard(robots_url)
        payload, headers, final_url, status = _request(robots_url, max_bytes=512_000, allowed_status=(404, 410), redirect_guard=guard, cancel_event=cancel_event)
        guard(final_url)
    except SearchCancelled:
        raise
    except PublicFetchError:
        raise PublicFetchError("无法确认网站 robots.txt 规则，已跳过正文抓取") from None
    if status in (404, 410):
        return
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(_decode(payload, headers).splitlines())
    if not parser.can_fetch(USER_AGENT, url):
        raise PublicFetchError("网站 robots.txt 不允许抓取该页面，保留搜索摘要")
    delay = parser.crawl_delay(USER_AGENT)
    rate = parser.request_rate(USER_AGENT)
    if delay or rate:
        raise PublicFetchError("网站设置了爬虫访问频率规则，本次保留搜索摘要")


def fetch_result_details(result: dict, max_chars=12000, cancel_event=None) -> dict:
    """Fetch bounded replies only for an existing, precisely scoped API hit.

    The caller decides whether this is worth its evidence-fetch budget. This
    function never fans out into commenters, related questions, or next pages.
    """
    url = canonical_url(result.get("url", "")) if isinstance(result, dict) else ""
    output = {"url": url, "title": "", "text": ""}
    try:
        _check_cancel_event(cancel_event)
        parts = urllib.parse.urlsplit(url)
        limit = max(200, min(int(max_chars), 50000))
        config = {"_cancel_event": cancel_event}
        sections = []
        issue = re.fullmatch(r"/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]{0,9})/?", parts.path)
        question = re.match(r"/questions/([1-9][0-9]{0,11})(?:/|$)", parts.path)
        if parts.hostname == "github.com" and issue:
            owner, repo, number = issue.groups()
            target = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments?per_page=20&page=1"
            value = _public_api_json(target, _GITHUB_GATE, config, {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}, allow_list=True)
            if not isinstance(value, list):
                raise PublicFetchError("GitHub 未返回公开评论集合")
            for item in value[:20]:
                if not isinstance(item, dict) or not isinstance(item.get("body"), str) or not item["body"].strip():
                    continue
                user = item.get("user") if isinstance(item.get("user"), dict) else {}
                author = _clean(user.get("login") or "作者未提供", 100)
                reference = canonical_url(item.get("html_url", ""))
                if reference != url:
                    continue
                # Preserve the actual fragment for attribution, after checking
                # that its canonical target is this same issue.
                fragment = urllib.parse.urlsplit(item["html_url"]).fragment
                reference = reference + ("#" + fragment if re.fullmatch(r"issuecomment-\d+", fragment) else "")
                sections.append(f"公开评论 {len(sections) + 1}（作者：{author}；来源：{reference}）\n" + item["body"].strip())
            output["coverage"] = f"GitHub 评论首页，接口最多请求 20 条；提取 {len(sections)} 条有正文且属于当前 issue 的评论"
        elif parts.hostname in ("stackoverflow.com", "www.stackoverflow.com") and question:
            number = question.group(1)
            target = f"https://api.stackexchange.com/2.3/questions/{number}/answers?" + urllib.parse.urlencode({"site": "stackoverflow", "pagesize": 5, "page": 1, "order": "desc", "sort": "votes", "filter": "withbody"})
            value = _public_api_json(target, _STACKOVERFLOW_GATE, config)
            if not isinstance(value.get("items"), list):
                raise PublicFetchError("Stack Overflow 未返回公开回答集合")
            for item in value["items"][:5]:
                if not isinstance(item, dict) or _numeric(item.get("question_id")) != int(number):
                    continue
                answer_id, body = _numeric(item.get("answer_id")), _clean(item.get("body", ""), 50000)
                if not answer_id or not body:
                    continue
                owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
                author = _clean(owner.get("display_name") or "作者未提供", 100)
                accepted = "；提问者已采纳" if item.get("is_accepted") is True else ""
                sections.append(f"公开回答 {len(sections) + 1}（作者：{author}{accepted}；来源：https://stackoverflow.com/a/{answer_id}）\n" + body)
            output["coverage"] = f"Stack Overflow 按票数读取前 5 个公开回答；提取 {len(sections)} 条当前问题的回答，不含评论"
        else:
            raise PublicFetchError("仅支持已返回的 GitHub issue 或 Stack Overflow 问题公开回复；不读取 PR 或其他地址")
        _check_cancel_event(cancel_event)
        output["text"] = "\n\n".join(sections)[:limit]
    except SearchCancelled as exc:
        output.update(cancelled=True, error=str(exc))
    except PublicFetchError as exc:
        output["error"] = str(exc)
    except Exception:
        output["error"] = "公开回复格式异常，未生成推测性内容"
    return output


def _bilibili_video_text(document, url):
    """Only the matching video's metadata is evidence; recommendations are not."""
    parts = urllib.parse.urlsplit(url)
    match = re.match(r"/video/(BV[A-Za-z0-9]+|av\d+)(?:/|$)", parts.path, re.I)
    if not match or not _host_matches(parts.hostname or "", "bilibili.com"):
        return None
    requested = match.group(1)
    decoder = json.JSONDecoder()
    for assignment in re.finditer(r"(?:window\.)?__INITIAL_STATE__\s*=\s*", document):
        try:
            state, _ = decoder.raw_decode(document, assignment.end())
        except (ValueError, RecursionError):
            continue
        data = state.get("videoData") if isinstance(state, dict) else None
        if not isinstance(data, dict):
            continue
        if requested.lower().startswith("av"):
            matches = str(data.get("aid", "")) == requested[2:]
        else:
            matches = str(data.get("bvid", "")).lower() == requested.lower()
        if not matches or not isinstance(data.get("title"), str):
            continue
        title = _clean(data["title"], 500)
        description = data.get("desc") if isinstance(data.get("desc"), str) else ""
        if not description and isinstance(data.get("desc_v2"), list):
            description = "".join(item["raw_text"] for item in data["desc_v2"] if isinstance(item, dict) and isinstance(item.get("raw_text"), str))
        text = "当前视频标题：" + title
        if description.strip():
            text += "\n当前视频简介：" + _clean(description, 30000)
        links = []
        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
        mid = str(owner.get("mid", ""))
        if re.fullmatch(r"[1-9][0-9]{0,19}", mid):
            # The route is derived only from the matching video's actual owner
            # metadata. It is a navigation lead, never evidence about an answer.
            links.append({"url": "https://space.bilibili.com/" + mid,
                          "title": (_clean(owner.get("name", ""), 450) or "当前视频作者") + "的频道（导航）",
                          "platform": "bilibili", "kind": "channel"})
        return {"title": title, "text": text, "links": links, "coverage": "仅当前视频标题和简介；未读取字幕、评论或推荐视频"}
    return {"title": "", "text": "", "links": [], "error": "未取得与当前视频匹配的公开简介；已忽略推荐列表，保留搜索摘要"}


def _public_link_kind(url):
    """Classify definite public video routes, without inventing target URLs."""
    parts = urllib.parse.urlsplit(url)
    host, path = parts.hostname or "", parts.path
    if _host_matches(host, "bilibili.com"):
        if re.fullmatch(r"/video/(?:BV[A-Za-z0-9]+|av[0-9]+)/?", path, re.I) or re.fullmatch(r"/bangumi/play/(?:ep|ss)[0-9]+/?", path):
            return "video"
        if host == "space.bilibili.com" and re.match(r"/[1-9][0-9]*(?:/|$)", path):
            return "channel"
    if (_host_matches(host, "douyin.com") or _host_matches(host, "iesdouyin.com")) and re.fullmatch(r"/(?:share/)?video/[0-9]+/?", path):
        return "video"
    if _host_matches(host, "youtube.com") or _host_matches(host, "youtube-nocookie.com"):
        video_id = urllib.parse.parse_qs(parts.query).get("v", [""])[0]
        if path == "/watch" and re.fullmatch(r"[A-Za-z0-9_-]{6,64}", video_id) or re.fullmatch(r"/(?:shorts|live|embed)/[A-Za-z0-9_-]{6,64}/?", path):
            return "video"
        if re.match(r"/(?:channel/|user/|c/|@)[^/]+", path):
            return "channel"
    if host in ("youtu.be", "www.youtu.be") and re.fullmatch(r"/[A-Za-z0-9_-]{6,64}/?", path):
        return "video"
    if _host_matches(host, "vimeo.com") and re.fullmatch(r"/(?:video/)?[0-9]+/?", path):
        return "video"
    if re.search(r"\.(?:mp4|webm|ogv|mov)$", path, re.I):
        return "video"
    return "page"


def _page_node_excluded(node):
    # Visible site navigation is useful for exploration. Search-result parsing
    # excludes nav/footer, whereas actual page links may legitimately use them.
    if node.tag in ("script", "style", "form", "template", "noscript", "svg", "canvas"):
        return True
    if node.tag in ("nav", "footer"):
        return "hidden" in node.attrs or str(node.attrs.get("aria-hidden", "")).strip().lower() == "true" or bool(re.search(r"(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse)|content-visibility\s*:\s*hidden)", re.sub(r"/\*.*?\*/", "", str(node.attrs.get("style", "")), flags=re.S), re.I))
    return _search_node_excluded(node)


def _page_hidden(node):
    while node is not None:
        if _page_node_excluded(node):
            return True
        node = node.parent
    return False


def _page_login_or_challenge(parser, url):
    if _search_challenge(parser, url):
        return True
    for node in _search_nodes(parser.root):
        if node.tag == "title":
            title = _search_text(node, 500).lower()
            if any(term in title for term in ("captcha", "just a moment", "人机验证", "安全验证", "访问验证", "登录 - ")) or re.match(r"^(?:登录|登入|sign in|log in)(?:\s|[-|—]|$)", title):
                return True
        if node.tag == "input" and str(node.attrs.get("type", "")).lower() == "password":
            # A visible credential form is not a public evidence page. Hidden
            # login overlays/templates alone do not invalidate a public article.
            ancestor = node
            concealed = False
            while ancestor is not None:
                if ancestor.tag in ("script", "style", "template", "noscript") or "hidden" in ancestor.attrs or str(ancestor.attrs.get("aria-hidden", "")).lower() == "true" or re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", ancestor.attrs.get("style", ""), re.I):
                    concealed = True
                    break
                ancestor = ancestor.parent
            if not concealed:
                return True
    return False


def _page_links(parser, base_url, allowed_domains=None):
    links, seen = [], {canonical_url(base_url)}
    for node in _search_nodes(parser.root):
        if node.tag == "base" and "href" in node.attrs and not _page_hidden(node):
            declared = canonical_url(urllib.parse.urljoin(base_url, node.attrs.get("href") or ""))
            if declared:
                base_url = declared
            break
    for node in _search_nodes(parser.root):
        if node.tag not in ("a", "video", "source", "iframe") or _page_hidden(node):
            continue
        raw = node.attrs.get("href" if node.tag == "a" else "src", "")
        if not isinstance(raw, str) or not raw.strip() or raw.strip().startswith("#") or "download" in node.attrs:
            continue
        url = canonical_url(urllib.parse.urljoin(base_url, raw.strip()))
        if not url or url in seen:
            continue
        host, path = urllib.parse.urlsplit(url).hostname or "", urllib.parse.urlsplit(url).path
        if allowed_domains is not None and not any(_host_matches(host, domain) for domain in allowed_domains):
            continue
        if re.search(r"/(?:login|signin|signup|register|logout|captcha|antispider|showcaptcha)(?:/|$)", path, re.I):
            continue
        kind = _public_link_kind(url)
        if node.tag != "a" and kind != "video":
            continue
        title = _search_text(node, 500) or str(node.attrs.get("title") or node.attrs.get("aria-label") or "").strip()[:500]
        if not title:
            for child in _search_nodes(node):
                if child.tag == "img" and not _page_hidden(child) and child.attrs.get("alt"):
                    title = str(child.attrs["alt"]).strip()[:500]
                    break
        if not title and kind == "video":
            title = "页面中的视频链接"
        if not title:
            continue
        seen.add(url)
        links.append({"url": url, "title": title, "platform": platform_of(url), "kind": kind})
        if len(links) >= 40:
            break
    return links


def _page_visible_text(parser, limit):
    pieces, length = [], 0
    pending = [parser.root]
    while pending and length < limit:
        node = pending.pop()
        if isinstance(node, str):
            pieces.append(node[:limit - length])
            length += len(pieces[-1])
        elif node.tag not in ("head", "title", "svg", "canvas", "iframe") and not _search_node_excluded(node):
            if node.tag in _PlainText.BLOCKS:
                pieces.append("\n")
                pending.append("\n")
            pending.extend(reversed(node.children))
    return "\n".join(line for line in (re.sub(r"\s+", " ", value).strip() for value in "".join(pieces).splitlines()) if line)


def fetch_public_page(url: str, max_chars=12000, cancel_event=None, allowed_domains=None) -> dict:
    canonical = canonical_url(url)
    result = {"url": canonical, "title": "", "text": "", "links": []}
    try:
        _check_cancel_event(cancel_event)
        allowed_domains = _page_domains(allowed_domains)

        def guard(target):
            _check_cancel_event(cancel_event)
            _require_page_scope(target, allowed_domains)
            _check_robots(target, cancel_event=cancel_event, allowed_domains=allowed_domains)

        guard(canonical)
        payload, headers, final_url, _ = _request(canonical, headers={"Accept": "text/html,text/plain;q=0.9"},
                                                redirect_guard=guard, cancel_event=cancel_event)
        _check_cancel_event(cancel_event)
        _require_page_scope(final_url, allowed_domains)
        result["url"] = final_url
        content_type = next((str(v).lower() for k, v in headers.items() if k.lower() == "content-type"), "")
        if not any(kind in content_type for kind in ("text/html", "application/xhtml+xml", "text/plain")):
            raise PublicFetchError("来源不是可提取正文的 HTML 或文本页面")
        document = _decode(payload, headers)
        max_chars = max(200, min(int(max_chars), 50000))
        tree = None
        if "text/plain" not in content_type:
            tree = _SearchHTML()
            tree.feed(document)
            if _page_login_or_challenge(tree, final_url):
                raise PublicFetchError("来源显示登录或人机验证页面，未绕过")
        video = _bilibili_video_text(document, final_url)
        if video is not None:
            result.update(video)
            result["text"] = result["text"][:max_chars]
            result["links"] = [link for link in result["links"] if allowed_domains is None or any(_host_matches(urllib.parse.urlsplit(link["url"]).hostname or "", domain) for domain in allowed_domains)]
            _check_cancel_event(cancel_event)
            return result
        parser = _PlainText()
        parser.feed(document)
        title = re.sub(r"\s+", " ", "".join(parser.title_parts)).strip()
        text = _page_visible_text(tree, MAX_BYTES) if tree is not None else document
        if any(term in title.lower() for term in ("captcha", "just a moment", "人机验证", "安全验证", "访问验证", "登录 - ")):
            raise PublicFetchError("来源显示登录或人机验证页面，未绕过")
        links = _page_links(tree, final_url, allowed_domains) if tree is not None else []
        _check_cancel_event(cancel_event)
        result.update(title=title[:500], text=text[:max_chars], links=links)
        if len(text.strip()) < 60:
            result["error"] = "页面正文不足，可能依赖 JavaScript、登录或访问权限；保留搜索摘要"
    except SearchCancelled as exc:
        result.update(cancelled=True, error=str(exc), title="", text="", links=[])
    except PublicFetchError as exc:
        result["error"] = str(exc)
    except Exception:
        result["error"] = "页面正文解析失败"
    return result
