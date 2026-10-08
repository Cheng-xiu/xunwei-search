"""Offline regressions for obscure-query recall without broad-topic inflation."""
import unittest
from unittest.mock import Mock, patch

from search_app import engine
from search_app.retrieval import anchor_coverage, depth_profile, is_excluded, query_anchors


class RetrievalBudgetTests(unittest.TestCase):
    def test_depth_profiles_have_explicit_request_and_read_budgets(self):
        for depth, requests, pages in [('quick', 20, 3), ('deep', 32, 6), ('research', 48, 10)]:
            with self.subTest(depth=depth):
                profile = depth_profile(depth)
                self.assertEqual(profile['requests'], requests)
                self.assertEqual(profile['pages'], pages)

    def test_actual_single_search_reading_respects_each_depth_budget(self):
        rows = [{'title': f'中科大桃李苑线索 {index}', 'url': f'https://example.com/post/{index}',
                 'snippet': '桃李苑餐饮记录，仍需核对中区及具体菜品。', 'platform': 'web', 'source': 'bing'}
                for index in range(14)]
        library = Mock()
        library.search_documents.return_value = []
        for depth, pages in [('quick', 3), ('deep', 6), ('research', 10)]:
            final = {}
            job = {'query': '中科大中区桃李苑哪道菜好吃', 'platforms': ['web'], 'depth': depth,
                   'adaptive': False, 'use_ai': False, 'fetch_pages': True, 'warnings': []}
            with self.subTest(depth=depth), \
                 patch.object(engine, 'build_tasks', return_value=[('bing', '桃李苑', ['web'])]), \
                 patch.object(engine, 'search_provider', return_value={'results': rows, 'status': {'provider': 'bing', 'ok': True, 'count': len(rows)}}), \
                 patch.object(engine, 'fetch_public_page', return_value={'text': '桃李苑可读公开正文，具体菜名待核对。'}) as fetch, \
                 patch.object(engine, 'chat', side_effect=AssertionError('No AI call authorized in offline tests')):
                engine.run_search(job, {}, library, lambda **fields: final.update(fields))
            self.assertEqual(final['state'], 'done')
            self.assertEqual(fetch.call_count, pages)
            self.assertEqual(sum(row['content_level'] == 'page' for row in final['results']), pages)


class QueryAnchorTests(unittest.TestCase):
    def test_food_question_rejects_same_campus_non_food_topics(self):
        query = '中科大中区桃李苑哪道菜好吃'
        for text in ('中科大桃李苑毕业晚会', '中科大宿舍介绍', '中国科学技术大学考研经验'):
            with self.subTest(text=text):
                self.assertFalse(engine.has_query_signal(query, {'title': text}))

    def test_food_question_keeps_cafeteria_lead_but_still_needs_entity(self):
        query = '中科大中区桃李苑哪道菜好吃'
        self.assertTrue(engine.has_query_signal(query, {'title': 'USTC cafeteria menu'}))
        self.assertFalse(engine.has_query_signal(query, {'title': '合肥美食餐馆推荐'}))

    def test_model_terms_must_be_literal_and_cannot_invent_a_required_entity(self):
        query = '合肥四牌楼人行天桥哪年拆除'
        groups = query_anchors(query, {'retrieval_terms': ['四牌楼', '合肥', '中国', '三孝口', '清华大学', 123]})
        self.assertEqual(groups, [['四牌楼']])
        self.assertFalse(engine.has_query_signal(query, {'title': '清华大学三孝口资料'}, {'retrieval_terms': ['四牌楼', '清华大学']}))

    def test_known_school_and_restaurant_spellings_share_anchor_groups(self):
        groups = query_anchors('中科大中区桃李苑哪道菜好吃', {'retrieval_terms': ['中科大', '桃李苑']})
        self.assertEqual(len(groups), 2)
        school = next(group for group in groups if '中科大' in group)
        food = next(group for group in groups if '桃李苑' in group)
        self.assertTrue({'中科大', '中国科大', '中国科学技术大学'} <= set(school))
        self.assertTrue({'桃李苑', '桃李园'} <= set(food))

    def test_china_ustc_alias_also_works_as_the_original_query(self):
        query = '中国科大桃李园菜品'
        groups = query_anchors(query)
        school = next((group for group in groups if '中国科大' in group), [])
        self.assertIn('中科大', school)
        self.assertTrue(engine.has_query_signal(query, {'title': '中科大食堂记录'}))

    def test_common_city_or_country_mentions_do_not_pass_rare_anchor_filter(self):
        cases = [('中国科学技术大学桃李苑菜单', {'title': '中国百科词条'} , {}),
                 ('合肥四牌楼人行天桥拆除', {'title': '合肥旅游景点大全'}, {'retrieval_terms': ['四牌楼', '合肥']})]
        for query, row, plan in cases:
            with self.subTest(query=query):
                self.assertFalse(engine.has_query_signal(query, row, plan))

    def test_one_rare_entity_is_a_lead_even_without_all_original_conditions(self):
        query = '中科大中区桃李苑哪道菜好吃'
        row = {'title': '桃李园窗口记录', 'snippet': '作者只写了当日体验，未注明校区。'}
        self.assertEqual(anchor_coverage(query, row), 0.5)
        self.assertTrue(engine.has_query_signal(query, row))
        # Retrieval admission must not itself label this incomplete lead verified.
        cleaned = engine.clean_result({**row, 'url': 'https://example.com/lead'}, query)
        self.assertEqual(cleaned['match'], 'unverified')

    def test_exact_version_identifier_survives_without_ai_terms(self):
        query = '查找 v4.4.1-91-g36823b45cc 的中断故障'
        self.assertTrue(engine.has_query_signal(query, {'title': 'reproducer uses v4.4.1-91-g36823b45cc'}))
        self.assertFalse(engine.has_query_signal(query, {'title': 'reproducer uses v4.4.2-91-g36823b45cc'}))
        self.assertFalse(engine.has_query_signal(query, {'title': '通用中断故障教程'}))

    def test_rewrite_cannot_override_missing_rare_original_entity(self):
        query = '合肥四牌楼天桥历史'
        row = {'title': '合肥旅游景点大全'}
        self.assertFalse(engine.has_query_signal(query, row, {'retrieval_terms': ['四牌楼']}, '合肥旅游景点大全'))

    def test_no_anchor_fallback_requires_original_threshold_or_stronger_rewrite(self):
        query = '普通中文查询'
        self.assertEqual(query_anchors(query), [])
        for original, rewritten, expected in [(0.119, 0.349, False), (0.12, 0.0, True),
                                                (0.0, 0.35, True), (0.01, 0.10, False)]:
            with self.subTest(original=original, rewritten=rewritten), \
                 patch.object(engine, 'relevance', side_effect=lambda text, row: original if text == query else rewritten):
                self.assertEqual(engine.has_query_signal(query, {'title': '测试资料'}, retrieval_query='改写检索词'), expected)


class ContradictionTests(unittest.TestCase):
    def source(self, **changes):
        value = {'id': 'post1', 'title': '浙江财经大学桃李苑', 'snippet': '这里是浙江财经大学的食堂。',
                 'body': '', 'content_level': 'snippet', 'score': 60, 'match': 'unverified', 'evidence': []}
        value.update(changes)
        return value

    def assess(self, row, evidence, conditions=('学校为中科大', '地点名为桃李苑')):
        engine.apply_assessments([row], [{'id': row['id'], 'evidence': evidence}], list(conditions), lambda message: None)
        return row

    def test_literal_wrong_school_overrides_other_supported_conditions(self):
        row = self.assess(self.source(), [
            {'condition': '学校为中科大', 'status': 'contradicted', 'quote': '浙江财经大学'},
            {'condition': '地点名为桃李苑', 'status': 'supported', 'quote': '桃李苑'},
        ])
        self.assertEqual(row['match'], 'excluded')
        self.assertEqual(row['score'], 0)
        self.assertTrue(is_excluded(row))

    def test_nonexistent_counterquote_cannot_exclude_a_candidate(self):
        row = self.assess(self.source(title='中科大桃李苑', snippet='桃李苑窗口体验'), [
            {'condition': '学校为中科大', 'status': 'contradicted', 'quote': '浙江财经大学'},
            {'condition': '地点名为桃李苑', 'status': 'supported', 'quote': '桃李苑'},
        ])
        self.assertEqual(row['match'], 'partial')
        self.assertEqual(row['evidence'][0], {'condition': '学校为中科大', 'quote': '', 'status': 'unknown'})
        self.assertFalse(is_excluded(row))

    def test_counterquote_outside_the_sent_body_cannot_exclude(self):
        row = self.source(title='中科大桃李苑', snippet='桃李苑体验', body='甲' * 7000 + '浙江财经大学')
        self.assess(row, [{'condition': '学校为中科大', 'status': 'contradicted', 'quote': '浙江财经大学'}])
        self.assertNotEqual(row['match'], 'excluded')
        self.assertEqual(row['evidence'][0]['status'], 'unknown')

    def test_unknown_result_id_does_not_mutate_real_candidate(self):
        row = self.source()
        engine.apply_assessments([row], [{'id': 'invented-id', 'evidence': [
            {'condition': '学校为中科大', 'status': 'contradicted', 'quote': '浙江财经大学'}]}], ['学校为中科大'], lambda message: None)
        self.assertEqual(row['match'], 'unverified')
        self.assertEqual(row['evidence'], [])


class NativeRoutingTests(unittest.TestCase):
    def plan(self, count=25):
        return {'queries': [{'query': f'rare-version query {index}'} for index in range(count)]}

    def test_github_and_stackoverflow_never_search_an_unselected_scope(self):
        providers = ['github', 'stackoverflow', 'bing', 'duckduckgo', 'bilibili']
        with patch.object(engine, 'available_providers', return_value=providers):
            tasks = engine.build_tasks(self.plan(), ['web', 'zhihu', 'bilibili'], 'research', {})
        self.assertTrue(tasks)
        self.assertFalse(any(provider in ('github', 'stackoverflow') for provider, _, _ in tasks))
        self.assertTrue(all(scope[0] in ('web', 'zhihu', 'bilibili') for _, _, scope in tasks))

    def test_native_sources_are_first_scoped_and_bounded_at_every_depth(self):
        providers = ['bing', 'duckduckgo', 'github', 'stackoverflow', 'bilibili']
        selected = ['web', 'zhihu', 'bilibili', 'github', 'stackoverflow', 'tieba', 'douban']
        direct = {'bilibili', 'github', 'stackoverflow'}
        for depth, budget in [('quick', 20), ('deep', 32), ('research', 48)]:
            with self.subTest(depth=depth), patch.object(engine, 'available_providers', return_value=providers):
                tasks = engine.build_tasks(self.plan(), selected, depth, {})
            self.assertEqual(len(tasks), budget)
            first_generic = next(index for index, task in enumerate(tasks) if task[0] not in direct)
            self.assertTrue(all(task[0] in direct for task in tasks[:first_generic]))
            self.assertFalse(any(task[0] in direct for task in tasks[first_generic:]))
            for provider in direct:
                scoped = [task for task in tasks if task[0] == provider]
                self.assertTrue(scoped)
                self.assertTrue(all(scope == [provider] and 'site:' not in query for _, query, scope in scoped))
            self.assertEqual(len(tasks), len({(provider, query, tuple(scope)) for provider, query, scope in tasks}))

    def test_only_native_providers_available_does_not_turn_them_into_web_engines(self):
        with patch.object(engine, 'available_providers', return_value=['github', 'stackoverflow']):
            tasks = engine.build_tasks(self.plan(), ['web', 'zhihu'], 'quick', {})
        self.assertEqual(tasks, [])

    def test_paid_engine_keeps_free_fallback_without_routing_native_generically(self):
        providers = ['tavily', 'brave', 'github', 'stackoverflow', 'bing', 'duckduckgo']
        with patch.object(engine, 'available_providers', return_value=providers):
            tasks = engine.build_tasks(self.plan(), ['web', 'github'], 'quick', {})
        web_tasks = [provider for provider, _, scope in tasks if scope == ['web']]
        self.assertIn('tavily', web_tasks)
        self.assertIn('bing', web_tasks)
        self.assertNotIn('github', web_tasks)
        self.assertNotIn('stackoverflow', web_tasks)
        self.assertLessEqual(len(tasks), 20)


if __name__ == '__main__':
    unittest.main()
