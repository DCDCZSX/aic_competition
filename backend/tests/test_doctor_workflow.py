"""医生确认范围后的模拟，不能复用患者预分析或迟到的旧轮次。"""
import threading
import tempfile
from pathlib import Path
import time
import unittest
from unittest import mock

from services.case_flow import CaseFlow
from services.store import Store
from tests.test_mdt_screening import CASE_TEXT
from tests import test_mdt_screening as screening_tests
from services.mdt_analyst import SpecialtyAnalysis
from schemas import SpecialistOpinionCard


class DoctorWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.flow = CaseFlow(store=self.store)
        self.case_id = self.flow.create_case(medical_history=CASE_TEXT).case_id

    def tearDown(self):
        self.store.close()

    def route(self, **changes):
        values = dict(primary_department='内分泌科', secondary_departments=['眼科'],
                      decision='recommend_mdt', purpose='明确本次共同处理的问题',
                      reason='根据已核对资料共同讨论', reviewer_id='doctor')
        values.update(changes)
        return self.flow.submit_doctor_routing(self.case_id, **values)

    def wait_result(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            view = self.flow.get_doctor_mdt_simulation(self.case_id)
            if view['state'] != 'running':
                return view
            time.sleep(.01)
        self.fail('模拟未结束')

    def test_invalid_route_does_not_partially_save_confirmation(self):
        before = self.store.load_case(self.case_id)['payload']
        result = self.route(secondary_departments=[])
        self.assertFalse(result['ok'])
        self.assertEqual(self.store.load_case(self.case_id)['payload'], before)

    def test_route_saves_one_authoritative_scope_and_target(self):
        result = self.route()
        self.assertTrue(result['ok'], result)
        payload = self.store.load_case(self.case_id)['payload']
        self.assertEqual(payload['triage_doctor_confirmation']['selected'],
                         payload['mdt_screening']['departments'])
        self.assertEqual(payload['mdt_screening']['departments_source'], 'doctor_routing')

    def test_completed_simulation_survives_database_reopen(self):
        self.route()
        result = {'analyses': [{'department': '眼科', 'status': 'ok', 'facts': ['已保存的病例事实']}],
                  'aggregation': {'consensus': [{'topic': '已保存的共同事项'}]}}
        with mock.patch.object(self.flow, '_run_mdt', return_value=result):
            self.flow.start_doctor_mdt_simulation(self.case_id, reviewer_id='doctor')
            saved = self.wait_result()
        case = self.store.load_case(self.case_id)
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'saved-case.sqlite')
            store = Store(path)
            store.save_case(self.case_id, payload=case['payload'])
            store.close()
            reopened = Store(path)
            try:
                view = CaseFlow(store=reopened).get_doctor_mdt_simulation(self.case_id)
                self.assertEqual(view['state'], 'done')
                self.assertEqual(view['run_id'], saved['run_id'])
                self.assertEqual(view['result'], saved['result'])
                self.assertTrue(view['finished_at'])
            finally:
                reopened.close()

    def test_simulation_requires_confirmed_mdt_decision(self):
        self.assertFalse(self.flow.start_doctor_mdt_simulation(self.case_id, reviewer_id='doctor')['ok'])
        self.route(decision='single_specialty', secondary_departments=[])
        self.assertFalse(self.flow.start_doctor_mdt_simulation(self.case_id, reviewer_id='doctor')['ok'])

    def test_simulation_uses_all_confirmed_departments_and_target_without_overwriting_preanalysis(self):
        self.route(secondary_departments=['眼科', '肾内科', '心内科'])
        original = self.store.load_case(self.case_id)['payload'].get('mdt')
        result = dict(analyses=[{'department': '内分泌科', 'card': {}}], aggregation={}, failed_departments=[])
        with mock.patch.object(self.flow, '_run_mdt', return_value=result) as run:
            self.assertTrue(self.flow.start_doctor_mdt_simulation(self.case_id, reviewer_id='doctor')['ok'])
            view = self.wait_result()
        self.assertEqual(view['state'], 'done')
        self.assertEqual(run.call_args.kwargs['outcome'].selected, ['内分泌科', '眼科', '肾内科', '心血管内科'])
        self.assertEqual(run.call_args.kwargs['purpose'], '明确本次共同处理的问题')
        self.assertEqual(run.call_args.kwargs['max_departments'], 6)
        self.assertEqual(self.store.load_case(self.case_id)['payload'].get('mdt'), original)

    def test_running_request_is_deduplicated_and_old_result_cannot_replace_new_scope(self):
        self.route()
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def delayed(**kwargs):
            started.set()
            release.wait(2)
            return {'analyses': [], 'aggregation': {}, 'failed_departments': kwargs['outcome'].selected}
        with mock.patch.object(self.flow, '_run_mdt', side_effect=delayed):
            first = self.flow.start_doctor_mdt_simulation(self.case_id, reviewer_id='doctor')
            self.assertTrue(started.wait(1))
            second = self.flow.start_doctor_mdt_simulation(self.case_id, reviewer_id='doctor')
            self.assertEqual(first['simulation']['run_id'], second['simulation']['run_id'])
            self.route(purpose='变更后的共同讨论目标')
            self.assertEqual(self.flow.get_doctor_mdt_simulation(self.case_id)['state'], 'stale')
            release.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                stored = self.store.load_case(self.case_id)['payload']['doctor_mdt_simulation']
                if stored['state'] != 'running':
                    finished.set()
                    break
                time.sleep(.01)
            self.assertTrue(finished.is_set())
        self.assertEqual(self.flow.get_doctor_mdt_simulation(self.case_id)['state'], 'stale')

    def test_model_failure_is_explicit_and_does_not_block_real_formal_record(self):
        self.route()
        with mock.patch.object(self.flow, '_run_mdt', side_effect=RuntimeError('test unavailable')):
            self.flow.start_doctor_mdt_simulation(self.case_id, reviewer_id='doctor')
            view = self.wait_result()
        self.assertEqual(view['state'], 'failed')
        self.assertTrue(view['message'])
        completed = self.flow.submit_mdt_formal_record(self.case_id, status='completed',
                            conclusion='实际医生共同确认后续安排', participants=[], reviewer_id='doctor')
        self.assertTrue(completed['ok'], completed)
        blockers = self.flow.get_review(self.case_id)['publication_blockers']
        self.assertNotIn('mdt_record', [item['code'] for item in blockers])

    def test_case_content_change_marks_saved_result_stale(self):
        self.route()
        with mock.patch.object(self.flow, '_run_mdt', return_value={'analyses': [{}], 'aggregation': {}}):
            self.flow.start_doctor_mdt_simulation(self.case_id, reviewer_id='doctor')
            self.wait_result()
        self.flow._patch_payload(self.case_id, masked_history=CASE_TEXT + '新增检查资料')
        self.assertEqual(self.flow.get_doctor_mdt_simulation(self.case_id)['state'], 'stale')

    def test_formal_completion_must_be_reconfirmed_after_new_case_content(self):
        self.route()
        result = self.flow.submit_mdt_formal_record(self.case_id, status='completed', participants=[],
                      conclusion='实际共同讨论后的结论', reviewer_id='doctor')
        self.assertTrue(result['ok'], result)
        self.flow._patch_payload(self.case_id, masked_history=CASE_TEXT + '新检查结果待核对')
        review = self.flow.get_review(self.case_id)
        self.assertTrue(review['mdt_formal_record']['needs_reconfirmation'])
        self.assertIn('mdt_record', [x['code'] for x in review['publication_blockers']])

    def test_real_pipeline_covers_four_departments_and_injects_target_as_focus(self):
        self.route(secondary_departments=['眼科', '肾内科', '心内科'])
        def analysis(**kwargs):
            return SpecialtyAnalysis(department=kwargs['department'],
                card=SpecialistOpinionCard(case_id=self.case_id, specialty_code='test',
                                          specialty_name=kwargs['department'], focus='独立分析'))
        with mock.patch('services.case_flow.analyze_specialty', side_effect=analysis) as analyze:
            self.flow.start_doctor_mdt_simulation(self.case_id, reviewer_id='doctor')
            view = self.wait_result()
        self.assertEqual(view['state'], 'done')
        self.assertEqual(analyze.call_count, 4)
        self.assertTrue(all('明确本次共同处理的问题' in x.kwargs['focus'] for x in analyze.call_args_list))

    def test_late_previous_run_is_archived_without_overwriting_new_run(self):
        self.route()
        entered, release = threading.Event(), threading.Event()
        def analysis(**kwargs):
            if kwargs['purpose'] == '明确本次共同处理的问题':
                entered.set()
                release.wait(2)
            return {'analyses': [{'department': '眼科', 'status': 'ok'}], 'aggregation': {}}
        with mock.patch.object(self.flow, '_run_mdt', side_effect=analysis):
            old = self.flow.start_doctor_mdt_simulation(self.case_id, reviewer_id='doctor')['simulation']['run_id']
            self.assertTrue(entered.wait(1))
            self.route(purpose='新的讨论目标')
            new = self.flow.start_doctor_mdt_simulation(self.case_id, reviewer_id='doctor')['simulation']['run_id']
            self.assertEqual(self.wait_result()['run_id'], new)
            release.set()
            deadline = time.monotonic() + 2
            while old in self.flow._doctor_mdt_runs and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertNotIn(old, self.flow._doctor_mdt_runs)
        self.assertEqual(self.flow.get_doctor_mdt_simulation(self.case_id)['run_id'], new)
        history = self.store.load_case(self.case_id)['payload']['doctor_mdt_simulation_history']
        self.assertEqual(history[-1]['run_id'], old)
        self.assertEqual(history[-1]['state'], 'stale')

    def test_new_simulation_round_does_not_reuse_old_consensus_approval(self):
        self.route()
        result = {'analyses': [{'department': '眼科', 'status': 'ok'}],
                  'aggregation': {'consensus': [{'topic': '共同关注事项', 'supporters': ['眼科']}]}}
        with mock.patch.object(self.flow, '_run_mdt', return_value=result):
            self.flow.start_doctor_mdt_simulation(self.case_id, reviewer_id='doctor')
            self.wait_result()
            items = self.flow.get_review(self.case_id)['items']
            first = next(x for x in items if x['item_id'] == 'consensus_item:C-01')
            self.flow.submit_decision(self.case_id, item_id=first['item_id'], action='accept', reviewer_name='doctor')
            self.flow.start_doctor_mdt_simulation(self.case_id, reviewer_id='doctor')
            self.wait_result()
        second = next(x for x in self.flow.get_review(self.case_id)['items'] if x['item_id'] == first['item_id'])
        self.assertGreater(second['content_revision'], first['content_revision'])
        self.assertNotEqual(second['state'], 'confirmed')

    def test_changed_disagreement_never_prefills_previous_round_resolution(self):
        self.route()
        def result(topic):
            return {'analyses': [{'department': '眼科', 'status': 'ok'}], 'aggregation': {
                'disagreements': [{'topic': topic, 'positions': {'眼科': '需复查', '内分泌科': '需核对'}}]}}
        with mock.patch.object(self.flow, '_run_mdt', return_value=result('第一轮问题')):
            self.flow.start_doctor_mdt_simulation(self.case_id, reviewer_id='doctor')
            self.wait_result()
        saved = self.flow.submit_disagreement_decision(self.case_id, 'X-01', 'resolved',
            choice='第一轮旧处理结果', reason='第一轮依据', reviewer_name='doctor')
        self.assertTrue(saved['ok'], saved)
        with mock.patch.object(self.flow, '_run_mdt', return_value=result('第二轮新问题')):
            self.flow.start_doctor_mdt_simulation(self.case_id, reviewer_id='doctor')
            self.wait_result()
        review = self.flow.get_review(self.case_id)
        self.assertIn('X-01', review['pending_disagreements'])
        self.assertEqual(review['mdt_disagreements'][0]['分歧主题'], '第二轮新问题')
        self.assertNotEqual(review['mdt_disagreements'][0]['牵头医生决定'], '第一轮旧处理结果')

    def test_late_review_and_disagreement_submissions_are_rejected_after_regeneration(self):
        self.route()
        result = {'analyses': [{'department': '眼科', 'status': 'ok'}], 'aggregation': {
            'consensus': [{'topic': '共同事项', 'supporters': ['眼科']}],
            'disagreements': [{'topic': '需要核对', 'positions': {'眼科': '需复查', '内分泌科': '需核对'}}]}}
        with mock.patch.object(self.flow, '_run_mdt', return_value=result):
            self.flow.start_doctor_mdt_simulation(self.case_id, reviewer_id='doctor')
            self.wait_result()
            revisions = {x['item_id']: x['content_revision'] for x in self.flow.get_review(self.case_id)['items']}
            self.flow.start_doctor_mdt_simulation(self.case_id, reviewer_id='doctor')
            self.wait_result()
        decision = self.flow.submit_decision(self.case_id, 'consensus_item:C-01', 'modify',
            after='旧表单结论', reason='旧表单依据', reviewer_name='doctor',
            expected_content_revision=revisions['consensus_item:C-01'])
        disagreement = self.flow.submit_disagreement_decision(self.case_id, 'X-01', 'resolved',
            choice='旧表单处理', reason='旧表单依据', reviewer_name='doctor',
            expected_content_revision=revisions['consensus_item:X-01'])
        self.assertFalse(decision['ok'])
        self.assertFalse(disagreement['ok'])
        self.assertEqual(decision['code'], 'content_revision_changed')
        self.assertEqual(disagreement['code'], 'content_revision_changed')
        self.assertEqual(self.store.list_decisions(self.case_id), [])
        self.assertIn('X-01', self.flow.get_review(self.case_id)['pending_disagreements'])

    def test_published_snapshot_does_not_apply_old_modified_consensus_to_new_round(self):
        self.route()
        def result(topic):
            return {'analyses': [{'department': '眼科', 'status': 'ok'}], 'aggregation': {
                'consensus': [{'topic': topic, 'supporters': ['眼科']}]}}
        with mock.patch.object(self.flow, '_run_mdt', return_value=result('第一轮问题')):
            self.flow.start_doctor_mdt_simulation(self.case_id, reviewer_id='doctor')
            self.wait_result()
        self.flow.submit_decision(self.case_id, 'consensus_item:C-01', 'modify',
            after='第一轮医生旧结论', reason='第一轮判断', reviewer_name='doctor')
        with mock.patch.object(self.flow, '_run_mdt', return_value=result('第二轮新问题')):
            self.flow.start_doctor_mdt_simulation(self.case_id, reviewer_id='doctor')
            self.wait_result()
        self.flow.submit_mdt_formal_record(self.case_id, status='completed', participants=[],
            conclusion='实际会诊结论', reviewer_id='doctor')
        for item in self.flow.get_review(self.case_id)['items']:
            if item['needs_doctor_review']:
                self.flow.submit_decision(self.case_id, item['item_id'], 'accept', reviewer_name='doctor')
        published = self.flow.publish_review(self.case_id, reviewer_name='doctor', final_advice='实际最终建议')
        self.assertTrue(published['ok'], published)
        snapshot = self.store.get_report_version(self.case_id, published['report_version'])
        self.assertNotIn('第一轮医生旧结论', str(snapshot))


class DoctorWorkflowHttpTest(unittest.TestCase):
    setUp = screening_tests.MdtScreeningHttpTest.setUp
    tearDown = screening_tests.MdtScreeningHttpTest.tearDown
    _token = screening_tests.MdtScreeningHttpTest._token
    _headers = staticmethod(screening_tests.MdtScreeningHttpTest._headers)

    def test_publication_preview_requires_primary_doctor_access(self):
        url='/api/v2/case/C-MDT/publication-preview'
        body={'final_advice':'给患者的最终建议'}
        self.assertEqual(self.client.post(url,json=body).status_code,401)
        patient=self._token('alice','alice-password')
        self.assertEqual(self.client.post(url,headers=self._headers(patient),json=body).status_code,403)
        collaborator=self._token('doctor_eye','doctor-password')
        self.store.grant_case_access('C-MDT','doctor_eye',granted_by='doctor',case_role='collaborator')
        self.assertEqual(self.client.post(url,headers=self._headers(collaborator),json=body).status_code,403)
        primary=self._token('doctor','doctor-password')
        blocked=self.client.post(url,headers=self._headers(primary),json=body)
        self.assertEqual(blocked.status_code,400)
        self.assertTrue(blocked.get_json()['publication_blockers'])
        self.assertEqual(self.store.list_report_versions('C-MDT'),[])
        self.store.revoke_case_access('C-MDT','doctor',revoked_by='alice')
        self.assertEqual(self.client.post(url,headers=self._headers(primary),json=body).status_code,404)

    def test_missing_revision_is_rejected_when_routing_changes_before_service_lock(self):
        lead = self._token('doctor', 'doctor-password')
        flow = screening_tests.app_module.case_flow
        original_submit = flow.submit_decision
        def route_before_lock(*args, **kwargs):
            route = flow.submit_doctor_routing('C-MDT', primary_department='内分泌科',
                secondary_departments=['眼科'], decision='recommend_mdt', reason='共同判断',
                purpose='本次目标', reviewer_id='doctor')
            self.assertTrue(route['ok'], route)
            result = {'analyses': [{'department': '眼科', 'status': 'ok'}], 'aggregation': {
                'consensus': [{'topic': '新一轮共同事项', 'supporters': ['眼科']}]}}
            with mock.patch.object(flow, '_run_mdt', return_value=result):
                flow.start_doctor_mdt_simulation('C-MDT', reviewer_id='doctor')
                deadline = time.monotonic() + 2
                while flow.get_doctor_mdt_simulation('C-MDT')['state'] == 'running' and time.monotonic() < deadline:
                    time.sleep(.01)
            return original_submit(*args, **kwargs)
        with mock.patch.object(flow, 'submit_decision', side_effect=route_before_lock):
            response = self.client.post('/api/v2/case/C-MDT/decisions',
                headers=self._headers(lead), json={'item_id': 'consensus_item:C-01',
                    'action': 'modify', 'after': '旧页面提交', 'reason': '旧依据'})
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(response.get_json()['code'], 'content_revision_changed')
        self.assertEqual(self.store.list_decisions('C-MDT'), [])

    def test_review_http_rejects_old_round_and_accepts_current_revision(self):
        lead = self._token('doctor', 'doctor-password')
        flow = screening_tests.app_module.case_flow
        route = flow.submit_doctor_routing('C-MDT', primary_department='内分泌科',
            secondary_departments=['眼科'], decision='recommend_mdt', reason='共同判断',
            purpose='本次目标', reviewer_id='doctor')
        self.assertTrue(route['ok'], route)
        result = {'analyses': [{'department': '眼科', 'status': 'ok'}], 'aggregation': {
            'consensus': [{'topic': '共同事项', 'supporters': ['眼科']}],
            'disagreements': [{'topic': '需要核对', 'positions': {'眼科': '需复查', '内分泌科': '需核对'}}]}}
        with mock.patch.object(flow, '_run_mdt', return_value=result):
            for _ in range(2):
                flow.start_doctor_mdt_simulation('C-MDT', reviewer_id='doctor')
                deadline = time.monotonic() + 2
                while flow.get_doctor_mdt_simulation('C-MDT')['state'] == 'running' and time.monotonic() < deadline:
                    time.sleep(.01)
        items = {x['item_id']: x['content_revision'] for x in flow.get_review('C-MDT')['items']}
        for endpoint, body, item_id in (
            ('decisions', {'item_id': 'consensus_item:C-01', 'action': 'accept'}, 'consensus_item:C-01'),
            ('disagreements', {'disagreement_id': 'X-01', 'status': 'resolved',
                               'choice': '核对后处理', 'reason': '当前依据'}, 'consensus_item:X-01')):
            for revision in (None, items[item_id] - 1):
                request = dict(body, expected_content_revision=revision)
                response = self.client.post('/api/v2/case/C-MDT/' + endpoint,
                    headers=self._headers(lead), json=request)
                self.assertEqual(response.status_code, 400, response.get_json())
                self.assertEqual(response.get_json()['code'], 'content_revision_changed')
            response = self.client.post('/api/v2/case/C-MDT/' + endpoint,
                headers=self._headers(lead), json=dict(body, expected_content_revision=items[item_id]))
            self.assertEqual(response.status_code, 200, response.get_json())

    def test_unified_route_authorizes_collaborator_but_only_lead_can_generate(self):
        lead = self._token('doctor', 'doctor-password')
        route = self.client.post('/api/v2/case/C-MDT/doctor-routing', headers=self._headers(lead), json={
            'primary_department': '内分泌科', 'secondary_departments': ['眼科'],
            'decision': 'recommend_mdt', 'reason': '共同讨论', 'purpose': '明确本次安排'})
        self.assertEqual(route.status_code, 200, route.get_json())
        self.assertEqual(self.store.get_case_access_role('C-MDT', 'doctor'), 'primary')
        collaborator = self._token('doctor_eye', 'doctor-password')
        self.assertEqual(self.client.get('/api/v2/case/C-MDT/doctor-mdt-simulation', headers=self._headers(collaborator)).status_code, 200)
        self.assertEqual(self.client.post('/api/v2/case/C-MDT/doctor-mdt-simulation', headers=self._headers(collaborator)).status_code, 403)

    def test_patient_and_unauthenticated_user_cannot_generate_or_read_simulation(self):
        path = '/api/v2/case/C-MDT/doctor-mdt-simulation'
        self.assertEqual(self.client.post(path).status_code, 401)
        patient = self._token('alice', 'alice-password')
        self.assertEqual(self.client.post(path, headers=self._headers(patient)).status_code, 403)
        self.assertEqual(self.client.get(path, headers=self._headers(patient)).status_code, 403)
        self.flow._patch_payload('C-MDT', doctor_mdt_simulation={'requested_by': 'doctor'},
                               doctor_mdt_simulation_history=[{'requested_by': 'doctor'}])
        result = self.client.get('/api/v2/case/C-MDT', headers=self._headers(patient)).get_json()
        self.assertNotIn('doctor_mdt_simulation', result['case']['payload'])
        self.assertNotIn('doctor_mdt_simulation_history', result['case']['payload'])


if __name__ == '__main__':
    unittest.main()
