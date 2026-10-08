import io
import gzip
import json
import socket
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse
import zlib

from search_app import providers as p


class URLSafetyTests(unittest.TestCase):
    def test_rejects_local_credentials_unusual_ports_and_non_http(self):
        for url in ("http://localhost/test", "http://127.0.0.1/", "http://2130706433/", "http://0177.0.0.1/", "http://0x7f000001/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://169.254.169.254/", "https://10.0.0.1/", "http://192.168.1.1/", "http://172.16.0.1/", "https://user:secret@zhihu.com/", "https://zhihu.com:8443/", "https://machine.local/a", "https://intranet/", "file:///etc/passwd", "javascript:alert(1)", "https://zhihu.com\\@evil.com/", "https://zhihu.com/\nfoo"):
            with self.subTest(url=url):
                self.assertEqual(p.canonical_url(url), "")

    def test_canonical_keeps_required_query_and_encoded_path(self):
        self.assertEqual(p.canonical_url("https://WWW.ZHIHU.COM:443/a%2Fb?utm_source=x&q=%E4%B8%AD%E7%A7%91%E5%A4%A7#reply"), "https://www.zhihu.com/a%2Fb?q=%E4%B8%AD%E7%A7%91%E5%A4%A7")
        self.assertIn("xsec_token=public-token", p.canonical_url("https://www.xiaohongshu.com/explore/123?xsec_token=public-token&utm_source=test"))

    def test_platform_classification_cannot_be_spoofed(self):
        self.assertEqual(p.platform_of("https://www.zhihu.com/question/1"), "zhihu")
        self.assertEqual(p.platform_of("https://www.zhihu.com.evil.example/a"), "web")
        self.assertEqual(p.platform_of("https://evil.example/?url=https://zhihu.com"), "web")
        self.assertEqual(p.platform_of("https://b23.tv/abc"), "bilibili")

    @patch.object(p.socket, "getaddrinfo")
    def test_mixed_public_private_dns_is_rejected(self, lookup):
        lookup.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)), (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with self.assertRaises(p.PublicFetchError):
            p._resolve_public("example.com", 443)

    @patch.object(p.socket, "socket")
    @patch.object(p.socket, "getaddrinfo")
    def test_socket_connects_to_validated_ip_without_second_dns(self, lookup, socket_factory):
        lookup.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        connection = socket_factory.return_value
        self.assertIs(p._public_socket("example.com", 443, 2), connection)
        lookup.assert_called_once()
        connection.connect.assert_called_once_with(("8.8.8.8", 443))

    @patch.object(p.urllib.request, "build_opener")
    def test_redirect_to_private_address_is_rejected(self, build):
        build.return_value.open.side_effect = urllib.error.HTTPError("https://example.com/", 302, "Found", {"Location": "http://127.0.0.1/secret"}, io.BytesIO())
        with self.assertRaises(p.PublicFetchError):
            p._request("https://example.com/")
        self.assertEqual(build.return_value.open.call_count, 1)

    @patch.object(p.urllib.request, "build_opener")
    def test_api_key_is_never_forwarded_on_redirect(self, build):
        build.return_value.open.side_effect = urllib.error.HTTPError("https://example.com/", 302, "Found", {"Location": "https://other.example/"}, io.BytesIO())
        with self.assertRaisesRegex(p.PublicFetchError, "凭据"):
            p._request("https://example.com/", headers={"Authorization": "Bearer test-private"})
        self.assertEqual(build.return_value.open.call_count, 1)


class _FakeBilibiliClock(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.sleeps = []
        for name, value in (("_bilibili_next_request_at", 0.0), ("_bilibili_cooldown_until", 0.0)):
            reset = patch.object(p, name, value)
            reset.start()
            self.addCleanup(reset.stop)
        monotonic = patch.object(p.time, "monotonic", side_effect=lambda: self.now)
        monotonic.start()
        self.addCleanup(monotonic.stop)
        sleep = patch.object(p.time, "sleep", side_effect=self.advance)
        sleep.start()
        self.addCleanup(sleep.stop)

    def advance(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class ProviderTests(_FakeBilibiliClock):
    def test_host_filtering_and_tracking_deduplication(self):
        rows = [{"title": "A", "url": "https://www.zhihu.com/question/1?utm_source=a"}, {"title": "B", "url": "https://www.zhihu.com/question/1?utm_source=b"}, {"title": "C", "url": "https://www.zhihu.com.evil.example/a"}, {"title": "D", "url": "https://www.bilibili.com/video/BV123"}]
        result = p._normalize_results(rows, ["zhihu"], "bing", 10)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], "https://www.zhihu.com/question/1")
        self.assertEqual(len(p._normalize_results(rows, ["web"], "bing", 10)), 3)

    @patch.object(p, "_request")
    def test_bing_reads_actual_rss_fields_and_filters(self, request):
        request.return_value = (b'<rss><channel><item><title>A &amp; B</title><link>https://www.zhihu.com/question/1</link><description>Evidence text</description></item><item><title>Unrelated site</title><link>https://example.com/</link></item></channel></rss>', {}, "https://www.bing.com/", 200)
        result = p.search_provider("bing", "test", ["zhihu"], 10, {})
        self.assertTrue(result["status"]["ok"])
        self.assertEqual(result["status"]["count"], 1)
        self.assertEqual(result["results"][0]["title"], "A & B")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.call_args.args[0]).query)["q"][0]
        self.assertIn("site:zhihu.com", query)

    @patch.object(p, "_request")
    def test_bing_html_challenge_is_not_a_successful_empty_search(self, request):
        request.return_value = (b"<html><body>Login</body></html>", {}, "https://www.bing.com/", 200)
        result = p.search_provider("bing", "test", ["web"], 10, {})
        self.assertFalse(result["status"]["ok"])

    @patch.object(p, "_request")
    def test_ddg_decodes_public_result_redirect_and_nested_title(self, request):
        request.return_value = (b'<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.zhihu.com%2Fquestion%2F1">A <b>result</b></a><a class="result__snippet">Real <b>evidence</b>.</a></div>', {}, "https://html.duckduckgo.com/", 200)
        result = p.search_provider("duckduckgo", "test", ["zhihu"], 10, {})
        self.assertEqual(result["results"][0]["title"], "A result")
        self.assertEqual(result["results"][0]["snippet"], "Real evidence.")
        self.assertEqual(result["results"][0]["url"], "https://www.zhihu.com/question/1")

    @patch.object(p, "_request")
    def test_challenge_is_explicit_and_has_no_fabricated_results(self, request):
        request.return_value = (b'<form id="challenge-form">bots use duckduckgo</form>', {}, "https://html.duckduckgo.com/", 202)
        result = p.search_provider("duckduckgo", "test", ["web"], 10, {})
        self.assertFalse(result["status"]["ok"])
        self.assertEqual(result["results"], [])
        self.assertIn("人机验证", result["status"]["error"])

    @patch.object(p, "_json_request")
    def test_bilibili_preserves_measured_views_and_coverage(self, request):
        request.return_value = {"code": 0, "data": {"result": [{"title": '<em class="keyword">食堂</em>', "bvid": "BV123", "description": "原始简介", "play": 7, "pubdate": 1700000000}]}}
        result = p.search_provider("bilibili", "食堂", ["bilibili"], 10, {})
        self.assertEqual(result["results"][0]["views"], 7)
        self.assertEqual(result["results"][0]["title"], "食堂")
        self.assertIn("公开视频", result["status"]["coverage"])
        self.assertEqual(request.call_count, 1)

    @patch.object(p, "_json_request")
    def test_bilibili_deep_interleaves_three_passes_and_dedupes(self, request):
        def upstream(title, bvid, play=None):
            return {"title": title, "bvid": bvid, "play": play}
        request.side_effect = [
            {"code": 0, "data": {"result": [upstream("popular", "BV1", 2000), upstream("duplicate", "BV2", 100)]}},
            {"code": 0, "data": {"result": [upstream("recent", "BV3", 7), upstream("duplicate again", "BV2", 100)]}},
            {"code": 0, "data": {"result": [upstream("older recent", "BV4"), upstream("another", "BV5", 0)]}},
        ]
        result = p.search_provider("bilibili", "食堂", ["bilibili"], 4, {"_search_depth": "deep"})
        requests = [urllib.parse.parse_qs(urllib.parse.urlsplit(call.args[0]).query) for call in request.call_args_list]
        self.assertEqual([(r["order"][0], r["page"][0]) for r in requests], [("totalrank", "1"), ("pubdate", "1"), ("pubdate", "2")])
        self.assertEqual([row["title"] for row in result["results"]], ["popular", "recent", "older recent", "duplicate"])
        self.assertEqual(result["results"][1]["views"], 7)
        self.assertNotIn("views", result["results"][2])
        self.assertIn("最新发布第 2 页", result["status"]["coverage"])

    @patch.object(p, "_json_request")
    def test_bilibili_stops_further_pages_when_denied_preserves_partial_results(self, request):
        request.side_effect = [{"code": 0, "data": {"result": [{"title": "Real result", "bvid": "BV123", "play": 7}]}}, {"code": -412}, AssertionError("Must not request another page")]
        result = p.search_provider("bilibili", "食堂", ["bilibili"], 30, {"_search_depth": "deep"})
        self.assertEqual(request.call_count, 2)
        self.assertEqual(len(result["results"]), 1)
        self.assertTrue(result["status"]["partial"])
        self.assertIn("-412", result["status"]["warning"])
        self.assertNotIn("最新发布第 2 页", result["status"]["coverage"])

    @patch.object(p, "_bing", side_effect=RuntimeError("secret-key-that-must-not-leak"))
    def test_unexpected_errors_do_not_leak_credentials(self, _):
        result = p.search_provider("bing", "test", ["web"], 10, {})
        self.assertNotIn("secret-key", str(result))
        self.assertFalse(result["status"]["ok"])

    def test_optional_provider_config_aliases(self):
        self.assertEqual(p.available_providers({}), ["baidu", "bing", "google", "yandex", "duckduckgo", "bilibili", "github", "stackoverflow"])
        self.assertEqual(p.available_providers({"tavily_key": "t", "brave_key": "b", "searxng_url": "https://example.com/"}), ["baidu", "bing", "google", "yandex", "duckduckgo", "tavily", "brave", "searxng", "bilibili", "github", "stackoverflow"])

    def test_native_search_links_use_encoded_query(self):
        links = p.native_search_links("中科大 & 食堂", ["xiaohongshu", "zhihu", "bilibili"])
        self.assertEqual(len(links), 3)
        for link in links:
            self.assertNotIn(" ", link["url"])
            self.assertIn("%26", link["url"])


class BilibiliRateGuardTests(_FakeBilibiliClock):
    @patch.object(p, "_json_request", return_value={"code": 0, "data": {"result": []}})
    def test_outbound_requests_are_spaced_without_real_sleep(self, request):
        starts = []
        request.side_effect = lambda *args, **kwargs: (starts.append(self.now) or {"code": 0})
        for _ in range(3):
            p._bilibili_request("https://api.bilibili.com/test")
        self.assertEqual(starts, [1000.0, 1001.0, 1002.0])
        self.assertEqual(self.sleeps, [1.0, 1.0])

    @patch.object(p, "_json_request")
    def test_denial_cools_down_queued_calls_then_allows_later_user_search(self, request):
        for code in (403, 412, 429):
            with self.subTest(code=code):
                p._bilibili_next_request_at = p._bilibili_cooldown_until = 0
                request.reset_mock()
                request.side_effect = [p.PublicFetchError("upstream access denied", http_status=code), {"code": 0}]
                with self.assertRaises(p.PublicFetchError):
                    p._bilibili_request("https://api.bilibili.com/test")
                for _ in range(3):
                    with self.assertRaisesRegex(p.PublicFetchError, "冷却"):
                        p._bilibili_request("https://api.bilibili.com/test")
                self.assertEqual(request.call_count, 1)
                self.advance(60)
                self.assertEqual(p._bilibili_request("https://api.bilibili.com/test"), {"code": 0})
                self.assertEqual(request.call_count, 2)

    @patch.object(p, "_json_request", return_value={"code": -412})
    def test_json_access_denial_also_prevents_further_outbound_requests(self, request):
        first = p.search_provider("bilibili", "test", ["bilibili"], 30, {"_search_depth": "deep"})
        second = p.search_provider("bilibili", "another", ["bilibili"], 30, {"_search_depth": "deep"})
        self.assertIn("-412", first["status"]["error"])
        self.assertIn("冷却", second["status"]["error"])
        self.assertEqual(request.call_count, 1)

    @patch.object(p, "_bing", return_value=[{"title": "Other provider", "url": "https://example.com/"}])
    def test_bilibili_cooldown_does_not_block_other_providers(self, bing):
        p._bilibili_cooldown_until = self.now + 60
        result = p.search_provider("bing", "test", ["web"], 10, {})
        self.assertTrue(result["status"]["ok"])
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(self.sleeps, [])

    @patch.object(p, "_json_request")
    def test_process_wide_gate_prevents_concurrent_outbound_calls(self, request):
        entered_first = threading.Event()
        second_attempted = threading.Event()
        release_first = threading.Event()
        real_gate = threading.Semaphore(1)
        count_lock = threading.Lock()
        attempts = [0]

        class ObservableGate:
            def __enter__(self):
                with count_lock:
                    attempts[0] += 1
                    if attempts[0] == 2:
                        second_attempted.set()
                real_gate.acquire()

            def __exit__(self, *args):
                real_gate.release()

        def upstream(*args, **kwargs):
            if not entered_first.is_set():
                entered_first.set()
                if not release_first.wait(2):
                    raise AssertionError("test failed to release request")
            return {"code": 0}

        request.side_effect = upstream
        with patch.object(p, "_BILIBILI_GATE", ObservableGate()), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(p._bilibili_request, "https://api.bilibili.com/test")
            self.assertTrue(entered_first.wait(1))
            second = pool.submit(p._bilibili_request, "https://api.bilibili.com/test")
            try:
                self.assertTrue(second_attempted.wait(1))
                self.assertEqual(request.call_count, 1)
            finally:
                release_first.set()
            self.assertEqual(first.result(), {"code": 0})
            self.assertEqual(second.result(), {"code": 0})
        self.assertEqual(request.call_count, 2)


class PageExtractionTests(unittest.TestCase):
    @patch.object(p, "_request")
    def test_robots_disallow_prevents_page_fetch(self, request):
        request.return_value = (b"User-agent: *\nDisallow: /private\n", {"Content-Type": "text/plain"}, "https://example.com/robots.txt", 200)
        result = p.fetch_public_page("https://example.com/private")
        self.assertEqual(result["text"], "")
        self.assertIn("robots.txt", result["error"])
        self.assertEqual(request.call_count, 1)

    @patch.object(p, "_request")
    def test_extracts_real_text_excludes_scripts_and_limits_output(self, request):
        request.side_effect = [(b"", {}, "https://example.com/robots.txt", 404), (("<html><title>Page title</title><script>malicious script</script><article><p>" + "actual evidence " * 60 + "</p></article></html>").encode(), {"Content-Type": "text/html; charset=utf-8"}, "https://example.com/article", 200)]
        result = p.fetch_public_page("https://example.com/article", max_chars=200)
        self.assertEqual(result["title"], "Page title")
        self.assertNotIn("malicious script", result["text"])
        self.assertEqual(len(result["text"]), 200)
        self.assertNotIn("error", result)

    @patch.object(p, "_request", side_effect=p.PublicFetchError("network error"))
    def test_inaccessible_robots_fails_closed(self, request):
        result = p.fetch_public_page("https://example.com/article")
        self.assertEqual(result["text"], "")
        self.assertIn("robots.txt", result["error"])
        self.assertEqual(request.call_count, 1)


class CompressedResponseTests(unittest.TestCase):
    def response(self, payload, headers):
        response = Mock()
        response.headers = {"Content-Length": str(len(payload)), **headers}
        response.read1.side_effect = io.BytesIO(payload).read1
        response.status = 200
        return response

    @patch.object(p, "_check_robots")
    @patch.object(p.urllib.request, "build_opener")
    def test_gzip_html_is_decompressed_before_page_extraction(self, build, robots):
        text = "桃李苑的香菇滑鸡很好吃，鸡肉比较嫩。" * 8
        payload = gzip.compress(("<html><title>午餐记录</title><article>" + text + "</article></html>").encode())
        build.return_value.open.return_value = self.response(payload, {"cOnTeNt-EnCoDiNg": "GZip", "Content-Type": "text/html; charset=utf-8"})
        result = p.fetch_public_page("https://example.com/article")
        self.assertEqual(result["title"], "午餐记录")
        self.assertEqual(result["text"], text)
        self.assertNotIn("error", result)
        self.assertNotIn("\ufffd", result["text"])

    @patch.object(p.urllib.request, "build_opener")
    def test_gzip_json_is_decompressed_before_json_parser(self, build):
        expected = {"code": 0, "title": "真实中文搜索结果"}
        payload = gzip.compress(json.dumps(expected, ensure_ascii=False).encode())
        build.return_value.open.return_value = self.response(payload, {"Content-Encoding": "gzip", "Content-Type": "application/json"})
        self.assertEqual(p._json_request("https://example.com/api"), expected)

    @patch.object(p.urllib.request, "build_opener")
    def test_gzip_rss_is_decompressed_for_xml_provider(self, build):
        xml = b'<rss><channel><item><title>Public result</title><link>https://www.zhihu.com/question/1</link></item></channel></rss>'
        build.return_value.open.return_value = self.response(gzip.compress(xml), {"Content-Encoding": "gzip", "Content-Type": "application/rss+xml"})
        result = p.search_provider("bing", "test", ["zhihu"], 10, {})
        self.assertTrue(result["status"]["ok"])
        self.assertEqual(result["results"][0]["title"], "Public result")

    def test_decode_detects_missing_header_gzip_and_raw_or_wrapped_deflate(self):
        text = "中科大食堂原文，完整可读。"
        encoded = text.encode()
        raw_encoder = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        raw = raw_encoder.compress(encoded) + raw_encoder.flush()
        for payload, headers in ((gzip.compress(encoded), {}), (gzip.compress(encoded), {"Content-Encoding": "identity"}), (zlib.compress(encoded), {"Content-Encoding": "deflate"}), (raw, {"CONTENT-ENCODING": "Deflate"})):
            with self.subTest(headers=headers):
                self.assertEqual(p._decode(payload, headers), text)

    def test_truncated_or_corrupt_gzip_and_deflate_are_explicit_errors(self):
        encoded = ("证据原文" * 20).encode()
        for payload, coding in ((gzip.compress(encoded)[:-8], "gzip"), (gzip.compress(encoded)[:-1] + b"\xff", "gzip"), (zlib.compress(encoded)[:-3], "deflate")):
            with self.subTest(coding=coding, payload_length=len(payload)):
                with self.assertRaisesRegex(p.PublicFetchError, "压缩数据"):
                    p._decode(payload, {"Content-Encoding": coding})

    def test_decompression_bomb_is_bounded_at_default_limit(self):
        bomb = gzip.compress(b"x" * (p.MAX_BYTES + 1))
        self.assertLess(len(bomb), p.MAX_BYTES)
        with self.assertRaisesRegex(p.PublicFetchError, "解压后响应过大"):
            p._decode(bomb, {"Content-Encoding": "gzip"})

    @patch.object(p.urllib.request, "build_opener")
    def test_request_preserves_its_smaller_expanded_byte_limit(self, build):
        payload = gzip.compress(b"x" * 101)
        build.return_value.open.return_value = self.response(payload, {"Content-Encoding": "gzip"})
        with self.assertRaisesRegex(p.PublicFetchError, "解压后响应过大"):
            p._request("https://example.com/article", max_bytes=100)

    def test_unsupported_coding_and_invalid_trailing_data_are_rejected(self):
        for payload, coding in ((b"some compressed bytes", "br"), (gzip.compress(b"text") + b"junk", "gzip")):
            with self.subTest(coding=coding):
                with self.assertRaises(p.PublicFetchError):
                    p._decode(payload, {"Content-Encoding": coding})

    def test_multiple_gzip_members_share_one_expanded_limit(self):
        payload = gzip.compress(b"first ") + gzip.compress(b"second")
        self.assertEqual(p._decode(payload, {"Content-Encoding": "gzip"}), "first second")
        # Large enough compressed input, but the total expansion exceeds it.
        expanded = gzip.compress(b"a" * 100) + gzip.compress(b"b" * 100)
        with self.assertRaisesRegex(p.PublicFetchError, "解压后响应过大"):
            p._decode_content(expanded, {"Content-Encoding": "gzip"}, max_bytes=150)


class ExtendedPlatformTests(unittest.TestCase):
    def test_platform_domains_are_exactly_scoped_without_parent_host_leaks(self):
        cases = {"https://mp.weixin.qq.com/s/abc": "wechat", "https://news.qq.com/a": "web", "https://fake.mp.weixin.qq.com/a": "web", "https://mp.weixin.qq.com.evil.example/a": "web", "https://www.meituan.com/item/1": "meituan", "https://m.dianping.com/shop/1": "dianping", "https://www.douyin.com/video/1": "douyin", "https://www.iesdouyin.com/share/1": "douyin", "https://tieba.baidu.com/p/1": "tieba", "https://baike.baidu.com/item/1": "web", "https://www.douban.com/group/topic/1": "douban"}
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(p.platform_of(url), expected)

    def test_catalog_explains_access_and_native_links_use_verified_routes(self):
        catalog = {row["id"]: row for row in p.platform_catalog()}
        self.assertEqual(catalog["wechat"]["domains"], ["mp.weixin.qq.com"])
        self.assertEqual(catalog["bilibili"]["access"], "public_api")
        for platform in ("wechat", "meituan", "dianping", "douyin", "tieba", "douban"):
            self.assertEqual(catalog[platform]["access"], "public_index")
            self.assertTrue(catalog[platform]["description"])
        links = {row["platform"]: row for row in p.native_search_links("中科大 & 食堂", list(catalog))}
        self.assertIn("weixin.sogou.com/weixin?type=2", links["wechat"]["url"])
        self.assertIn("公开索引", links["meituan"]["label"])
        self.assertIn("公开索引", links["dianping"]["label"])
        self.assertIn("/search/", links["douyin"]["url"])
        self.assertIn("%26", links["douyin"]["url"])
        self.assertIn("qw=", links["tieba"]["url"])

    def test_new_platform_filter_rejects_parent_domain_and_spoof(self):
        rows = [{"title": "real", "url": "https://mp.weixin.qq.com/s/a"}, {"title": "parent", "url": "https://news.qq.com/a"}, {"title": "spoof", "url": "https://mp.weixin.qq.com.evil.example/a"}]
        self.assertEqual(len(p._normalize_results(rows, ["wechat"], "bing", 20)), 1)
        scoped = p._scoped_query("test", ["douyin", "wechat"])
        for domain in ("douyin.com", "iesdouyin.com", "mp.weixin.qq.com"):
            self.assertIn("site:" + domain, scoped)


class CustomSiteTests(unittest.TestCase):
    def test_normalize_domain_urls_dedupes_and_preserves_template(self):
        sites = p.normalize_custom_sites([{"domain": "https://EXAMPLE.com/some/path?a=1", "name": "Example", "search_url": "https://search.example.com/find?q={query}&lang=zh"}, "example.com"])
        self.assertEqual(sites, [{"domain": "example.com", "name": "Example", "search_url": "https://search.example.com/find?q={query}&lang=zh"}])

    def test_domain_and_template_ssrf_and_injection_are_rejected(self):
        domains = ["localhost", "http://127.0.0.1", "https://[::1]/", "http://2130706433/", "https://user:password@example.com/", "https://example.com:8443/", "example.internal", "https://8.8.8.8/"]
        for domain in domains:
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                p.normalize_custom_sites([domain])
        templates = ["http://example.com/?q={query}", "https://127.0.0.1/?q={query}", "https://user:secret@example.com/?q={query}", "https://example.com.evil.example/?q={query}", "https://{query}.example.com/", "https://example.com/#q={query}", "https://example.com/?q={query}&p={query}", "https://example.com/?q={query}&p={secret}", "https://example.com/?q={query}&p=%7Bsecret%7D", "https://example.com/?q=%7Bquery%7D"]
        for template in templates:
            with self.subTest(template=template), self.assertRaises(ValueError):
                p.normalize_custom_sites([{"domain": "example.com", "search_url": template}])
        with self.assertRaises(ValueError):
            p.normalize_custom_sites([f"site{i}.example.com" for i in range(7)])

    def test_custom_links_quote_user_query_as_data(self):
        query = '中科大 &x="value" # /'
        links = p.custom_search_links(query, [{"domain": "example.com", "search_url": "https://example.com/search/{query}"}, "other.example.com"])
        self.assertEqual(links[0]["url"], "https://example.com/search/" + urllib.parse.quote(query, safe=""))
        self.assertEqual(urllib.parse.urlsplit(links[0]["url"]).query, "")
        self.assertIn("公开索引", links[1]["label"])
        self.assertIn("site:other.example.com", urllib.parse.parse_qs(urllib.parse.urlsplit(links[1]["url"]).query)["q"][0])

    @patch.object(p, "search_provider")
    def test_domain_index_filters_exact_host_scope_and_reports_actual_source(self, search):
        search.return_value = {"results": [{"title": "Actual post", "url": "https://blog.example.com/posts/1", "snippet": "Actual evidence"}, {"title": "Spoofed scope", "url": "https://example.com.evil.example/posts/2"}, {"title": "Other site", "url": "https://other.com/"}], "status": {"provider": "brave", "ok": True, "count": 3}}
        result = p.search_custom_site({"domain": "example.com"}, "food", 10, {"_engine": "brave"})
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["platform"], "website")
        self.assertEqual(result["results"][0]["domain"], "example.com")
        self.assertEqual(result["status"]["engine"], "brave")
        self.assertEqual(result["status"]["count"], 1)
        self.assertIn("site:example.com", search.call_args.args[1])

    @patch.object(p, "_check_robots")
    @patch.object(p, "_request")
    def test_internal_html_keeps_real_result_anchors_and_nearby_text(self, request, robots):
        document = '''<html><title>Search results</title><nav><h2><a href="/menu">Main menu</a></h2></nav>
        <form action="/search"><article><h3><a href="/posts/1?q=a&amp;b=2">桃李苑 <em>食堂</em></a></h3><p>作者推荐香菇滑鸡，鸡肉比较嫩。</p><a href="/authors/1">作者主页</a></article></form>
        <li class="search-result"><a href="https://blog.example.com/posts/2">第二篇真实帖子</a><p>原文摘要 &amp; 补充。</p></li>
        <h3><a href="https://example.com.evil.example/steal">伪造域名页面</a></h3>
        <h3><a href="https://other.com/page">外部页面</a></h3><h3><a href="/login">登录</a></h3>
        <script><a href="/fake">不能成为结果</a></script></html>'''
        request.return_value = (document.encode(), {"Content-Type": "text/html; charset=utf-8"}, "https://example.com/search?q=food", 200)
        result = p.search_custom_site({"domain": "example.com", "search_url": "https://example.com/search?q={query}"}, "food", 10, {})
        self.assertTrue(result["status"]["ok"])
        self.assertEqual([r["title"] for r in result["results"]], ["桃李苑 食堂", "第二篇真实帖子"])
        self.assertIn("香菇滑鸡", result["results"][0]["snippet"])
        self.assertEqual(result["results"][0]["url"], "https://example.com/posts/1?q=a&b=2")
        robots.assert_called_once()

    @patch.object(p, "_check_robots")
    @patch.object(p, "_request")
    def test_internal_redirect_guard_rejects_outside_domain(self, request, robots):
        def redirect(url, **kwargs):
            kwargs["redirect_guard"]("https://example.com.evil.example/login")
            raise AssertionError("Must not fetch external redirect")
        request.side_effect = redirect
        result = p.search_custom_site({"domain": "example.com", "search_url": "https://example.com/?q={query}"}, "food", 10, {})
        self.assertFalse(result["status"]["ok"])
        self.assertIn("范围之外", result["status"]["error"])
        self.assertEqual(result["results"], [])

    @patch.object(p, "_request")
    @patch.object(p, "_check_robots", side_effect=p.PublicFetchError("robots.txt disallows"))
    def test_internal_robots_denial_prevents_search_fetch(self, robots, request):
        result = p.search_custom_site({"domain": "example.com", "search_url": "https://example.com/?q={query}"}, "food", 10, {})
        self.assertFalse(result["status"]["ok"])
        request.assert_not_called()

    @patch.object(p, "_check_robots")
    @patch.object(p, "_request")
    def test_internal_challenge_and_dynamic_shell_are_honest_errors(self, request, robots):
        for html in ("<title>安全验证</title><h3><a href='/fake'>A fake result</a></h3>", "<html><script>loadResults()</script></html>"):
            request.return_value = (html.encode(), {"Content-Type": "text/html"}, "https://example.com/search", 200)
            result = p.search_custom_site({"domain": "example.com", "search_url": "https://example.com/?q={query}"}, "food", 10, {})
            self.assertFalse(result["status"]["ok"])
            self.assertEqual(result["results"], [])

    def test_unnamed_result_list_keeps_snippet_inside_its_own_item(self):
        parser = p._InternalSearchResults("https://example.com/search", "example.com")
        parser.feed('<ul><li><h3><a href="/one">First result</a></h3><p>First evidence only.</p></li><li><h3><a href="/two">Second result</a></h3><p>Second evidence only.</p></li></ul>')
        rows = parser.rows()
        self.assertEqual(len(rows), 2)
        self.assertIn("First evidence only.", rows[0]["snippet"])
        self.assertNotIn("Second evidence", rows[0]["snippet"])


class CooperativeCancellationTests(_FakeBilibiliClock):
    @patch.object(p, "_request")
    def test_cancelled_wrapper_never_calls_network(self, request):
        event = threading.Event()
        event.set()
        result = p.search_provider("bing", "food", ["web"], 10, {"_cancel_event": event})
        self.assertTrue(result["status"]["cancelled"])
        result = p.search_custom_site({"domain": "example.com"}, "food", 10, {"_cancel_event": event})
        self.assertTrue(result["status"]["cancelled"])
        self.assertTrue(p.fetch_public_page("https://example.com/post", cancel_event=event)["cancelled"])
        request.assert_not_called()

    @patch.object(p, "_json_request")
    def test_bilibili_interval_wait_is_interruptible(self, request):
        event = Mock()
        event.is_set.side_effect = [False, False, False, True]
        event.wait.return_value = True
        p._bilibili_next_request_at = self.now + 1
        with self.assertRaises(p.SearchCancelled):
            p._bilibili_request("https://api.bilibili.com/test", {"_cancel_event": event})
        request.assert_not_called()
        event.wait.assert_called_once_with(1)
        self.assertEqual(self.sleeps, [])

    @patch.object(p, "_json_request")
    def test_bilibili_semaphore_queue_can_cancel_without_outbound_call(self, request):
        event = threading.Event()
        gate = Mock()
        def deny_then_cancel(**kwargs):
            event.set()
            return False
        gate.acquire.side_effect = deny_then_cancel
        with patch.object(p, "_BILIBILI_GATE", gate), self.assertRaises(p.SearchCancelled):
            p._bilibili_request("https://api.bilibili.com/test", {"_cancel_event": event})
        request.assert_not_called()
        gate.release.assert_not_called()


if __name__ == "__main__":
    unittest.main()
