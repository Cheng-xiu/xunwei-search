import io
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from search_app.ai import AIError, NoRedirect, _PublicAIDeadline, _PublicAIHTTPSHandler, _public_ai_socket, chat
from search_app.providers import _HTTPSHandler


class PublicAITransportTests(unittest.TestCase):
    def config(self, base='https://models.example.com/v1', public=True):
        return {'api_key': 'synthetic-visitor-key', 'model': 'test-model',
                'base_url': base, '_public_network': public}

    def response(self):
        return io.BytesIO(json.dumps({'choices': [{'message': {'content': 'OK'}}]}).encode())

    def test_public_private_literal_urls_fail_before_a_request(self):
        for base in ('http://localhost:8877', 'http://127.0.0.1:8877',
                     'https://127.0.0.1', 'https://10.0.0.1',
                     'https://169.254.169.254', 'https://[::1]',
                     'https://models.example.com:8443/v1'):
            with self.subTest(base=base), patch('search_app.ai.urllib.request.build_opener') as opener:
                with self.assertRaises(AIError):
                    chat(self.config(base), 'test', 'test')
                opener.assert_not_called()

    def test_public_opener_pins_dns_disables_proxies_and_does_not_redirect(self):
        with patch('search_app.ai.urllib.request.build_opener') as build:
            build.return_value.open.return_value = self.response()
            self.assertEqual(chat(self.config(), 'test', 'test'), 'OK')
            handlers = build.call_args.args
            self.assertTrue(any(isinstance(item, _HTTPSHandler) for item in handlers))
            self.assertTrue(any(isinstance(item, NoRedirect) for item in handlers))
            proxy = next(item for item in handlers if isinstance(item, urllib.request.ProxyHandler))
            self.assertEqual(proxy.proxies, {})
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic-visitor-key')
            self.assertEqual(request.full_url, 'https://models.example.com/v1/chat/completions')

    def test_public_dns_private_or_mixed_answers_never_connect(self):
        private = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', 443))
        public = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('8.8.8.8', 443))
        for addresses in ([private], [public, private]):
            with self.subTest(addresses=len(addresses)), \
                    patch('search_app.providers.socket.getaddrinfo', return_value=addresses), \
                    patch('search_app.providers.socket.socket') as make_socket:
                with self.assertRaises(AIError):
                    chat(self.config(), 'test', 'test')
                make_socket.assert_not_called()

    def test_local_mode_still_accepts_a_loopback_model(self):
        with patch('search_app.ai.urllib.request.build_opener') as build:
            build.return_value.open.return_value = self.response()
            self.assertEqual(chat(self.config('http://127.0.0.1:9000/v1', False), 'test', 'test'), 'OK')
            self.assertEqual(len(build.call_args.args), 1)
            self.assertIsInstance(build.call_args.args[0], NoRedirect)

    def test_redirect_cannot_forward_visitor_credentials(self):
        with patch('search_app.ai.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = urllib.error.HTTPError(
                'https://models.example.com/v1/chat/completions', 302, 'redirect',
                {'Location': 'https://elsewhere.example.com'}, None)
            with self.assertRaises(AIError):
                chat(self.config(), 'test', 'test')
            self.assertEqual(build.return_value.open.call_count, 1)

    def test_slow_drip_body_has_a_total_deadline(self):
        class Drip(io.BytesIO):
            reads = 0
            def read1(self, size=-1):
                self.reads += 1
                time.sleep(0.025)
                return b'x'
        response = Drip()
        with patch('search_app.ai.urllib.request.build_opener') as build:
            build.return_value.open.return_value = response
            started = time.monotonic()
            with self.assertRaises(AIError):
                chat(self.config(), 'test', 'test', timeout=0.08)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertLess(response.reads, 10)

    def test_deadline_closes_a_socket_still_waiting_for_headers(self):
        class WaitingSocket:
            closed = threading.Event()
            shutdowns = 0
            def shutdown(self, how):
                self.shutdowns += 1
                self.closed.set()
            def close(self):
                self.closed.set()
        connection = WaitingSocket()
        def build(*handlers):
            handler = next(item for item in handlers if isinstance(item, _PublicAIHTTPSHandler))
            class Opener:
                def open(self, request, timeout):
                    handler.request_deadline.attach(connection)
                    connection.closed.wait(1)
                    return io.BytesIO(b'{}')
            return Opener()
        with patch('search_app.ai.urllib.request.build_opener', side_effect=build):
            started = time.monotonic()
            with self.assertRaises(AIError):
                chat(self.config(), 'test', 'test', timeout=0.08)
            self.assertTrue(connection.closed.is_set())
            self.assertGreater(connection.shutdowns, 0)
            self.assertLess(time.monotonic() - started, 0.5)

    def test_cancellation_closes_an_inflight_public_socket(self):
        cancel = threading.Event()
        closed = threading.Event()
        class WaitingSocket:
            def shutdown(self, how):
                closed.set()
            def close(self):
                closed.set()
        def build(*handlers):
            handler = next(item for item in handlers if isinstance(item, _PublicAIHTTPSHandler))
            class Opener:
                def open(self, request, timeout):
                    handler.request_deadline.attach(WaitingSocket())
                    cancel.set()
                    closed.wait(1)
                    return io.BytesIO(b'{}')
            return Opener()
        config = {**self.config(), '_cancel_event': cancel}
        with patch('search_app.ai.urllib.request.build_opener', side_effect=build):
            with self.assertRaises(AIError):
                chat(config, 'test', 'test', timeout=5)
            self.assertTrue(closed.wait(0.5))

    def test_slow_dns_does_not_extend_request_deadline_and_resolvers_are_bounded(self):
        release = threading.Event()
        started = threading.Event()
        def resolve(host, port):
            started.set()
            release.wait(1)
            return []
        with patch('search_app.ai._resolve_public', side_effect=resolve), \
                patch('search_app.ai._PUBLIC_AI_DNS_GATE', threading.BoundedSemaphore(1)):
            deadline = _PublicAIDeadline(0.08)
            try:
                begin = time.monotonic()
                with self.assertRaises(AIError):
                    _public_ai_socket('models.example.com', 443, deadline)
                self.assertTrue(started.is_set())
                self.assertLess(time.monotonic() - begin, 0.5)
                another = _PublicAIDeadline(1)
                try:
                    with self.assertRaises(Exception) as error:
                        _public_ai_socket('models.example.com', 443, another)
                    self.assertIn('繁忙', str(error.exception))
                finally:
                    another.finish()
            finally:
                release.set()
                deadline.finish()

    def test_initial_tcp_connect_is_closed_at_absolute_deadline(self):
        closed = threading.Event()
        class SlowConnection:
            def settimeout(self, timeout):
                pass
            def connect(self, address):
                closed.wait(1)
                raise OSError('closed')
            def shutdown(self, how):
                closed.set()
            def close(self):
                closed.set()
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('8.8.8.8', 443))]
        with patch('search_app.ai._resolve_public', return_value=addresses), \
                patch('search_app.ai.socket.socket', return_value=SlowConnection()):
            deadline = _PublicAIDeadline(0.08)
            try:
                begin = time.monotonic()
                with self.assertRaises(AIError):
                    _public_ai_socket('models.example.com', 443, deadline)
                self.assertTrue(closed.is_set())
                self.assertLess(time.monotonic() - begin, 0.5)
            finally:
                deadline.finish()


if __name__ == '__main__':
    unittest.main()
