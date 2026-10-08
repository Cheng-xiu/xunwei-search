import concurrent.futures
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from search_app.safety import check_query
from search_app.storage import Storage


class SafetyTests(unittest.TestCase):
    def test_food_and_general_financial_learning(self):
        for query in ("中科大中区桃李苑哪道菜好吃", "跨平台搜索冷门美食帖", "了解 A8 家庭资产含义", "如何计算家庭净资产", "个人净资产怎么算", "家庭资产估算方法", "女性年薪统计", "211 大学 2007-2010 年招生历史"):
            with self.subTest(query=query):
                self.assertTrue(check_query(query)["allowed"])

    def test_ambiguous_dating_years_require_clarification(self):
        decision = check_query("一个07-10年的在合肥上大学，来自珠三角，211以上，家境a8以上的女生相亲贴")
        self.assertFalse(decision["allowed"])
        self.assertIn("出生年份、入学年份", decision["message"])
        self.assertTrue(decision["warnings"])
        self.assertTrue(decision["public_post_only"])
        self.assertFalse(check_query("09年的女生公开相亲帖")["allowed"])

    def test_explicit_minors_are_blocked_only_in_dating(self):
        for query in ("找 16 岁女生相亲帖", "2007-2010 年出生的女生相亲贴", "生于2011年的男生征婚", "2011年生女生相亲", "找高中生恋爱对象", "找17-19岁女生相亲", "十七岁女生征婚"):
            with self.subTest(query=query):
                self.assertFalse(check_query(query)["allowed"])
        self.assertTrue(check_query("2011 年出生学生的教材推荐")["allowed"])

    def test_birth_year_edge_requires_adult_confirmation(self):
        year = date.today().year - 18
        self.assertFalse(check_query(f"找{year}年出生女生相亲帖")["allowed"])
        self.assertTrue(check_query(f"找{year}年出生、已满18岁成年女生本人公开相亲帖")["allowed"])

    def test_adult_public_dating_and_school_years(self):
        for query in ("合肥成年人本人公开相亲帖", "2007-2010年入学的成年校友公开征婚帖", "成年人本人相亲帖，排除未成年人", "1988年出生的成年男生公开征婚帖"):
            with self.subTest(query=query):
                decision = check_query(query)
                self.assertTrue(decision["allowed"])
                self.assertTrue(decision["public_post_only"])

    def test_private_profile_and_account_linking(self):
        for query in ("家境 A8 以上的女生相亲帖", "推断这个博主的家庭资产", "跨平台关联同一个人账号", "找出她所有的小号", "扒出这个人的家庭住址"):
            with self.subTest(query=query):
                self.assertFalse(check_query(query)["allowed"])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "library.sqlite3")

    def tearDown(self):
        self.storage.close()
        self.temp.cleanup()

    def test_chinese_retrieval_prefers_exact_place_and_preserves_evidence(self):
        self.storage.add_document("其他学校的桃李食堂", "", "食堂哪道菜好吃，今天吃面。", "local")
        target = self.storage.add_document("中科大中区桃李苑探店", "https://example.com/post/1", "中科大中区桃李苑，推荐香菇滑鸡，米饭不错。", "bilibili")
        result = self.storage.search_documents("中科大中区桃李苑哪道菜好吃")
        self.assertEqual(result[0]["id"], target["id"])
        self.assertIn("香菇滑鸡", result[0]["body"])
        self.assertEqual(result[0]["source"], "local")
        self.assertEqual(result[0]["content_level"], "local")
        self.assertEqual(self.storage.search_documents("无关的量子引力"), [])
        self.assertTrue(self.storage.delete_document(target["id"]))
        self.assertFalse(self.storage.delete_document(target["id"]))

    def test_no_url_stays_local_and_no_secret_is_persisted(self):
        key = "sk-FAKE_TEST_SECRET_12345678901234567890"
        doc = self.storage.add_document("本地摘录", "", f"API key: {key}", "local")
        self.assertEqual(doc["url"], "")
        self.assertNotIn(key, json.dumps(self.storage.list_documents()))
        self.storage.save_job({"id": "secret-job", "query": "测试", "state": "done", "apiKey": key, "config": {"api_key": key}, "results": [{"snippet": key}], "events": [f"error {key}"]})
        saved = self.storage.get_job("secret-job")
        self.assertNotIn("apiKey", saved)
        self.assertNotIn("config", saved)
        self.assertNotIn(key, json.dumps(saved))

    def test_only_terminal_jobs_are_saved_and_history_is_bounded(self):
        self.storage.save_job({"id": "running", "state": "running", "query": "test"})
        self.assertIsNone(self.storage.get_job("running"))
        for index in range(103):
            self.storage.save_job({"id": str(index), "state": "done", "query": "菜品", "results": [{"title": str(index)}]})
        history = self.storage.list_history(200)
        self.assertEqual(len(history), 100)
        self.assertEqual(history[0]["count"], 1)
        self.assertIsNone(self.storage.get_job("0"))
        self.assertIsNotNone(self.storage.get_job("102"))

    def test_parallel_writes_use_independent_connections(self):
        def insert(index):
            return self.storage.add_document(f"菜品 {index}", "", "桃李苑推荐", "local")
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            documents = list(pool.map(insert, range(24)))
        self.assertEqual(len(documents), 24)
        self.assertEqual(len(self.storage.list_documents()), 24)


if __name__ == "__main__":
    unittest.main()
