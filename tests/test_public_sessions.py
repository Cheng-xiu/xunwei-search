"""Isolated public visitor tests; synthetic credentials and no real AI calls."""
import http.client
import json
from concurrent.futures import ThreadPoolExecutor
import socket
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from search_app.public_sessions import LIMITS, PublicSessions
from search_app.server import App, DEFAULTS, Handler, LocalHTTPServer
from search_app.transport import PublicAccessError, TransportPolicy


OWNER = {'AI_API_KEY': 'synthetic-owner-key-never-send-live', 'AI_BASE_URL': 'https://owner.example/v1', 'AI_MODEL': 'owner-model'}
ADMIN = 'synthetic-admin-access-token-123456'
ORIGIN = 'https://cheng-xiu.github.io'
ENV = {'XUNWEI_ACCESS_TOKEN': ADMIN, 'XUNWEI_PUBLIC_HOSTS': 'public.example',
       'XUNWEI_ALLOWED_ORIGINS': ORIGIN, 'XUNWEI_PUBLIC_MODE': '1'}


class VisitorManagerTests(unittest.TestCase):
    def setUp(self):
        self.now = [1700000000.0]
        self.manager = PublicSessions(OWNER, clock=lambda: self.now[0], start_janitor=False)

    def tearDown(self):
        self.manager.close()

    def create(self, mode='custom', peer='127.0.0.1'):
        response = self.manager.create({'mode': mode}, peer)
        return response, self.manager.authenticate(response['access_token'])

    def test_public_mode_requires_explicit_deployment_guards(self):
        with self.assertRaises(ValueError):
            TransportPolicy.from_environment(environ={'XUNWEI_PUBLIC_MODE': '1'})
        with self.assertRaises(ValueError):
            TransportPolicy.from_environment(environ=dict(ENV, XUNWEI_PUBLIC_MODE='yes'))
        self.assertTrue(TransportPolicy.from_environment('0.0.0.0', ENV).public_mode)

    def test_custom_never_inherits_owner_or_process_provider_credentials(self):
        with patch.dict('os.environ', dict(OWNER, BRAVE_API_KEY='process-brave-secret', TAVILY_API_KEY='process-tavily-secret')):
            response, session = self.create()
        self.assertEqual(session.app.config['api_key'], '')
        self.assertEqual(session.app.config['brave_key'], '')
        self.assertEqual(session.app.config['tavily_key'], '')
        self.assertEqual(session.app.config['base_url'], DEFAULTS['base_url'])
        self.assertNotIn(OWNER['AI_API_KEY'], json.dumps(session.app.public_config()))
        self.assertFalse(session.app.config_path.exists())
        self.assertNotIn('access_token', self.manager.describe(session))
        self.assertNotIn(response['access_token'], repr(session))

    def test_shared_custom_switch_restores_own_config_without_owner_secret(self):
        _, session = self.create()
        app = session.app
        app.save_config({'api_key': 'visitor-private-key', 'base_url': 'https://visitor.example/v1', 'model': 'visitor-model',
                         'brave_key': 'visitor-brave-key'})
        self.manager.switch(session, 'shared')
        self.assertEqual(app.config['api_key'], OWNER['AI_API_KEY'])
        self.assertEqual(app.config['base_url'], OWNER['AI_BASE_URL'])
        self.assertEqual(app.config['brave_key'], 'visitor-brave-key')
        for data in ({'base_url': 'https://attacker.example/v1'}, {'api_key': ''}, {'model': 'replacement'},
                     {'clear_secrets': ['api_key']}, {'base_url': OWNER['AI_BASE_URL'], 'api_key': 'replacement'}):
            with self.subTest(data=list(data)), self.assertRaises(PublicAccessError):
                app.save_config(data)
        self.assertEqual(app.config['api_key'], OWNER['AI_API_KEY'])
        app.save_config({'tavily_key': 'visitor-tavily-key'})
        self.manager.switch(session, 'custom')
        self.assertEqual(app.config['api_key'], 'visitor-private-key')
        self.assertEqual(app.config['base_url'], 'https://visitor.example/v1')
        self.assertEqual(app.config['model'], 'visitor-model')
        self.assertEqual(app.config['tavily_key'], 'visitor-tavily-key')
        self.assertTrue(app.config['_public_network'])
        app.save_config({'clear_secrets': ['api_key']})
        self.assertFalse(app.config['api_key'])
        self.assertTrue(app.config['_public_network'])
        self.assertFalse(app.config_path.exists())

    def test_first_shared_to_custom_starts_without_any_owner_ai_values(self):
        _, session = self.create('shared')
        self.assertEqual(session.app.config['api_key'], OWNER['AI_API_KEY'])
        self.manager.switch(session, 'custom')
        self.assertEqual(session.app.config['api_key'], '')
        self.assertNotEqual(session.app.config['base_url'], OWNER['AI_BASE_URL'])
        self.assertNotEqual(session.app.config['model'], OWNER['AI_MODEL'])

    def test_missing_shared_key_is_explicit_and_never_silently_falls_back(self):
        manager = PublicSessions({}, start_janitor=False)
        try:
            self.assertFalse(manager.capabilities()['shared_available'])
            response = manager.create({}, 'peer')
            self.assertEqual(response['mode'], 'custom')
            with self.assertRaises(PublicAccessError) as error:
                manager.create({'mode': 'shared'}, 'peer')
            self.assertEqual(error.exception.status, 409)
            session = manager.authenticate(response['access_token'])
            with self.assertRaises(PublicAccessError):
                manager.switch(session, 'shared')
            self.assertEqual(session.app.mode, 'custom')
        finally:
            manager.close()

    def test_config_url_network_guard_marker_cannot_be_removed(self):
        _, session = self.create()
        for url in ('http://localhost:8877/v1', 'https://127.0.0.1/v1', 'https://192.168.1.1/v1',
                    'https://visitor.example:8443/v1'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                session.app.save_config({'base_url': url})
        session.app.save_config({'model': 'valid-model', '_public_network': False})
        self.assertTrue(session.app.config['_public_network'])
        self.assertNotIn('_public_network', session.app.public_config())

    def test_session_storage_and_history_are_isolated(self):
        _, first = self.create()
        _, second = self.create()
        doc = first.app.storage.add_document('独有资料', '', '仅属于第一位访客的公开测试文字。', 'web')
        job = {'id': 'a' * 32, 'query': '访客一历史', 'state': 'done', 'results': [], 'created_at': '2026-10-08'}
        first.app.storage.save_job(job)
        self.assertEqual(len(first.app.storage.list_documents()), 1)
        self.assertEqual(second.app.storage.list_documents(), [])
        self.assertFalse(second.app.storage.delete_document(doc['id']))
        self.assertEqual(second.app.storage.list_history(), [])
        self.assertIsNone(second.app.get_job(job['id']))
        self.manager.switch(first, 'shared')
        self.assertEqual(second.app.mode, 'custom')
        self.assertFalse(second.app.config['api_key'])

    def test_mode_change_and_public_round_budget_protect_active_work(self):
        _, session = self.create()
        app = session.app
        for rounds in (0, 4, 12):
            with self.subTest(rounds=rounds), self.assertRaises(ValueError):
                app.create_job({'query': '公开测试', 'max_rounds': rounds})
        app.jobs['a' * 32] = {'id': 'a' * 32, 'state': 'running'}
        with self.assertRaises(PublicAccessError) as error:
            self.manager.switch(session, 'shared')
        self.assertEqual(error.exception.status, 409)

    def test_visitor_concurrency_limits_are_explicit_and_invalid_values_do_not_mutate(self):
        _, session = self.create()
        app = session.app
        self.assertEqual(app.max_active_jobs, 2)
        self.assertEqual(app.max_search_concurrency, 4)
        app.save_config({'search_concurrency': 2})
        prior = app.public_config()
        job_id = 'c' * 32
        app.storage.save_job({'id': job_id, 'query': '公开检索资料', 'state': 'done', 'results': [],
                              'max_rounds': 1, 'search_concurrency': 2, 'created_at': '2026-10-08'})
        with patch('search_app.server.threading.Thread') as worker:
            for value in (0, -1, 5, 12, True, False, None, 2.0, '2', [], {}):
                for operation in ('config', 'create', 'continue'):
                    with self.subTest(value=value, operation=operation), self.assertRaises(ValueError):
                        if operation == 'config':
                            app.save_config({'search_concurrency': value})
                        elif operation == 'create':
                            app.create_job({'query': '公开检索资料', 'max_rounds': 1, 'search_concurrency': value})
                        else:
                            app.continue_job(job_id, {'max_rounds': 1, 'search_concurrency': value})
            worker.assert_not_called()
        self.assertEqual(app.public_config(), prior)
        self.assertEqual(app.jobs, {})
        self.assertEqual(app.get_job(job_id)['state'], 'done')

    def test_shared_concurrency_setting_is_visitor_local_and_survives_mode_changes(self):
        _, first = self.create('shared')
        _, second = self.create('shared')
        self.assertEqual(LIMITS['max_concurrent_jobs'], 4)
        self.assertEqual(self.manager.capabilities()['public_limits']['max_session_jobs'], 2)
        self.assertEqual(self.manager.capabilities()['public_limits']['search_concurrency'], 4)
        first.app.save_config({'search_concurrency': 1})
        self.assertEqual(first.app.public_config()['search_concurrency'], 1)
        self.assertEqual(second.app.public_config()['search_concurrency'], 4)
        self.assertEqual(first.app.config['api_key'], OWNER['AI_API_KEY'])
        self.manager.switch(first, 'custom')
        self.assertEqual(first.app.config['search_concurrency'], 1)
        self.assertEqual(first.app.config['api_key'], '')
        first.app.save_config({'search_concurrency': 4})
        self.manager.switch(first, 'shared')
        self.assertEqual(first.app.public_config()['search_concurrency'], 4)
        self.assertFalse(first.app.config_path.exists())

    def test_two_real_search_threads_overlap_and_stopping_one_preserves_the_other(self):
        _, session = self.create('shared')
        app = session.app
        queries = ('公开甲方食堂菜品', '公开乙方固件故障')
        started = {query: threading.Event() for query in queries}
        release = {query: threading.Event() for query in queries}
        finished = {query: threading.Event() for query in queries}
        observed = {}
        original = app._run

        def run(job, config, storage, update):
            query = job['query']
            observed[query] = {'cancel': config['_cancel_event'], 'concurrency': job['search_concurrency']}
            update(state='running', results=[{'id': job['id'], 'title': query}])
            started[query].set()
            release[query].wait(3)
            update(state='done', results=[{'id': job['id'], 'title': query + '最终结果'}])

        def tracked(job_id, config):
            query = app.get_job(job_id)['query']
            try:
                return original(job_id, config)
            finally:
                finished[query].set()

        ids = []
        with patch('search_app.server.run_search', side_effect=run), patch.object(app, '_run', side_effect=tracked):
            try:
                for index, query in enumerate(queries):
                    with self.manager.work_slot(session):
                        ids.append(app.create_job({'query': query, 'max_rounds': 1, 'use_ai': False,
                                                   'search_concurrency': index + 2})['job_id'])
                    self.assertTrue(started[query].wait(1))
                self.assertFalse(any(event.is_set() for event in finished.values()))
                self.assertIsNot(observed[queries[0]]['cancel'], observed[queries[1]]['cancel'])
                self.assertEqual([observed[query]['concurrency'] for query in queries], [2, 3])
                with self.assertRaises(ValueError):
                    app.create_job({'query': '第三个公开检索', 'max_rounds': 1, 'use_ai': False})
                app.save_config({'search_concurrency': 4})
                self.assertEqual(app.get_job(ids[0])['search_concurrency'], 2)
                app.stop_job(ids[0])
                self.assertTrue(observed[queries[0]]['cancel'].is_set())
                self.assertFalse(observed[queries[1]]['cancel'].is_set())
                self.assertEqual(app.get_job(ids[1])['state'], 'running')
                release[queries[0]].set()
                self.assertTrue(finished[queries[0]].wait(1))
                self.assertEqual(app.get_job(ids[0])['state'], 'stopped')
                self.assertEqual(app.get_job(ids[0])['results'][0]['title'], queries[0])
                self.assertEqual(app.get_job(ids[1])['results'][0]['title'], queries[1])
                release[queries[1]].set()
                self.assertTrue(finished[queries[1]].wait(1))
                self.assertEqual(app.get_job(ids[1])['state'], 'done')
                self.assertEqual(app.get_job(ids[1])['results'][0]['title'], queries[1] + '最终结果')
                self.assertEqual(app.storage.get_job(ids[0])['state'], 'stopped')
                self.assertEqual(app.storage.get_job(ids[1])['state'], 'done')
            finally:
                for event in release.values():
                    event.set()
                for query in queries:
                    if started[query].is_set():
                        finished[query].wait(2)

    def test_continue_accepts_public_boundary_and_preserves_other_jobs(self):
        _, session = self.create()
        app = session.app
        with patch('search_app.server.threading.Thread'):
            job_id = app.create_job({'query': '公开检索资料', 'max_rounds': 1,
                                     'search_concurrency': 1, 'use_ai': False})['job_id']
            app.stop_job(job_id)
            other_id = app.create_job({'query': '另一个公开检索', 'max_rounds': 1,
                                       'search_concurrency': 2, 'use_ai': False})['job_id']
            app.continue_job(job_id, {'max_rounds': 1, 'search_concurrency': 4})
        self.assertEqual(app.get_job(job_id)['search_concurrency'], 4)
        self.assertEqual(app.get_job(other_id)['search_concurrency'], 2)
        self.assertFalse(app.controls[other_id].is_set())

    def test_expiry_revokes_token_cancels_work_and_forbids_late_disk_writes(self):
        response, session = self.create()
        app = session.app
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        observed = {}
        def blocked(job, config, storage, update):
            observed['cancel'] = config['_cancel_event']
            update(state='running')
            started.set()
            release.wait(2)
            try:
                update(state='done', results=[{'title': 'late'}])
                storage.save_job({'id': job['id'], 'query': job['query'], 'state': 'done', 'results': []})
            except PublicAccessError:
                observed['late_write_denied'] = True
            finally:
                finished.set()
        with patch('search_app.server.run_search', side_effect=blocked):
            created = app.create_job({'query': '公开测试', 'max_rounds': 1})
            self.assertTrue(started.wait(1))
            directory = app.data_dir
            self.now[0] += 3601
            with self.assertRaises(PublicAccessError) as error:
                self.manager.authenticate(response['access_token'])
            self.assertEqual(error.exception.status, 401)
            self.assertTrue(observed['cancel'].is_set())
            self.assertTrue(app.lifetime_cancel.is_set())
            self.assertEqual(app.jobs[created['job_id']]['state'], 'stopped')
            self.assertFalse(directory.exists())
            release.set()
            self.assertTrue(finished.wait(1))
        self.assertTrue(observed['late_write_denied'])
        self.assertFalse(directory.exists())
        self.assertEqual(app.jobs[created['job_id']]['results'], [])

    def test_explicit_revoke_cannot_reopen_deleted_workspace(self):
        response, session = self.create()
        directory = session.app.data_dir
        self.manager.revoke(session)
        with self.assertRaises(PublicAccessError):
            self.manager.authenticate(response['access_token'])
        with self.assertRaises(PublicAccessError):
            session.app.storage.add_document('late', '', 'late text', 'web')
        self.assertFalse(directory.exists())

    def test_document_count_budget_and_delete_reclaims_capacity(self):
        _, session = self.create()
        storage = session.app.storage
        documents = [storage.add_document(str(index), '', '资料文字', 'web') for index in range(20)]
        with self.assertRaises(PublicAccessError) as error:
            storage.add_document('超额', '', '资料文字', 'web')
        self.assertEqual(error.exception.status, 400)
        self.assertEqual(len(storage.list_documents()), 20)
        self.assertTrue(storage.delete_document(documents[0]['id']))
        storage.add_document('回收后导入', '', '资料文字', 'web')
        self.assertEqual(len(storage.list_documents()), 20)

    def test_document_byte_budget_counts_utf8_title_url_and_body_and_reclaims(self):
        _, session = self.create()
        storage = session.app.storage
        title, url = '资料标题', 'https://example.com/资料'
        overhead = len(title.encode('utf-8')) + len(url.encode('utf-8'))
        # Exactly fill the budget with a Chinese UTF-8 prefix and ASCII padding.
        body = '中文' + 'a' * (2_000_000 - overhead - len('中文'.encode('utf-8')))
        document = storage.add_document(title, url, body, 'web')
        with self.assertRaises(PublicAccessError) as error:
            storage.add_document('x', '', '', 'web')
        self.assertEqual(error.exception.status, 400)
        self.assertEqual(len(storage.list_documents()), 1)
        storage.delete_document(document['id'])
        storage.add_document('新资料', '', '删除旧资料后可正常导入。', 'web')
        self.assertEqual(len(storage.list_documents()), 1)
        with self.assertRaises(PublicAccessError):
            storage.add_document('超大', '', '中' * 666_667, 'web')

    def test_concurrent_imports_cannot_race_past_document_count_budget(self):
        _, session = self.create()
        storage = session.app.storage
        for index in range(19):
            storage.add_document(str(index), '', '资料文字', 'web')
        barrier = threading.Barrier(6)
        def attempt(index):
            barrier.wait(2)
            try:
                storage.add_document('并发' + str(index), '', '资料文字', 'web')
                return True
            except PublicAccessError:
                return False
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(attempt, range(6)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(storage.list_documents()), 20)

    def test_concurrent_imports_cannot_race_past_total_byte_budget(self):
        _, session = self.create()
        storage = session.app.storage
        storage.add_document('x', '', 'a' * (2_000_000 - 1 - 101), 'web')
        barrier = threading.Barrier(2)
        def attempt(_):
            barrier.wait(2)
            try:
                storage.add_document('x', '', 'b' * 100, 'web')
                return True
            except PublicAccessError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(storage.list_documents()), 2)

    def test_admin_storage_does_not_inherit_visitor_document_limits(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            admin = App(directory)
            for index in range(21):
                admin.storage.add_document(str(index), '', '资料文字', 'web')
            self.assertEqual(len(admin.storage.list_documents()), 21)
            admin.storage.add_document('大资料', '', 'a' * 2_000_001, 'web')
            self.assertEqual(len(admin.storage.list_documents()), 22)

    def test_creation_pool_and_request_rate_are_bounded(self):
        self.manager.limits['max_sessions'] = 1
        response, session = self.create()
        with self.assertRaises(PublicAccessError) as error:
            self.create(peer='other-peer')
        self.assertEqual(error.exception.status, 429)
        self.manager.limits['requests_per_minute'] = 2
        self.manager.authenticate(response['access_token'])
        with self.assertRaises(PublicAccessError) as error:
            self.manager.authenticate(response['access_token'])
        self.assertEqual(error.exception.status, 429)
        self.now[0] += 61
        self.assertIs(self.manager.authenticate(response['access_token']), session)

    def test_expensive_limits_and_global_parallel_limit(self):
        _, first = self.create()
        _, second = self.create()
        self.manager.limits['expensive_requests_per_minute'] = 1
        self.manager.limits['max_concurrent_jobs'] = 1
        with self.manager.work_slot(first):
            with self.assertRaises(PublicAccessError) as error:
                with self.manager.work_slot(second):
                    self.fail('Second concurrent work should not run')
            self.assertEqual(error.exception.status, 429)
        with self.assertRaises(PublicAccessError):
            with self.manager.work_slot(first):
                self.fail('Rate limit should block another operation')

    def test_revoke_and_recreate_cannot_bypass_peer_creation_rate(self):
        for _ in range(10):
            _, session = self.create(peer='reverse-proxy-peer')
            self.manager.revoke(session)
        with self.assertRaises(PublicAccessError) as error:
            self.create(peer='reverse-proxy-peer')
        self.assertEqual(error.exception.status, 429)
        _, other = self.create(peer='different-direct-peer')
        self.manager.revoke(other)
        self.now[0] += 61
        self.assertEqual(self.create(peer='reverse-proxy-peer')[0]['mode'], 'custom')

    def test_revoke_cancels_summary_and_rejects_its_late_completion(self):
        _, session = self.create('shared')
        app = session.app
        job = {'id': 'b' * 32, 'query': '公开资料', 'state': 'done', 'results': [{'id': 'result1', 'title': '公开资料'}],
               'created_at': '2026-10-08', 'ai_summary': {'state': 'empty', 'points': []}}
        app.storage.save_job(job)
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        observed = {}
        def blocked(query, results, config, *args):
            observed['cancel'] = config['_cancel_event']
            started.set()
            release.wait(2)
            return {'state': 'ready', 'points': [{'text': 'late summary'}]}
        original = app._summarize
        def tracked(*args):
            try:
                return original(*args)
            finally:
                finished.set()
        with patch('search_app.server.summarize_results', side_effect=blocked), patch.object(app, '_summarize', side_effect=tracked):
            app.create_summary(job['id'])
            self.assertTrue(started.wait(1))
            directory = app.data_dir
            self.manager.revoke(session)
            self.assertTrue(observed['cancel'].is_set())
            release.set()
            self.assertTrue(finished.wait(1))
            self.assertFalse(directory.exists())
            self.assertFalse(app.summary_jobs)
            self.assertNotEqual(app.jobs[job['id']]['ai_summary']['state'], 'ready')


class PublicHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        with patch.dict('os.environ', {}, clear=True):
            self.admin = App(self.directory.name)
        self.admin.storage.add_document('管理员资料', '', '这是一段不得提供给访客的管理员测试资料。', 'web')
        self.manager = PublicSessions(OWNER, start_janitor=False)
        policy = TransportPolicy.from_environment('127.0.0.1', ENV)
        self.server = LocalHTTPServer(('127.0.0.1', 0), Handler, transport=policy)
        self.server.app = self.admin
        self.server.public_sessions = self.manager
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)
        self.manager.close()
        self.directory.cleanup()

    def request(self, method, path, data=None, token=None, headers=None):
        values = {'Host': 'public.example', 'Origin': ORIGIN}
        if token:
            values['Authorization'] = 'Bearer ' + token
        values.update(headers or {})
        body = json.dumps(data).encode() if data is not None else None
        if body is not None:
            values['Content-Type'] = 'application/json'
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body, values)
            response = connection.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), json.loads(raw) if raw else None
        finally:
            connection.close()

    def create(self, mode='custom'):
        status, _, response = self.request('POST', '/api/session', {'mode': mode})
        self.assertEqual(status, 201)
        return response

    def test_public_discovery_session_creation_and_cors(self):
        status, headers, health = self.request('GET', '/api/health')
        self.assertEqual(status, 200)
        self.assertTrue(health['public_mode'])
        self.assertTrue(health['shared_available'])
        self.assertTrue(health['session_required'])
        self.assertEqual(health['public_limits']['max_rounds'], 3)
        self.assertEqual(health['public_limits']['max_session_jobs'], 2)
        self.assertEqual(health['public_limits']['search_concurrency'], 4)
        self.assertEqual(health['public_limits']['max_documents'], 20)
        self.assertEqual(health['public_limits']['max_document_bytes'], 2_000_000)
        self.assertNotIn(OWNER['AI_API_KEY'], json.dumps(health))
        self.assertEqual(headers['Access-Control-Allow-Origin'], ORIGIN)
        self.assertEqual(self.request('GET', '/api/config')[0], 401)
        session = self.create('shared')
        status, _, config = self.request('GET', '/api/config', token=session['access_token'])
        self.assertEqual(status, 200)
        self.assertEqual(config['api_mode'], 'shared')
        self.assertTrue(config['ai_config_readonly'])
        self.assertNotIn(OWNER['AI_API_KEY'], json.dumps(config))
        self.assertNotIn(ADMIN, json.dumps(session))
        self.assertEqual(self.request('GET', '/api/config', token=session['session_id'])[0], 401)
        info = self.request('GET', '/api/session', token=session['access_token'])[2]
        self.assertNotIn('access_token', info)
        self.assertEqual(info['mode'], 'shared')

    def test_session_creation_still_checks_host_origin_and_preflight(self):
        for headers in ({'Host': 'attacker.example'}, {'Origin': 'https://attacker.example'}):
            self.assertEqual(self.request('POST', '/api/session', {}, headers=headers)[0], 403)
        self.assertEqual(len(self.manager.sessions), 0)
        status, headers, _ = self.request('OPTIONS', '/api/session', headers={'Access-Control-Request-Method': 'POST',
                                                                                 'Access-Control-Request-Headers': 'Content-Type'})
        self.assertEqual(status, 204)
        self.assertEqual(headers['Access-Control-Allow-Origin'], ORIGIN)

    def test_visitors_cannot_read_admin_or_other_visitor_documents_and_job_ids(self):
        first, second = self.create(), self.create()
        token_a, token_b = first['access_token'], second['access_token']
        self.assertEqual(self.request('GET', '/api/library', token=token_a)[2]['items'], [])
        self.assertEqual(len(self.request('GET', '/api/library', token=ADMIN)[2]['items']), 1)
        status, _, doc = self.request('POST', '/api/import', {'title': '访客A资料', 'text': '这是只属于访客A的公开测试文本。'}, token_a)
        self.assertEqual(status, 201)
        self.assertEqual(self.request('GET', '/api/library', token=token_b)[2]['items'], [])
        self.assertEqual(self.request('DELETE', '/api/library/' + doc['id'], token=token_b)[0], 404)
        app_a = self.manager.authenticate(token_a).app
        job = {'id': 'a' * 32, 'query': '访客一历史', 'state': 'done', 'results': [], 'created_at': '2026-10-08'}
        app_a.storage.save_job(job)
        app_a.jobs[job['id']] = job
        listing = self.request('GET', '/api/jobs', token=token_a)[2]
        self.assertEqual([item['id'] for item in listing['items']], [job['id']])
        self.assertEqual(listing['max_active_jobs'], 2)
        self.assertEqual(listing['max_search_concurrency'], 4)
        self.assertEqual(self.request('GET', '/api/jobs', token=token_b)[2]['items'], [])
        self.assertEqual(len(self.request('GET', '/api/history', token=token_a)[2]['items']), 1)
        self.assertEqual(self.request('GET', '/api/history', token=token_b)[2]['items'], [])
        self.assertEqual(self.request('GET', '/api/jobs/' + job['id'], token=token_b)[0], 404)
        for action in ('stop', 'continue', 'summarize'):
            self.assertEqual(self.request('POST', '/api/jobs/' + job['id'] + '/' + action, {}, token_b)[0], 404)

    def test_model_test_and_mode_switch_never_pair_owner_key_with_custom_url(self):
        session = self.create('shared')
        token = session['access_token']
        with patch('search_app.server.test_connection', return_value={'ok': True}) as model_test:
            self.assertEqual(self.request('POST', '/api/ai/test', {'base_url': 'https://attacker.example/v1'}, token)[0], 200)
            shared_config = model_test.call_args.args[0]
            self.assertEqual(shared_config['api_key'], OWNER['AI_API_KEY'])
            self.assertEqual(shared_config['base_url'], OWNER['AI_BASE_URL'])
            self.assertTrue(shared_config['_public_network'])
            self.assertIn('_cancel_event', shared_config)
            self.assertEqual(self.request('PUT', '/api/config', {'base_url': 'https://attacker.example/v1'}, token)[0], 403)
            self.assertEqual(self.request('PUT', '/api/config', {'clear_secrets': ['api_key']}, token)[0], 403)
            changed = self.request('PUT', '/api/session', {'mode': 'custom'}, token)
            self.assertEqual(changed[0], 200)
            self.assertEqual(changed[2]['mode'], 'custom')
            self.assertEqual(self.request('PUT', '/api/config', {'base_url': 'https://visitor.example/v1', 'api_key': 'visitor-only-key', 'model': 'visitor-model'}, token)[0], 200)
            self.assertEqual(self.request('POST', '/api/ai/test', {}, token)[0], 200)
            custom_config = model_test.call_args.args[0]
            self.assertEqual(custom_config['api_key'], 'visitor-only-key')
            self.assertEqual(custom_config['base_url'], 'https://visitor.example/v1')
            self.assertNotIn(OWNER['AI_API_KEY'], str(custom_config))
        self.assertFalse(self.manager.authenticate(token).app.config_path.exists())

    def test_logout_revokes_only_own_session_and_keeps_admin_workspace(self):
        first, second = self.create(), self.create()
        self.assertEqual(self.request('DELETE', '/api/session', token=first['access_token'])[0], 200)
        status, headers, _ = self.request('GET', '/api/config', token=first['access_token'])
        self.assertEqual(status, 401)
        self.assertEqual(headers['Access-Control-Allow-Origin'], ORIGIN)
        self.assertEqual(self.request('GET', '/api/config', token=second['access_token'])[0], 200)
        self.assertEqual(len(self.admin.storage.list_documents()), 1)
        self.assertEqual(self.request('PUT', '/api/session', {'mode': 'custom'}, ADMIN)[0], 403)

    def raw_socket(self):
        connection = socket.create_connection(('127.0.0.1', self.server.server_port), timeout=1)
        connection.settimeout(1)
        return connection

    def read_until_closed(self, connection):
        received = bytearray()
        try:
            while True:
                block = connection.recv(4096)
                if not block:
                    break
                received.extend(block)
        except ConnectionResetError:
            pass
        return bytes(received)

    def test_public_request_line_and_partial_headers_have_hard_deadline(self):
        prefixes = [b'POST /api/session HTTP/1.1',
                    b'POST /api/session HTTP/1.1\r\nHost: public.example\r\nX-Half: unfinished']
        for prefix in prefixes:
            with self.subTest(prefix=prefix), patch('search_app.server.PUBLIC_READ_TIMEOUT', 0.12):
                connection = self.raw_socket()
                try:
                    start = time.monotonic()
                    connection.sendall(prefix)
                    self.read_until_closed(connection)
                    self.assertLess(time.monotonic() - start, 0.8)
                finally:
                    connection.close()
        self.assertEqual(len(self.manager.sessions), 0)

    def test_dripping_headers_and_body_are_cut_off_despite_frequent_bytes(self):
        headers = ('POST /api/session HTTP/1.1\r\nHost: public.example\r\nOrigin: ' + ORIGIN + '\r\n').encode()
        prefixes = [headers + b'X-Slow: ',
                    headers + b'Content-Type: application/json\r\nContent-Length: 50000\r\n\r\n{']
        for prefix in prefixes:
            with self.subTest(phase='body' if prefix.endswith(b'{') else 'headers'), patch('search_app.server.PUBLIC_READ_TIMEOUT', 0.16):
                connection = self.raw_socket()
                stop = threading.Event()
                writes = []
                def drip():
                    while not stop.wait(0.02):
                        try:
                            connection.sendall(b' ')
                            writes.append(1)
                        except OSError:
                            break
                try:
                    connection.sendall(prefix)
                    writer = threading.Thread(target=drip, daemon=True)
                    writer.start()
                    start = time.monotonic()
                    self.read_until_closed(connection)
                    elapsed = time.monotonic() - start
                    self.assertGreaterEqual(len(writes), 3)
                    self.assertLess(elapsed, 0.8)
                finally:
                    stop.set()
                    connection.close()
                    writer.join(1)
        self.assertEqual(len(self.manager.sessions), 0)

    def test_partial_json_body_never_creates_session_after_deadline(self):
        # Even an otherwise-valid JSON prefix must not be accepted as the declared full body.
        headers = ('POST /api/session HTTP/1.1\r\nHost: public.example\r\nOrigin: ' + ORIGIN
                   + '\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{}').encode()
        with patch('search_app.server.PUBLIC_READ_TIMEOUT', 0.12):
            connection = self.raw_socket()
            try:
                start = time.monotonic()
                connection.sendall(headers)
                self.read_until_closed(connection)
                self.assertLess(time.monotonic() - start, 0.8)
            finally:
                connection.close()
        self.assertEqual(len(self.manager.sessions), 0)

    def test_header_deadline_is_cancelled_and_body_gets_its_own_budget(self):
        first = ('POST /api/session HTTP/1.1\r\nHost: public.example\r\nOrigin: ' + ORIGIN + '\r\n').encode()
        with patch('search_app.server.PUBLIC_READ_TIMEOUT', 0.3):
            connection = self.raw_socket()
            try:
                connection.sendall(first)
                time.sleep(0.18)
                connection.sendall(b'Content-Type: application/json\r\nContent-Length: 2\r\n\r\n')
                time.sleep(0.18)
                connection.sendall(b'{}')
                response = self.read_until_closed(connection)
                self.assertIn(b'201 Created', response)
            finally:
                connection.close()
        self.assertEqual(len(self.manager.sessions), 1)

    def test_public_connection_cap_rejects_excess_and_returns_slot_after_close(self):
        self.server._public_connections = threading.BoundedSemaphore(1)
        began = threading.Event()
        admitted = []
        original_setup = Handler.setup
        def setup(handler):
            original_setup(handler)
            admitted.append(1)
            began.set()
        def health_status():
            try:
                return self.request('GET', '/api/health')[0]
            except OSError:
                # A rejected socket may reset before the tiny 503 is delivered on Windows.
                return None
        with patch.object(Handler, 'setup', setup), patch('search_app.server.PUBLIC_READ_TIMEOUT', 0.5):
            blocked = self.raw_socket()
            try:
                self.assertTrue(began.wait(0.5))
                self.assertIn(health_status(), (503, None))
                self.assertEqual(len(admitted), 1)
            finally:
                blocked.close()
            deadline = time.monotonic() + 0.8
            while True:
                status = health_status()
                if status == 200 or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            self.assertEqual(status, 200)

    def test_private_mode_does_not_add_read_timers_or_socket_timeout(self):
        self.server.transport = TransportPolicy()
        self.server._public_connections = None
        observed = []
        original = Handler.read_json
        def read_json(handler):
            observed.append(handler.connection.gettimeout())
            return original(handler)
        host = '127.0.0.1:' + str(self.server.server_port)
        with patch.object(Handler, 'read_json', read_json), patch('search_app.server.threading.Timer') as timer:
            status, _, _ = self.request('PUT', '/api/config', {'model': 'private-model'},
                                        headers={'Host': host, 'Origin': 'http://' + host})
            self.assertEqual(status, 200)
            timer.assert_not_called()
        self.assertEqual(observed, [None])


if __name__ == '__main__':
    unittest.main()
