import copy
import hashlib
import json
import threading
import time
import unittest
from unittest.mock import patch

from search_app import adaptive


class Library:
    def search_documents(self, *args, **kwargs):
        return []


class AdaptiveTests(unittest.TestCase):
    def job(self, **changes):
        value = {'query': '中科大桃李苑香菇滑鸡', 'platforms': ['zhihu', 'xiaohongshu'], 'custom_sites': [],
                 'use_ai': False, 'fetch_pages': False, 'depth': 'quick', 'max_rounds': 2,
                 'round': 0, 'rounds': [], 'results': [], 'provider_status': [], 'warnings': []}
        value.update(changes)
        return value

    def result(self, platform, query, title='中科大桃李苑香菇滑鸡口味记录'):
        host = {'zhihu': 'www.zhihu.com', 'xiaohongshu': 'www.xiaohongshu.com', 'web': 'example.com'}[platform]
        slug = hashlib.sha256(query.encode()).hexdigest()[:12]
        return {'title': title, 'url': f'https://{host}/post/{slug}', 'snippet': '中科大桃李苑的香菇滑鸡很好吃，鸡肉鲜嫩。',
                'platform': platform, 'source': 'bing'}

    def run_search(self, job, search, stop=None, on_update=None, ai_planner=None, assessment=None, summary=None,
                   plan_override=None, available=None, library=None):
        state = copy.deepcopy(job)
        updates = []
        stop = stop or threading.Event()
        def update(**fields):
            state.update(copy.deepcopy(fields))
            updates.append(copy.deepcopy(fields))
            if on_update:
                on_update(state)
        initial_plan = {'intent': job['query'], 'queries': [{'query': job['query'], 'reason': '原始问题'}], 'must_have': [job['query']], 'uncertain': [], 'ai_used': False}
        if plan_override is not None:
            initial_plan = plan_override
        with patch.object(adaptive.providers, 'available_providers', return_value=available or ['bing']), \
             patch.object(adaptive.providers, 'search_provider', side_effect=search), \
             patch('search_app.engine.make_plan', return_value=initial_plan), \
             patch('search_app.engine.assess', side_effect=assessment or (lambda *args: 0)), \
             patch.object(adaptive, 'chat', side_effect=ai_planner or (lambda *args, **kwargs: '{"directions":[]}')), \
             patch.object(adaptive, 'summarize_results', side_effect=summary or (lambda *args: {'state': 'ready', 'points': [], 'source_count': 0, 'considered_count': len(args[1]), 'limitations': []})):
            adaptive.run_adaptive_search(job, {}, library or Library(), update, stop)
        return state, updates

    def test_first_round_covers_all_selected_scopes_then_focuses_relevant_platform(self):
        def search(provider, query, scope, limit, config):
            rows = [self.result(scope[0], query)] if scope[0] == 'zhihu' else []
            return {'results': rows, 'status': {'provider': provider, 'ok': True, 'count': len(rows)}}
        state, _ = self.run_search(self.job(), search)
        first, second = state['rounds']
        self.assertEqual({task['platform'] for task in first['queries']}, {'zhihu', 'xiaohongshu'})
        hot = sum(task['platform'] == 'zhihu' for task in second['queries'])
        cold = sum(task['platform'] == 'xiaohongshu' for task in second['queries'])
        self.assertGreater(hot, cold)
        self.assertTrue(any('口味记录' in task['query'] for task in second['queries']))
        self.assertEqual(state['state'], 'awaiting_user')
        self.assertEqual(state['stop_reason'], 'round_limit')
        self.assertTrue(all(len(record['queries']) <= 20 for record in state['rounds']))

    def test_ai_adaptation_receives_actual_evidence_and_cannot_expand_scope(self):
        calls = []
        def search(provider, query, scope, limit, config):
            calls.append((provider, query, scope[0]))
            return {'results': [self.result(scope[0], query)], 'status': {'provider': provider, 'ok': True, 'count': 1}}
        payloads = []
        def ai_plan(config, system, user, **kwargs):
            payloads.append(json.loads(user))
            return json.dumps({'reason': '已找到实际口味记录，追问制作细节', 'stop': True, 'directions': [
                {'platform': 'zhihu', 'query': '香菇滑鸡制作细节'},
                {'platform': 'wechat', 'query': '违规扩大平台'},
                {'platform': 'zhihu', 'query': 'site:unselected.example 任意关键词'},
            ]}, ensure_ascii=False)
        state, _ = self.run_search(self.job(use_ai=True), search, ai_planner=ai_plan)
        self.assertEqual(state['round'], 2)
        self.assertEqual(len(payloads), 1)
        self.assertTrue(payloads[0]['evidence'])
        self.assertIn('口味记录', payloads[0]['evidence'][0]['title'])
        self.assertIn(self.job()['query'], payloads[0]['required_conditions'])
        self.assertTrue(all(scope in ('zhihu', 'xiaohongshu') for _, _, scope in calls))
        self.assertIn(self.job()['query'], state['plan']['must_have'])
        self.assertTrue(any(query == '香菇滑鸡制作细节' for _, query, _ in calls))
        self.assertFalse(any('unselected.example' in query for _, query, _ in calls))
        self.assertTrue(any('制作细节' in query for _, query, _ in calls))

    def test_first_round_uses_ai_rewrite_but_assessment_keeps_original(self):
        query = '请问中科大中区桃李苑哪道菜好吃，想找食客的真实体验'
        rewritten = '中国科学技术大学 桃李苑 菜品推荐'
        plan = {'queries': [{'query': query}, {'query': rewritten}], 'must_have': ['真实食客体验']}
        calls, assessed = [], []
        def search(provider, text, scope, limit, config):
            calls.append((text, scope[0]))
            return {'results': [self.result(scope[0], text)], 'status': {'provider': provider, 'ok': True}}
        def assessment(original, results, current_plan, *args):
            assessed.append((original, copy.deepcopy(current_plan['must_have'])))
            return 0
        state, _ = self.run_search(self.job(query=query, use_ai=True, max_rounds=1), search,
                                  plan_override=plan, assessment=assessment)
        self.assertEqual(state['rounds'][0]['queries'][0]['query'], rewritten)
        self.assertEqual({scope for text, scope in calls if text == rewritten}, {'zhihu', 'xiaohongshu'})
        self.assertTrue(assessed)
        self.assertEqual(assessed[0][0], query)
        self.assertIn(query, assessed[0][1])
        self.assertIn('真实食客体验', assessed[0][1])

    def test_rewrite_safety_uses_original_context_and_rejects_scope_operators(self):
        self.assertEqual(adaptive._safe_direction('桃李苑有什么好吃的', '中国科大 桃李苑 口味'), '中国科大 桃李苑 口味')
        self.assertIsNone(adaptive._safe_direction('合肥成年人本人公开相亲帖', '17岁 女生'))
        self.assertIsNone(adaptive._safe_direction('桃李苑菜品', 'site:unselected.example 菜品'))
        self.assertIsNone(adaptive._safe_direction('桃李苑菜品', 'https://unselected.example'))

    def test_interrupted_queries_retry_but_completed_queries_do_not(self):
        original = self.job()['query']
        for interrupted_status in ('running', 'queued', 'cancelled'):
            with self.subTest(status=interrupted_status):
                unfinished = original + ' 未完成的线索'
                done = original + ' 已完成线索'
                job = self.job(platforms=['zhihu'], max_rounds=1, round=1, rounds=[{'number': 1, 'queries': [
                    {'query': unfinished, 'platform': 'zhihu', 'provider': 'bing', 'status': interrupted_status},
                    {'query': done, 'platform': 'zhihu', 'provider': 'bing', 'status': 'completed'},
                ]}], plan={'queries': [{'query': done}], 'must_have': [original]})
                calls = []
                def search(provider, query, scope, limit, config):
                    calls.append(query)
                    return {'results': [], 'status': {'provider': provider, 'ok': True}}
                state, _ = self.run_search(job, search)
                self.assertIn(unfinished, calls)
                self.assertNotIn(done, calls)
                self.assertEqual(calls[0], unfinished)

    def test_dead_paid_key_retains_free_fallback(self):
        calls = []
        def search(provider, query, scope, limit, config):
            calls.append(provider)
            if provider == 'tavily':
                return {'results': [], 'status': {'provider': provider, 'ok': False, 'error': 'invalid test key'}}
            return {'results': [self.result(scope[0], query)], 'status': {'provider': provider, 'ok': True}}
        state, _ = self.run_search(self.job(platforms=['zhihu'], max_rounds=1), search, available=['tavily', 'bing', 'duckduckgo'])
        self.assertIn('tavily', calls)
        self.assertIn('bing', calls)
        self.assertEqual(state['stop_reason'], 'round_limit')
        self.assertTrue(state['results'])

    def test_native_bilibili_is_not_starved_by_twenty_request_budget(self):
        platforms = [item['id'] for item in adaptive.providers.platform_catalog()]
        def search(provider, query, scope, limit, config):
            return {'results': [], 'status': {'provider': provider, 'ok': True}}
        state, _ = self.run_search(self.job(platforms=platforms, max_rounds=1), search, available=['bing', 'duckduckgo', 'bilibili'])
        tasks = state['rounds'][0]['queries']
        self.assertLessEqual(len(tasks), 20)
        self.assertEqual({task['platform'] for task in tasks}, set(platforms))
        self.assertEqual(next(task['provider'] for task in tasks if task['platform'] == 'bilibili'), 'bilibili')

    def test_no_duplicate_searches_across_continue(self):
        calls = []
        def search(provider, query, scope, limit, config):
            calls.append((provider, query, tuple(scope)))
            return {'results': [self.result(scope[0], query)], 'status': {'provider': provider, 'ok': True, 'count': 1}}
        state, _ = self.run_search(self.job(max_rounds=1), search)
        prior_ids = {result['id'] for result in state['results']}
        state['max_rounds'] = 1
        continued, _ = self.run_search(state, search)
        self.assertEqual(continued['round'], 2)
        self.assertEqual(len(calls), len(set(calls)))
        self.assertTrue(prior_ids.issubset({result['id'] for result in continued['results']}))
        self.assertEqual(continued['stop_reason'], 'round_limit')

    def test_unavailable_sources_pause_immediately_even_unlimited(self):
        def search(provider, query, scope, limit, config):
            return {'results': [], 'status': {'provider': provider, 'ok': False, 'error': '测试不可用'}}
        state, _ = self.run_search(self.job(max_rounds=0), search)
        self.assertEqual(state['round'], 1)
        self.assertEqual(state['stop_reason'], 'sources_unavailable')
        self.assertEqual(state['state'], 'awaiting_user')

    def test_two_empty_rounds_pause_instead_of_infinite_loop(self):
        def search(provider, query, scope, limit, config):
            return {'results': [], 'status': {'provider': provider, 'ok': True, 'count': 0}}
        state, _ = self.run_search(self.job(max_rounds=0), search)
        self.assertEqual(state['round'], 2)
        self.assertEqual(state['stop_reason'], 'no_new_results')

    def test_no_available_new_direction_pauses_without_request(self):
        job = self.job(max_rounds=0, platforms=['zhihu'])
        query = job['query']
        prior = [{'provider': 'bing', 'platform': 'zhihu', 'query': value, 'status': 'completed'}
                 for value in [query] + [query + ' ' + suffix for suffix in ('原帖', '实际体验', '详细评价', '讨论', '最新')]]
        job.update(round=1, rounds=[{'number': 1, 'queries': prior, 'new_results': 0}], plan={'must_have': [query], 'queries': [{'query': query}]})
        def fail(*args):
            self.fail('No repeated request should execute')
        state, _ = self.run_search(job, fail)
        self.assertEqual(state['round'], 1)
        self.assertEqual(state['stop_reason'], 'exhausted')

    def test_stop_with_blocking_provider_returns_fast_and_keeps_partial_results(self):
        release = threading.Event()
        blocked = threading.Event()
        stop = threading.Event()
        previous_summary = {'state': 'ready', 'points': [{'text': '上次已有结论', 'citations': []}], 'source_count': 0}
        def search(provider, query, scope, limit, config):
            self.assertIs(config['_cancel_event'], stop)
            if scope[0] == 'xiaohongshu':
                blocked.set()
                release.wait(3)
                return {'results': [], 'status': {'provider': provider, 'ok': True}}
            blocked.wait(1)
            return {'results': [self.result(scope[0], query)], 'status': {'provider': provider, 'ok': True, 'count': 1}}
        def update(state):
            if state.get('results') and state.get('stage') == 'searching':
                stop.set()
        before = time.monotonic()
        try:
            state, _ = self.run_search(self.job(ai_summary=previous_summary), search, stop=stop, on_update=update)
            elapsed = time.monotonic() - before
        finally:
            release.set()
        self.assertLess(elapsed, 1)
        self.assertEqual(state['state'], 'stopped')
        self.assertGreaterEqual(len(state['results']), 1)
        self.assertTrue(all(result['platform'] == 'zhihu' for result in state['results']))
        self.assertEqual(state['ai_summary']['points'], previous_summary['points'])
        self.assertTrue(state['ai_summary']['stale'])

    def test_stop_during_fetch_or_summary_keeps_previous_publication(self):
        for stage in ('reading', 'summarizing'):
            with self.subTest(stage=stage):
                stop, started, release = threading.Event(), threading.Event(), threading.Event()
                job = self.job(use_ai=True, platforms=['zhihu'], max_rounds=1, fetch_pages=stage == 'reading',
                               ai_summary={'state': 'ready', 'points': [{'text': '上一版结论'}], 'source_count': 0})
                def search(provider, query, scope, limit, config):
                    return {'results': [self.result(scope[0], query)], 'status': {'provider': provider, 'ok': True}}
                def blocker(*args, **kwargs):
                    started.set()
                    release.wait(3)
                    return {'state': 'ready', 'points': [{'text': '不可覆盖的晚到结论'}], 'text': '晚到正文'}
                result_box = []
                with patch.object(adaptive.providers, 'fetch_public_page', side_effect=blocker):
                    thread = threading.Thread(target=lambda: result_box.append(self.run_search(job, search, stop=stop,
                                              summary=blocker if stage == 'summarizing' else None)))
                    thread.start()
                    try:
                        self.assertTrue(started.wait(1))
                        stop.set()
                        thread.join(0.9)
                        self.assertFalse(thread.is_alive())
                    finally:
                        release.set()
                        thread.join(2)
                state = result_box[0][0]
                self.assertEqual(state['state'], 'stopped')
                self.assertTrue(state['results'])
                self.assertEqual(state['ai_summary']['points'], job['ai_summary']['points'])

    def test_same_source_from_new_queries_is_only_kept_once(self):
        def search(provider, query, scope, limit, config):
            result = self.result(scope[0], 'stable-source')
            return {'results': [result, result], 'status': {'provider': provider, 'ok': True, 'count': 2}}
        state, _ = self.run_search(self.job(platforms=['zhihu']), search)
        self.assertEqual(len(state['results']), 1)
        self.assertEqual(state['rounds'][0]['new_results'], 1)
        self.assertEqual(state['rounds'][1]['new_results'], 0)

    def test_stop_during_ai_wait_preserves_results_and_previous_summary(self):
        stop, started, release = threading.Event(), threading.Event(), threading.Event()
        state = self.job(use_ai=True, platforms=['zhihu'], max_rounds=1,
                         ai_summary={'state': 'ready', 'points': [{'text': '上一版结论'}], 'source_count': 0})
        def search(provider, query, scope, limit, config):
            return {'results': [self.result(scope[0], query)], 'status': {'provider': provider, 'ok': True}}
        def assessment(*args):
            started.set()
            release.wait(3)
            return 0
        result_box = []
        thread = threading.Thread(target=lambda: result_box.append(self.run_search(state, search, stop=stop, assessment=assessment)))
        thread.start()
        try:
            self.assertTrue(started.wait(1))
            before = time.monotonic()
            stop.set()
            thread.join(0.9)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - before, 1)
        finally:
            release.set()
            thread.join(2)
        final = result_box[0][0]
        self.assertEqual(final['state'], 'stopped')
        self.assertTrue(final['results'])
        self.assertEqual(final['ai_summary']['points'], state['ai_summary']['points'])

    def test_scope_filter_rejects_provider_results_from_unselected_domain(self):
        def search(provider, query, scope, limit, config):
            return {'results': [self.result('web', query)], 'status': {'provider': provider, 'ok': True, 'count': 1}}
        state, _ = self.run_search(self.job(platforms=['zhihu'], max_rounds=1), search)
        self.assertEqual(state['results'], [])

    def test_custom_site_is_separate_scope_and_uses_custom_connector(self):
        site = {'name': '校园社区', 'domain': 'campus.example.com', 'search_url': 'https://campus.example.com/search?q={query}'}
        def search(*args):
            self.fail('Custom-only job must use its custom connector')
        def custom(site_arg, query, limit, config):
            self.assertEqual(site_arg, site)
            self.assertEqual(limit, 6)
            return {'results': [{'title': '中科大桃李苑香菇滑鸡记录', 'url': 'https://campus.example.com/posts/1', 'snippet': '香菇滑鸡很好吃',
                                 'platform': 'website', 'source': 'website', 'site': site['name'], 'domain': site['domain']}],
                    'status': {'provider': 'website', 'ok': True, 'count': 1}}
        with patch.object(adaptive.providers, 'search_custom_site', side_effect=custom) as connector:
            state, _ = self.run_search(self.job(platforms=[], custom_sites=[site], max_rounds=1), search)
        self.assertGreaterEqual(connector.call_count, 1)
        self.assertLessEqual(connector.call_count, 3)
        self.assertEqual(state['results'][0]['platform'], 'website')
        self.assertEqual(state['rounds'][0]['queries'][0]['platform'], 'website:campus.example.com')

    def test_custom_only_search_accepts_matching_imported_url(self):
        site = {'name': '校园社区', 'domain': 'campus.example.com', 'search_url': 'https://campus.example.com/search?q={query}'}
        class ImportedLibrary:
            def search_documents(self, *args, **kwargs):
                return [{'id': 'local-entry', 'title': '中科大桃李苑香菇滑鸡', 'url': 'https://notes.campus.example.com/post/1',
                         'snippet': '香菇滑鸡很好吃，鸡肉鲜嫩。', 'body': '中科大桃李苑香菇滑鸡推荐',
                         'source': 'local', 'content_level': 'local', 'platform': 'web'}]
        with patch.object(adaptive.providers, 'search_custom_site', return_value={'results': [], 'status': {'provider': 'website', 'ok': True}}):
            state, _ = self.run_search(self.job(platforms=[], custom_sites=[site], max_rounds=1), lambda *args: None,
                                      library=ImportedLibrary())
        self.assertEqual(len(state['results']), 1)
        self.assertEqual(state['results'][0]['content_level'], 'local')
        self.assertEqual(state['results'][0]['discovery_scopes'], ['website:campus.example.com'])

    def test_richer_same_url_evidence_is_kept_and_assessed_again(self):
        from search_app.engine import clean_result
        query = self.job()['query']
        raw = self.result('zhihu', 'stable-source')
        raw['snippet'] = '中科大桃李苑记录'
        prior = clean_result(raw, query)
        prior['evidence'] = [{'condition': query, 'status': 'unknown', 'quote': ''}]
        prior['discovery_scopes'] = ['zhihu']
        calls = []
        def search(provider, text, scope, limit, config):
            richer = {**raw, 'snippet': '中科大桃李苑的香菇滑鸡很好吃，鸡肉鲜嫩，食客推荐这道菜。'}
            return {'results': [richer], 'status': {'provider': provider, 'ok': True}}
        def assessment(original, results, plan, *args):
            calls.append(copy.deepcopy(results))
            return len(results)
        state, _ = self.run_search(self.job(platforms=['zhihu'], use_ai=True, max_rounds=1, results=[prior]), search,
                                  assessment=assessment)
        self.assertTrue(calls)
        self.assertIn('食客推荐', calls[0][0]['snippet'])
        self.assertIn('食客推荐', state['results'][0]['snippet'])
        self.assertEqual(state['rounds'][0]['updated_results'], 1)
        self.assertEqual(state['rounds'][0]['new_results'], 0)

    def test_pure_synonym_retrieval_enters_candidates_before_original_verification(self):
        query = '中科大'
        rewritten = '中国科学技术大学'
        plan = {'queries': [{'query': query}, {'query': rewritten}], 'must_have': [query]}
        assessment_queries = []
        def search(provider, text, scope, limit, config):
            return {'results': [{'title': '中国科学技术大学简介', 'snippet': '中国科学技术大学是一所大学。',
                                 'url': 'https://www.zhihu.com/post/alias', 'platform': 'zhihu', 'source': provider}],
                    'status': {'provider': provider, 'ok': True}}
        def assessment(original, results, current_plan, *args):
            assessment_queries.append(original)
            self.assertIn(original, current_plan['must_have'])
            return len(results)
        state, _ = self.run_search(self.job(query=query, platforms=['zhihu'], use_ai=True, max_rounds=1), search,
                                  plan_override=plan, assessment=assessment)
        self.assertTrue(state['results'])
        self.assertEqual(assessment_queries, [query])
        self.assertEqual(state['results'][0]['match'], 'unverified')

    def test_weak_school_alias_overlap_does_not_admit_generic_china_pages(self):
        query = '合肥中科大附近，有哪些餐馆比较好吃'
        rewritten = '中国科学技术大学 周边 美食 攻略'
        plan = {'queries': [{'query': rewritten}], 'must_have': [query]}
        def search(provider, text, scope, limit, config):
            return {'results': [{'title': '中国百科网', 'snippet': '中国历史与地理百科。',
                                 'url': 'https://example.com/china', 'platform': 'web', 'source': provider}],
                    'status': {'provider': provider, 'ok': True}}
        state, _ = self.run_search(self.job(query=query, platforms=['web'], use_ai=True, max_rounds=1), search,
                                  plan_override=plan)
        self.assertEqual(state['results'], [])
        self.assertEqual(state['rounds'][0]['new_results'], 0)

    def test_pure_english_alias_with_substantial_coverage_is_retained_unverified(self):
        query = '中科大'
        rewritten = 'University of Science and Technology of China'
        plan = {'queries': [{'query': rewritten}], 'must_have': [query]}
        def search(provider, text, scope, limit, config):
            return {'results': [{'title': 'University of Science and Technology of China', 'snippet': 'University admissions information.',
                                 'url': 'https://example.com/university', 'platform': 'web', 'source': provider}],
                    'status': {'provider': provider, 'ok': True}}
        state, _ = self.run_search(self.job(query=query, platforms=['web'], use_ai=True, max_rounds=1), search,
                                  plan_override=plan)
        self.assertEqual(len(state['results']), 1)
        self.assertEqual(state['results'][0]['match'], 'unverified')
        self.assertIn(query, state['plan']['must_have'])

    def test_dating_candidates_are_never_published_before_adult_verification(self):
        query = '合肥成年人本人公开相亲帖'
        def search(provider, text, scope, limit, config):
            return {'results': [{'title': '合肥相亲：17岁征友', 'url': 'https://www.zhihu.com/post/1', 'snippet': '我今年17岁，合肥人。',
                                 'platform': 'zhihu', 'source': 'bing'}], 'status': {'provider': provider, 'ok': True, 'count': 1}}
        state, updates = self.run_search(self.job(query=query, platforms=['zhihu'], public_post_only=True, max_rounds=1), search)
        self.assertEqual(state['results'], [])
        self.assertTrue(all(not update.get('results') for update in updates))


if __name__ == '__main__':
    unittest.main()
