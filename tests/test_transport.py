"""Regression for opening the local UI by clicking a link on another page."""
from email.message import Message
import unittest

from search_app.transport import TransportPolicy


class CrossSiteNavigationTests(unittest.TestCase):
    def setUp(self):
        self.policy = TransportPolicy.from_environment(environ={})

    def headers(self, **changes):
        values = {'Host': '127.0.0.1:8877', 'Sec-Fetch-Site': 'cross-site',
                  'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document', 'Sec-Fetch-User': '?1'}
        values.update(changes)
        headers = Message()
        for key, value in values.items():
            if value is not None:
                headers[key] = value
        return headers

    def test_browser_top_level_get_and_head_navigation_pass_policy(self):
        # These are the browser headers that previously produced 403 for GET /.
        for method in ('GET', 'HEAD'):
            for path in ('/', '/index.html'):
                with self.subTest(method=method, path=path):
                    self.assertIsNone(self.policy.check(self.headers(), 8877, method, path))

    def test_same_navigation_headers_do_not_grant_api_access(self):
        for method in ('GET', 'HEAD'):
            for path in ('/api', '/api/health', '/api/config', '/api/history'):
                with self.subTest(method=method, path=path):
                    self.assertEqual(self.policy.check(self.headers(), 8877, method, path)[0], 403)

    def test_navigation_exception_does_not_allow_writes_or_preflight(self):
        for method in ('POST', 'PUT', 'DELETE', 'OPTIONS'):
            with self.subTest(method=method):
                self.assertEqual(self.policy.check(self.headers(), 8877, method, '/')[0], 403)

    def test_iframe_subresources_and_missing_navigation_mode_stay_rejected(self):
        for changes in ({'Sec-Fetch-Dest': 'iframe'}, {'Sec-Fetch-Dest': 'script'},
                        {'Sec-Fetch-Dest': None}, {'Sec-Fetch-Mode': 'cors'},
                        {'Sec-Fetch-Mode': 'no-cors'}, {'Sec-Fetch-Mode': None}):
            with self.subTest(changes=changes):
                self.assertEqual(self.policy.check(self.headers(**changes), 8877, 'GET', '/')[0], 403)

    def test_explicit_unlisted_or_multiple_origins_are_still_rejected(self):
        self.assertIsNone(self.policy.check(self.headers(Origin='http://127.0.0.1:8877'), 8877, 'GET', '/'))
        for origin in ('https://unlisted.example', 'null', 'http://127.0.0.1:8890'):
            with self.subTest(origin=origin):
                self.assertEqual(self.policy.check(self.headers(Origin=origin), 8877, 'GET', '/')[0], 403)
        headers = self.headers(Origin='http://127.0.0.1:8877')
        headers['Origin'] = 'http://127.0.0.1:8877'
        self.assertEqual(self.policy.check(headers, 8877, 'GET', '/')[0], 403)

    def test_bad_missing_or_multiple_hosts_cannot_use_document_exception(self):
        for host in ('attacker.example', '127.0.0.1:8890', None):
            headers = self.headers(**{'Host': host, 'X-Forwarded-Host': '127.0.0.1:8877'})
            with self.subTest(host=host):
                self.assertEqual(self.policy.check(headers, 8877, 'GET', '/')[0], 403)
        headers = self.headers()
        headers['Host'] = '127.0.0.1:8877'
        self.assertEqual(self.policy.check(headers, 8877, 'GET', '/')[0], 403)

    def test_public_static_navigation_still_obeys_explicit_host_and_origin(self):
        policy = TransportPolicy.from_environment('0.0.0.0', {
            'XUNWEI_ACCESS_TOKEN': 'synthetic-test-only',
            'XUNWEI_PUBLIC_HOSTS': 'search.example',
            'XUNWEI_ALLOWED_ORIGINS': 'https://pages.example'})
        headers = self.headers(Host='search.example')
        self.assertIsNone(policy.check(headers, 8877, 'GET', '/'))
        self.assertEqual(policy.check(headers, 8877, 'GET', '/api/health')[0], 403)
        headers['Origin'] = 'https://unlisted.example'
        self.assertEqual(policy.check(headers, 8877, 'GET', '/')[0], 403)


if __name__ == '__main__':
    unittest.main()
