"""报告审核范围、工作量及既有病例记录的回归验证。"""
import copy
import unittest

from services import doctor_mdt, report_service, review_service as rs
from services.case_flow import CaseFlow
from services.store import Store
from tests.test_review import sample_case


class ReviewScopeTest(unittest.TestCase):
    def case(self):
        case = sample_case()
        case['payload']['gaps'] = []
        return case

    def test_specialty_analysis_volume_does_not_expand_review_queue(self):
        case = self.case()
        case['payload']['mdt']['analyses'] = [
            {'department': dept, 'status': 'ok',
             'inferences': [{'text': f'{dept}判断{i}'} for i in range(30)],
             'checks': [{'content': f'{dept}待核查{i}'} for i in range(30)],
             'referral': '协作建议'}
            for dept in ['眼科', '肾内科', '内分泌科', '骨科', '心内科', '呼吸内科']
        ]
        case['payload']['mdt']['aggregation']['open_questions'] += [f'讨论问题{i}' for i in range(100)]
        items = rs.build_items(case)
        self.assertEqual(sum(x.target_type == 'ai_analysis' for x in items), 6)
        self.assertEqual(sum(x.target_type == 'path_step' for x in items), 2)
        self.assertEqual(sum(x.target_type == 'consensus_item' for x in items), 2)
        summary = rs.summarize(rs.apply_decisions(items, []))
        self.assertEqual((summary['total'], summary['pending'], summary['sample_pending'], summary['auto_processed']),
                         (10, 8, 1, 1))
        self.assertEqual(summary['specialty_total'], 6)

    def test_normal_consensus_is_sample_and_risks_still_require_review(self):
        case = self.case()
        normal = {x.item_id: x for x in rs.build_items(case)}
        self.assertEqual(normal['consensus_item:C-01'].review_tier, rs.SAMPLE)
        self.assertEqual(normal['path_step:S1'].review_tier, rs.AUTOMATIC)
        self.assertEqual(normal['consensus_item:X-01'].review_tier, rs.REQUIRED)
        for field, value in [('high_risk', True), ('analysis', {'state': 'failed'})]:
            with self.subTest(field=field):
                risky = copy.deepcopy(case)
                risky['payload'][field] = value
                items = rs.build_items(risky)
                self.assertTrue(all(x.review_tier == rs.REQUIRED and not x.batch_eligible for x in items))

    def test_mdt_consensus_uses_current_simulation_revision(self):
        case = self.case()
        payload = case['payload']
        payload['mdt_screening'] = {'decision': 'recommend_mdt', 'departments_source': 'doctor_routing'}
        payload['doctor_mdt_simulation'] = {
            'state': 'done', 'content_revision': 4,
            'input_fingerprint': doctor_mdt.input_fingerprint(payload),
            'result': {'analyses': [], 'aggregation': {'consensus': [
                {'topic': '当前模拟关注点', 'example': '当前模拟意见', 'supporters': ['眼科']}
            ]}}
        }
        item = next(x for x in rs.build_items(case) if x.target_type == 'consensus_item')
        self.assertEqual(item.ai_content, '当前模拟意见')
        self.assertEqual(item.review_tier, rs.SAMPLE)
        self.assertEqual(item.content_revision, 4)
        old = {'target_type': 'consensus_item', 'target_id': item.target_id, 'action': 'accept', 'content_revision': 3}
        self.assertEqual(rs.apply_decisions([item], [old])[0].state, rs.AWAITING)
        payload['mdt_screening']['purpose'] = '已修改的目标'
        self.assertFalse(any(x.target_type == 'consensus_item' for x in rs.build_items(case)))

    def test_prior_analysis_decision_is_history_and_does_not_override_current_report(self):
        case = self.case()
        case['payload']['mdt']['analyses'] = [{
            'department': '眼科', 'status': 'ok', 'inferences': [{'text': '分析参考正文'}]
        }]
        record = {'decision_id': 'D-OLD', 'case_id': 'C0001', 'target_type': 'ai_analysis',
                  'target_id': doctor_mdt.analysis_target_id('眼科', 'inference', 1),
                  'action': 'modify', 'before': '分析参考正文', 'after': '历史分析修改',
                  'reason': '此前审核', 'content_revision': 1, 'reviewer': {'name': '医生'}}
        case['payload']['decisions'] = [record]
        store = Store(':memory:')
        try:
            store.save_case('C0001', payload=case['payload'], case_state='awaiting_doctor')
            store.save_decision(record)
            flow = CaseFlow(store=store)
            review = flow.get_review('C0001')
            self.assertEqual([x['decision_id'] for x in review['stale_decisions']], ['D-OLD'])
            self.assertEqual(store.list_decisions('C0001')[0]['after'], '历史分析修改')
            effective = report_service._reviewed_case(case)
            self.assertEqual(effective['payload']['mdt']['analyses'][0]['inferences'][0]['text'], '分析参考正文')
            case['case_state'] = 'doctor_confirmed'
            report = report_service.build_full_report(case)
            self.assertNotIn('历史分析修改', str(report.sections['医生最终意见']))
            self.assertEqual(report.sections['审核记录与版本']['最近操作'][0]['原因'], '此前审核')
        finally:
            store.close()
