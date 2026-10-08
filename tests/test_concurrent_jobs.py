"""Real worker overlap and configuration checks without upstream network I/O."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from search_app.server import App


class ConcurrentJobsTests(unittest.TestCase):
    def test_invalid_saved_concurrency_cannot_poison_default_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'settings.json').write_text(json.dumps({'search_concurrency': '6'}), encoding='utf-8')
            with patch('builtins.print'):
                app = App(directory, environment=False)
            self.assertEqual(app.public_config()['search_concurrency'], 4)
            with patch('search_app.server.threading.Thread'):
                job_id = app.create_job({'query': '配置恢复后的搜索'})['job_id']
            self.assertEqual(app.get_job(job_id)['search_concurrency'], 4)

    def test_numeric_concurrency_roundtrips_and_is_snapshotted_per_job(self):
        with tempfile.TemporaryDirectory() as directory:
            app = App(directory, environment=False)
            self.assertEqual(app.public_config()['search_concurrency'], 4)
            app.save_config({'search_concurrency': 12})
            restored = App(directory, environment=False)
            self.assertEqual(restored.public_config()['search_concurrency'], 12)
            with patch('search_app.server.threading.Thread'):
                job_id = app.create_job({'query': '中科大桃李苑菜品', 'search_concurrency': 6})['job_id']
            self.assertEqual(app.get_job(job_id)['search_concurrency'], 6)
            self.assertEqual(app.config['search_concurrency'], 12)
            app.stop_job(job_id)
            before = app.get_job(job_id)
            for value in (True, None, 0, 13, -1, '4', 2.5, {}, []):
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        app.save_config({'search_concurrency': value})
                    with patch('search_app.server.threading.Thread') as worker:
                        with self.assertRaises(ValueError):
                            app.create_job({'query': '中科大桃李苑菜品', 'search_concurrency': value})
                        with self.assertRaises(ValueError):
                            app.continue_job(job_id, {'search_concurrency': value})
                        worker.assert_not_called()
                    self.assertEqual(app.get_job(job_id), before)
                    self.assertEqual(app.config['search_concurrency'], 12)
            with patch('search_app.server.threading.Thread'):
                app.continue_job(job_id, {'search_concurrency': 8})
            self.assertEqual(app.get_job(job_id)['search_concurrency'], 8)

    def test_four_workers_overlap_and_stopping_one_preserves_the_others(self):
        ready, release, finished = threading.Event(), threading.Event(), threading.Event()
        lock = threading.Lock()
        entered, returned = [], []
        with tempfile.TemporaryDirectory() as directory:
            app = App(directory, environment=False)
            original_run = app._run
            def tracked_run(job_id, config):
                try:
                    original_run(job_id, config)
                finally:
                    with lock:
                        returned.append(job_id)
                        if len(returned) == 4:
                            finished.set()
            app._run = tracked_run
            def worker(job, config, storage, update):
                update(state='running', stage='searching', results=[{'id': job['id'], 'title': job['query']}])
                with lock:
                    entered.append(job['id'])
                    if len(entered) == 4:
                        ready.set()
                release.wait(3)
                update(state='done', stage='done', results=[{'id': job['id'], 'title': '独立结果 ' + job['query']}])
            with patch('search_app.server.run_search', side_effect=worker):
                ids = [app.create_job({'query': f'并行问题 {index}', 'use_ai': False})['job_id'] for index in range(4)]
                try:
                    self.assertTrue(ready.wait(2), 'All four workers must enter before any is released')
                    with self.assertRaises(ValueError):
                        app.create_job({'query': '第五个问题'})
                    self.assertEqual(app.list_jobs()['active_jobs'], 4)
                    app.stop_job(ids[0])
                    first_snapshot = copy.deepcopy(app.get_job(ids[0]))
                    self.assertEqual(app.list_jobs()['active_jobs'], 3)
                    self.assertTrue(app.controls[ids[0]].is_set())
                    self.assertTrue(all(not app.controls[job_id].is_set() for job_id in ids[1:]))
                finally:
                    release.set()
                    self.assertTrue(finished.wait(3))
                self.assertEqual(app.get_job(ids[0]), first_snapshot, 'A late worker cannot overwrite its stopped task')
                for index, job_id in enumerate(ids[1:], 1):
                    result = app.get_job(job_id)
                    self.assertEqual(result['state'], 'done')
                    self.assertEqual(result['results'][0]['id'], job_id)
                    self.assertIn(f'并行问题 {index}', result['results'][0]['title'])
                    self.assertEqual(app.storage.get_job(job_id)['results'], result['results'])

    def test_summary_jobs_share_capacity_and_metadata_contains_no_model_config(self):
        with tempfile.TemporaryDirectory() as directory:
            app = App(directory, environment=False)
            app.config['api_key'] = 'synthetic-private-value'
            with patch('search_app.server.threading.Thread'):
                ids = [app.create_job({'query': f'独立问题 {index}'})['job_id'] for index in range(4)]
                app.jobs[ids[0]].update(state='done', results=[{'id': 'evidence'}])
                app.create_summary(ids[0])
                with self.assertRaises(ValueError):
                    app.create_job({'query': '容量已满的搜索'})
            snapshot = app.list_jobs()
            self.assertEqual(snapshot['active_jobs'], 4)
            row = next(item for item in snapshot['items'] if item['id'] == ids[0])
            self.assertTrue(row['active'])
            self.assertEqual(row['ai_summary_state'], 'running')
            self.assertFalse({'api_key', 'config', 'results', 'body'}.intersection(row))
            self.assertEqual(snapshot['max_active_jobs'], 4)
            self.assertEqual(snapshot['max_search_concurrency'], 12)


if __name__ == '__main__':
    unittest.main()
