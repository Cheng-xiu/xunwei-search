"""Offline integration regressions for depth, scheduling and native evidence."""
import copy
import hashlib
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from search_app import adaptive, engine, providers
from search_app.retrieval import DIRECT_PROVIDERS
from search_app.server import App


class DepthLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = App(self.directory.name)
        self.app.config.update(api_key='', tavily_key='', brave_key='', searxng_url='')

    def tearDown(self):
        self.app.storage.close()
        self.directory.cleanup()

    def create(self, depth='quick'):
        with patch('search_app.server.threading.Thread') as thread:
            job_id = self.app.create_job({'query': '中科大桃李苑菜品', 'depth': depth,
                                          'use_ai': False, 'platforms': ['web']})['job_id']
        thread.return_value.start.assert_called_once_with()
        return job_id, thread.call_args.kwargs['args'][1]

    def finish_offline(self, job_id, config):
        received = []

        def search(job, worker_config, storage, update):
            received.append(job['depth'])
            update(state='awaiting_user', stage='waiting', stop_reason='round_limit')

        # Run the actual worker/persistence boundary without a background thread
        # or any retrieval implementation.
        with patch('search_app.server.run_search', side_effect=search):
            self.app._run(job_id, config)
        return received

    def test_all_create_depths_reach_worker_and_persist(self):
        for depth in ('quick', 'deep', 'research'):
            with self.subTest(depth=depth):
                job_id, config = self.create(depth)
                self.assertEqual(self.app.get_job(job_id)['depth'], depth)
                self.assertEqual(self.finish_offline(job_id, config), [depth])
                self.assertEqual(self.app.storage.get_job(job_id)['depth'], depth)

    def test_continue_changes_depth_for_worker_and_restored_job(self):
        for original in ('quick', 'deep', 'research'):
            job_id, config = self.create(original)
            self.finish_offline(job_id, config)
            for chosen in ('quick', 'deep', 'research'):
                with self.subTest(original=original, chosen=chosen):
                    with patch('search_app.server.threading.Thread') as thread:
                        resumed = self.app.continue_job(job_id, {'depth': chosen, 'max_rounds': 1})
                    self.assertEqual(resumed['job_id'], job_id)
                    self.assertEqual(self.app.get_job(job_id)['depth'], chosen)
                    self.assertEqual(self.finish_offline(job_id, thread.call_args.kwargs['args'][1]), [chosen])
                    self.assertEqual(self.app.storage.get_job(job_id)['depth'], chosen)
                    # Read through a fresh App to check the serialized job,
                    # rather than only the live in-memory object.
                    restored = App(self.directory.name)
                    try:
                        self.assertEqual(restored.get_job(job_id)['depth'], chosen)
                    finally:
                        restored.storage.close()
            with patch('search_app.server.threading.Thread') as thread:
                self.app.continue_job(job_id, {'max_rounds': 1})
            self.assertEqual(self.finish_offline(job_id, thread.call_args.kwargs['args'][1]), ['research'])

    def test_invalid_create_depth_has_no_storage_or_thread_side_effects(self):
        for value in ('normal', 'DEEP', '', None, True, 1, [], {}):
            with self.subTest(value=value), patch.object(self.app.storage, 'save_job') as save, \
                    patch('search_app.server.threading.Thread') as thread:
                before = copy.deepcopy(self.app.jobs)
                with self.assertRaises(ValueError):
                    self.app.create_job({'query': '中科大桃李苑菜品', 'depth': value})
                save.assert_not_called()
                thread.assert_not_called()
                self.assertEqual(self.app.jobs, before)

    def test_invalid_continue_depth_preserves_saved_job_and_control(self):
        job_id, config = self.create('deep')
        self.finish_offline(job_id, config)
        before = self.app.get_job(job_id)
        saved = self.app.storage.get_job(job_id)
        control = self.app.controls[job_id]
        epoch = self.app.epochs[job_id]
        for value in ('normal', 'DEEP', '', None, True, 1, [], {}):
            with self.subTest(value=value), patch.object(self.app.storage, 'save_job') as save, \
                    patch('search_app.server.threading.Thread') as thread:
                with self.assertRaises(ValueError):
                    self.app.continue_job(job_id, {'depth': value})
                save.assert_not_called()
                thread.assert_not_called()
                self.assertEqual(self.app.get_job(job_id), before)
                self.assertEqual(self.app.storage.get_job(job_id), saved)
                self.assertIs(self.app.controls[job_id], control)
                self.assertEqual(self.app.epochs[job_id], epoch)


class SinglePassScopeTests(unittest.TestCase):
    def test_quick_first_twenty_cover_all_sixteen_scopes_and_keep_native_scoped(self):
        selected = [item['id'] for item in providers.platform_catalog()]
        self.assertEqual(len(selected), 16)
        plan = engine.fallback_plan('中科大中区桃李苑哪道菜好吃')
        self.assertGreaterEqual(len(plan['queries']), 2)
        for engines in (['bing', 'duckduckgo'], ['tavily', 'bing', 'duckduckgo']):
            with self.subTest(engines=engines), \
                    patch.object(engine, 'available_providers', return_value=engines + sorted(DIRECT_PROVIDERS)):
                tasks = engine.build_tasks(plan, selected, 'quick', {})
                self.assertLessEqual(len(tasks), 20)
                self.assertEqual({scope for _, _, scopes in tasks for scope in scopes}, set(selected))
                for native in DIRECT_PROVIDERS:
                    self.assertTrue(any(provider == native for provider, _, _ in tasks), native)
                for provider, _, scopes in tasks:
                    if provider in DIRECT_PROVIDERS:
                        self.assertEqual(scopes, [provider], 'Native APIs must not act as general search engines')


class AdaptiveDepthOrchestrationTests(unittest.TestCase):
    def test_custom_scopes_over_budget_are_explored_before_reinforcing_hot_source(self):
        targets = [{'id': f'website:site{index}.example', 'domains': [f'site{index}.example']} for index in range(22)]
        directions = {target['id']: ['rare keyword', 'rare keyword details'] for target in targets}
        first = adaptive._weighted_tasks('rare keyword', targets, ['bing'], set(), directions, [], set(), True)
        self.assertEqual(len(first), 20)
        searched = {adaptive._query_key(task['provider'], task['query'], task['platform']) for task in first}
        stats = [{'platform': targets[0]['id'], 'score': 100}]
        second = adaptive._weighted_tasks('rare keyword', targets, ['bing'], set(), directions, stats, searched, False)
        self.assertEqual([task['platform'] for task in second[:2]], [target['id'] for target in targets[-2:]])
        self.assertLessEqual(len(second), 20)

    @staticmethod
    def job(**changes):
        job = {'query': '中科大桃李苑香菇滑鸡', 'platforms': ['zhihu'], 'custom_sites': [],
               'use_ai': False, 'fetch_pages': False, 'depth': 'quick', 'max_rounds': 2,
               'round': 0, 'rounds': [], 'results': [], 'provider_status': [], 'warnings': []}
        job.update(changes)
        return job

    def run_offline(self, job, search, available, details=None, page_fetch=None, stop=None):
        state = copy.deepcopy(job)
        library = Mock()
        library.search_documents.return_value = []

        def update(**fields):
            state.update(copy.deepcopy(fields))

        with patch.object(adaptive.providers, 'available_providers', return_value=available), \
                patch.object(adaptive.providers, 'search_provider', side_effect=search), \
                patch.object(adaptive.providers, 'fetch_public_page', side_effect=page_fetch or AssertionError('Unexpected public page fetch')) as page, \
                patch.object(adaptive.providers, 'fetch_result_details', side_effect=details or AssertionError('Unexpected native detail fetch')) as detail, \
                patch.object(adaptive, 'chat', side_effect=AssertionError('No AI calls in offline regression')), \
                patch.object(adaptive, 'summarize_results', side_effect=AssertionError('No summary calls in offline regression')), \
                patch.object(adaptive, 'build_progress_report', return_value={'state': 'disabled', 'findings': []}), \
                patch('urllib.request.urlopen', side_effect=AssertionError('No network in offline regression')):
            adaptive.run_adaptive_search(job, {}, library, update, stop or threading.Event())
        if page_fetch is None:
            page.assert_not_called()
        return state, detail.call_args_list

    def test_failed_provider_is_suppressed_but_other_provider_and_fresh_continue_work(self):
        calls = []
        selected = ['zhihu', 'xiaohongshu', 'wechat', 'douban', 'tieba', 'web']

        def search(provider, query, scopes, limit, config):
            calls.append((provider, query, scopes[0]))
            if provider == 'tavily':
                return {'results': [], 'status': {'provider': provider, 'ok': False, 'error': 'offline failure'}}
            host = providers.PLATFORM_HOSTS.get(scopes[0], ('example.org',))[0]
            suffix = hashlib.sha256(query.encode()).hexdigest()[:12]
            row = {'title': query + ' 菜品口味记录', 'url': f'https://{host}/post/{suffix}',
                   'snippet': '中科大桃李苑香菇滑鸡的详细口味记录', 'platform': scopes[0], 'source': provider}
            return {'results': [row], 'status': {'provider': provider, 'ok': True}}

        state, _ = self.run_offline(self.job(platforms=selected), search, ['tavily', 'bing'])
        self.assertEqual(state['round'], 2)
        first, second = state['rounds']
        failed = [task for task in first['queries'] if task['provider'] == 'tavily']
        self.assertGreaterEqual(sum(task['status'] == 'completed' for task in failed), 2)
        self.assertTrue(any(task['status'] == 'skipped' for task in failed), 'Unscheduled requests should be skipped')
        self.assertTrue(any(task['provider'] == 'bing' and task['status'] == 'completed' for task in first['queries']))
        self.assertTrue(second['queries'])
        self.assertTrue(all(task['provider'] == 'bing' for task in second['queries']))
        self.assertTrue(state['results'])
        self.assertTrue(any('tavily' in warning and '连续' in warning for warning in state['warnings']))
        first_bad_count = sum(provider == 'tavily' for provider, _, _ in calls)
        # At the second consumed failure, up to three other requests can already
        # be in flight; suppressing them retroactively is not required.
        self.assertLessEqual(first_bad_count, 5)
        calls.clear()
        before_ids = {row['id'] for row in state['results']}
        state['max_rounds'] = 1
        resumed, _ = self.run_offline(state, search, ['tavily', 'bing'])
        self.assertTrue(any(provider == 'tavily' for provider, _, _ in calls), 'A fresh continuation should retry the source')
        self.assertEqual(resumed['round'], 3)
        self.assertTrue(before_ids.issubset({row['id'] for row in resumed['results']}))

    def test_native_reply_reading_preserves_first_posts_and_does_not_spend_on_repo_or_pr(self):
        query = 'gpio wakeup interrupt'
        rows = []
        specifications = [
            ('github', 'issue', 'https://github.com/example/firmware/issues/1', True),
            ('github', 'issue', 'https://github.com/example/firmware/issues/2', True),
            ('stackoverflow', 'question', 'https://stackoverflow.com/questions/101/example', True),
            ('stackoverflow', 'question', 'https://stackoverflow.com/questions/102/example', True),
            ('github', 'repository', 'https://github.com/example/firmware', False),
            ('github', 'pull_request', 'https://github.com/example/firmware/pull/3', True),
            ('github', 'pull_request', 'https://github.com/example/firmware/pull/4', False),
        ]
        for source, kind, url, has_body in specifications:
            row = {'title': query + ' ' + kind, 'url': url, 'snippet': query + ' source description',
                   'platform': source, 'source': source, 'content_kind': kind}
            if has_body:
                row.update(body=query + ' original first-post body', content_level='page')
            rows.append(row)

        def search(provider, text, scopes, limit, config):
            return {'results': copy.deepcopy([row for row in rows if row['platform'] == scopes[0]]),
                    'status': {'provider': provider, 'ok': True}}

        def details(row, *args, **kwargs):
            empty = row['url'].endswith('/2') or '/102/' in row['url']
            return {'text': '' if empty else query + ' public reply text',
                    'coverage': '接口未返回有正文的公开回复。' if empty else '限量公开回复，非全部评论。'}

        for depth in ('quick', 'deep', 'research'):
            with self.subTest(depth=depth):
                state, calls = self.run_offline(self.job(query=query, platforms=['github', 'stackoverflow'],
                                                       depth=depth, max_rounds=1, fetch_pages=True),
                                                search, ['github', 'stackoverflow'], details)
                self.assertEqual(len(state['results']), len(rows))
                fetched_urls = [call.args[0]['url'] for call in calls]
                expected = [url for _, kind, url, _ in specifications if kind in ('issue', 'question')]
                self.assertCountEqual(fetched_urls, [] if depth == 'quick' else expected)
                by_url = {row['url']: row for row in state['results']}
                for original in rows:
                    result = by_url[original['url']]
                    if original['content_kind'] in ('repository', 'pull_request') or depth == 'quick':
                        self.assertFalse(result.get('details_read'))
                        self.assertNotIn('details_error', result)
                    elif original['url'].endswith('/2') or '/102/' in original['url']:
                        self.assertTrue(result['details_read'])
                        self.assertEqual(result['body'], original['body'])
                        self.assertEqual(result['content_level'], 'page')
                        self.assertIn('未返回', result['details_coverage'])
                        self.assertFalse(result.get('details_text'))
                        self.assertNotIn('fetch_error', result)
                        self.assertNotIn('details_error', result)
                    else:
                        self.assertTrue(result['details_read'])
                        self.assertIn(original['body'], result['body'])
                        self.assertIn('public reply text', result['details_text'])

    def test_empty_first_post_reads_replies_once_only_in_deeper_modes(self):
        query = 'gpio wakeup interrupt'
        row = {'title': query + ' issue', 'url': 'https://github.com/example/firmware/issues/10',
               'snippet': query, 'body': '', 'platform': 'github', 'source': 'github', 'content_kind': 'issue'}

        def search(provider, text, scopes, limit, config):
            return {'results': [copy.deepcopy(row)], 'status': {'provider': provider, 'ok': True}}

        for depth in ('quick', 'deep', 'research'):
            with self.subTest(depth=depth):
                normal_page = Mock(return_value={'text': query + ' readable public page'})
                replies = Mock(return_value={'text': query + ' exact public comment', 'coverage': '公开评论首页'})
                state, calls = self.run_offline(self.job(query=query, platforms=['github'], depth=depth,
                                                       max_rounds=1, fetch_pages=True), search, ['github'],
                                                details=replies, page_fetch=normal_page)
                self.assertEqual(len(state['results']), 1)
                result = state['results'][0]
                if depth == 'quick':
                    self.assertEqual(calls, [])
                    normal_page.assert_called_once()
                    self.assertFalse(result.get('details_read'))
                    self.assertIn('readable public page', result['body'])
                else:
                    self.assertEqual(len(calls), 1, 'An empty first post must not enter both read queues')
                    normal_page.assert_not_called()
                    self.assertTrue(result['details_read'])
                    self.assertIn('exact public comment', result['details_text'])

    def test_failed_or_cancelled_reply_reading_can_retry_in_fresh_continuation(self):
        query = 'gpio wakeup interrupt'
        row = {'title': query + ' issue', 'url': 'https://github.com/example/firmware/issues/20',
               'snippet': query, 'body': query + ' original first post', 'content_level': 'page',
               'platform': 'github', 'source': 'github', 'content_kind': 'issue'}
        prior = engine.clean_result(row, query)
        prior.update(match='partial', evidence=[{'condition': query, 'quote': row['body'], 'status': 'supported'}])

        def search(provider, text, scopes, limit, config):
            # No fresh result is needed to retry an already assessed first post.
            return {'results': [], 'status': {'provider': provider, 'ok': True}}

        for failure in ('response_error', 'exception', 'cancelled'):
            with self.subTest(failure=failure):
                stop = threading.Event()

                def first_read(*args, **kwargs):
                    if failure == 'cancelled':
                        stop.set()
                        return {'error': 'offline cancellation', 'cancelled': True}
                    if failure == 'exception':
                        raise RuntimeError('offline read failure')
                    return {'error': 'offline read failure'}

                job = self.job(query=query, platforms=['github'], depth='deep', max_rounds=1,
                               fetch_pages=True, results=[copy.deepcopy(prior)])
                failed, calls = self.run_offline(job, search, ['github'], details=first_read, stop=stop)
                self.assertEqual(len(calls), 1)
                result = failed['results'][0]
                self.assertFalse(result.get('details_read'))
                self.assertEqual(result['body'], prior['body'])
                self.assertEqual(result['evidence'], prior['evidence'])
                self.assertNotIn('fetch_error', result)
                self.assertEqual(failed['state'], 'stopped' if failure == 'cancelled' else 'awaiting_user')
                if failure != 'cancelled':
                    self.assertTrue(result.get('details_error'))

                empty_success = Mock(return_value={'text': '', 'coverage': '接口返回成功，公开评论为 0 条。'})
                resumed, calls = self.run_offline(failed, search, ['github'], details=empty_success)
                self.assertEqual(len(calls), 1, 'A fresh continuation must retry unsuccessful reads')
                result = resumed['results'][0]
                self.assertTrue(result['details_read'], 'Successful zero-reply reads are completed reads')
                self.assertNotIn('details_error', result)
                self.assertNotIn('fetch_error', result)
                self.assertEqual(result['body'], prior['body'])
                self.assertEqual(result['evidence'], prior['evidence'])
                self.assertIn('0 条', result['details_coverage'])
                _, repeated_calls = self.run_offline(resumed, search, ['github'])
                self.assertEqual(repeated_calls, [], 'A completed zero-reply read must not be retried')


if __name__ == '__main__':
    unittest.main()
