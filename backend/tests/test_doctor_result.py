"""医生发布最终建议、患者读取结果的 HTTP 闭环。"""
import unittest
import app as app_module
from services.auth import AuthService, ROLE_DOCTOR, ROLE_PATIENT
from services.case_flow import CaseFlow
from services.store import Store


class DoctorResultHttpTest(unittest.TestCase):
    def setUp(self):
        self.old_flow, self.old_auth = app_module.case_flow, app_module.auth_service
        self.old_store = app_module._shared_store
        self.store = Store(':memory:')
        app_module._shared_store = self.store
        app_module.case_flow = CaseFlow(store=self.store, use_llm=False)
        app_module.auth_service = AuthService(accounts={
            name:{'password':name+'-password','role':ROLE_DOCTOR if name=='doctor' else ROLE_PATIENT,'label':name}
            for name in ('alice','bob','doctor')})
        self.store.save_case('C-RESULT',owner_id='alice',case_state='awaiting_doctor',payload={
            'profile':{'chief_complaint':'膝部不适两周'},
            'triage':{'primary':{'department':'骨科'},'selected':['骨科']},
            'path':{'steps':[{'step_id':'S1','title':'向医生确认后续评估安排','requires_doctor':True}]}})
        self.store.grant_case_access('C-RESULT','doctor',granted_by='alice')
        app_module.app.config['TESTING']=True
        self.client=app_module.app.test_client()
        self.headers={}
        for name in ('alice','bob','doctor'):
            login=self.client.post('/api/login',json={'username':name,'password':name+'-password'})
            self.headers[name]={'Authorization':'Bearer '+login.get_json()['session']['token']}

    def tearDown(self):
        app_module.case_flow, app_module.auth_service = self.old_flow, self.old_auth
        app_module._shared_store = self.old_store
        self.store.close()

    def test_doctor_publishes_final_advice_and_patient_reads_same_version(self):
        pending=self.client.get('/api/v2/case/C-RESULT/report',headers=self.headers['alice']).get_json()
        self.assertFalse(pending['doctor_result']['published'])
        for item in app_module.case_flow.get_review('C-RESULT')['items']:
            if item['needs_doctor_review']:
                decision=self.client.post('/api/v2/case/C-RESULT/decisions',headers=self.headers['doctor'],json={
                    'item_id':item['item_id'],'action':'accept'})
                self.assertEqual(decision.status_code,200,decision.get_json())
        self._confirm_routing()
        advice='请携带已有检查资料复诊，并向接诊医生确认后续安排。'
        published=self.client.post('/api/v2/case/C-RESULT/publish',headers=self.headers['doctor'],json={'final_advice':advice})
        self.assertEqual(published.status_code,200,published.get_json())
        result=self.client.get('/api/v2/case/C-RESULT/report',headers=self.headers['alice']).get_json()
        self.assertTrue(result['doctor_result']['published'])
        self.assertEqual(result['doctor_result']['final_advice'],advice)
        self.assertEqual(result['doctor_result']['report_version'],published.get_json()['report_version'])

    def _confirm_routing(self, decision='single_specialty', secondary=None):
        response=self.client.post('/api/v2/case/C-RESULT/triage-confirmation',headers=self.headers['doctor'],json={
            'primary_department':'骨科','secondary_departments':secondary or [],'reason':'由骨科牵头评估'})
        self.assertEqual(response.status_code,200,response.get_json())
        response=self.client.post('/api/v2/case/C-RESULT/mdt-screening',headers=self.headers['doctor'],json={
            'decision':decision,'reason':'根据已核对的资料确定处理方式','purpose':'共同确认下一步安排' if decision=='recommend_mdt' else ''})
        self.assertEqual(response.status_code,200,response.get_json())

    def test_zero_pending_still_explains_routing_blockers_and_publish_cannot_bypass_them(self):
        for item in app_module.case_flow.get_review('C-RESULT')['items']:
            if item['needs_doctor_review']:
                app_module.case_flow.submit_decision('C-RESULT',item['item_id'],'accept')
        review=self.client.get('/api/v2/case/C-RESULT/review',headers=self.headers['doctor']).get_json()
        self.assertEqual(review['summary']['pending'],0)
        self.assertEqual({b['code'] for b in review.get('publication_blockers',[])},{'triage','mdt_screening'})
        response=self.client.post('/api/v2/case/C-RESULT/publish',headers=self.headers['doctor'],json={'final_advice':'按安排复诊','accept_all':True})
        self.assertEqual(response.status_code,400,response.get_json())
        self.assertEqual({b['screen'] for b in response.get_json()['publication_blockers']},{'triage'})
        self.assertFalse(app_module.case_flow.get_review('C-RESULT')['published'])

    def test_formal_mdt_blocks_publication_until_record_completed(self):
        self._confirm_routing('recommend_mdt',['眼科'])
        for item in app_module.case_flow.get_review('C-RESULT')['items']:
            if item['needs_doctor_review']:
                app_module.case_flow.submit_decision('C-RESULT',item['item_id'],'accept')
        response=self.client.post('/api/v2/case/C-RESULT/publish',headers=self.headers['doctor'],json={'final_advice':'按会诊安排复评'})
        self.assertEqual(response.status_code,400,response.get_json())
        self.assertIn('mdt_record',{b['code'] for b in response.get_json()['publication_blockers']})
        completed=self.client.post('/api/v2/case/C-RESULT/mdt-formal-record',headers=self.headers['doctor'],json={
            'status':'completed','conclusion':'已共同确认后续安排','participants':['骨科医生','眼科医生']})
        self.assertEqual(completed.status_code,200,completed.get_json())
        published=self.client.post('/api/v2/case/C-RESULT/publish',headers=self.headers['doctor'],json={'final_advice':'按会诊安排复评'})
        self.assertEqual(published.status_code,200,published.get_json())

    def test_publish_requires_nonempty_final_advice_and_doctor_role(self):
        for body in ({},{'final_advice':' '},{'final_advice':[]},{'final_advice':'字'*4001}):
            response=self.client.post('/api/v2/case/C-RESULT/publish',headers=self.headers['doctor'],json=body)
            self.assertEqual(response.status_code,400)
            self.assertIn('final_advice',response.get_json()['error'])
        self.assertEqual(self.client.post('/api/v2/case/C-RESULT/publish',headers=self.headers['alice'],json={'final_advice':'意见'}).status_code,403)
        self.assertEqual(self.client.get('/api/v2/case/C-RESULT/report',headers=self.headers['bob']).status_code,404)

    def test_changed_routing_cannot_reuse_old_mdt_decision_and_completed_record(self):
        self._confirm_routing('recommend_mdt',['眼科'])
        self.assertTrue(app_module.case_flow.submit_mdt_formal_record(
            'C-RESULT',status='completed',participants=['骨科医生'],conclusion='按原范围确认安排',reviewer_id='doctor')['ok'])
        response=self.client.post('/api/v2/case/C-RESULT/triage-confirmation',headers=self.headers['doctor'],json={
            'primary_department':'眼科','secondary_departments':[],'reason':'改由眼科负责后续评估'})
        self.assertEqual(response.status_code,200,response.get_json())
        review=self.client.get('/api/v2/case/C-RESULT/review',headers=self.headers['doctor']).get_json()
        self.assertIsNone(review['mdt_screening'])
        self.assertIsNone(review['mdt_formal_record'])
        self.assertIn('mdt_screening',{b['code'] for b in review['publication_blockers']})

    def test_batch_publish_blocked_by_workflow_does_not_accept_items(self):
        before=app_module.case_flow.get_review('C-RESULT')
        response=self.client.post('/api/v2/case/C-RESULT/publish',headers=self.headers['doctor'],json={
            'accept_all':True,'final_advice':'按医生确认的安排复诊'})
        self.assertEqual(response.status_code,400,response.get_json())
        after=app_module.case_flow.get_review('C-RESULT')
        self.assertEqual(after['summary']['pending'],before['summary']['pending'])
        self.assertEqual(after['decisions'],[])
