"""Offline public-markup fixtures; these are not claims of live engine access.

Organic result examples use documented/publicly used HTML class structures.
The Baidu blank-script and Yandex challenge cases reproduce the actual response
shapes observed on 2026-10-08, without retaining challenge identifiers/cookies.
"""
import base64
import io
import json
import threading
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

from search_app import providers as p


BAIDU_RESULT = '''<html><head><title>GPIO - 百度搜索</title></head><body>
<div id="1" class="result c-container xpath-log new-pmd" mu="https://github.com/espressif/esp-idf/issues/123">
 <h3 class="t"><a href="https://www.baidu.com/link?url=opaque">ESP32 <em>GPIO</em> issue</a></h3>
 <div class="c-abstract">Reported light sleep interrupt behavior.</div>
</div></body></html>'''
GOOGLE_RESULT = '''<html><head><title>GPIO - Google Search</title></head><body>
<div class="MjjYud"><div class="g"><div class="yuRUbf">
 <a href="/url?q=https%3A%2F%2Fgithub.com%2Fespressif%2Fesp-idf%2Fissues%2F123&amp;sa=U"><h3>ESP32 GPIO issue</h3></a>
 </div><div class="VwiC3b">Reported light sleep <em>interrupt</em> behavior.</div></div></div>
</body></html>'''
YANDEX_RESULT = '''<html><head><title>GPIO — Yandex: search results</title></head><body>
<li class="serp-item"><div class="Organic"><h2 class="OrganicTitle">
 <a class="Link OrganicTitle-Link" href="https://github.com/espressif/esp-idf/issues/123">ESP32 GPIO issue</a>
 </h2><div class="OrganicTextContent"><span class="OrganicTextContentSpan">Reported light sleep interrupt behavior.</span></div></div></li>
</body></html>'''


class SearchEngineTests(unittest.TestCase):
    def setUp(self):
        gates = {engine: p._SourceGate(engine, 0) for engine in ('baidu', 'google', 'yandex')}
        replacement = patch.object(p, '_SEARCH_HTML_GATES', gates)
        replacement.start()
        self.addCleanup(replacement.stop)

    @staticmethod
    def response(document, engine='google', final=None):
        return document.encode(), {'Content-Type': 'text/html; charset=utf-8'}, final or p._ENGINE_SEARCH_URLS[engine].replace('{query}', 'GPIO'), 200

    def test_catalog_separates_engines_from_platforms_and_never_exposes_keys(self):
        config = {'tavily_key': 'synthetic-tavily-secret', 'brave_key': 'synthetic-brave-secret', 'searxng_url': 'https://search.example.com/prefix/'}
        catalog = p.search_engine_catalog(config)
        self.assertEqual(tuple(row['id'] for row in catalog), p.SEARCH_ENGINE_IDS)
        self.assertTrue(all(row['available'] for row in catalog))
        self.assertFalse(set(p.SEARCH_ENGINE_IDS).intersection(row['id'] for row in p.platform_catalog()))
        self.assertNotIn('synthetic-', json.dumps(catalog))
        self.assertEqual(next(row for row in catalog if row['id'] == 'searxng')['search_url'], 'https://search.example.com/prefix/search?q={query}')
        self.assertFalse(next(row for row in p.search_engine_catalog() if row['id'] == 'tavily')['configured'])

    def test_selected_engines_filter_only_generic_sources(self):
        direct = ['bilibili', 'github', 'stackoverflow']
        self.assertEqual(p.available_providers({'search_engines': ['google', 'yandex']}), ['google', 'yandex'] + direct)
        self.assertEqual(p.available_providers({'search_engines': ['tavily', 'brave']}), direct)
        self.assertEqual(p.available_providers({'search_engines': ['brave'], 'brave_key': 'synthetic'}), ['brave'] + direct)
        self.assertEqual(p.available_providers({'search_engines': []}), p.available_providers({}))
        self.assertNotIn('website', p.available_providers({'search_engines': ['website']}))

    def test_native_engine_links_encode_queries_and_omit_nonexistent_tavily_ui(self):
        query = '桃李苑 site:zhihu.com & more'
        links = p.search_engine_links(query, ['baidu', 'google', 'tavily', 'searxng'], {'searxng_url': 'https://search.example.com/'})
        self.assertEqual([row['engine'] for row in links], ['baidu', 'google', 'searxng'])
        for row in links:
            self.assertNotIn('platform', row)
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(row['url']).query)
            self.assertEqual(params.get('q', params.get('wd')), [query])

    def test_organic_markup_returns_actual_title_target_and_same_card_snippet(self):
        for engine, fixture in (('baidu', BAIDU_RESULT), ('google', GOOGLE_RESULT), ('yandex', YANDEX_RESULT)):
            with self.subTest(engine=engine), patch.object(p, '_request', return_value=self.response(fixture, engine)) as request:
                result = p.search_provider(engine, 'ESP32 GPIO', ['github'], 10, {})
                self.assertTrue(result['status']['ok'], result['status'])
                self.assertEqual(len(result['results']), 1)
                row = result['results'][0]
                self.assertEqual(row['url'], 'https://github.com/espressif/esp-idf/issues/123')
                self.assertEqual(row['title'], 'ESP32 GPIO issue')
                self.assertIn('light sleep interrupt behavior.', row['snippet'])
                self.assertEqual(row['platform'], 'github')
                self.assertEqual(row['engine'], engine)
                self.assertNotIn('body', row)
                self.assertEqual(request.call_count, 1)
                self.assertIn('site%3Agithub.com', request.call_args.args[0])

    def test_full_unicode_query_and_platform_filter_survive_the_new_sources(self):
        query = '中科大中区桃李苑哪道菜好吃'
        fixture = GOOGLE_RESULT.replace('github.com%2Fespressif%2Fesp-idf%2Fissues%2F123', 'zhihu.com%2Fquestion%2F123')
        with patch.object(p, '_request', return_value=self.response(fixture)) as request:
            result = p.search_provider('google', query, ['bilibili'], 10, {})
        self.assertEqual(result['results'], [])
        requested = urllib.parse.parse_qs(urllib.parse.urlsplit(request.call_args.args[0]).query)['q'][0]
        self.assertTrue(requested.startswith(query))
        self.assertIn('site:bilibili.com', requested)

    def test_static_wrapper_unwrap_is_bounded_and_rejects_private_or_ad_targets(self):
        destination = 'https://zhihu.com/question/123?utm_source=google'
        encoded = base64.urlsafe_b64encode(destination.encode()).decode().rstrip('=')
        wrappers = ['https://www.bing.com/ck/a?u=a1' + encoded,
                    'https://www.google.com/url?q=' + urllib.parse.quote(destination, safe=''),
                    'https://yandex.com/clck/jsredir?url=' + urllib.parse.quote(destination, safe='')]
        for wrapper in wrappers:
            self.assertEqual(p._unwrap_search_url(wrapper), 'https://zhihu.com/question/123')
        for target in ('http://127.0.0.1/secret', 'https://10.0.0.1/', 'https://u:p@example.com/', 'javascript:alert(1)', 'https://example.com:8443/', 'https://www.googleadservices.com/pagead/aclk?adurl=https://example.com/'):
            self.assertEqual(p._unwrap_search_url('https://www.google.com/url?q=' + urllib.parse.quote(target, safe='')), '')
        spoof = 'https://www.google.com.evil.example/url?q=https://zhihu.com/question/123'
        self.assertEqual(p._unwrap_search_url(spoof), p.canonical_url(spoof))
        self.assertEqual(p._unwrap_search_url('https://www.google.com/search?q=anything'), '')
        self.assertEqual(p._unwrap_search_url('https://www.bing.com/ck/a?u=a1%ZZ'), '')

    def test_bing_rss_wrapper_targets_also_use_the_shared_normalizer(self):
        target = base64.urlsafe_b64encode(b'https://zhihu.com/question/123').decode().rstrip('=')
        rss = '<rss><channel><item><title>Real title</title><link>https://www.bing.com/ck/a?u=a1' + target + '</link><description>Source summary</description></item></channel></rss>'
        with patch.object(p, '_request', return_value=(rss.encode(), {}, 'https://www.bing.com/search', 200)):
            result = p.search_provider('bing', 'query', ['zhihu'], 10, {})
        self.assertEqual(result['results'][0]['url'], 'https://zhihu.com/question/123')

    def test_ads_untitled_and_engine_self_links_do_not_become_results(self):
        cases = [GOOGLE_RESULT.replace('<div class="MjjYud">', '<div id="tads" class="MjjYud">'),
                 GOOGLE_RESULT.replace('ESP32 GPIO issue', ''),
                 GOOGLE_RESULT.replace('/url?q=https%3A%2F%2Fgithub.com%2Fespressif%2Fesp-idf%2Fissues%2F123&amp;sa=U', '/search?q=other'),
                 BAIDU_RESULT.replace('href="https://www.baidu.com/link?url=opaque"', 'href="https://www.baidu.com/baidu.php?url=advertisement"')]
        for fixture in cases:
            engine = 'baidu' if '百度' in fixture else 'google'
            with self.subTest(engine=engine), patch.object(p, '_request', return_value=self.response(fixture, engine)):
                self.assertEqual(p.search_provider(engine, 'query', ['web'], 10, {})['results'], [])

    def test_captcha_word_in_a_genuine_result_is_not_a_challenge(self):
        fixture = GOOGLE_RESULT.replace('ESP32 GPIO issue', 'How a CAPTCHA works').replace('GPIO - Google Search', 'CAPTCHA - Google Search')
        with patch.object(p, '_request', return_value=self.response(fixture)):
            self.assertTrue(p.search_provider('google', 'CAPTCHA', ['github'], 10, {})['status']['ok'])

    def test_non_result_and_hidden_ancestors_cannot_forge_organic_hits(self):
        wrappers = [('nav', ''), ('footer', ''), ('form', ''), ('template', ''), ('noscript', ''),
                    ('div', ' hidden'), ('div', ' aria-hidden="true"'), ('div', ' style="display:none"'),
                    ('div', ' style="DISPLAY: /* comment */ none !important;"'),
                    ('div', ' style="visibility:hidden"'), ('div', ' style="content-visibility: hidden"')]
        for engine, fixture in (('google', GOOGLE_RESULT), ('baidu', BAIDU_RESULT), ('yandex', YANDEX_RESULT)):
            for tag, attrs in wrappers:
                with self.subTest(engine=engine, tag=tag, attrs=attrs):
                    parser = p._SearchHTML()
                    parser.feed(f'<{tag}{attrs}>' + fixture + f'</{tag}>')
                    self.assertEqual(p._search_html_rows(parser, engine, p._ENGINE_SEARCH_URLS[engine]), [])

    def test_hidden_card_or_title_is_excluded_while_visible_neighbor_survives(self):
        for attributes in (' hidden', ' aria-hidden=" TRUE "', ' style="display: none;"'):
            for marker in ('<div class="g"', '<h3'):
                with self.subTest(attributes=attributes, marker=marker):
                    hidden = GOOGLE_RESULT.replace(marker, marker + attributes).replace('issues%2F123', 'issues%2F999')
                    fixture = hidden + GOOGLE_RESULT
                    with patch.object(p, '_request', return_value=self.response(fixture)):
                        result = p.search_provider('google', 'query', ['github'], 10, {})
                    self.assertTrue(result['status']['ok'])
                    self.assertEqual([row['url'] for row in result['results']], ['https://github.com/espressif/esp-idf/issues/123'])

    def test_visible_results_ignore_hidden_title_and_snippet_fragments(self):
        fixture = GOOGLE_RESULT.replace('<h3>ESP32 GPIO issue</h3>', '<h3>ESP32<span hidden>FAKE</span> GPIO issue</h3>')
        fixture = fixture.replace('Reported light sleep', '<span aria-hidden="true">Hidden snippet</span>Reported light sleep')
        fixture = fixture.replace('<div class="g">', '<div class="g" aria-hidden="false" style="display:block; visibility:visible">')
        with patch.object(p, '_request', return_value=self.response(fixture)):
            result = p.search_provider('google', 'query', ['github'], 10, {})
        self.assertEqual(result['results'][0]['title'], 'ESP32 GPIO issue')
        self.assertEqual(result['results'][0]['snippet'], 'Reported light sleep interrupt behavior.')

    def test_live_observed_challenge_and_blank_script_are_errors_not_zero_hits(self):
        cases = [('yandex', '<title>Are you not a robot?</title><form action="/showcaptcha"><button>I am not a robot</button></form>', 'https://yandex.com/showcaptcha?cc=1'),
                 ('baidu', '<html><head><script>location.replace(location.href.replace("https://", "http://"));</script></head><body><noscript><meta http-equiv="refresh" content="0;url=http://www.baidu.com/"></noscript></body></html>', 'https://www.baidu.com/s?wd=test'),
                 ('google', '<title>Before you continue to Google</title>', 'https://consent.google.com/m')]
        for engine, fixture, final in cases:
            with self.subTest(engine=engine), patch.object(p, '_request', return_value=self.response(fixture, engine, final)):
                result = p.search_provider(engine, 'query', ['web'], 10, {})
                self.assertFalse(result['status']['ok'])
                self.assertTrue(result['status']['error'])
                self.assertEqual(result['results'], [])

    def test_challenge_causes_cooldown_and_stops_queued_queries(self):
        with patch.object(p, '_request', return_value=self.response('<title>Are you not a robot?</title>', 'yandex')) as request:
            first = p.search_provider('yandex', 'query one', ['web'], 10, {})
            second = p.search_provider('yandex', 'query two', ['web'], 10, {})
        self.assertIn('验证', first['status']['error'])
        self.assertIn('冷却', second['status']['error'])
        self.assertEqual(request.call_count, 1)

    def test_http_denial_is_honest_and_does_not_retry(self):
        with patch.object(p, '_request', side_effect=p.PublicFetchError('HTTP 429', http_status=429)) as request:
            result = p.search_provider('google', 'query', ['web'], 10, {})
            again = p.search_provider('google', 'query', ['web'], 10, {})
        self.assertFalse(result['status']['ok'])
        self.assertIn('429', result['status']['error'])
        self.assertIn('冷却', again['status']['error'])
        self.assertEqual(request.call_count, 1)

    def test_explicit_no_results_is_success_but_unknown_markup_is_error(self):
        for document, expected in (('<title>Search</title><p>Your search did not match any documents.</p>', True), ('<title>Search</title><main></main>', False)):
            with patch.object(p, '_request', return_value=self.response(document)):
                result = p.search_provider('google', 'query', ['web'], 10, {})
            self.assertEqual(result['status']['ok'], expected)
            self.assertEqual(result['results'], [])

    def test_baidu_prefers_complete_embedded_url_never_guesses_display_breadcrumb(self):
        fixture = BAIDU_RESULT.replace(' mu="https://github.com/espressif/esp-idf/issues/123"', '')
        fixture = fixture.replace('<div class="c-abstract">', '<span class="c-showurl">github.com/.../issues/...</span><div class="c-abstract">')
        with patch.object(p, '_request', return_value=self.response(fixture, 'baidu')), patch.object(p, '_baidu_target', return_value='') as resolve:
            result = p.search_provider('baidu', 'query', ['web'], 10, {})
        self.assertEqual(result['results'], [])
        resolve.assert_called_once()
        self.assertIn('目标 URL', result['status']['warning'])

    def test_baidu_opaque_resolution_budget_is_two_and_preserves_verified_rows(self):
        card = '<div id="{id}" class="result c-container"><h3><a href="https://www.baidu.com/link?url=opaque{id}">Result {id}</a></h3><div class="c-abstract">Actual summary</div></div>'
        fixture = '<html>' + ''.join(card.format(id=i) for i in range(1, 8)) + '</html>'
        with patch.object(p, '_request', return_value=self.response(fixture, 'baidu')) as request, patch.object(p, '_baidu_target', side_effect=['https://example.com/1', 'https://example.com/2']) as resolve:
            result = p.search_provider('baidu', 'query', ['web'], 10, {})
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(request.call_count, 1)
        self.assertEqual([row['url'] for row in result['results']], ['https://example.com/1', 'https://example.com/2'])
        self.assertIn('最多 2 次', result['status']['coverage'])

    def test_baidu_location_is_captured_without_opening_target_body(self):
        response = urllib.error.HTTPError('https://www.baidu.com/link?url=opaque', 302, 'Found', {'Location': 'https://github.com/owner/project/issues/1'}, io.BytesIO())
        with patch.object(p.urllib.request, 'build_opener') as build:
            build.return_value.open.side_effect = response
            self.assertEqual(p._baidu_target('https://www.baidu.com/link?url=opaque', None), 'https://github.com/owner/project/issues/1')
        self.assertEqual(build.return_value.open.call_count, 1)

    def test_baidu_private_location_is_rejected_without_connecting(self):
        response = urllib.error.HTTPError('https://www.baidu.com/link?url=opaque', 302, 'Found', {'Location': 'http://127.0.0.1/admin'}, io.BytesIO())
        with patch.object(p.urllib.request, 'build_opener') as build:
            build.return_value.open.side_effect = response
            with self.assertRaises(p.PublicFetchError):
                p._baidu_target('https://www.baidu.com/link?url=opaque', None)
        self.assertEqual(build.return_value.open.call_count, 1)

    def test_baidu_meta_target_does_not_execute_javascript(self):
        html = '<meta http-equiv="refresh" content="0;url=https://example.com/post"><script>location="http://127.0.0.1/"</script>'
        with patch.object(p, '_request', return_value=self.response(html, 'baidu')):
            self.assertEqual(p._baidu_target('https://www.baidu.com/link?url=opaque', None), 'https://example.com/post')

    def test_cancel_before_and_after_serp_prevents_more_work(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(p, '_request') as request:
            result = p.search_provider('google', 'query', ['web'], 10, {'_cancel_event': cancel})
        request.assert_not_called()
        self.assertTrue(result['status']['cancelled'])
        cancel.clear()
        def respond(*args, **kwargs):
            cancel.set()
            return self.response(BAIDU_RESULT, 'baidu')
        with patch.object(p, '_request', side_effect=respond), patch.object(p, '_baidu_target') as resolve:
            result = p.search_provider('baidu', 'query', ['web'], 10, {'_cancel_event': cancel})
        resolve.assert_not_called()
        self.assertTrue(result['status']['cancelled'])

    def test_cancellation_interrupts_a_source_rate_wait(self):
        event = threading.Event()
        p._SEARCH_HTML_GATES['google'].next_request_at = p.time.monotonic() + 5
        timer = threading.Timer(0.02, event.set)
        timer.start()
        try:
            with patch.object(p, '_request') as request:
                result = p.search_provider('google', 'query', ['web'], 10, {'_cancel_event': event})
            self.assertTrue(result['status']['cancelled'])
            request.assert_not_called()
        finally:
            timer.cancel()

    def test_all_three_new_engines_route_custom_sites_and_filter_spoofed_domains(self):
        for engine in ('baidu', 'google', 'yandex'):
            rows = [{'title': 'Scoped', 'url': 'https://forum.example.com/thread', 'snippet': 'Source'}, {'title': 'Spoof', 'url': 'https://example.com.evil.test/thread', 'snippet': 'Do not include'}]
            with self.subTest(engine=engine), patch.object(p, 'search_provider', return_value={'results': rows, 'status': {'provider': engine, 'ok': True, 'count': 2}}) as search:
                result = p.search_custom_site({'domain': 'example.com'}, 'query', 10, {'_engine': engine})
                self.assertEqual(search.call_args.args[:3], (engine, 'query site:example.com', ['web']))
                self.assertEqual(len(result['results']), 1)
                self.assertEqual(result['results'][0]['platform'], 'website')
                self.assertEqual(result['results'][0]['engine'], engine)
                self.assertEqual(result['status']['count'], 1)

    def test_custom_sites_cannot_route_a_direct_platform_as_a_generic_engine(self):
        with patch.object(p, 'search_provider') as search:
            result = p.search_custom_site({'domain': 'example.com'}, 'query', 10, {'_engine': 'github'})
        self.assertFalse(result['status']['ok'])
        search.assert_not_called()


if __name__ == '__main__':
    unittest.main()
