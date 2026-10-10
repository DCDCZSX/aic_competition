"""审核修改、分析来源和患者报告预览的回归验证。"""
import copy
import unittest
from unittest import mock
from services import review_service as rs, report_service as reports, doctor_mdt
from services.case_flow import CaseFlow
from services.store import Store
from tests.test_review import sample_case

def analysis_case():
    case = sample_case()
    case['payload']['gaps'] = []
    case['payload']['mdt']['analyses'] = [
        {'department': '眼科', 'status':'ok', 'focus':'核对视力变化',
         'facts':['既往检查记录'],
         'inferences':[{'text':'视力变化需结合既往检查评估', 'evidence_refs':['检查记录']}],
         'checks':[{'priority':'high', 'content':'是否需进一步眼科检查'}],
         'information_gaps':['缺少近期视力记录']},
        {'department':'肾内科', 'status':'ok', 'inferences':[{'text':'浮肿原因需要专科评估','evidence_refs':[]}]}]
    return case

class ReviewPublicationTest(unittest.TestCase):
    def test_single_specialty_report_uses_related_preanalysis(self):
        case=analysis_case();p=case['payload']
        p['triage_doctor_confirmation']={'status':'confirmed','selected':['眼科'],'primary_department':'眼科'}
        p['mdt_screening']={'status':'decided','decision':'single_specialty','departments_source':'doctor_routing'}
        specialty = [x for x in rs.build_items(case) if x.target_type == 'ai_analysis']
        self.assertEqual([x.department for x in specialty], ['眼科'])
        analyses=reports._reviewed_case(case)['payload']['mdt']['analyses']
        self.assertEqual([a['department'] for a in analyses],['眼科'])
        self.assertEqual(analyses[0]['inferences'][0]['text'],'视力变化需结合既往检查评估')

    def test_switch_to_single_removes_simulation_review_entries(self):
        case=analysis_case();p=case['payload']
        p['triage_doctor_confirmation']={'status':'confirmed','primary_department':'眼科','selected':['眼科']}
        p['mdt_screening']={'status':'decided','decision':'recommend_mdt','departments_source':'doctor_routing'}
        p['doctor_mdt_simulation']={'state':'done','content_revision':2,'input_fingerprint':doctor_mdt.input_fingerprint(p),
            'result':{'aggregation':{'consensus':[{'topic':'模拟关注点','example':'模拟意见'}]}}}
        simulation=next(x for x in rs.build_items(case) if x.target_type=='consensus_item')
        decision={'target_type':simulation.target_type,'target_id':simulation.target_id,'action':'modify',
                  'after':'模拟意见的医生改写','content_revision':2}
        p['mdt_screening']['decision']='single_specialty'
        self.assertNotIn(simulation.item_id,[x.item_id for x in rs.build_items(case)])
        self.assertEqual(rs.current_decisions(rs.build_items(case),[decision]),[])

    def test_rejected_disagreement_does_not_renumber_remaining_decision(self):
        case=analysis_case();p=case['payload']
        p['mdt']['aggregation']['disagreements']=[{'topic':'第一主题'},{'topic':'第二主题'}]
        p['decisions']=[{'target_type':'consensus_item','target_id':'X-01','action':'reject','reason':'不采用'},
                        {'target_type':'consensus_item','target_id':'X-02','action':'accept'}]
        p['disagreement_decisions']=[{'disagreement_id':'X-01','status':'resolved','choice':'第一处理结果'},
                                   {'disagreement_id':'X-02','status':'resolved','choice':'第二处理结果'}]
        effective=reports._reviewed_case(case)['payload']['mdt']['aggregation']['disagreements']
        result=reports.disagreements_for_report(effective,p['disagreement_decisions'])
        self.assertEqual(result[0]['分歧编号'],'X-02')
        self.assertEqual(result[0]['牵头医生决定'],'第二处理结果')

    def test_preanalysis_retry_invalidates_changed_consensus_and_evidence(self):
        store=Store(':memory:');flow=CaseFlow(store=store)
        try:
            case=analysis_case();store.save_case('C0001',payload=case['payload'],case_state='awaiting_doctor')
            old=next(x for x in rs.build_items(case) if x.item_id=='consensus_item:C-01')
            flow.submit_decision('C0001',old.item_id,'modify',after='旧医生修改',reason='旧依据',reviewer_name='doctor')
            changed=copy.deepcopy(case['payload']['mdt']);changed['aggregation']['consensus'][0]['example']='预分析重试后的新意见'
            flow._patch_payload('C0001',mdt=changed)
            current=next(x for x in flow.get_review('C0001')['items'] if x['item_id']==old.item_id)
            self.assertEqual(current['state'],rs.AWAITING);self.assertGreater(current['content_revision'],old.content_revision)
            self.assertEqual(current['ai_content'],'预分析重试后的新意见')
            flow.submit_decision('C0001',old.item_id,'accept',reviewer_name='doctor')
            changed['aggregation']['consensus'][0]['supporters']=['眼科','肾内科']
            flow._patch_payload('C0001',mdt=changed)
            self.assertEqual(next(x for x in flow.get_review('C0001')['items'] if x['item_id']==old.item_id)['state'],rs.AWAITING)
        finally:store.close()

    def test_worker_retry_reopens_published_preanalysis_and_preserves_version(self):
        store=Store(':memory:');flow=CaseFlow(store=store)
        try:
            case=analysis_case();case['payload']['mdt']['aggregation']={'consensus':[{'topic':'综合关注点','example':'原始综合意见','supporters':['眼科']}]}
            store.save_case('C0001',payload=case['payload'],case_state='awaiting_doctor')
            for item in rs.build_items(case):flow.submit_decision('C0001',item.item_id,'accept',reviewer_name='doctor')
            published=flow.publish_review('C0001',reviewer_name='doctor')
            self.assertTrue(published['ok'],published)
            version=copy.deepcopy(store.get_report_version('C0001',published['report_version']))
            with mock.patch.object(flow,'_run_mdt',return_value=copy.deepcopy(case['payload']['mdt'])):flow._analysis_worker('C0001',phases=['mdt'])
            self.assertEqual(flow.get_review('C0001')['case_state'],'doctor_confirmed')
            self.assertTrue(flow.get_review('C0001')['published'])
            changed=copy.deepcopy(case['payload']['mdt']);changed['aggregation']['consensus'][0]['example']='重试后的新共识'
            with mock.patch.object(flow,'_run_mdt',return_value=changed):flow._analysis_worker('C0001',phases=['mdt'])
            current=flow.get_review('C0001')
            self.assertFalse(current['published']);self.assertEqual(current['case_state'],'awaiting_recheck')
            claim=next(x for x in current['items'] if x['ai_content']=='重试后的新共识')
            self.assertEqual(claim['state'],rs.AWAITING)
            self.assertEqual(store.get_report_version('C0001',published['report_version']),version)
        finally:store.close()

    def test_disagreement_question_copy_follows_rejection_and_modification(self):
        case=analysis_case();p=case['payload'];topic=p['mdt']['aggregation']['disagreements'][0]['topic']
        p['mdt']['aggregation']['open_questions']=['分歧待裁决：'+topic]
        p['decisions']=[{'target_type':'consensus_item','target_id':'X-01','action':'reject','reason':'不采用'}]
        self.assertNotIn(topic,str(reports.build_patient_summary(case).sections))
        p['decisions'][0].update(action='modify',after='医生改写后的待确认问题')
        summary=reports.build_patient_summary(case).sections
        self.assertNotIn('分歧待裁决：'+topic,str(summary));self.assertIn('医生改写后的待确认问题',str(summary))

    def test_preview_is_read_only_and_stale_preview_cannot_publish(self):
        store=Store(':memory:');flow=CaseFlow(store=store)
        try:
            case=analysis_case();p=case['payload']
            p['mdt']['aggregation']={}
            p['triage_doctor_confirmation']={'status':'confirmed','primary_department':'眼科','selected':['眼科']}
            p['mdt_screening']={'status':'decided','decision':'single_specialty','departments_source':'doctor_routing'}
            store.save_case('C0001',payload=p,case_state='awaiting_doctor',report_version=1)
            for item in rs.build_items(store.load_case('C0001')):
                flow.submit_decision('C0001',item.item_id,'accept',reviewer_name='doctor')
            before=copy.deepcopy(store.load_case('C0001'));versions=store.list_report_versions('C0001')
            preview=flow.preview_publication('C0001',final_advice='请按医生安排复诊',reviewer_name='doctor')
            self.assertTrue(preview['ok'],preview);self.assertEqual(store.load_case('C0001'),before)
            self.assertEqual(store.list_report_versions('C0001'),versions)
            result=flow.publish_review('C0001',final_advice='修改后的最终建议',reviewer_name='doctor',expected_preview_revision=preview['preview_revision'])
            self.assertFalse(result['ok']);self.assertIn('重新预览',result['error'])
            good=flow.publish_review('C0001',final_advice='请按医生安排复诊',reviewer_name='doctor',expected_preview_revision=preview['preview_revision'])
            self.assertTrue(good['ok'],good)
            conflict=flow.publish_review('C0001',final_advice='另一位医生的不同建议',reviewer_name='doctor',expected_preview_revision=preview['preview_revision'])
            self.assertFalse(conflict['ok']);self.assertIn('重新预览',conflict['error'])
        finally: store.close()
