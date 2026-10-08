"""Offline fixtures for actual-page leads and scoped public research tools."""
import json
import threading
import unittest
from unittest.mock import patch

from search_app import providers as p
from search_app import research_tools as research


class InspectPageTests(unittest.TestCase):
    def page(self, document, url="https://example.com/articles/topic"):
        return document.encode(), {"Content-Type": "text/html; charset=utf-8"}, url, 200

    @patch.object(p, "_check_robots")
    @patch.object(p, "_request")
    def test_actual_website_links_include_video_and_resolve_relative_once(self, request, robots):
        request.return_value = self.page('''<title>研究主页</title><article><p>真实网页正文有来源依据。</p>
            <a href="https://www.bilibili.com/video/BV123abc/?utm_source=test">食堂视频</a>
            <a href="../follow-up">后续记录</a><nav><a href="/archive">全部记录</a></nav>
            <iframe src="https://www.youtube.com/embed/abcdef12345" title="公开视频"></iframe>
            <a href="../follow-up#details">重复网址</a></article>''' + "公开文章内容。" * 30)
        result = research.inspect_page("https://example.com/articles/topic")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result["title"], "研究主页")
        self.assertEqual([(row["url"], row["kind"]) for row in result["links"]], [
            ("https://www.bilibili.com/video/BV123abc/", "video"),
            ("https://example.com/follow-up", "page"),
            ("https://example.com/archive", "page"),
            ("https://www.youtube.com/embed/abcdef12345", "video")])
        self.assertEqual(result["links"][0]["platform"], "bilibili")

    @patch.object(p, "_check_robots")
    @patch.object(p, "_request")
    def test_hidden_script_login_and_unsafe_links_are_not_leads_or_body(self, request, robots):
        links = "".join('<div ' + attr + '><a href="/hidden-' + str(index) + '">隐藏线索</a></div>' for index, attr in enumerate(("hidden", 'aria-hidden="true"', 'style="display: none !important"', 'style="visibility: hidden"', 'style="content-visibility:hidden"')))
        excluded = "".join('<' + tag + '><a href="/excluded-' + tag + '">隐藏线索</a></' + tag + '>' for tag in ("script", "template", "noscript", "form"))
        unsafe = "".join('<a href="' + href + '">危险链接</a>' for href in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html,x", "http://127.0.0.1/x", "https://user:pass@example.com/x", "https://example.com:8443/x", "/login", "/register", "#part"))
        request.return_value = self.page(links + excluded + unsafe + '<a href="/real">真实记录</a><p>' + "实际正文。" * 50 + "</p>")
        result = research.inspect_page("https://example.com/articles/topic")
        self.assertEqual([row["url"] for row in result["links"]], ["https://example.com/real"])
        self.assertNotIn("隐藏线索", result["text"])

    @patch.object(p, "_check_robots")
    @patch.object(p, "_request")
    def test_base_href_limit_and_explicit_scope_do_not_expand_parent_domain(self, request, robots):
        document = '<base href="https://news.example.com/posts/"><p>' + "实际正文。" * 50 + '</p>'
        document += '<a href="https://example.com/parent">不可提升父域</a><a href="https://news.example.com.evil.org/spoof">伪造域名</a>'
        document += "".join('<a href="' + str(index) + '">记录' + str(index) + '</a>' for index in range(50))
        request.return_value = self.page(document, "https://news.example.com/start")
        result = research.inspect_page("https://news.example.com/start", allowed_domains=["news.example.com"])
        self.assertEqual(len(result["links"]), 40)
        self.assertEqual(result["links"][0]["url"], "https://news.example.com/posts/0")
        self.assertTrue(all(row["url"].startswith("https://news.example.com/posts/") for row in result["links"]))

    @patch.object(p, "_check_robots")
    @patch.object(p, "_request")
    def test_challenge_and_visible_login_pages_expose_no_links(self, request, robots):
        for challenge in ('<title>安全验证</title>', '<form id="challenge-form"></form>', '<form><input type="password"></form>', '<title>Sign in - Example</title>'):
            with self.subTest(challenge=challenge):
                request.return_value = self.page(challenge + '<a href="/answer">假线索</a>' + "验证页内容。" * 30)
                result = research.inspect_page("https://example.com/articles/topic")
                self.assertIn("error", result)
                self.assertEqual(result["links"], [])
                self.assertEqual(result["text"], "")
        request.return_value = self.page('<title>验证码实现教程</title><div hidden><form><input type="password"></form></div><a href="/article">实际文章</a>' + "正常教程正文。" * 30)
        result = research.inspect_page("https://example.com/articles/topic")
        self.assertNotIn("error", result)
        self.assertEqual(len(result["links"]), 1)

    @patch.object(p, "_request")
    @patch.object(p, "_check_robots")
    def test_scope_rejection_precedes_initial_and_redirect_robots_network(self, robots, request):
        result = research.inspect_page("https://outside.example/post", allowed_domains=["allowed.example"])
        self.assertIn("范围", result["error"])
        robots.assert_not_called()
        request.assert_not_called()
        def redirect(url, **kwargs):
            kwargs["redirect_guard"]("https://outside.example/post")
            self.fail("scope escape must fail before the next request")
        request.side_effect = redirect
        result = research.inspect_page("https://allowed.example/post", allowed_domains=["allowed.example"])
        self.assertIn("范围", result["error"])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(robots.call_count, 1)
        self.assertEqual(result["links"], [])

    @patch.object(p, "_request")
    def test_robots_redirect_cannot_escape_selected_domain(self, request):
        def redirect(url, **kwargs):
            self.assertEqual(url, "https://allowed.example/robots.txt")
            kwargs["redirect_guard"]("https://outside.example/robots.txt")
            self.fail("robots redirect escaped scope")
        request.side_effect = redirect
        result = research.inspect_page("https://allowed.example/post", allowed_domains=["allowed.example"])
        self.assertIn("robots", result["error"])
        self.assertEqual(request.call_count, 1)

    @patch.object(p, "_check_robots")
    @patch.object(p, "_request")
    def test_cancellation_and_empty_scope_do_not_request_pages(self, request, robots):
        event = threading.Event()
        event.set()
        self.assertTrue(research.inspect_page("https://example.com/", cancel_event=event)["cancelled"])
        self.assertIn("error", research.inspect_page("https://example.com/", allowed_domains=[]))
        request.assert_not_called()
        robots.assert_not_called()

    @patch.object(p, "_check_robots")
    @patch.object(p, "_request")
    def test_bilibili_current_metadata_owner_only_and_no_recommendation_leads(self, request, robots):
        url = "https://www.bilibili.com/video/BV123abc/"
        state = {"videoData": {"bvid": "BV123abc", "title": "当前视频", "desc": "实得视频简介", "owner": {"mid": 12345, "name": "作者"}}, "related": [{"bvid": "BVrecommended", "owner": {"mid": 999}}]}
        document = '<script>window.__INITIAL_STATE__=' + json.dumps(state) + ';</script><a href="/video/BVrecommended/">接下来播放</a><a href="https://space.bilibili.com/999">推荐作者</a>'
        request.return_value = self.page(document, url)
        result = research.inspect_page(url, allowed_domains=["bilibili.com"])
        self.assertEqual(result["links"], [{"url": "https://space.bilibili.com/12345", "title": "作者的频道（导航）", "platform": "bilibili", "kind": "channel"}])
        self.assertNotIn("接下来播放", result["text"])
        self.assertNotIn("999", str(result))
        self.assertEqual(research.inspect_page(url, allowed_domains=["www.bilibili.com"])["links"], [])
        state["videoData"]["bvid"] = "BVother"
        request.return_value = self.page('<script>window.__INITIAL_STATE__=' + json.dumps(state) + ';</script><a href="/video/BVrecommended/">推荐</a>', url)
        result = research.inspect_page(url)
        self.assertEqual(result["links"], [])
        self.assertEqual(result["text"], "")
        self.assertIn("error", result)


class SearchSiteTests(unittest.TestCase):
    @patch.object(p, "search_provider")
    def test_selected_engine_real_site_query_and_host_filter(self, search):
        search.return_value = {"status": {"provider": "bing", "ok": True, "count": 4}, "results": [
            {"title": "主页文章", "url": "https://news.example.com/article", "snippet": "Actual snippet"},
            {"title": "子域文章", "url": "https://video.news.example.com/story", "snippet": "Actual snippet"},
            {"title": "父域不在范围内", "url": "https://example.com/story"},
            {"title": "域名伪造", "url": "https://news.example.com.evil.org/story"}]}
        result = research.search_site("news.example.com", "食堂 视频", "bing", 8, {"search_engines": ["bing"]})
        self.assertEqual(search.call_args.args[:4], ("bing", "食堂 视频 site:news.example.com", ["web"], 8))
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["status"]["count"], 2)
        self.assertTrue(result["status"]["ok"])

    @patch.object(p, "search_custom_site")
    def test_video_only_keeps_definite_video_paths_not_searches_channels_or_spoofs(self, search):
        for domain, paths, expected in (("bilibili.com", ["/video/BV123abc/", "/search/all?keyword=food", "/read/cv123"], 1),
                                        ("youtube.com", ["/watch?v=abcdef12345", "/shorts/abcdef12345", "/channel/UCabcdef12345", "/results?search_query=food"], 2),
                                        ("douyin.com", ["/video/123456789", "/user/12345", "/search/food"], 1),
                                        ("vimeo.com", ["/123456", "/categories/food"], 1)):
            with self.subTest(domain=domain):
                search.return_value = {"status": {"ok": True}, "results": [{"title": "实际来源返回", "url": "https://www." + domain + path} for path in paths] + [{"title": "域名欺骗", "url": "https://" + domain + ".evil.org/video/BV123abc/"}]}
                result = research.search_site(domain, "食堂", "bing", 10, {"search_engines": ["bing"]}, video_only=True)
                self.assertEqual(len(result["results"]), expected)
                self.assertTrue(all(row["kind"] == "video" for row in result["results"]))

    @patch.object(p, "search_custom_site")
    def test_unselected_unconfigured_direct_invalid_scope_and_cancel_never_search(self, search):
        for provider, domain in (("google", "example.com"), ("tavily", "example.com"), ("github", "example.com"), ("bing", "http://127.0.0.1"), ("bing", "example.com/path"), ("bing", "user@example.com")):
            self.assertFalse(research.search_site(domain, "food", provider, 8, {"search_engines": ["bing"]})["status"]["ok"])
        event = threading.Event()
        event.set()
        self.assertTrue(research.search_site("example.com", "food", "bing", 8, {"search_engines": ["bing"], "_cancel_event": event})["status"]["cancelled"])
        search.assert_not_called()

    @patch.object(p, "search_custom_site")
    def test_source_failure_and_late_cancellation_are_honest(self, search):
        search.return_value = {"status": {"ok": False, "error": "来源要求验证码"}, "results": []}
        self.assertIn("验证码", research.search_site("example.com", "food", "bing", 8, {})["status"]["error"])
        event = threading.Event()
        def finish(*args, **kwargs):
            event.set()
            return {"status": {"ok": True}, "results": [{"url": "https://example.com/late", "title": "晚到线索"}]}
        search.side_effect = finish
        result = research.search_site("example.com", "food", "bing", 8, {"_cancel_event": event})
        self.assertTrue(result["status"]["cancelled"])
        self.assertEqual(result["results"], [])


if __name__ == "__main__":
    unittest.main()
