import copy
import hashlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from search_app import adaptive
from search_app.engine import run_search, fallback_plan
from search_app.server import App


class Library:
    def search_documents(self, *args, **kwargs):
        return []


def report(number=1):
    return {'state': 'ready', 'round': number, 'progress': '已找到可核对的线索。',
            'findings': [], 'stats': {'new_results': 1, 'total_results': 1},
            'assessment': {'likelihood': 'medium', 'reason': '仍需补充条件。', 'blockers': [], 'next_steps': []}}


class ReportLifecycleTests(unittest.TestCase):
    def job(self, **changes):
        value = {'query': '合肥中科大附近餐馆', 'platforms': ['web'], 'custom_sites': [],
                 'adaptive': True, 'use_ai': True, 'fetch_pages': False, 'depth': 'quick', 'max_rounds': 2,
                 'round': 0, 'rounds': [], 'results': [], 'warnings': [], 'provider_status': []}
        value.update(changes)
        return value

    def run_adaptive(self, job, reporter, search=None, stop=None, on_update=None):
        state, updates = copy.deepcopy(job), []
        stop = stop or threading.Event()

        def update(**fields):
            state.update(copy.deepcopy(fields))
            updates.append(copy.deepcopy(fields))
            if on_update:
                on_update(state)

        def retrieval(provider, query, scope, limit, config):
            slug = hashlib.sha256(query.encode()).hexdigest()[:12]
            return {'results': [{'title': '合肥中科大附近餐馆评价', 'url': 'https://example.org/' + slug,
                                 'snippet': '合肥中科大附近餐馆的面条评价。', 'platform': 'web', 'source': 'bing'}],
                    'status': {'provider': provider, 'ok': True, 'count': 1}}

        with patch.object(adaptive.providers, 'available_providers', return_value=['bing']), \
             patch.object(adaptive.providers, 'search_provider', side_effect=search or retrieval), \
             patch('search_app.engine.make_plan', return_value=fallback_plan(job['query'])), \
             patch('search_app.engine.assess', return_value=0), \
             patch.object(adaptive, 'chat', return_value='{"directions":[]}'), \
             patch.object(adaptive, 'summarize_results', return_value={'state': 'ready', 'points': [], 'message': '已有总结'}), \
             patch.object(adaptive, 'build_progress_report', side_effect=reporter) as generate:
            adaptive.run_adaptive_search(job, {}, Library(), update, stop)
        return state, updates, generate

    def test_every_completed_round_reports_before_budget_pause(self):
        calls = []

        def generate(query, results, config, plan, record, statuses, previous):
            calls.append((record['number'], len(results), copy.deepcopy(previous)))
            self.assertEqual(record['state'], 'completed')
            self.assertTrue(config['use_ai'])
            self.assertIn(query, plan['must_have'])
            return report(record['number'])

        state, updates, _ = self.run_adaptive(self.job(), generate)
        self.assertEqual([item[0] for item in calls], [1, 2])
        self.assertEqual(calls[1][2]['round'], 1)
        self.assertEqual(state['stop_reason'], 'round_limit')
        self.assertEqual([item['report']['round'] for item in state['rounds']], [1, 2])
        self.assertEqual(state['progress_report']['round'], 2)
        self.assertEqual(sum(item.get('stage') == 'reporting' for item in updates), 2)

    def test_failed_sources_and_zero_results_still_receive_report(self):
        def failed(provider, *args):
            return {'results': [], 'status': {'provider': provider, 'ok': False, 'error': '访问失败'}}
        state, _, generate = self.run_adaptive(self.job(), lambda *args: report(args[4]['number']), search=failed)
        generate.assert_called_once()
        self.assertEqual(generate.call_args.args[1], [])
        self.assertEqual(state['stop_reason'], 'sources_unavailable')
        self.assertEqual(state['rounds'][0]['report']['state'], 'ready')

    def test_report_error_does_not_abort_remaining_rounds(self):
        state, _, generate = self.run_adaptive(self.job(), RuntimeError('do-not-return-upstream-secret'))
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(state['stop_reason'], 'round_limit')
        self.assertTrue(state['results'])
        self.assertTrue(all(item['report']['state'] == 'error' for item in state['rounds']))
        self.assertNotIn('do-not-return-upstream-secret', str(state))

    def test_stopping_in_report_keeps_previous_ready_report_and_current_results(self):
        cancel = threading.Event()
        release = threading.Event()
        snapshots = []
        old = report(1)
        job = self.job(round=1, rounds=[{'number': 1, 'state': 'completed', 'queries': [], 'report': old}],
                       progress_report=copy.deepcopy(old), max_rounds=1)

        def generate(*args):
            cancel.set()
            release.wait(2)
            return report(2)

        state, _, _ = self.run_adaptive(job, generate, stop=cancel, on_update=lambda value: snapshots.append(copy.deepcopy(value)))
        release.set()
        self.assertEqual(state['state'], 'stopped')
        self.assertEqual(state['progress_report'], old)
        self.assertEqual(state['rounds'][-1]['report']['state'], 'stopped')
        self.assertTrue(state['results'])
        self.assertEqual(state['ai_summary']['state'], 'ready')
        self.assertEqual(snapshots[-1], state)

    def test_single_round_reports_while_running_then_finishes_even_on_report_error(self):
        job = self.job(adaptive=False)
        updates = []

        def generate(*args):
            self.assertEqual(updates[-1]['state'], 'running')
            self.assertEqual(updates[-1]['stage'], 'reporting')
            self.assertEqual(updates[-1]['rounds'][0]['report']['state'], 'running')
            raise RuntimeError('private upstream error')

        with patch('search_app.engine.make_plan', return_value=fallback_plan(job['query'])), \
             patch('search_app.engine.build_tasks', return_value=[]), \
             patch('search_app.engine.summarize_results', return_value={'state': 'empty', 'points': []}), \
             patch('search_app.engine.build_progress_report', side_effect=generate):
            run_search(job, {}, Library(), lambda **fields: updates.append(copy.deepcopy(fields)))
        self.assertEqual(updates[-1]['state'], 'done')
        self.assertEqual(updates[-1]['progress_report']['state'], 'error')
        self.assertEqual(updates[-1]['rounds'][0]['report']['state'], 'error')
        self.assertNotIn('private upstream error', str(updates[-1]))

    def test_stop_and_restore_preserve_prior_report_and_cancel_only_pending_one(self):
        with tempfile.TemporaryDirectory() as directory:
            app = App(directory)
            with patch('search_app.server.threading.Thread'):
                job_id = app.create_job({'query': '合肥中科大附近餐馆'})['job_id']
            old = report(1)
            app.jobs[job_id].update(state='running', stage='reporting', progress_report=copy.deepcopy(old),
                rounds=[{'number': 1, 'state': 'completed', 'report': copy.deepcopy(old)},
                        {'number': 2, 'state': 'completed', 'report': {'state': 'running', 'round': 2}}])
            app.stop_job(job_id)
            restored = App(directory)
            saved = restored.get_job(job_id)
            self.assertEqual(saved['progress_report'], old)
            self.assertEqual(saved['rounds'][0]['report'], old)
            self.assertEqual(saved['rounds'][1]['report']['state'], 'stopped')
            self.assertEqual(saved['state'], 'stopped')
            restored.storage.close()
            app.storage.close()

    def test_single_round_tracks_requests_and_normalizes_history_for_resume(self):
        job = self.job(adaptive=False, use_ai=False, platforms=['zhihu'], custom_sites=[
            {'name': '公开索引', 'domain': 'example.org', 'search_url': ''},
            {'name': '站内搜索', 'domain': 'example.com', 'search_url': 'https://example.com/search?q={query}'},
        ])
        updates = []
        response = {'results': [], 'status': {'provider': 'test', 'ok': True}}
        with patch('search_app.engine.build_tasks', return_value=[('bing', job['query'] + ' site:zhihu.com', ['zhihu'])]), \
             patch('search_app.engine.search_provider', return_value=response), \
             patch('search_app.engine.search_custom_site', return_value=response), \
             patch('search_app.engine.build_progress_report', return_value=None):
            run_search(job, {}, Library(), lambda **fields: updates.append(copy.deepcopy(fields)))
        final = updates[-1]
        self.assertEqual(final['state'], 'done')
        self.assertEqual(final['progress_report']['state'], 'error')
        self.assertEqual(final['searches_count'], 3)
        self.assertEqual([(task['provider'], task['platform'], task['query']) for task in final['rounds'][0]['queries']], [
            ('bing', 'zhihu', job['query']), ('bing', 'website:example.org', job['query']),
            ('website', 'website:example.com', job['query'])])

    def test_null_legacy_report_does_not_break_stop(self):
        stop = threading.Event()
        stop.set()
        state, _, generate = self.run_adaptive(self.job(round=1, rounds=[{'number': 1, 'queries': [], 'report': None}],
                                                       progress_report=None), lambda *args: report(2), stop=stop)
        generate.assert_not_called()
        self.assertEqual(state['state'], 'stopped')


if __name__ == '__main__':
    unittest.main()
