import copy
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from search_app.ai import AIError, chat
from search_app.server import App
from search_app.engine import build_tasks, fallback_plan


class ControlsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = App(self.directory.name)
        self.app.config['api_key'] = 'test-key-no-network'

    def tearDown(self):
        self.app.storage.close()
        self.directory.cleanup()

    def create_without_thread(self, **values):
        with patch('search_app.server.threading.Thread'):
            created = self.app.create_job({'query': '合肥附近餐馆', **values})
        return created['job_id']

    def test_expanded_platforms_and_custom_sites_round_trip(self):
        platforms = ['wechat', 'meituan', 'dianping', 'douyin', 'tieba', 'douban']
        sites = [{'name': 'Python 文档', 'domain': 'www.python.org', 'search_url': 'https://www.python.org/search/?q={query}'}]
        public = self.app.save_config({'custom_sites': sites})
        self.assertEqual(public['custom_sites'], sites)
        job_id = self.create_without_thread(platforms=platforms, max_rounds=0)
        job = self.app.get_job(job_id)
        self.assertEqual(job['platforms'], platforms)
        self.assertEqual(job['custom_sites'], sites)
        self.assertEqual(job['max_rounds'], 0)
        self.assertTrue(job['adaptive'])
        self.app.stop_job(job_id)
        custom_only = self.create_without_thread(platforms=[], custom_sites=['python.org'])
        self.assertEqual(self.app.get_job(custom_only)['platforms'], [])

    def test_invalid_budgets_and_scopes_fail_before_network(self):
        for value in (True, -1, 13, '3'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.create_without_thread(max_rounds=value)
        with self.assertRaises(ValueError):
            self.create_without_thread(platforms=[], custom_sites=[])
        with self.assertRaises(ValueError):
            self.create_without_thread(custom_sites=['127.0.0.1'])

    def test_quick_search_covers_every_selected_platform(self):
        selected = ['bilibili', 'xiaohongshu', 'zhihu', 'wechat', 'meituan', 'dianping', 'douyin', 'tieba', 'douban', 'web']
        tasks = build_tasks(fallback_plan('合肥附近餐馆'), selected, 'quick', {})
        covered = {scope for _, _, scopes in tasks for scope in scopes}
        self.assertEqual(covered, set(selected))

    def test_immediate_summary_persists_new_results_before_changing_epoch(self):
        job_id = self.create_without_thread(adaptive=False)
        self.app.jobs[job_id].update(state='done', results=[{'id': 'fresh-not-yet-saved'}])
        self.assertIsNone(self.app.storage.get_job(job_id))
        with patch('search_app.server.threading.Thread'):
            self.app.create_summary(job_id)
        saved = self.app.storage.get_job(job_id)
        self.assertEqual(saved['state'], 'done')
        self.assertEqual(saved['results'][0]['id'], 'fresh-not-yet-saved')
        self.assertNotEqual(saved.get('ai_summary', {}).get('state'), 'running')

    def test_stop_is_immediate_persisted_and_idempotent(self):
        job_id = self.create_without_thread()
        self.app.jobs[job_id]['results'] = [{'id': 'existing', 'title': '已找到的结果'}]
        cancel = self.app.controls[job_id]
        start = time.monotonic()
        self.app.stop_job(job_id)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertTrue(cancel.is_set())
        self.assertEqual(self.app.get_job(job_id)['state'], 'stopped')
        self.assertEqual(self.app.storage.get_job(job_id)['results'][0]['id'], 'existing')
        before = self.app.get_job(job_id)
        self.app.stop_job(job_id)
        self.assertEqual(before, self.app.get_job(job_id))

    def test_late_worker_cannot_overwrite_stopped_or_resumed_job(self):
        ready, release, finished = threading.Event(), threading.Event(), threading.Event()

        def slow_search(job, config, storage, update):
            try:
                update(state='running', results=[{'id': 'kept'}], rounds=[{'number': 1, 'queries': []}], round=1)
                ready.set()
                release.wait(3)
                update(state='done', results=[{'id': 'late-do-not-apply'}])
            finally:
                finished.set()

        with patch('search_app.server.run_search', side_effect=slow_search):
            job_id = self.app.create_job({'query': '合肥附近餐馆'})['job_id']
            self.assertTrue(ready.wait(2))
            self.app.stop_job(job_id)
            original_cancel = self.app.controls[job_id]
            with patch('search_app.server.threading.Thread'):
                self.app.continue_job(job_id, {'max_rounds': 6})
            self.assertIsNot(self.app.controls[job_id], original_cancel)
            self.assertFalse(self.app.controls[job_id].is_set())
            release.set()
            self.assertTrue(finished.wait(2))
            # Wait for the old worker's finally block to observe the new epoch.
            with self.app.lock:
                resumed = self.app.get_job(job_id)
            self.assertEqual(resumed['results'], [{'id': 'kept'}])
            self.assertEqual(resumed['state'], 'queued')
            self.assertEqual(resumed['round'], 1)
            self.assertEqual(resumed['max_rounds'], 6)
            self.assertEqual(self.app.storage.get_job(job_id)['state'], 'stopped')

    def test_stop_preserves_completed_requests_and_marks_unfinished_retryable(self):
        job_id = self.create_without_thread()
        self.app.jobs[job_id].update(state='running', rounds=[{
            'number': 1, 'state': 'running', 'queries': [
                {'query': '已完成', 'status': 'completed'},
                {'query': '在途', 'status': 'running'},
                {'query': '排队', 'status': 'queued'},
            ],
        }])
        self.app.stop_job(job_id)
        record = self.app.storage.get_job(job_id)['rounds'][0]
        self.assertEqual(record['state'], 'stopped')
        self.assertEqual([item['status'] for item in record['queries']], ['completed', 'cancelled', 'cancelled'])

    def test_round_progress_is_recoverable_without_ai(self):
        job_id = self.create_without_thread(use_ai=False)
        saved = []

        def search(job, config, storage, update):
            update(state='running', rounds=[{'number': 1, 'state': 'running', 'queries': [
                {'status': 'running', 'query': '未完成'}]}], results=[{'id': 'preserved'}],
                ai_summary={'state': 'disabled'})
            saved.append(storage.get_job(job_id))
            update(state='awaiting_user', stop_reason='round_limit')

        with patch('search_app.server.run_search', side_effect=search):
            self.app._run(job_id, {**self.app.config, '_run_token': self.app.epochs[job_id]})
        self.assertEqual(saved[0]['state'], 'awaiting_user')
        self.assertEqual(saved[0]['results'], [{'id': 'preserved'}])
        self.assertEqual(saved[0]['rounds'][0]['queries'][0]['status'], 'cancelled')
        self.assertEqual(saved[0]['ai_summary']['state'], 'disabled')

    def test_paused_search_is_restored_and_can_be_summarized(self):
        job_id = self.create_without_thread()
        self.app.jobs[job_id].update(state='awaiting_user', stage='waiting', stop_reason='round_limit',
                                   results=[{'id': 'kept'}], rounds=[{'number': 1, 'queries': []}])
        self.app.storage.save_job(self.app.jobs[job_id])
        restored = App(self.directory.name)
        restored.config['api_key'] = 'test-key'
        self.assertEqual(restored.get_job(job_id)['stop_reason'], 'round_limit')
        with patch('search_app.server.threading.Thread'):
            restored.create_summary(job_id)
        restored.stop_job(job_id)
        self.assertEqual(restored.get_job(job_id)['state'], 'awaiting_user')
        self.assertEqual(restored.get_job(job_id)['ai_summary']['state'], 'disabled')
        restored.storage.close()

    def test_stopped_manual_summary_keeps_previous_answer_and_ignores_late_reply(self):
        job_id = self.create_without_thread()
        old = {'state': 'ready', 'points': [{'text': '原先的总结', 'citations': []}]}
        self.app.jobs[job_id].update(state='done', results=[{'id': 'kept'}], ai_summary=copy.deepcopy(old))
        with patch('search_app.server.threading.Thread') as factory:
            self.app.create_summary(job_id)
            job, config = factory.call_args.kwargs['args']
        self.app.stop_job(job_id)
        with patch('search_app.server.summarize_results', return_value={'state': 'ready', 'points': [{'text': '晚到回复'}]}):
            self.app._summarize(job, config)
        self.assertEqual(self.app.get_job(job_id)['ai_summary'], old)
        self.assertEqual(self.app.get_job(job_id)['state'], 'done')


class AICancellationTests(unittest.TestCase):
    def test_cancel_between_dispatch_and_thread_start_sends_no_request(self):
        cancel = threading.Event()

        class DelayedStart:
            def __init__(self, target, **kwargs):
                self.target = target

            def start(self):
                cancel.set()
                self.target()

        with patch('search_app.ai.threading.Thread', DelayedStart), patch('search_app.ai._chat_request') as request:
            with self.assertRaises(AIError):
                chat({'_cancel_event': cancel}, 'system', 'data')
        request.assert_not_called()

    def test_stop_does_not_wait_for_an_already_issued_ai_request(self):
        cancel, issued, release, returned = (threading.Event() for _ in range(4))
        outcome = []

        def request(*args):
            issued.set()
            release.wait(3)
            return 'late response'

        def caller():
            try:
                outcome.append(chat({'_cancel_event': cancel}, 'system', 'data'))
            except AIError:
                outcome.append('cancelled')
            finally:
                returned.set()

        with patch('search_app.ai._chat_request', side_effect=request):
            worker = threading.Thread(target=caller)
            worker.start()
            self.assertTrue(issued.wait(1))
            cancel.set()
            self.assertTrue(returned.wait(0.6))
            self.assertEqual(outcome, ['cancelled'])
            release.set()
            worker.join(1)


if __name__ == '__main__':
    unittest.main()
