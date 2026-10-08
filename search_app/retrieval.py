"""Search budgets and exact query signals, independent of any search backend."""
from __future__ import annotations

import re

DIRECT_PROVIDERS = {'bilibili', 'github', 'stackoverflow'}
DEPTH_PROFILES = {
    'quick': {'requests': 20, 'pages': 3, 'candidates': 16, 'limit': 6, 'variants': 2},
    'deep': {'requests': 32, 'pages': 6, 'candidates': 24, 'limit': 12, 'variants': 4},
    'research': {'requests': 48, 'pages': 10, 'candidates': 24, 'limit': 20, 'variants': 6},
}
_ALIASES = [('中科大', '中国科大', '中国科学技术大学', 'USTC', 'University of Science and Technology of China'), ('桃李苑', '桃李园')]
_COMMON = {'推荐', '附近', '餐馆', '大学', '好吃', '搜索', '原文', '合肥', '中国', '相关', '问题',
           'gpio', 'isr', 'idle', 'after', 'before', 'with', 'what', 'where', 'which', 'the', 'and', 'for'}


def depth_profile(depth):
    return DEPTH_PROFILES.get(depth, DEPTH_PROFILES['quick'])


def query_anchors(query, plan=None):
    """A model may select literal query terms, never invent required entities.

    Matching one anchor allows discovery of incomplete leads. Final evidence
    assessment still checks every condition. Known spelling aliases are grouped.
    """
    terms = []
    for term in (plan or {}).get('retrieval_terms', [])[:8]:
        if not isinstance(term, str):
            continue
        term = term.strip()
        if 2 <= len(term) <= 80 and term.casefold() in query.casefold() and term.casefold() not in _COMMON:
            terms.append(term)
    # Distinct identifiers survive even when the AI planner is unavailable.
    terms.extend(re.findall(r'(?<![\w])[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)+(?![\w])', query))
    for aliases in _ALIASES:
        if any(alias.casefold() in query.casefold() for alias in aliases):
            terms.append(next(alias for alias in aliases if alias.casefold() in query.casefold()))
    groups = []
    for term in dict.fromkeys(terms):
        group = next((list(aliases) for aliases in _ALIASES if term in aliases), [term])
        if group not in groups:
            groups.append(group)
    return groups


def anchor_coverage(query, result, plan=None):
    groups = query_anchors(query, plan)
    if not groups:
        return None
    text = ' '.join(str(result.get(field, '')) for field in ('title', 'snippet', 'body')).casefold()
    return sum(any(term.casefold() in text for term in group) for group in groups) / len(groups)


def is_excluded(result):
    return result.get('match') == 'excluded' or any(
        isinstance(item, dict) and item.get('status') == 'contradicted' and item.get('quote')
        for item in result.get('evidence', []))
