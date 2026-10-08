import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from search_app.ai import AIError, parse_json_response, validate_base_url
from search_app.engine import apply_assessments, clean_result, run_search, make_plan, assess
from search_app.server import App, Handler, LocalHTTPServer
from search_app.storage import Storage


class EvidenceTests(unittest.TestCase):
    def test_hallucinated_quote_cannot_be_marked_verified(self):
        r = clean_result({'title': '食堂体验', 'url': 'https://www.zhihu.com/question/123', 'snippet': '桃李苑有面条'}, '桃李苑面条')
        apply_assessments([r], [{'id': r['id'], 'evidence': [{'condition': '牛肉面好吃', 'status': 'supported', 'quote': '牛肉面特别好吃'}]}], ['牛肉面好吃'], lambda _: None)
        self.assertEqual(r['match'], 'unverified')
        self.assertEqual(r['evidence'][0]['quote'], '')

    def test_missing_condition_remains_unknown(self):
        r = clean_result({'title': '用餐记录', 'url': 'https://www.zhihu.com/question/124', 'snippet': '桃李苑有面条'}, '桃李苑面条')
        apply_assessments([r], [{'id': r['id'], 'evidence': [{'condition': '地点', 'status': 'supported', 'quote': '桃李苑有面条'}]}], ['地点', '好吃'], lambda _: None)
        self.assertEqual(r['match'], 'partial')
        self.assertEqual(r['evidence'][1]['status'], 'unknown')
        self.assertIn('摘要', r['reason'])

    def test_nonmatching_provider_output_is_not_presented_as_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / 'test.db'))
            job = {'query': '中科大桃李苑', 'platforms': ['web'], 'depth': 'quick', 'use_ai': False, 'fetch_pages': False}
            snapshot = {}
            response = {'results': [{'title': 'Blue dog', 'snippet': 'Nothing relevant', 'url': 'https://example.org/dog'}], 'status': {'provider': 'fake', 'ok': True, 'count': 1}}
            with patch('search_app.engine.available_providers', return_value=['bing']), patch('search_app.engine.search_provider', return_value=response):
                run_search(job, {}, storage, lambda **kw: snapshot.update(kw))
            self.assertEqual(snapshot['state'], 'done')
            self.assertEqual(snapshot['results'], [])
            self.assertIn('没有找到', snapshot['summary'])

    def test_ai_failure_is_visible_and_retrieval_plan_survives(self):
        warnings = []
        with patch('search_app.engine.chat', side_effect=AIError('测试上游失败')):
            plan = make_plan('中科大桃李苑', {}, True, warnings.append)
        self.assertFalse(plan['ai_used'])
        self.assertEqual(warnings, ['测试上游失败'])
        self.assertEqual(plan['queries'][0]['query'], '中科大桃李苑')

    def test_json_fence_parser_and_transport_validation(self):
        self.assertEqual(parse_json_response('```json\n{"ok":true}\n```'), {'ok': True})
        with self.assertRaises(ValueError):
            validate_base_url('http://remote.example/v1')
        with self.assertRaises(ValueError):
            validate_base_url('https://secret@example.org/v1')

    def test_assessment_keeps_every_planned_condition_with_bounded_candidates(self):
        query = '原始问题' * 40
        conditions = [query] + [f'条件{i}' for i in range(8)]
        results = [clean_result({'title': '测试资料', 'snippet': '原始问题', 'url': f'https://example.org/{i}'}, query) for i in range(30)]
        with patch('search_app.engine.chat', return_value='{"results":[]}') as model:
            count = assess(query, results, {'must_have': conditions}, {}, lambda _: None)
        payload = json.loads(model.call_args.args[2])
        self.assertEqual(payload['conditions'], conditions)
        self.assertEqual(len(payload['sources']), count)
        self.assertLessEqual(count * len(conditions), 96)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = App(self.directory.name)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.app = self.app
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.directory.cleanup()

    def request(self, path, data=None, method=None, headers=None):
        h = {'Content-Type': 'application/json', **(headers or {})}
        request = urllib.request.Request(self.base + path, data=json.dumps(data).encode() if data is not None else None, headers=h, method=method)
        with urllib.request.urlopen(request) as r:
            return json.load(r)

    def test_secret_never_returned_and_blank_save_preserves(self):
        masked = self.request('/api/config', {'api_key': 'test-private-value'}, 'PUT')
        self.assertTrue(masked['has_api_key'])
        self.assertNotIn('api_key', masked)
        self.request('/api/config', {'api_key': ''}, 'PUT')
        self.assertEqual(self.app.config['api_key'], 'test-private-value')
        self.request('/api/config', {'clear_secrets': ['api_key']}, 'PUT')
        self.assertFalse(self.app.config['api_key'])

    def test_cross_origin_and_host_rebinding_are_rejected(self):
        for headers in ({'Origin': 'https://attacker.example'}, {'Host': 'attacker.example'}):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.request('/api/config', {'model': 'bad'}, 'PUT', headers)
            self.assertEqual(ctx.exception.code, 403)
        self.assertNotEqual(self.app.config['model'], 'bad')

    def test_arbitrary_local_files_cannot_be_served(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request('/%2e%2e/.local/settings.json')
        self.assertEqual(ctx.exception.code, 404)

    def test_import_search_and_delete(self):
        result = self.request('/api/import', {'title': '测试资料', 'text': '这是用于验证本地导入和搜索的一段合规测试文本。'})
        self.assertTrue(result['ok'])
        docs = self.request('/api/library')['items']
        self.assertEqual(len(docs), 1)
        self.assertNotIn('text', docs[0])
        self.assertTrue(self.app.storage.search_documents('本地导入'))
        self.assertTrue(self.request('/api/library/' + result['id'], method='DELETE')['ok'])

    def test_ambiguous_dating_query_stops_before_network(self):
        with patch('search_app.server.run_search') as search:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.request('/api/search', {'query': '07-10年的在合肥上大学的女生相亲贴'})
            self.assertEqual(ctx.exception.code, 400)
            search.assert_not_called()

    def test_server_port_is_exclusive(self):
        first = LocalHTTPServer(('127.0.0.1', 0), Handler)
        try:
            with self.assertRaises(OSError):
                LocalHTTPServer(('127.0.0.1', first.server_port), Handler)
        finally:
            first.server_close()


if __name__ == '__main__':
    unittest.main()
