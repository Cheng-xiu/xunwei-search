"""Regression coverage from an independent review; all upstream calls are mocked."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search_app import ai, engine
from search_app.server import App


class _Response:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return json.dumps(self.data).encode('utf-8')


class AIResponseTests(unittest.TestCase):
    def test_bad_choices_and_message_shapes_have_safe_errors(self):
        shapes = [None, [], {}, {'choices': None}, {'choices': [None]}, {'choices': ['bad']},
                  {'choices': [{'message': None}]}, {'choices': [{'message': 'bad'}]}]
        for shape in shapes:
            with self.subTest(shape=shape), patch('urllib.request.build_opener') as opener:
                opener.return_value.open.return_value = _Response(shape)
                with self.assertRaises(ai.AIError):
                    ai.chat({'api_key': 'not-a-secret-test-fixture', 'base_url': 'https://example.com/v1', 'model': 'test'}, 'system', 'user')


class GroundingReviewTests(unittest.TestCase):
    def test_original_query_survives_ai_planner_omission(self):
        query = '中科大中区桃李苑哪道菜好吃'
        with patch.object(engine, 'chat', return_value=json.dumps({'must_have': ['有菜品推荐'], 'queries': []}, ensure_ascii=False)):
            plan = engine.make_plan(query, {}, True, lambda message: None)
        self.assertIn(query, plan['must_have'])
        result = engine.clean_result({'title': '清华大学食堂', 'url': 'https://example.com/post', 'snippet': '好吃推荐菜：宫保鸡丁'}, query)
        engine.apply_assessments([result], [{'id': result['id'], 'evidence': [
            {'condition': '有菜品推荐', 'status': 'supported', 'quote': '好吃推荐菜：宫保鸡丁'}
        ]}], plan['must_have'], lambda message: None)
        self.assertNotEqual(result['match'], 'strong')
        original = next(item for item in result['evidence'] if item['condition'] == query)
        self.assertEqual(original['status'], 'unknown')

    def test_dating_conditions_survive_planning_and_ai_failure(self):
        query = '合肥成年人本人公开相亲帖'
        for use_ai in (False, True):
            with self.subTest(use_ai=use_ai), patch.object(engine, 'chat', side_effect=ai.AIError('测试服务不可用')):
                plan = engine.make_plan(query, {}, use_ai, lambda message: None, public_post_only=True)
            self.assertIn(query, plan['must_have'])
            for condition in engine.DATING_CONDITIONS:
                self.assertIn(condition, plan['must_have'])

    def test_local_evidence_does_not_claim_authenticity(self):
        result = engine.clean_result({'id': 'local1', 'title': '桃李苑推荐', 'url': '', 'snippet': '香菇滑鸡很好吃', 'source': 'local', 'content_level': 'local'}, '菜品推荐')
        engine.apply_assessments([result], [{'id': 'local1', 'evidence': [
            {'condition': '有推荐菜', 'status': 'supported', 'quote': '香菇滑鸡很好吃'}
        ]}], ['有推荐菜'], lambda message: None)
        self.assertIn('未核实公开来源或内容真实性', result['reason'])
        self.assertEqual(result['content_level'], 'local')


class _Library:
    def __init__(self, documents=None):
        self.documents = documents or []

    def search_documents(self, *args, **kwargs):
        return list(self.documents)


class SearchLifecycleReviewTests(unittest.TestCase):
    def job(self, **changes):
        value = {'query': '合肥成年人本人公开相亲帖', 'platforms': ['web'], 'use_ai': False,
                 'depth': 'quick', 'fetch_pages': False, 'public_post_only': True, 'warnings': []}
        value.update(changes)
        return value

    def run_mock_search(self, job, library, **patches):
        final = {}
        with patch.object(engine, 'build_tasks', return_value=[]), patch.object(engine, 'chat', side_effect=ai.AIError('测试服务不可用')):
            engine.run_search(job, {}, library, lambda **fields: final.update(fields))
        return final

    def test_keyword_and_failed_ai_do_not_display_unverified_dating_candidates(self):
        library = _Library([{'id': 'local1', 'title': '合肥相亲：17岁征友', 'url': '',
                             'snippet': '我今年17岁，合肥人，征友相亲。', 'body': '我今年17岁，合肥人，征友相亲。',
                             'platform': 'local', 'source': 'local', 'content_level': 'local'}])
        for use_ai in (False, True):
            with self.subTest(use_ai=use_ai):
                final = self.run_mock_search(self.job(use_ai=use_ai), library)
                self.assertEqual(final['state'], 'done')
                self.assertEqual(final['results'], [])
                self.assertTrue(any('隐藏' in warning for warning in final['warnings']))

    def test_imported_text_cannot_independently_establish_public_dating_post(self):
        query = '合肥成年人本人公开相亲帖'
        library = _Library([{'id': 'local1', 'title': query, 'url': 'https://example.com/adult',
                             'snippet': '我25岁，在合肥，本人公开征婚。', 'body': '我25岁，在合肥，本人公开征婚。',
                             'platform': 'web', 'source': 'local', 'content_level': 'local'}])
        def pretend_supported(query, results, plan, config, warn, public_post_only=False):
            for result in results:
                result['match'] = 'strong'
                result['evidence'] = [{'condition': condition, 'status': 'supported', 'quote': result['snippet']}
                                      for condition in (query, *engine.DATING_CONDITIONS)]
        final = {}
        with patch.object(engine, 'build_tasks', return_value=[]), patch.object(engine, 'chat', side_effect=ai.AIError('测试服务不可用')), patch.object(engine, 'assess', side_effect=pretend_supported):
            engine.run_search(self.job(use_ai=True), {}, library, lambda **fields: final.update(fields))
        self.assertEqual(final['results'], [])

    def test_deep_passes_extended_provider_budget_without_mutating_config(self):
        config = {'model': 'test'}
        final = {}
        with patch.object(engine, 'build_tasks', return_value=[('bilibili', '桃李苑', ['bilibili'])]), patch.object(engine, 'search_provider', return_value={'results': [], 'status': {'provider': 'bilibili', 'ok': True, 'count': 0}}) as provider:
            engine.run_search(self.job(query='桃李苑菜品', platforms=['bilibili'], depth='deep', public_post_only=False), config, _Library(), lambda **fields: final.update(fields))
        self.assertEqual(provider.call_args.args[3], 30)
        self.assertEqual(provider.call_args.args[4]['_search_depth'], 'deep')
        self.assertNotIn('_search_depth', config)

    def test_platform_selection_applies_to_classified_import_without_url(self):
        library = _Library([{'id': 'local1', 'title': '桃李苑菜品', 'url': '', 'snippet': '香菇滑鸡很好吃',
                             'body': '桃李苑的香菇滑鸡很好吃', 'platform': 'bilibili', 'source': 'local', 'content_level': 'local'}])
        final = self.run_mock_search(self.job(query='桃李苑菜品', platforms=['zhihu'], public_post_only=False), library)
        self.assertEqual(final['results'], [])

    def test_total_external_failure_is_explicit(self):
        final = {}
        with patch.object(engine, 'build_tasks', return_value=[('bing', '桃李苑', ['web'])]), patch.object(engine, 'search_provider', return_value={'results': [], 'status': {'provider': 'bing', 'ok': False, 'count': 0, 'error': '测试网络失败'}}):
            engine.run_search(self.job(query='桃李苑菜品', public_post_only=False), {}, _Library(), lambda **fields: final.update(fields))
        self.assertTrue(any('全部外部检索来源均不可用' in warning for warning in final['warnings']))

    def test_server_propagates_public_post_only(self):
        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory))
            with patch('search_app.server.threading.Thread'):
                response = app.create_job({'query': '合肥成年人本人公开相亲帖', 'use_ai': False})
            job = app.get_job(response['job_id'])
            self.assertTrue(job['public_post_only'])
            app.storage.close()


if __name__ == '__main__':
    unittest.main()
