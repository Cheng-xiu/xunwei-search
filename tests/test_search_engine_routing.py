"""Offline integration checks for selected engines, scopes and visitor config."""
import copy
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from search_app import adaptive, engine, providers
from search_app.public_sessions import VisitorApp
from search_app.server import App
from search_app.retrieval import depth_profile


FREE = ['baidu', 'bing', 'google', 'yandex', 'duckduckgo']


class EngineConfigurationTests(unittest.TestCase):
    def test_selection_roundtrips_and_invalid_values_preserve_config(self):
        with tempfile.TemporaryDirectory() as directory:
            app = App(directory, environment=False)
            selected = ['google', 'yandex', 'google']
            saved = app.save_config({'search_engines': selected})
            self.assertEqual(saved['search_engines'], ['google', 'yandex'])
            selected.append('baidu')
            saved['search_engines'].append('bing')
            self.assertEqual(app.public_config()['search_engines'], ['google', 'yandex'])
            restored = App(directory, environment=False)
            self.assertEqual(restored.public_config()['search_engines'], ['google', 'yandex'])
            for value in ('google', None, {}, [True], [1], ['unknown'], [[]], FREE * 2):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    app.save_config({'search_engines': value})
                self.assertEqual(app.config['search_engines'], ['google', 'yandex'])
            self.assertEqual(app.save_config({'search_engines': []})['search_engines'], [])

    def test_job_freezes_selected_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            app = App(directory, environment=False)
            app.save_config({'search_engines': ['google', 'yandex']})
            with patch('search_app.server.threading.Thread'):
                job_id = app.create_job({'query': '中科大桃李苑菜品', 'use_ai': False})['job_id']
            app.save_config({'search_engines': ['bing']})
            self.assertEqual(app.get_job(job_id)['search_engines'], ['google', 'yandex'])
            app.jobs[job_id].update(state='awaiting_user')
            with patch('search_app.server.threading.Thread') as worker:
                app.continue_job(job_id, {})
            self.assertEqual(app.get_job(job_id)['search_engines'], ['bing'])
            self.assertEqual(worker.call_args.kwargs['args'][1]['search_engines'], ['bing'])

    def test_visitor_selection_is_isolated_and_survives_model_switch(self):
        owner = {'base_url': 'https://model.example/v1', 'model': 'example', 'api_key': 'synthetic-test-key'}
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            visitor_a, visitor_b = VisitorApp(first, owner, 'shared'), VisitorApp(second, owner, 'shared')
            visitor_a.save_config({'search_engines': ['google', 'yandex']})
            self.assertEqual(visitor_b.public_config()['search_engines'], [])
            visitor_a.set_mode('custom')
            self.assertEqual(visitor_a.public_config()['search_engines'], ['google', 'yandex'])
            self.assertEqual(visitor_a.config['api_key'], '')
            visitor_a.set_mode('shared')
            self.assertEqual(visitor_a.public_config()['search_engines'], ['google', 'yandex'])
            self.assertFalse(visitor_a.config_path.exists())


class EngineRoutingTests(unittest.TestCase):
    def test_single_pass_covers_all_engines_without_expanding_content_scope(self):
        platforms = [item['id'] for item in providers.platform_catalog()]
        with patch.object(engine, 'available_providers', return_value=FREE + ['bilibili', 'github', 'stackoverflow']):
            tasks = engine.build_tasks(engine.fallback_plan('中科大桃李苑哪道菜好吃'), platforms, 'quick', {})
        self.assertLessEqual(len(tasks), 20)
        self.assertEqual({scope[0] for _, _, scope in tasks}, set(platforms))
        self.assertTrue(set(FREE).issubset({source for source, _, _ in tasks}))
        for source, _, scopes in tasks:
            if source in engine.DIRECT_PROVIDERS:
                self.assertEqual(scopes, [source])

    def test_no_implicit_bing_when_user_selects_other_sources(self):
        with patch.object(engine, 'available_providers', return_value=['google', 'tavily']):
            tasks = engine.build_tasks(engine.fallback_plan('rare firmware problem'), ['web'], 'deep', {})
        self.assertEqual({source for source, _, _ in tasks}, {'google', 'tavily'})

    def test_custom_sites_and_native_sources_share_depth_budget(self):
        platforms = [item['id'] for item in providers.platform_catalog()]
        sites = [{'domain': f'site{index}.example', 'name': f'Site {index}'} for index in range(6)]
        plan = {'queries': [{'query': f'rare identifier variant {index}'} for index in range(6)]}
        for depth in ('quick', 'deep', 'research'):
            with self.subTest(depth=depth), patch.object(engine, 'available_providers', return_value=FREE + ['bilibili', 'github', 'stackoverflow']):
                tasks = engine.build_tasks(plan, platforms, depth, {}, sites)
            self.assertLessEqual(len(tasks), depth_profile(depth)['requests'])
            self.assertTrue({'website:' + site['domain'] for site in sites}.issubset({scope[0] for _, _, scope in tasks}))
            self.assertTrue(set(FREE).issubset({source for source, _, _ in tasks}))

    def test_single_pass_custom_search_receives_actual_selected_engine(self):
        config = {'search_engines': ['google', 'yandex']}
        job = {'query': '中科大桃李苑菜品', 'platforms': [], 'custom_sites': [{'name': 'Campus', 'domain': 'campus.example'}],
               'depth': 'quick', 'adaptive': False, 'use_ai': False, 'fetch_pages': False}
        storage = Mock()
        storage.search_documents.return_value = []
        state, requested = {}, []
        def custom(site, query, limit, worker_config):
            requested.append((site['domain'], worker_config['_engine']))
            return {'results': [], 'status': {'provider': 'website', 'engine': worker_config['_engine'], 'ok': True, 'count': 0}}
        with patch.object(engine, 'search_custom_site', side_effect=custom), patch.object(engine, 'search_provider') as generic:
            engine.run_search(job, config, storage, lambda **fields: state.update(fields))
        generic.assert_not_called()
        self.assertEqual({source for _, source in requested}, {'google', 'yandex'})
        self.assertTrue(all(domain == 'campus.example' for domain, _ in requested))
        self.assertEqual({task['provider'] for task in state['rounds'][0]['queries']}, {'google', 'yandex'})

    def test_adaptive_tail_sources_work_after_first_sources_fail(self):
        query = '中科大桃李苑菜品'
        job = {'query': query, 'platforms': ['web'], 'custom_sites': [], 'use_ai': False, 'fetch_pages': False,
               'depth': 'quick', 'max_rounds': 1, 'rounds': [], 'results': [], 'warnings': []}
        state, requested = copy.deepcopy(job), []
        storage = Mock()
        storage.search_documents.return_value = []
        def search(source, text, scopes, limit, config):
            requested.append(source)
            if source in FREE[:2]:
                return {'results': [], 'status': {'provider': source, 'ok': False, 'error': 'Synthetic source unavailable'}}
            return {'results': [{'title': query, 'snippet': '桃李苑菜品口味体验', 'url': f'https://campus.example/{source}', 'source': source}],
                    'status': {'provider': source, 'ok': True, 'count': 1}}
        with patch.object(adaptive.providers, 'available_providers', return_value=FREE), \
                patch.object(adaptive.providers, 'search_provider', side_effect=search):
            adaptive.run_adaptive_search(job, {}, storage, lambda **fields: state.update(copy.deepcopy(fields)), threading.Event())
        self.assertTrue(set(FREE).issubset(requested))
        self.assertTrue(state['results'])
        self.assertLessEqual(len(requested), 20)

    def test_adaptive_large_scope_selection_explores_new_engines(self):
        targets = [{'id': item['id'], 'domains': item['domains']} for item in providers.platform_catalog()]
        directions = {target['id']: ['rare identifier', 'rare identifier details'] for target in targets}
        tasks = adaptive._weighted_tasks('rare identifier', targets, FREE, set(engine.DIRECT_PROVIDERS), directions, [], set(), True)
        self.assertEqual({task['platform'] for task in tasks}, {target['id'] for target in targets})
        self.assertTrue(set(FREE).issubset({task['provider'] for task in tasks}))
        self.assertLessEqual(len(tasks), 20)


if __name__ == '__main__':
    unittest.main()
