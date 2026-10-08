import copy
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from search_app.engine import run_search, fallback_plan
from search_app.server import App, Handler, LocalHTTPServer

JOB_ID = 'e' * 32
RESULT = {'id': 'food-1', 'title': '测试餐馆原文', 'url': 'https://example.org/food',
          'snippet': '合肥中科大附近的测试面馆推荐牛肉面。', 'body': '', 'content_level': 'snippet',
          'source': 'test', 'platform': 'web', 'match': 'partial', 'score': 55, 'evidence': []}
READY = {'state': 'ready', 'points': [{'text': '来源推荐测试面馆的牛肉面。', 'citations': []}],
         'limitations': [], 'source_count': 1, 'considered_count': 1, 'message': '已完成'}


def saved_job():
    return {'id': JOB_ID, 'query': '合肥中科大附近餐馆', 'state': 'done', 'stage': 'done',
            'results': [copy.deepcopy(RESULT)], 'use_ai': False, 'warnings': [],
            'created_at': '2026-10-08T00:00:00+00:00', 'summary': '找到 1 条候选。'}


class SummaryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = App(self.directory.name)
        self.app.config['api_key'] = 'test-key-not-a-real-key'
        self.app.storage.save_job(saved_job())

    def tearDown(self):
        self.app.storage.close()
        self.directory.cleanup()

    def test_existing_history_can_be_summarized_without_retrieval(self):
        before = self.app.get_job(JOB_ID)
        with patch('search_app.server.threading.Thread') as thread, patch('search_app.server.summarize_results', return_value=READY) as summarize:
            response = self.app.create_summary(JOB_ID)
            self.assertEqual(response['job_id'], JOB_ID)
            self.assertEqual(self.app.get_job(JOB_ID)['ai_summary']['state'], 'running')
            self.assertEqual(self.app.get_job(JOB_ID)['results'], before['results'])
            snapshot, config = thread.call_args.kwargs['args']
            self.app._summarize(snapshot, config)
            summarize.assert_called_once()
        self.assertEqual(self.app.get_job(JOB_ID)['ai_summary'], READY)
        self.assertEqual(self.app.storage.get_job(JOB_ID)['ai_summary'], READY)
        self.assertEqual(self.app.storage.get_job(JOB_ID)['results'], before['results'])
        self.assertEqual(self.app.summary_jobs, set())
        # A fresh app restores both summary and results without a migration.
        restored = App(self.directory.name)
        self.assertEqual(restored.get_job(JOB_ID)['ai_summary']['state'], 'ready')
        restored.storage.close()

    def test_double_click_reuses_active_summary(self):
        with patch('search_app.server.threading.Thread') as thread:
            first = self.app.create_summary(JOB_ID)
            second = self.app.create_summary(JOB_ID)
        self.assertEqual(first, second)
        self.assertEqual(thread.call_count, 1)
        self.assertNotIn('ai_summary', self.app.storage.get_job(JOB_ID))

    def test_search_completion_cannot_overwrite_a_new_manual_summary(self):
        search_saving = threading.Event()
        release_save = threading.Event()
        summary_saved = threading.Event()
        original_save = self.app.storage.save_job
        self.app.jobs[JOB_ID] = saved_job()

        def ordered_save(job):
            if 'ai_summary' not in job:
                search_saving.set()
                if not release_save.wait(3):
                    raise RuntimeError('test save gate timed out')
            original_save(job)
            if job.get('ai_summary', {}).get('state') == 'ready':
                summary_saved.set()

        with patch('search_app.server.run_search'), \
             patch('search_app.server.summarize_results', return_value=READY), \
             patch.object(self.app.storage, 'save_job', side_effect=ordered_save):
            worker = threading.Thread(target=self.app._run, args=(JOB_ID, {}))
            worker.start()
            self.assertTrue(search_saving.wait(2))
            requester = threading.Thread(target=self.app.create_summary, args=(JOB_ID,))
            requester.start()
            release_save.set()
            worker.join(3)
            requester.join(3)
            self.assertTrue(summary_saved.wait(2))
        self.assertEqual(self.app.storage.get_job(JOB_ID)['ai_summary'], READY)

    def test_failed_summary_preserves_results_and_can_retry(self):
        with patch('search_app.server.threading.Thread') as thread:
            self.app.create_summary(JOB_ID)
            args = thread.call_args.kwargs['args']
        with patch('search_app.server.summarize_results', side_effect=RuntimeError('sensitive upstream detail')):
            self.app._summarize(*args)
        job = self.app.get_job(JOB_ID)
        self.assertEqual(job['state'], 'done')
        self.assertEqual(job['results'], saved_job()['results'])
        self.assertEqual(job['ai_summary']['state'], 'error')
        self.assertNotIn('sensitive', json.dumps(job))
        with patch('search_app.server.threading.Thread'):
            self.assertEqual(self.app.create_summary(JOB_ID)['job_id'], JOB_ID)

    def test_empty_no_key_and_missing_history_fail_before_work(self):
        with self.assertRaises(LookupError):
            self.app.create_summary('d' * 32)
        self.app.config['api_key'] = ''
        with self.assertRaisesRegex(ValueError, '密钥'):
            self.app.create_summary(JOB_ID)
        self.app.config['api_key'] = 'test-key'
        empty = saved_job()
        empty['results'] = []
        self.app.storage.save_job(empty)
        with self.assertRaisesRegex(ValueError, '没有'):
            self.app.create_summary(JOB_ID)

    def test_automatic_summary_sees_results_after_ranking_and_preserves_failures(self):
        source = {**RESULT, 'source': 'local', 'content_level': 'local'}
        self.app.storage.add_document(source['title'], source['url'], source['snippet'], 'web')
        job = {'query': '合肥中科大附近餐馆', 'platforms': ['web'], 'depth': 'quick', 'use_ai': True, 'fetch_pages': False}
        updates = []
        with patch('search_app.engine.make_plan', return_value=fallback_plan(job['query'])), \
             patch('search_app.engine.build_tasks', return_value=[]), \
             patch('search_app.engine.assess', return_value=0), \
             patch('search_app.engine.summarize_results', return_value=READY) as summarize:
            run_search(job, {}, self.app.storage, lambda **fields: updates.append(fields))
        self.assertEqual(updates[-1]['ai_summary'], READY)
        self.assertTrue(any(update.get('stage') == 'summarizing' and update.get('results') for update in updates))
        self.assertEqual(summarize.call_args.args[0], job['query'])
        self.assertEqual(updates[-1]['results'], summarize.call_args.args[1])
        with patch('search_app.engine.build_tasks', return_value=[]), patch('search_app.engine.summarize_results') as summarize:
            run_search({**job, 'use_ai': False}, {}, self.app.storage, lambda **fields: updates.append(fields))
        summarize.assert_not_called()
        self.assertEqual(updates[-1]['ai_summary']['state'], 'disabled')

    def test_summary_endpoint_and_cross_origin_guard(self):
        server = LocalHTTPServer(('127.0.0.1', 0), Handler)
        server.app = self.app
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}/api/jobs/{JOB_ID}/summarize'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            req = urllib.request.Request(url, data=b'{}', headers={'Content-Type': 'application/json', 'Origin': 'https://untrusted.example'})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                opener.open(req)
            self.assertEqual(caught.exception.code, 403)
            req = urllib.request.Request(url, data=b'{}', headers={'Content-Type': 'application/json'})
            with patch.object(self.app, 'create_summary', return_value={'job_id': JOB_ID}) as create:
                with opener.open(req) as response:
                    self.assertEqual(response.status, 202)
                    self.assertEqual(json.load(response)['job_id'], JOB_ID)
                create.assert_called_once_with(JOB_ID)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()
