"""Real-thread concurrency and stop regressions; all source/AI calls are local fixtures."""
import copy
from contextlib import ExitStack
import threading
import time
import unittest
from unittest.mock import patch

from search_app import adaptive, engine, retrieval


class Library:
    def search_documents(self, *args, **kwargs):
        return []


class Probe:
    def __init__(self):
        self.condition = threading.Condition()
        self.release = threading.Event()
        self.active = self.peak = self.total = 0

    def block(self, value):
        with self.condition:
            self.active += 1
            self.total += 1
            self.peak = max(self.peak, self.active)
            self.condition.notify_all()
        try:
            if not self.release.wait(8):
                raise AssertionError('Test did not release blocked fixture')
            return value
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

    def wait_active(self, number, timeout=3):
        with self.condition:
            return self.condition.wait_for(lambda: self.active == number, timeout)


class SearchConcurrencyTests(unittest.TestCase):
    def job(self, adaptive_mode, **values):
        return {'id': 'fixture', 'query': 'rareword', 'platforms': ['web'], 'custom_sites': [],
                'depth': 'quick', 'adaptive': adaptive_mode, 'use_ai': False, 'fetch_pages': False,
                'max_rounds': 1, 'round': 0, 'rounds': [], 'results': [], 'warnings': [],
                'provider_status': [], **values}

    @staticmethod
    def response(provider, text, scope):
        platform = scope[0]
        host = 'www.bilibili.com' if platform == 'bilibili' else 'example.com'
        return {'results': [{'title': 'rareword ' + text, 'url': 'https://' + host + '/post/' + text.replace(' ', '-'),
                             'snippet': 'rareword source evidence', 'source': provider, 'platform': platform}],
                'status': {'provider': provider, 'ok': True, 'count': 1}}

    def patches(self, source=None, count=8, provider='bing', platform='web', reader=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        plan = {'queries': [{'query': 'rareword'}], 'must_have': ['rareword'], 'ai_used': False}
        stack.enter_context(patch.object(engine, 'make_plan', return_value=plan))
        stack.enter_context(patch.object(engine, 'build_tasks', side_effect=lambda *args: [(provider, f'rareword-{n}', [platform]) for n in range(count)]))
        stack.enter_context(patch.object(adaptive, '_weighted_tasks', side_effect=lambda *args: [
            {'provider': provider, 'query': f'rareword-{n}', 'platform': platform, 'status': 'queued'} for n in range(count)]))
        stack.enter_context(patch.object(adaptive.providers, 'available_providers', return_value=[provider]))
        source = source or (lambda p, q, scope, *rest: self.response(p, q, scope))
        stack.enter_context(patch.object(engine, 'search_provider', side_effect=source))
        stack.enter_context(patch.object(adaptive.providers, 'search_provider', side_effect=source))
        report = {'state': 'disabled', 'findings': [], 'assessment': {'likelihood': 'unknown'}}
        for module in (engine, adaptive):
            stack.enter_context(patch.object(module, 'build_progress_report', return_value=report))
            stack.enter_context(patch.object(module, 'summarize_results', return_value={'state': 'ready', 'points': []}))
        stack.enter_context(patch.object(engine, 'assess', return_value=0))
        if reader:
            stack.enter_context(patch.object(engine, 'fetch_public_page', side_effect=reader))
            stack.enter_context(patch.object(adaptive.providers, 'fetch_public_page', side_effect=reader))
        return stack

    def start(self, job, config=None, on_update=None):
        cancel, finished = threading.Event(), threading.Event()
        state, updates, errors = copy.deepcopy(job), [], []
        def update(**fields):
            state.update(copy.deepcopy(fields))
            updates.append(copy.deepcopy(state))
            if on_update:
                on_update(state, cancel)
        def run():
            try:
                engine.run_search(copy.deepcopy(job), {**(config or {}), '_cancel_event': cancel}, Library(), update)
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()
        thread = threading.Thread(target=run)
        thread.start()
        return cancel, finished, state, updates, errors, thread

    def finish(self, running):
        _, finished, state, _, errors, thread = running
        self.assertTrue(finished.wait(3), 'coordinator did not finish')
        thread.join(1)
        self.assertEqual(errors, [])
        return state

    def stop_fast(self, running):
        started = time.monotonic()
        running[0].set()
        self.assertTrue(running[1].wait(0.65), 'stop waited for a blocked source/AI')
        self.assertLess(time.monotonic() - started, 0.65)
        self.assertEqual(running[4], [])
        self.assertEqual(running[2]['state'], 'stopped')

    def test_job_setting_wins_over_config_and_internal_bounds_are_defensive(self):
        self.assertEqual(retrieval.effective_search_concurrency({}, {}), 4)
        self.assertEqual(retrieval.effective_search_concurrency({}, {'search_concurrency': 7}), 7)
        self.assertEqual(retrieval.effective_search_concurrency({'search_concurrency': 2}, {'search_concurrency': 7}), 2)
        self.assertEqual(retrieval.effective_search_concurrency({'search_concurrency': 99}, {}), 12)
        self.assertEqual(retrieval.effective_search_concurrency({'search_concurrency': 0}, {}), 1)
        self.assertEqual(retrieval.effective_search_concurrency({'search_concurrency': True}, {}), 4)

    def test_both_modes_overlap_without_exceeding_selected_concurrency(self):
        for mode in (False, True):
            for concurrency in (1, 2, 6, 12):
                with self.subTest(adaptive=mode, concurrency=concurrency):
                    probe = Probe()
                    source = lambda p, q, scope, *rest: probe.block(self.response(p, q, scope))
                    with self.patches(source, count=15):
                        running = self.start(self.job(mode, search_concurrency=concurrency), {'search_concurrency': 3})
                        try:
                            self.assertTrue(probe.wait_active(concurrency))
                            self.assertEqual(probe.total, concurrency, 'queued requests were started eagerly')
                            probe.release.set()
                            state = self.finish(running)
                            self.assertEqual(state['search_concurrency'], concurrency)
                            self.assertEqual(probe.total, 15)
                            self.assertEqual(probe.peak, concurrency)
                            self.assertEqual(state['searches_count'], 15)
                            self.assertTrue(all(item['status'] == 'completed' for item in state['rounds'][-1]['queries']))
                        finally:
                            probe.release.set()
                            running[0].set()
                            running[-1].join(3)
                            self.assertTrue(probe.wait_active(0))

    def test_default_and_config_fallback_control_real_parallel_work(self):
        for mode, config, expected in ((False, {}, 4), (True, {'search_concurrency': 3}, 3)):
            probe = Probe()
            with self.subTest(adaptive=mode), self.patches(lambda p, q, scope, *rest: probe.block(self.response(p, q, scope))):
                running = self.start(self.job(mode), config)
                try:
                    self.assertTrue(probe.wait_active(expected))
                    probe.release.set()
                    self.finish(running)
                    self.assertEqual(probe.peak, expected)
                finally:
                    probe.release.set()
                    running[0].set()
                    running[-1].join(3)
                    self.assertTrue(probe.wait_active(0))

    def test_stop_after_one_result_preserves_it_and_never_dispatches_queued_or_publishes_late_native(self):
        for mode in (False, True):
            barrier, release, slow_done = threading.Barrier(2), threading.Event(), threading.Event()
            called = []
            def source(provider, text, scope, *args):
                called.append(text)
                barrier.wait(3)
                if text.endswith('-0'):
                    return self.response(provider, text, scope)
                try:
                    release.wait(5)
                    return self.response(provider, text, scope)
                finally:
                    slow_done.set()
            def update(state, cancel):
                if any(row.get('provider') == 'bilibili' for row in state.get('provider_status', [])):
                    cancel.set()
            with self.subTest(adaptive=mode), self.patches(source, count=7, provider='bilibili', platform='bilibili'):
                running = self.start(self.job(mode, platforms=['bilibili'], search_concurrency=2), on_update=update)
                try:
                    state = self.finish(running)
                    self.assertEqual(state['state'], 'stopped')
                    self.assertEqual(len(called), 2)
                    self.assertEqual(len(state['results']), 1)
                    statuses = [row['status'] for row in state['rounds'][-1]['queries']]
                    self.assertEqual(statuses.count('completed'), 1)
                    self.assertEqual(statuses.count('cancelled'), 6)
                    self.assertEqual(state['searches_count'], 1)
                    snapshot, published = copy.deepcopy(state), len(running[3])
                    release.set()
                    self.assertTrue(slow_done.wait(2))
                    self.assertEqual(state, snapshot)
                    self.assertEqual(len(running[3]), published)
                finally:
                    release.set()
                    barrier.abort()
                    running[0].set()
                    running[-1].join(3)

    def test_body_reads_in_both_modes_obey_concurrency_and_stop_without_late_mutation(self):
        for mode in (False, True):
            for concurrency in (1, 3):
                probe = Probe()
                reader = lambda *args, **kwargs: probe.block({'text': 'rareword fetched body'})
                with self.subTest(adaptive=mode, concurrency=concurrency), self.patches(count=8, reader=reader):
                    running = self.start(self.job(mode, depth='research', fetch_pages=True, search_concurrency=concurrency))
                    try:
                        self.assertTrue(probe.wait_active(concurrency))
                        self.stop_fast(running)
                        self.assertEqual(probe.total, concurrency)
                        snapshot, published = copy.deepcopy(running[2]), len(running[3])
                        probe.release.set()
                        self.assertTrue(probe.wait_active(0))
                        self.assertEqual(running[2], snapshot)
                        self.assertEqual(len(running[3]), published)
                        self.assertFalse(any(row.get('body') == 'rareword fetched body' for row in snapshot['results']))
                    finally:
                        probe.release.set()
                        running[0].set()
                        running[-1].join(3)

    def test_slow_ai_at_each_stage_never_delays_stop_or_applies_a_late_reply(self):
        for mode in (False, True):
            for stage in ('planning', 'evaluating', 'summarizing', 'reporting'):
                entered, release, returned = threading.Event(), threading.Event(), threading.Event()
                def slow(*args, **kwargs):
                    entered.set()
                    try:
                        release.wait(5)
                        if stage == 'planning':
                            return {'queries': [{'query': 'late query'}], 'must_have': ['rareword']}
                        if stage == 'evaluating':
                            args[1][0]['title'] = 'late mutation'
                            return 1
                        return {'state': 'ready', 'points': [{'text': 'late reply'}]}
                    finally:
                        returned.set()
                with self.subTest(adaptive=mode, stage=stage), self.patches(count=2) as stack:
                    if stage == 'planning':
                        stack.enter_context(patch.object(engine, 'make_plan', side_effect=slow))
                    elif stage == 'evaluating':
                        stack.enter_context(patch.object(engine, 'assess', side_effect=slow))
                    else:
                        name = 'summarize_results' if stage == 'summarizing' else 'build_progress_report'
                        stack.enter_context(patch.object(engine, name, side_effect=slow))
                        stack.enter_context(patch.object(adaptive, name, side_effect=slow))
                    running = self.start(self.job(mode, use_ai=True, search_concurrency=2))
                    try:
                        self.assertTrue(entered.wait(3))
                        self.stop_fast(running)
                        snapshot, published = copy.deepcopy(running[2]), len(running[3])
                        release.set()
                        self.assertTrue(returned.wait(2))
                        self.assertEqual(running[2], snapshot)
                        self.assertEqual(len(running[3]), published)
                    finally:
                        release.set()
                        running[0].set()
                        running[-1].join(3)

    def test_stopped_inflight_sources_retain_global_slots_across_new_jobs(self):
        for public, cap, per_job in ((True, 16, 4), (False, 48, 12)):
            probe, replacement_entered = Probe(), threading.Event()
            def source(provider, text, scope, *args):
                if args[-1].get('_test_replacement'):
                    replacement_entered.set()
                return probe.block(self.response(provider, text, scope))
            with self.subTest(public=public), self.patches(source, count=per_job):
                running = [self.start(self.job(index % 2 == 0, search_concurrency=per_job), {'_public_network': public}) for index in range(4)]
                replacement = None
                try:
                    self.assertTrue(probe.wait_active(cap))
                    for task in running:
                        self.stop_fast(task)
                    self.assertEqual(probe.active, cap, 'stopping released live external calls')
                    replacement = self.start(self.job(False, search_concurrency=1), {'_public_network': public, '_test_replacement': True})
                    # A replacement coordinator runs, but its source cannot get
                    # a slot while all detached calls are still blocked.
                    self.assertFalse(replacement_entered.wait(0.12))
                    self.stop_fast(replacement)
                    self.assertEqual(probe.total, cap)
                    self.assertEqual(probe.peak, cap)
                finally:
                    probe.release.set()
                    for task in running + ([replacement] if replacement else []):
                        task[0].set()
                        task[-1].join(3)
                    self.assertTrue(probe.wait_active(0))

    def test_cancelled_slot_wait_cannot_enter_or_leak_capacity(self):
        semaphore = threading.BoundedSemaphore(1)
        cancel, entered, finished = threading.Event(), threading.Event(), threading.Event()
        with patch.object(retrieval, '_PUBLIC_REQUEST_SLOTS', semaphore):
            semaphore.acquire()
            def waiting():
                try:
                    with retrieval.request_slot({'_public_network': True}, cancel):
                        entered.set()
                except retrieval.RetrievalCancelled:
                    pass
                finally:
                    finished.set()
            thread = threading.Thread(target=waiting)
            thread.start()
            cancel.set()
            self.assertTrue(finished.wait(0.5))
            self.assertFalse(entered.is_set())
            semaphore.release()
            thread.join(1)
            with retrieval.request_slot({'_public_network': True}, threading.Event()):
                pass

    def test_resume_legacy_counts_only_completed_queries(self):
        prior = {'number': 1, 'state': 'stopped', 'queries': [
            {'provider': 'bing', 'query': 'old-done', 'platform': 'web', 'status': 'completed'},
            {'provider': 'bing', 'query': 'old-cancelled', 'platform': 'web', 'status': 'cancelled'},
            {'provider': 'bing', 'query': 'old-queued', 'platform': 'web', 'status': 'queued'}]}
        with self.patches(count=1):
            state = self.finish(self.start(self.job(True, rounds=[prior], round=1)))
        self.assertEqual(state['searches_count'], 2)
        self.assertEqual(state['rounds'][0], prior)


if __name__ == '__main__':
    unittest.main()
