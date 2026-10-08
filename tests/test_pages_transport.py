"""Real loopback HTTP requests; never reach external services or real AI."""
import contextlib
from email.message import Message
import http.client
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from search_app.server import App, Handler, LocalHTTPServer, main
from search_app.transport import TransportPolicy, normalize_authority, normalize_origin, validate_port


TOKEN = 'local-test-access-token-0123456789'
ORIGIN = 'https://example.github.io'
PUBLIC_HOST = 'service.example.com'
DEPLOYMENT = {'XUNWEI_ACCESS_TOKEN': TOKEN, 'XUNWEI_PUBLIC_HOSTS': PUBLIC_HOST,
              'XUNWEI_ALLOWED_ORIGINS': ORIGIN}


class TransportValidationTests(unittest.TestCase):
    def test_default_is_loopback_and_deployment_fields_are_all_explicit(self):
        default = TransportPolicy.from_environment(environ={})
        self.assertFalse(default.remote)
        self.assertEqual(default.hosts_for(8877), {'127.0.0.1:8877', 'localhost:8877'})
        for host, env in [('0.0.0.0', {}), ('127.0.0.1', {'XUNWEI_ACCESS_TOKEN': TOKEN}),
                          ('127.0.0.1', {'XUNWEI_ALLOWED_ORIGINS': ORIGIN}),
                          ('127.0.0.1', {'XUNWEI_PUBLIC_HOSTS': PUBLIC_HOST})]:
            with self.subTest(host=host, keys=list(env)), self.assertRaises(ValueError):
                TransportPolicy.from_environment(host, env)
        self.assertTrue(TransportPolicy.from_environment('0.0.0.0', DEPLOYMENT).remote)
        self.assertNotIn(TOKEN, repr(TransportPolicy.from_environment('0.0.0.0', DEPLOYMENT)))

    def test_exact_origins_accept_https_and_only_loopback_http(self):
        for value, expected in [(ORIGIN, ORIGIN), ('https://EXAMPLE.com:443', 'https://example.com'),
                                ('http://localhost:8000', 'http://localhost:8000'),
                                ('http://127.0.0.1:8000', 'http://127.0.0.1:8000'),
                                ('http://[::1]:8000', 'http://[::1]:8000')]:
            self.assertEqual(normalize_origin(value), expected)
        for value in ('*', 'null', 'https://*.github.io', ORIGIN + '/', ORIGIN + '/repo',
                      ORIGIN + '?', ORIGIN + '#x', 'https://user:pass@example.com',
                      'http://public.example', 'http://localhost.attacker.example',
                      'https://example.com\\@attacker.example', 'https://[::1]evil',
                      'https://example.com:0', 'https://example.com:65536'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_origin(value)

    def test_public_hosts_are_authorities_without_wildcards_or_forwarded_syntax(self):
        self.assertEqual(normalize_authority('SERVICE.example.com:8443'), 'service.example.com:8443')
        self.assertEqual(normalize_authority('[::1]:8877'), '[::1]:8877')
        for value in ('*', '*.example.com', 'https://example.com', 'example.com/path',
                      'user@example.com', 'example.com?x', 'example.com#x', 'example.com:',
                      '0.0.0.0:8877', '[::]:8877', '[::1]evil', 'example..com', 'example.com:65536'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_authority(value)

    def test_port_validation_rejects_invalid_environment_values(self):
        for value in (0, 65536, True, '1.5', '-1', 'nine', '', ' 8877', '80\n'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_port(value)
        self.assertEqual(validate_port('80'), 80)

    def test_token_validation_never_echoes_value(self):
        for token in (' secret ', 'secret\r\nx', '密钥', 'x' * 4097):
            with self.subTest(kind=len(token)), self.assertRaises(ValueError) as raised:
                TransportPolicy.from_environment('0.0.0.0', dict(DEPLOYMENT, XUNWEI_ACCESS_TOKEN=token))
            self.assertNotIn(token, str(raised.exception))

    def test_compare_digest_used_and_duplicate_security_headers_rejected(self):
        policy = TransportPolicy.from_environment('0.0.0.0', DEPLOYMENT)
        headers = Message()
        headers['Host'] = PUBLIC_HOST
        headers['Authorization'] = 'Bearer ' + TOKEN
        with patch('search_app.transport.hmac.compare_digest', wraps=__import__('hmac').compare_digest) as compare:
            self.assertIsNone(policy.check(headers, 8877, 'GET', '/api/history'))
            compare.assert_called_once_with(TOKEN.encode(), TOKEN.encode())
        headers['Authorization'] = 'Bearer ' + TOKEN
        self.assertEqual(policy.check(headers, 8877, 'GET', '/api/history')[0], 401)
        headers['Host'] = 'attacker.example'
        self.assertEqual(policy.check(headers, 8877, 'GET', '/api/health')[0], 403)

    def test_main_fails_before_opening_port_or_data_directory(self):
        with patch.dict('os.environ', {}, clear=True), patch('search_app.server.LocalHTTPServer') as server, \
                patch('search_app.server.App') as app, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(['--host', '0.0.0.0'])
        self.assertEqual(raised.exception.code, 2)
        server.assert_not_called()
        app.assert_not_called()

    def test_cli_and_environment_precedence_and_remote_no_browser(self):
        for env, argv, port in [(dict(DEPLOYMENT, XUNWEI_HOST='0.0.0.0', PORT='9001'), [], 9001),
                                (dict(DEPLOYMENT, XUNWEI_HOST='0.0.0.0', PORT='9001', XUNWEI_PORT='9002'), [], 9002),
                                (dict(DEPLOYMENT, XUNWEI_HOST='0.0.0.0', PORT='9001', XUNWEI_PORT='9002'), ['--port', '9003'], 9003)]:
            server = MagicMock()
            server.serve_forever.side_effect = KeyboardInterrupt
            app = MagicMock()
            app.data_dir = Path('isolated-test-data')
            output = io.StringIO()
            with patch.dict('os.environ', env, clear=True), patch('search_app.server.LocalHTTPServer', return_value=server) as constructor, \
                    patch('search_app.server.App', return_value=app), patch('search_app.server.webbrowser.open') as browser, \
                    patch('search_app.server.threading.Timer') as timer, contextlib.redirect_stdout(output):
                self.assertEqual(main(argv), 0)
            self.assertEqual(constructor.call_args.args[0], ('0.0.0.0', port))
            browser.assert_not_called()
            timer.assert_not_called()
            self.assertNotIn(TOKEN, output.getvalue())
            server.server_close.assert_called_once()


class PagesHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        with patch.dict('os.environ', {}, clear=True):
            self.app = App(self.directory.name)
        policy = TransportPolicy.from_environment('127.0.0.1', DEPLOYMENT)
        self.server = LocalHTTPServer(('127.0.0.1', 0), Handler, transport=policy)
        self.server.app = self.app
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)
        self.directory.cleanup()

    def request(self, method='GET', path='/api/config', headers=None, data=None, authenticated=True):
        final = {'Host': PUBLIC_HOST, 'Origin': ORIGIN}
        if authenticated:
            final['Authorization'] = 'Bearer ' + TOKEN
        final.update(headers or {})
        final = {key: value for key, value in final.items() if value is not None}
        body = json.dumps(data).encode() if data is not None else None
        if body:
            final.setdefault('Content-Type', 'application/json')
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body, final)
            response = connection.getresponse()
            content = response.read()
            return response.status, dict(response.getheaders()), json.loads(content) if content else None
        finally:
            connection.close()

    def test_health_public_but_all_other_api_routes_need_bearer(self):
        self.assertEqual(self.request(path='/api/health', authenticated=False)[0], 200)
        for path in ('/api/config', '/api/history', '/api/library', '/api/platforms', '/api/jobs/' + 'a' * 32, '/api', '/api/unknown', '/%61pi/config'):
            with self.subTest(path=path):
                status, headers, value = self.request(path=path, authenticated=False)
                self.assertEqual(status, 401)
                self.assertEqual(headers['Access-Control-Allow-Origin'], ORIGIN)
                self.assertEqual(headers['Vary'], 'Origin')
                self.assertIn('Bearer', headers['WWW-Authenticate'])
                self.assertNotIn(TOKEN, json.dumps(value))
        self.assertEqual(self.request(method='POST', path='/api/health', data={}, authenticated=False)[0], 401)
        self.assertEqual(self.request(method='PATCH', path='/api/config', data={}, authenticated=False)[0], 401)

    def test_rebinding_forwarded_headers_and_unlisted_origins_rejected(self):
        for path in ('/api/health', '/api/config'):
            for headers in ({'Host': 'attacker.example', 'X-Forwarded-Host': PUBLIC_HOST, 'Forwarded': 'host=' + PUBLIC_HOST},
                            {'Host': 'localhost:' + str(self.server.server_port)},
                            {'Origin': 'https://other.github.io'}, {'Origin': ORIGIN + '/repo'}, {'Origin': 'null'}):
                with self.subTest(path=path, headers=headers):
                    status, response_headers, _ = self.request(path=path, headers=headers)
                    self.assertEqual(status, 403)
                    self.assertEqual(response_headers['Vary'], 'Origin')
                    if headers.get('Origin'):
                        self.assertNotIn('Access-Control-Allow-Origin', response_headers)

    def test_allowed_pages_cross_site_and_cli_without_origin(self):
        self.assertEqual(self.request(headers={'Sec-Fetch-Site': 'cross-site'})[0], 200)
        self.assertEqual(self.request(headers={'Origin': None})[0], 200)
        self.assertEqual(self.request(headers={'Origin': None}, authenticated=False)[0], 401)
        self.assertEqual(self.request(headers={'Authorization': 'Bearer wrong'})[0], 401)
        self.assertEqual(self.request(headers={'Authorization': TOKEN})[0], 401)

    def test_strict_preflight_never_requires_token_or_credentials(self):
        for method in ('GET', 'POST', 'PUT', 'DELETE'):
            status, headers, value = self.request('OPTIONS', authenticated=False,
                                                 headers={'Access-Control-Request-Method': method,
                                                          'Access-Control-Request-Headers': 'Authorization, Content-Type'})
            self.assertEqual(status, 204)
            self.assertIsNone(value)
            self.assertEqual(headers['Access-Control-Allow-Origin'], ORIGIN)
            self.assertIn(method, headers['Access-Control-Allow-Methods'])
            self.assertIn('Authorization', headers['Access-Control-Allow-Headers'])
            self.assertNotIn('Access-Control-Allow-Credentials', headers)
            self.assertNotIn('*', headers.values())
        for headers in ({'Access-Control-Request-Method': 'PATCH'},
                        {'Access-Control-Request-Method': 'GET', 'Access-Control-Request-Headers': 'Cookie'},
                        {'Access-Control-Request-Method': 'GET', 'Access-Control-Request-Headers': 'Authorization,'},
                        {'Origin': None, 'Access-Control-Request-Method': 'GET'}, {}):
            self.assertEqual(self.request('OPTIONS', headers=headers, authenticated=False)[0], 403)

    def test_success_and_application_errors_share_cors(self):
        checks = [self.request(), self.request(path='/api/unknown'),
                  self.request('PUT', data={'model': ''})]
        with patch.object(self.app, 'create_job', side_effect=RuntimeError('upstream private value')):
            checks.append(self.request('POST', '/api/search', data={'query': '公开资料'}))
        with patch.object(self.app.storage, 'list_history', side_effect=RuntimeError('upstream private value')):
            checks.append(self.request(path='/api/history'))
        self.assertEqual([check[0] for check in checks], [200, 404, 400, 500, 500])
        for _, headers, value in checks:
            self.assertEqual(headers['Access-Control-Allow-Origin'], ORIGIN)
            self.assertEqual(headers['Vary'], 'Origin')
            self.assertNotIn('Access-Control-Allow-Credentials', headers)
            self.assertNotIn('upstream private value', json.dumps(value))

    def test_transport_token_not_in_public_config_or_saved_model_settings(self):
        self.app.config['_access_token'] = TOKEN
        status, _, public = self.request('PUT', data={'model': 'test-model', 'XUNWEI_ACCESS_TOKEN': TOKEN, 'access_token': TOKEN})
        self.assertEqual(status, 200)
        self.assertNotIn(TOKEN, json.dumps(public))
        self.assertNotIn(TOKEN, self.app.config_path.read_text(encoding='utf-8'))
        self.assertNotIn('_access_token', self.app.config)
        self.assertEqual(self.app.storage.list_history(), [])
        self.assertEqual(self.app.storage.list_documents(), [])

    def test_connection_csp_and_only_public_catalog_json_are_served(self):
        status, headers, catalog = self.request(path='/platforms.json', authenticated=False)
        self.assertEqual(status, 200)
        self.assertIn('application/json', headers['Content-Type'])
        self.assertIsInstance(catalog, (dict, list))
        self.assertIn("connect-src 'self' https: http://localhost:* http://127.0.0.1:*;", headers['Content-Security-Policy'])
        self.assertNotIn('[::1]', headers['Content-Security-Policy'])
        self.assertIn("script-src 'self'", headers['Content-Security-Policy'])
        for path in ('/settings.json', '/.local/settings.json', '/%2e%2e/.local/settings.json', '/arbitrary.json'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path=path, authenticated=False)[0], 404)

    def test_unauthorized_mutations_have_no_storage_or_config_effect(self):
        with patch.object(self.app, 'create_job') as create:
            self.assertEqual(self.request('POST', '/api/search', data={'query': '公开资料'}, authenticated=False)[0], 401)
            create.assert_not_called()
        self.assertEqual(self.request('POST', '/api/import', data={'title': '资料', 'text': '这是一段满足长度的公开测试文字'}, authenticated=False)[0], 401)
        self.assertEqual(self.request('PUT', data={'model': 'changed'}, authenticated=False)[0], 401)
        self.assertEqual(self.app.storage.list_documents(), [])
        self.assertFalse(self.app.config_path.exists())

    def test_authorized_crud_over_pages_origin(self):
        status, _, imported = self.request('POST', '/api/import', data={'title': '公开资料', 'text': '这是仅用于隔离接口测试的公开资料正文。'})
        self.assertEqual(status, 201)
        self.assertEqual(len(self.request(path='/api/library')[2]['items']), 1)
        self.assertEqual(self.request('DELETE', '/api/library/' + imported['id'])[0], 200)
        self.assertEqual(self.request(path='/api/library')[2]['items'], [])

    def test_default_local_origin_guard_still_rejects_cross_site(self):
        self.server.transport = TransportPolicy()
        host = '127.0.0.1:' + str(self.server.server_port)
        headers = {'Host': host, 'Origin': 'http://' + host, 'Authorization': None}
        self.assertEqual(self.request(headers=headers, authenticated=False)[0], 200)
        headers['Origin'] = ORIGIN
        self.assertEqual(self.request(headers=headers)[0], 403)
        headers['Origin'] = 'http://' + host
        headers['Sec-Fetch-Site'] = 'cross-site'
        self.assertEqual(self.request(headers=headers)[0], 403)


if __name__ == '__main__':
    unittest.main()
