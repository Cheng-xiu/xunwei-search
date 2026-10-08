"""Observe real tool results, let AI choose the next bounded public operation."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import re
from urllib.parse import urlsplit

from . import actions, providers, research_tools
from .ai import AIError
from .retrieval import depth_profile, effective_search_concurrency, parallel_calls, interruptible_call, request_slot, is_excluded
from .summarizer import summarize_results, _cleaner
from .progress_report import build_progress_report


def run_agentic_search(job, config, storage, update, stop_event):
    from .engine import fallback_plan, clean_result, has_query_signal, relevance, assess

    query = job['query']
    profile = depth_profile(job.get('depth', 'quick'))
    concurrency = effective_search_concurrency(job, config)
    config = {**config, '_cancel_event': stop_event, '_search_depth': job.get('depth', 'quick')}
    if re.search(r'\bissue\b|\bbug\b|\berror\b|不触发|报错|故障', query, re.I):
        config['_github_search_kind'] = 'issues'
    clean = _cleaner(config)
    plan = copy.deepcopy(job.get('plan')) if isinstance(job.get('plan'), dict) else fallback_plan(query)
    if not plan.get('agentic'):
        plan.update(queries=[], ai_used=False)
    plan.update(agentic=True, must_have=list(dict.fromkeys([query] + plan.get('must_have', []))))
    rounds = copy.deepcopy(job.get('rounds', []))
    statuses = copy.deepcopy(job.get('provider_status', []))[-200:]
    warnings = list(job.get('warnings', []))[-30:]
    summary = copy.deepcopy(job.get('ai_summary')) or {'state': 'empty', 'points': [], 'limitations': [], 'source_count': 0, 'considered_count': 0}
    progress_report = copy.deepcopy(job.get('progress_report'))
    number = max(int(job.get('round', 0)), max((int(record.get('number', 0)) for record in rounds), default=0))
    searches_count = int(job.get('searches_count', 0))
    round_limit = max(0, min(12, int(job.get('max_rounds', 3))))
    waves_limit = {'quick': 3, 'deep': 4, 'research': 6}.get(job.get('depth'), 3)
    available = providers.available_providers(config)
    if any(site.get('search_url') for site in job.get('custom_sites', [])):
        available = list(available) + ['website']
    completed, attempted, pending = set(), set(), []
    for record in rounds:
        for old in record.get('queries', []):
            if not isinstance(old, dict) or not old.get('action'):
                continue
            if old.get('status') == 'completed' and old.get('ok'):
                completed.add(actions.action_key(old))
            elif old.get('status') in ('queued', 'running', 'cancelled'):
                pending.append(old)
    attempted.update(completed)
    unique = {row.get('url') or 'local:' + str(row.get('id')): copy.deepcopy(row) for row in job.get('results', [])
              if isinstance(row, dict) and row.get('id') and (row.get('content_level') == 'local' or actions.url_allowed(row.get('url'), job))}
    leads = {lead['id']: copy.deepcopy(lead) for lead in job.get('navigation_leads', [])
             if isinstance(lead, dict) and isinstance(lead.get('id'), str) and actions.url_allowed(lead.get('url'), job)}
    assessed = {str(row['id']) for row in unique.values() if row.get('evidence')}
    observation_index = max((lead.get('observation_index', 0) for lead in leads.values()), default=0)
    protected_leads = set()
    started_at = job.get('started_at') or datetime.now(timezone.utc).isoformat()

    def warn(message):
        message = clean(str(message), 400)
        if message and message not in warnings:
            warnings.append(message)
            del warnings[:-30]

    def visible():
        return sorted((row for row in unique.values() if not is_excluded(row)), key=lambda row: (
            {'strong': 2, 'partial': 1}.get(row.get('match'), 0), row.get('score', 0), relevance(query, row)), reverse=True)[:240]

    def publish(**fields):
        update(results=copy.deepcopy(visible()), navigation_leads=copy.deepcopy(list(leads.values())), rounds=copy.deepcopy(rounds),
               provider_status=copy.deepcopy(statuses[-200:]), warnings=list(warnings), ai_summary=copy.deepcopy(summary),
               progress_report=copy.deepcopy(progress_report), plan=copy.deepcopy(plan), round=number,
               searches_count=searches_count, search_concurrency=concurrency, started_at=started_at,
               summary=f'已探索 {number} 轮，保留 {len(visible())} 条候选和 {len(leads)} 个导航入口；入口仍需核验。', **fields)

    def stop():
        if not stop_event.is_set():
            return False
        if rounds and rounds[-1].get('state') == 'running':
            rounds[-1]['state'] = 'stopped'
            for task in rounds[-1].get('queries', []):
                if task.get('status') in ('queued', 'running'):
                    task['status'] = 'cancelled'
        if rounds and (rounds[-1].get('report') or {}).get('state') == 'running':
            rounds[-1]['report'].update(state='stopped', message='本轮进展报告已停止，已有线索保留。')
        if progress_report and progress_report.get('state') == 'running':
            progress_report.update(state='stopped', message='本轮进展报告已停止，已有线索保留。')
        publish(state='stopped', stage='stopped', progress=100, stop_reason='user',
                message='已停止安排新动作，已有线索和结果保留；在途响应不会写入。')
        return True

    def finish(reason, message):
        publish(state='awaiting_user', stage='waiting', progress=100, stop_reason=reason, message=message,
                completed_at=datetime.now(timezone.utc).isoformat())

    def register(raw, task, text=None, inspected=False):
        nonlocal observation_index
        if not isinstance(raw, dict):
            return None, False
        url = providers.canonical_url(raw.get('url', ''))
        if not url or not actions.url_allowed(url, job):
            return None, False
        lead_id = 'lead-' + hashlib.sha256(url.encode()).hexdigest()[:16]
        previous = leads.get(lead_id)
        if previous is None and len(leads) >= 120:
            # Keep room for a newly observed child link. The active batch's
            # targets must survive until all its responses have been consumed.
            victim = next((key for key in leads if key not in protected_leads and key not in {item.get('lead_id') for item in pending}), None)
            if victim is None:
                return None, False
            del leads[victim]
        observation_index += 1
        lead = previous or {'id': lead_id, 'url': url, 'title': clean(str(raw.get('title') or urlsplit(url).hostname), 500),
                            'platform': providers.platform_of(url), 'kind': raw.get('kind') or providers._public_link_kind(url),
                            'snippet': clean(str(raw.get('snippet', '')), 1000), 'from_action': task.get('id', ''),
                            'round': number, 'inspected': False}
        lead['observation_index'] = observation_index
        lead['last_observed_round'] = number
        for key in ('source', 'content_kind', 'views', 'published', 'body'):
            if raw.get(key) is not None and key not in lead:
                lead[key] = copy.deepcopy(raw[key])
        if text is not None:
            lead.update(text=clean(str(text), 12000), inspected=inspected)
            if raw.get('title'):
                lead['title'] = clean(str(raw['title']), 500)
        if inspected:
            lead['inspected'] = True
        if previous and len(str(raw.get('snippet', ''))) > len(lead.get('snippet', '')):
            lead['snippet'] = clean(str(raw['snippet']), 1000)
        leads[lead_id] = lead
        return lead, previous is None

    def ingest(raw, task):
        if not isinstance(raw, dict):
            return False
        raw_url = providers.canonical_url(raw.get('url', ''))
        parts = urlsplit(raw_url)
        navigation = raw.get('kind') in ('website', 'channel') or providers._public_link_kind(raw_url) == 'channel'
        navigation = navigation or (raw.get('source') != 'local' and parts.path in ('', '/') and not parts.query)
        if navigation:
            return False
        row = clean_result(raw, query)
        if row is None or (row.get('content_level') != 'local' and not actions.url_allowed(row.get('url'), job)):
            return False
        if not has_query_signal(query, row, plan, task.get('query')):
            return False
        key = row.get('url') or 'local:' + row['id']
        previous = unique.get(key)
        if previous:
            changed = False
            for field in ('body', 'snippet'):
                if len(row.get(field, '')) > len(previous.get(field, '')):
                    previous[field] = row[field]
                    changed = True
            if not changed:
                return False
            if row.get('content_level') == 'page':
                previous['content_level'] = 'page'
            previous.pop('fetch_error', None)
            previous.update(evidence=[], match='unverified', reason='取得补充内容，等待按原问题重新核验。')
            assessed.discard(str(previous['id']))
            record['updated_results'] += 1
        else:
            row['discovery_scopes'] = [task.get('platform', row.get('platform'))]
            unique[key] = row
        if summary.get('state') == 'ready':
            summary['stale'] = True
        return previous is None

    for row in list(unique.values()):
        register(row, {'id': 'saved-result'}, inspected=row.get('content_level') == 'page')
    if stop():
        return
    publish(state='running', stage='planning', progress=3, message='AI 将先定位入口，再依据实际观察选择搜索、站内检索或读取动作。',
            native_links=providers.native_search_links(query, job.get('platforms', [])) + providers.custom_search_links(query, job.get('custom_sites', [])))
    segment, stagnant = 0, 0
    while not stop():
        number += 1
        segment += 1
        before_ids, before_leads = {row['id'] for row in visible()}, set(leads)
        record = {'number': number, 'planner': 'ai_actions', 'reason': '', 'ai_rationale': '', 'queries': [], 'decisions': [],
                  'new_results': 0, 'updated_results': 0, 'new_leads': 0, 'total_results': len(before_ids), 'state': 'running'}
        rounds.append(record)
        remaining, pages_remaining = profile['requests'], profile['pages']
        current_statuses = []
        if segment == 1:
            for row in storage.search_documents(query, limit=30):
                if stop():
                    return
                same_site = any((urlsplit(row.get('url', '')).hostname or '') == site['domain'] or
                                (urlsplit(row.get('url', '')).hostname or '').endswith('.' + site['domain'])
                                for site in job.get('custom_sites', []))
                if 'web' in job.get('platforms', []) or row.get('platform') in job.get('platforms', []) or row.get('platform') == 'local' or same_site:
                    ingest(row, {'query': query, 'platform': row.get('platform', 'local')})
        for wave in range(waves_limit):
            if stop():
                return
            if remaining <= 0:
                break
            context_job = {**job, 'ai_summary': summary}
            context = actions.build_context(context_job, plan, list(leads.values()), visible(), rounds, statuses, available,
                                            {'actions': remaining, 'page_reads': pages_remaining, 'decision_steps': waves_limit - wave})
            publish(state='running', stage='adapting' if record['queries'] else 'planning', progress=min(65, 6 + wave * 15),
                    message=f'第 {number} 轮：AI 根据已发现的网站、链接和未满足条件决定下一步工具。')
            decision = {}
            if pending:
                raw_plan = {'reason': '先继续上次中断的动作，再根据新观察调整。', 'actions': pending}
                pending = []
                outcome = 'ok'
            else:
                def decide():
                    try:
                        return {'response': actions.plan_actions(config, context), 'error': ''}
                    except AIError as error:
                        # AIError contains this client's safe diagnostics only.
                        message = clean(str(error), 400).replace('本轮使用关键词检索结果。', '已暂停等待重试。')
                        return {'response': None, 'error': message}
                    except Exception:
                        return {'response': None, 'error': 'AI 规划响应未完成或格式不兼容。'}
                outcome, decision = interruptible_call(decide, stop_event)
                raw_plan = decision.get('response') if outcome == 'ok' and isinstance(decision, dict) else None
            wave_planner = 'ai_actions'
            if stop():
                return
            if outcome != 'ok' or not isinstance(raw_plan, dict):
                diagnostic = decision.get('error', '') if isinstance(decision, dict) else ''
                warn(diagnostic or 'AI 动作规划暂不可用，已暂停；请检查模型连接或稍后继续。')
                record.update(state='error', reason='AI 动作规划未完成，未安排任何未经 AI 决定的新操作。')
                progress_report = {'state': 'error', 'round': number, 'progress': '本轮 AI 规划未完成，已取得的结果与导航入口保留。',
                                   'stats': {'new_results': record['new_results'], 'updated_results': record['updated_results'],
                                             'total_results': len(visible()), 'requests': len(current_statuses),
                                             'failed_requests': sum(not item.get('ok') for item in current_statuses)},
                                   'findings': [], 'assessment': {'likelihood': 'unknown', 'reason': '模型服务未完成规划，尚无法评估。',
                                                               'blockers': ['AI 服务暂不可用'], 'next_steps': ['检查模型连接后继续。']},
                                   'message': diagnostic or 'AI 规划失败；程序未自动改用固定关键词检索。'}
                record['report'] = copy.deepcopy(progress_report)
                finish('ai_unavailable', 'AI 暂未完成动作规划，已暂停等待；可以检查模型连接后继续。')
                return
            proposals, rejected = actions.normalize_actions(raw_plan, query, job, available, list(leads.values()), attempted,
                                                            max_actions=min(4, remaining), allow_read=job.get('fetch_pages', True) and pages_remaining > 0)
            reason = clean(str(raw_plan.get('reason') or '根据最新观察选择下一步。'), 500)
            if wave_planner == 'fallback' and proposals:
                reason = '备用路径：' + reason
            prior_planners = {task.get('planner') for task in record['queries']}
            record['planner'] = 'mixed' if prior_planners and prior_planners != {wave_planner} else wave_planner
            record['reason'] = (record['reason'] + ('\n' if record['reason'] else '') + reason)[-2000:]
            if wave_planner == 'ai_actions':
                record['ai_rationale'] = (record['ai_rationale'] + ('\n' if record['ai_rationale'] else '') + reason)[-2000:]
            if rejected:
                record.setdefault('rejected_actions', []).extend(rejected[:8])
            if not proposals:
                if rejected:
                    publish(state='running', stage='adapting', progress=min(65, 8 + wave * 15), message='本次动作超出范围或重复，已将具体校验原因交给 AI 调整。')
                    continue
                break
            batch = []
            plan['ai_used'] = True
            for proposal in proposals:
                reading = proposal['action'] in ('inspect', 'read_replies')
                if reading and pages_remaining <= 0:
                    continue
                if reading:
                    pages_remaining -= 1
                remaining -= 1
                task = {**proposal, 'id': f'r{number}-a{len(record["queries"]) + 1}', 'status': 'queued', 'wave': wave + 1,
                        'planner': wave_planner}
                if proposal['action'] in ('search', 'search_site', 'search_videos') and not any(item.get('query') == proposal['query'] for item in plan.get('queries', [])):
                    plan.setdefault('queries', []).append({'query': proposal['query'], 'reason': proposal.get('purpose', ''),
                                                           'action': proposal['action'], 'platform': proposal['platform']})
                record['queries'].append(task)
                attempted.add(actions.action_key(task))
                batch.append(task)
            if not batch:
                break
            record['decisions'].append({'step': wave + 1, 'reason': reason, 'action_ids': [task['id'] for task in batch]})
            publish(state='running', stage='searching', progress=min(69, 12 + wave * 15),
                    message=f'第 {number} 轮：执行 AI 选择的 {len(batch)} 个动作，随后观察实际结果再决定。')
            source_snapshots = {task['lead_id']: copy.deepcopy(leads[task['lead_id']]) for task in batch if task.get('lead_id') in leads}
            protected_leads = set(source_snapshots)

            def execute(task):
                with request_slot(config, stop_event):
                    kind = task['action']
                    if kind == 'inspect':
                        return research_tools.inspect_page(task['target_url'], 12000, cancel_event=stop_event, allowed_domains=actions.scope_domains(job))
                    if kind == 'read_replies':
                        # Full source metadata stays in our registry, never in model arguments.
                        return providers.fetch_result_details(source_snapshots[task['lead_id']], 12000, cancel_event=stop_event)
                    if kind == 'search_site':
                        return research_tools.search_site(task['domain'], task['query'], task['provider'], profile['limit'], config)
                    if task['platform'].startswith('website:'):
                        site = next(site for site in job.get('custom_sites', []) if 'website:' + site['domain'] == task['platform'])
                        return providers.search_custom_site(site, task['query'], profile['limit'], {**config, '_engine': task['provider']})
                    response = providers.search_provider(task['provider'], task['query'], [task['platform']], profile['limit'], config)
                    if kind == 'search_videos' and isinstance(response, dict):
                        response = {**response, 'results': [row for row in response.get('results', []) if isinstance(row, dict)
                                                          and providers._public_link_kind(row.get('url', '')) == 'video']}
                    return response

            def schedule(task):
                task['status'] = 'running'

            for task, outcome, response in parallel_calls(batch, execute, concurrency, stop_event, schedule):
                if stop():
                    return
                old_lead_ids, old_results = set(leads), len(unique)
                reading = task['action'] in ('inspect', 'read_replies')
                status = {'provider': task['provider'], 'ok': False, 'action': task['action'], 'platform': task['platform'],
                          'query': task.get('query', ''), 'round': number, 'action_id': task['id']}
                if outcome == 'ok' and isinstance(response, dict):
                    if reading:
                        if response.get('url') and not actions.url_allowed(response['url'], job):
                            response = {'error': '读取工具返回超出所选范围的目标，未使用正文或链接。', 'links': []}
                        source = leads[task['lead_id']]
                        source['inspected'] = True
                        source['coverage'] = clean(str(response.get('coverage', '仅取得该入口可公开访问的页面文字；未观看视频。')), 400)
                        source['error'] = clean(str(response.get('error', '')), 400)
                        source['text'] = clean(str(response.get('text', '')), 12000)
                        # Short menus can still provide valid navigation links;
                        # an error response supplies no answer evidence.
                        for link in response.get('links', [])[:40] if isinstance(response.get('links'), list) else []:
                            register(link, task)
                        status['ok'] = not bool(response.get('error'))
                        if source['error']:
                            status['error'] = source['error']
                            status['partial'] = bool(response.get('links'))
                        if response.get('text') and not response.get('error') and actions.url_allowed(response.get('url') or source['url'], job):
                            result_source = {**source, 'url': response.get('url') or source['url'], 'title': response.get('title') or source['title'],
                                             'body': response['text'], 'content_level': 'page', 'source': source.get('source') or 'website'}
                            if task['action'] == 'read_replies':
                                result_source['body'] = source.get('body', '') + '\n\n' + response['text']
                            register(result_source, task, text=response['text'], inspected=True)
                            ingest(result_source, task)
                    else:
                        status.update(response.get('status') or {})
                        for row in response.get('results', [])[:profile['limit'] * 2]:
                            register(row, task)
                            ingest(row, task)
                else:
                    status['error'] = '工具请求未成功返回，未生成推测性结果。'
                status.update(action=task['action'], action_id=task['id'], round=number, platform=task['platform'], query=task.get('query', ''))
                task.update(status='completed', ok=bool(status.get('ok')), discovered_count=len(set(leads) - old_lead_ids),
                            result_count=max(0, len(unique) - old_results))
                if status.get('error'):
                    task['error'] = clean(str(status['error']), 400)
                if task['ok']:
                    completed.add(actions.action_key(task))
                statuses.append(copy.deepcopy(status))
                del statuses[:-200]
                current_statuses.append(copy.deepcopy(status))
                searches_count += 1
                record.update(new_results=len({row['id'] for row in visible()} - before_ids), new_leads=len(set(leads) - before_leads), total_results=len(visible()))
                publish(state='running', stage='searching', progress=min(72, 20 + wave * 15),
                        message=f'已执行 {len(record["queries"])} 个动作，发现 {record["new_leads"]} 个入口；AI 将根据真实返回内容继续选择。')
            if stop():
                return
            protected_leads = set()
        candidates = [copy.deepcopy(row) for row in visible() if str(row['id']) not in assessed][:profile['candidates']]
        if candidates:
            publish(state='running', stage='evaluating', progress=77, message='只核验取得的候选内容；网站入口和频道链接不冒充答案。')
            assessment_warnings = []
            outcome, count = interruptible_call(lambda: assess(query, candidates, copy.deepcopy(plan), config, assessment_warnings.append), stop_event)
            if stop():
                return
            for message in assessment_warnings:
                warn(message)
            if outcome == 'ok':
                for row in candidates[:count] if isinstance(count, int) and count > 0 else candidates:
                    if row.get('evidence'):
                        unique[row.get('url') or 'local:' + row['id']] = row
                        assessed.add(str(row['id']))
            else:
                warn('AI 核验暂不可用，已有候选保留为未核实。')
        record.update(new_results=len({row['id'] for row in visible()} - before_ids), new_leads=len(set(leads) - before_leads),
                      total_results=len(visible()))
        if visible() and (record['new_results'] or record['updated_results'] or summary.get('state') != 'ready'):
            publish(state='running', stage='summarizing', progress=86, message='仅根据取得的候选证据生成带来源的总结。')
            outcome, new_summary = interruptible_call(lambda: summarize_results(query, copy.deepcopy(visible()), config, copy.deepcopy(plan), list(warnings)), stop_event)
            if stop():
                return
            if outcome == 'ok' and isinstance(new_summary, dict) and new_summary.get('state') == 'ready':
                summary = {**new_summary, 'stale': False, 'round': number}
            else:
                warn('本轮总结暂未更新，已有证据和上一版总结保留。')
        record['state'] = 'completed'
        record['report'] = {'state': 'running', 'round': number, 'findings': [], 'assessment': {'likelihood': 'unknown'}, 'message': '正在评估本轮进展。'}
        publish(state='running', stage='reporting', progress=94, message='评估本轮实际动作、已有证据和继续找到答案的可能性。')
        outcome, report = interruptible_call(lambda: build_progress_report(query, copy.deepcopy(visible()), {**config, 'use_ai': True},
                                                                         copy.deepcopy(plan), copy.deepcopy(record), copy.deepcopy(current_statuses),
                                                                         copy.deepcopy(progress_report)), stop_event)
        if stop():
            return
        if outcome == 'ok' and isinstance(report, dict):
            record['report'] = progress_report = report
        else:
            record['report'].update(state='error', message='AI 进展评估暂不可用，实际动作记录已保留。')
            progress_report = copy.deepcopy(record['report'])
        publish(state='running', stage='adapting', progress=97, message=f'第 {number} 轮完成；入口与核验结果分别保留。')
        if not record['queries']:
            finish('exhausted', 'AI 和备用路径目前没有可执行的新动作；等待你继续或调整范围。')
            return
        if current_statuses and not any(status.get('ok') or status.get('partial') for status in current_statuses):
            finish('sources_unavailable', '本轮工具均未取得可用内容；等待你调整来源或继续。')
            return
        stagnant = stagnant + 1 if not (record['new_results'] or record['updated_results'] or record['new_leads']) else 0
        if stagnant >= 2:
            finish('no_new_results', '连续两轮没有新证据或导航入口；已保留线索，等待你决定。')
            return
        if round_limit and segment >= round_limit:
            finish('round_limit', '已完成设定的轮数；你可以根据动作记录继续搜索或停止。')
            return
