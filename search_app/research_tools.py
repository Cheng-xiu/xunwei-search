"""Bounded public research operations; provenance and budgets belong to caller.

The orchestrator must authorize each known lead/domain, enforce platform scope,
and hold its request slot. These operations never recursively acquire slots.
"""
from __future__ import annotations

import urllib.parse

from . import providers


def inspect_page(url, max_chars=12000, cancel_event=None, allowed_domains=None):
    """Read one actual public page and at most 40 links found in that response."""
    return providers.fetch_public_page(url, max_chars=max_chars,
                                       cancel_event=cancel_event, allowed_domains=allowed_domains)


def search_site(domain, query, provider, limit, config, video_only=False):
    """Search a discovered exact hostname through an enabled generic engine."""
    config = config if isinstance(config, dict) else {}
    status = {"provider": "website", "engine": provider if isinstance(provider, str) else "",
              "ok": False, "count": 0}
    try:
        providers._check_cancel_event(config.get("_cancel_event"))
        if provider not in providers.SEARCH_ENGINE_IDS or provider not in providers.available_providers(config):
            raise providers.PublicFetchError("站内检索必须使用已选且已配置的通用搜索引擎")
        domains = providers._page_domains([domain])
        domain = domains[0]
        site = {"name": domain, "domain": domain}
        response = providers.search_custom_site(site, query, limit, {**config, "_engine": provider})
        providers._check_cancel_event(config.get("_cancel_event"))
        # Defensive filtering also applies when an upstream engine ignores site:.
        rows = []
        seen = set()
        for row in response.get("results", []):
            url = providers.canonical_url(row.get("url", ""))
            if not url or url in seen or not providers._host_matches(urllib.parse.urlsplit(url).hostname or "", domain):
                continue
            kind = providers._public_link_kind(url)
            if video_only and kind != "video":
                continue
            seen.add(url)
            rows.append({**row, "url": url, "kind": kind})
        status.update(response.get("status", {}), provider="website", engine=provider, domain=domain, count=len(rows))
        if video_only:
            status["coverage"] = "指定域名的公开索引；仅保留可识别的公开视频路径，不代表完整站内视频库"
            if not rows and status.get("ok"):
                status["message"] = "本轮未返回可核实的公开视频路径"
        return {"results": rows, "status": status}
    except providers.SearchCancelled as exc:
        status.update(cancelled=True, error=str(exc))
    except (providers.PublicFetchError, ValueError) as exc:
        status["error"] = str(exc)
    except Exception:
        status["error"] = "站内检索结果解析失败，未生成推测性链接"
    return {"results": [], "status": status}
