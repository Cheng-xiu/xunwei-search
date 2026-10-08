import json
import threading
import unittest
import urllib.parse
from unittest.mock import Mock, patch

from search_app import providers as p


class DirectProviderTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.sleeps = []
        for name, gate in (("_GITHUB_GATE", p._SourceGate("GitHub", 6.2)), ("_STACKOVERFLOW_GATE", p._SourceGate("Stack Overflow", 1.0))):
            replacement = patch.object(p, name, gate)
            replacement.start()
            self.addCleanup(replacement.stop)
        for replacement in (patch.object(p.time, "monotonic", side_effect=lambda: self.now),
                            patch.object(p.time, "time", return_value=1000000),
                            patch.object(p.time, "sleep", side_effect=self.advance)):
            replacement.start()
            self.addCleanup(replacement.stop)

    def advance(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    @staticmethod
    def response(data, headers=None, status=200):
        return json.dumps(data).encode(), headers or {}, "https://api.github.com/", status

    @staticmethod
    def issue(number, **extra):
        return {"title": f"Issue {number}", "html_url": f"https://github.com/example/project/issues/{number}", "body": "Actual issue body, with generic type List<T> preserved.", **extra}

    @staticmethod
    def question(number, **extra):
        return {"title": f"Question {number}", "link": f"https://stackoverflow.com/questions/{number}/title", "body": "<p>Actual question <code>code</code>.</p>", **extra}

    def test_catalog_new_hosts_and_scopes_cannot_be_spoofed(self):
        catalog = {row["id"]: row for row in p.platform_catalog()}
        for name, host in (("github", "github.com"), ("stackoverflow", "stackoverflow.com"), ("v2ex", "v2ex.com"), ("csdn", "blog.csdn.net"), ("cnblogs", "cnblogs.com"), ("reddit", "reddit.com")):
            self.assertEqual(p.platform_of("https://" + host + "/post"), name)
            self.assertEqual(p.platform_of("https://" + host + ".evil.example/post"), "web")
            self.assertIn(name, catalog)
        self.assertEqual(catalog["github"]["access"], "public_api")
        self.assertEqual(catalog["reddit"]["access"], "public_index")

    @patch.object(p, "_request")
    def test_github_preserves_real_body_no_invented_views_and_filters_wrong_host(self, request):
        request.return_value = self.response({"items": [self.issue(1, created_at="2020-01-01T00:00:00Z"), self.issue(1), self.issue(2, html_url="https://github.com.evil.example/a")], "total_count": 2})
        result = p.search_provider("github", "repo:example/project rare bug", ["github"], 6, {})
        self.assertEqual(len(result["results"]), 1)
        row = result["results"][0]
        self.assertIn("List<T>", row["body"])
        self.assertEqual(row["content_level"], "page")
        self.assertNotIn("views", row)
        self.assertEqual(row["content_kind"], "issue")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.call_args.args[0]).query)
        self.assertEqual(query["q"], ["repo:example/project rare bug"])
        self.assertNotIn("site:", query["q"][0])

    @patch.object(p, "_request")
    def test_github_research_bounds_pages_interleaves_and_does_not_fake_readme(self, request):
        request.side_effect = [self.response({"items": [self.issue(1), self.issue(2)], "total_count": 10}),
                               self.response({"items": [{"full_name": "example/tool", "html_url": "https://github.com/example/tool", "description": "actual repository description"}], "total_count": 1}),
                               self.response({"items": [self.issue(3), self.issue(4)], "total_count": 10})]
        result = p.search_provider("github", "rare library", ["github"], 3, {"_search_depth": "research"})
        self.assertEqual(request.call_count, 3)
        urls = [urllib.parse.urlsplit(call.args[0]) for call in request.call_args_list]
        self.assertEqual([url.path for url in urls], ["/search/issues", "/search/repositories", "/search/issues"])
        self.assertEqual(urllib.parse.parse_qs(urls[2].query)["page"], ["2"])
        self.assertEqual([row["content_kind"] for row in result["results"]], ["issue", "repository", "issue"])
        self.assertNotIn("body", result["results"][1])
        self.assertAlmostEqual(self.sleeps[0], 6.2)

    @patch.object(p, "_request")
    def test_research_without_more_issues_does_not_page_or_misuse_repo_qualifier(self, request):
        request.return_value = self.response({"items": [self.issue(1)], "total_count": 1})
        result = p.search_provider("github", "repo:example/project bug", ["github"], 6, {"_search_depth": "research"})
        self.assertTrue(result["status"]["ok"])
        self.assertEqual(request.call_count, 1)

    @patch.object(p, "_request")
    def test_issue_only_deep_uses_one_issue_page_even_when_more_exist(self, request):
        request.return_value = self.response({"items": [self.issue(1)], "total_count": 100})
        result = p.search_provider("github", "GPIO light sleep error", ["github"], 6,
                                   {"_search_depth": "deep", "_github_search_kind": "issues"})
        self.assertTrue(result["status"]["ok"])
        self.assertEqual(request.call_count, 1)
        url = urllib.parse.urlsplit(request.call_args.args[0])
        self.assertEqual(url.path, "/search/issues")
        self.assertEqual(urllib.parse.parse_qs(url.query)["page"], ["1"])

    @patch.object(p, "_request")
    def test_issue_only_research_reads_at_most_two_issue_pages_when_more_exist(self, request):
        request.side_effect = [self.response({"items": [self.issue(1)], "total_count": 100}),
                               self.response({"items": [self.issue(2)], "total_count": 100})]
        result = p.search_provider("github", "GPIO light sleep error", ["github"], 6,
                                   {"_search_depth": "research", "_github_search_kind": "issues"})
        self.assertTrue(result["status"]["ok"])
        urls = [urllib.parse.urlsplit(call.args[0]) for call in request.call_args_list]
        self.assertEqual([url.path for url in urls], ["/search/issues", "/search/issues"])
        self.assertEqual([urllib.parse.parse_qs(url.query)["page"][0] for url in urls], ["1", "2"])
        self.assertEqual(len(result["results"]), 2)
        self.assertNotIn("仓库", result["status"]["coverage"])

    @patch.object(p, "_request")
    def test_issue_only_research_stops_at_total_count_boundary_or_empty_first_page(self, request):
        for items, total in (([self.issue(1)], 1), ([self.issue(1)], 6), ([], 100)):
            with self.subTest(total_count=total, count=len(items)):
                request.reset_mock()
                request.return_value = self.response({"items": items, "total_count": total})
                result = p.search_provider("github", "GPIO light sleep error", ["github"], 6,
                                           {"_search_depth": "research", "_github_search_kind": "issues"})
                self.assertTrue(result["status"]["ok"])
                self.assertEqual(request.call_count, 1)

    @patch.object(p, "_request")
    def test_general_deep_keeps_issue_and_repository_search(self, request):
        request.side_effect = [self.response({"items": [self.issue(1)], "total_count": 100}),
                               self.response({"items": [{"full_name": "example/tool", "html_url": "https://github.com/example/tool", "description": "Public tool"}], "total_count": 1})]
        result = p.search_provider("github", "rare library", ["github"], 6, {"_search_depth": "deep"})
        self.assertTrue(result["status"]["ok"])
        self.assertEqual([urllib.parse.urlsplit(call.args[0]).path for call in request.call_args_list],
                         ["/search/issues", "/search/repositories"])
        self.assertEqual([row["content_kind"] for row in result["results"]], ["issue", "repository"])

    @patch.object(p, "_request")
    def test_rate_denial_stops_pages_and_other_queued_queries(self, request):
        request.side_effect = [self.response({"items": [self.issue(1)], "total_count": 20}), self.response({}, {"Retry-After": "120"}, 429)]
        result = p.search_provider("github", "rare issue", ["github"], 6, {"_search_depth": "research"})
        self.assertEqual(len(result["results"]), 1)
        self.assertTrue(result["status"]["partial"])
        self.assertEqual(request.call_count, 2)
        again = p.search_provider("github", "another", ["web"], 6, {})
        self.assertFalse(again["status"]["ok"])
        self.assertIn("冷却", again["status"]["error"])
        self.assertEqual(request.call_count, 2)

    @patch.object(p, "_request")
    def test_github_zero_remaining_preserves_last_result_without_extra_request(self, request):
        request.return_value = self.response({"items": [self.issue(1)], "total_count": 20}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1000200"})
        result = p.search_provider("github", "repo:example/project bug", ["github"], 6, {"_search_depth": "research"})
        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(result["results"]), 1)
        self.assertTrue(result["status"]["partial"])
        self.assertGreater(p._GITHUB_GATE.cooldown_until, self.now + 190)

    @patch.object(p, "_request")
    def test_stackoverflow_pages_have_real_views_and_question_bodies(self, request):
        request.side_effect = [self.response({"items": [self.question(1, view_count=7), self.question(2)], "has_more": True, "quota_remaining": 10}),
                               self.response({"items": [self.question(3, view_count=0)], "has_more": True, "quota_remaining": 9}),
                               self.response({"items": [self.question(4, view_count=148)], "has_more": True, "quota_remaining": 8})]
        result = p.search_provider("stackoverflow", "asyncio TaskGroup cancellation", ["stackoverflow"], 4, {"_search_depth": "research"})
        self.assertEqual(request.call_count, 3)
        rows = result["results"]
        self.assertEqual([row["title"] for row in rows], ["Question 1", "Question 3", "Question 4", "Question 2"])
        self.assertEqual([row.get("views") for row in rows], [7, 0, 148, None])
        self.assertEqual(rows[0]["body"], "Actual question code.")
        self.assertNotIn("<p>", rows[0]["body"])
        self.assertIn("不包含回答", result["status"]["coverage"])

    @patch.object(p, "_request")
    def test_stackoverflow_backoff_and_empty_page_stop_without_retries(self, request):
        request.return_value = self.response({"items": [self.question(1)], "has_more": True, "backoff": 40, "quota_remaining": 5})
        result = p.search_provider("stackoverflow", "rare", ["stackoverflow"], 6, {"_search_depth": "research"})
        self.assertTrue(result["status"]["partial"])
        self.assertEqual(request.call_count, 1)
        again = p.search_provider("stackoverflow", "another", ["stackoverflow"], 6, {})
        self.assertIn("冷却", again["status"]["error"])
        self.assertEqual(request.call_count, 1)
        self.advance(40)
        request.return_value = self.response({"items": [], "has_more": True, "quota_remaining": 4})
        self.assertTrue(p.search_provider("stackoverflow", "nothing", ["web"], 6, {"_search_depth": "research"})["status"]["ok"])
        self.assertEqual(request.call_count, 2)

    @patch.object(p, "_request")
    def test_stackoverflow_quota_exhaustion_and_error_codes_are_explicit(self, request):
        request.return_value = self.response({"items": [], "has_more": True, "quota_remaining": 0})
        result = p.search_provider("stackoverflow", "rare", ["stackoverflow"], 6, {"_search_depth": "deep"})
        self.assertEqual(request.call_count, 1)
        self.assertTrue(result["status"]["partial"])
        self.assertGreater(p._STACKOVERFLOW_GATE.cooldown_until, self.now)
        p._STACKOVERFLOW_GATE.cooldown_until = 0
        request.return_value = self.response({"error_id": 502, "error_message": "Untrusted upstream details must not leak", "backoff": 30})
        result = p.search_provider("stackoverflow", "rare", ["web"], 6, {})
        self.assertFalse(result["status"]["ok"])
        self.assertIn("502", result["status"]["error"])
        self.assertNotIn("Untrusted", result["status"]["error"])

    @patch.object(p, "_request")
    def test_scope_skip_and_cancellation_never_send_network(self, request):
        for provider in ("github", "stackoverflow"):
            self.assertTrue(p.search_provider(provider, "rare", ["zhihu"], 6, {})["status"]["skipped"])
        event = threading.Event()
        event.set()
        self.assertTrue(p.search_provider("github", "rare", ["github"], 6, {"_cancel_event": event})["status"]["cancelled"])
        request.assert_not_called()

    @patch.object(p, "_request")
    def test_rate_interval_wait_is_interruptible(self, request):
        event = threading.Event()
        p._GITHUB_GATE.next_request_at = self.now + 6.2
        with patch.object(event, "wait", side_effect=lambda delay: event.set()):
            result = p.search_provider("github", "rare", ["github"], 6, {"_cancel_event": event})
        self.assertTrue(result["status"]["cancelled"])
        request.assert_not_called()

    def test_long_chinese_technical_query_extracts_identifiers_without_target_ids(self):
        query = "ESP32-C3 ESP-IDF 项目开启 tickless idle 和 light_sleep_enable 后 GPIO 输入中断及 ISR 一直不触发，请帮我搜索原始问题"
        extracted = p._direct_query(query)
        self.assertIn("ESP32-C3", extracted)
        self.assertIn("light_sleep_enable", extracted)
        self.assertNotIn("请帮我", extracted)
        self.assertEqual(p._direct_query("repo:python/cpython asyncio TaskGroup cancellation"), "repo:python/cpython asyncio TaskGroup cancellation")

    @patch.object(p, "_request")
    def test_github_details_keep_reply_authors_and_reject_other_issue(self, request):
        request.return_value = self.response([
            {"body": "Public maintainer suggestion", "html_url": "https://github.com/example/project/issues/1#issuecomment-42", "user": {"login": "maintainer"}},
            {"body": "Unrelated reply", "html_url": "https://github.com/example/project/issues/2#issuecomment-43", "user": {"login": "other"}},
        ])
        details = p.fetch_result_details({"url": self.issue(1)["html_url"], "body": "Original first post"})
        self.assertIn("Public maintainer suggestion", details["text"])
        self.assertIn("maintainer", details["text"])
        self.assertIn("#issuecomment-42", details["text"])
        self.assertNotIn("Unrelated reply", details["text"])
        self.assertNotIn("Original first post", details["text"])
        self.assertIn("per_page=20", request.call_args.args[0])
        self.assertEqual(request.call_count, 1)

    @patch.object(p, "_request")
    def test_stackoverflow_details_are_bounded_current_question_answers_only(self, request):
        request.return_value = self.response({"items": [
            {"question_id": 123, "answer_id": 456, "body": "<p>Use actual documented configuration</p>", "is_accepted": True, "owner": {"display_name": "A &amp; B"}},
            {"question_id": 999, "answer_id": 457, "body": "unrelated"}], "quota_remaining": 10})
        details = p.fetch_result_details({"url": "https://stackoverflow.com/questions/123/title"})
        self.assertIn("A & B", details["text"])
        self.assertIn("https://stackoverflow.com/a/456", details["text"])
        self.assertIn("已采纳", details["text"])
        self.assertNotIn("unrelated", details["text"])
        self.assertIn("pagesize=5", request.call_args.args[0])
        self.assertEqual(request.call_count, 1)

    @patch.object(p, "_request")
    def test_details_reject_pr_spoof_private_and_non_question_urls(self, request):
        for url in ("https://github.com/example/project/pull/1", "https://github.com.evil.example/example/project/issues/1", "http://127.0.0.1/", "https://stackoverflow.com/users/1/name"):
            self.assertIn("error", p.fetch_result_details({"url": url}))
        request.assert_not_called()


class VideoEvidenceTests(unittest.TestCase):
    @patch.object(p, "_check_robots")
    @patch.object(p, "_request")
    def test_current_bilibili_video_metadata_excludes_recommendations(self, request, robots):
        state = {"videoData": {"bvid": "BV123abc", "title": "Current real video", "desc": "Current real description"}, "related": [{"title": "Unrelated recommended dish"}]}
        document = '<title>Current real video</title><script>window.__INITIAL_STATE__=' + json.dumps(state) + ';</script><h2>接下来播放</h2><p>Unrelated recommended dish</p>'
        request.return_value = (document.encode(), {"Content-Type": "text/html"}, "https://www.bilibili.com/video/BV123abc/", 200)
        result = p.fetch_public_page("https://www.bilibili.com/video/BV123abc/")
        self.assertIn("Current real description", result["text"])
        self.assertNotIn("recommended", result["text"])
        self.assertNotIn("接下来播放", result["text"])
        self.assertIn("仅当前视频", result["coverage"])

    @patch.object(p, "_check_robots")
    @patch.object(p, "_request")
    def test_missing_or_mismatched_bilibili_state_is_not_unrelated_body(self, request, robots):
        for document in ('<h2>接下来播放</h2><p>noise</p>', '<script>window.__INITIAL_STATE__={"videoData":{"bvid":"BVOTHER","title":"Wrong","desc":"Wrong"}};</script>'):
            request.return_value = (document.encode(), {"Content-Type": "text/html"}, "https://www.bilibili.com/video/BV123abc/", 200)
            result = p.fetch_public_page("https://www.bilibili.com/video/BV123abc/")
            self.assertEqual(result["text"], "")
            self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
