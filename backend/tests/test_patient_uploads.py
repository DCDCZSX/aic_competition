import os
import tempfile
import unittest
from io import BytesIO

import app as app_module
from services.auth import AuthService, ROLE_PATIENT
from services.patient_uploads import PatientUploadStore


class PatientUploadStoreTest(unittest.TestCase):
    def test_saves_supported_image_with_random_name_and_owner_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PatientUploadStore(tmp)
            result = store.save('alice', '病历 1.PNG', 'image/png', b'png-data')
            self.assertTrue(result['ok'])
            self.assertEqual(result['owner_id'], 'alice')
            self.assertEqual(result['content_type'], 'image/png')
            self.assertTrue(result['upload_id'])
            self.assertNotIn('病历', result['stored_name'])
            self.assertTrue(os.path.exists(os.path.join(tmp, result['stored_name'])))
            self.assertTrue(os.path.exists(os.path.join(tmp, result['upload_id'] + '.json')))

    def test_rejects_unsupported_type_and_oversized_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PatientUploadStore(tmp, max_bytes=4)
            self.assertEqual(store.save('alice', 'note.txt', 'text/plain', b'x')['code'], 'unsupported_type')
            self.assertEqual(store.save('alice', 'scan.jpg', 'image/jpeg', b'12345')['code'], 'file_too_large')


class PatientUploadHttpTest(unittest.TestCase):
    def setUp(self):
        self.old_auth = app_module.auth_service
        self.old_uploads = getattr(app_module, 'patient_uploads', None)
        self.tmp = tempfile.TemporaryDirectory()
        app_module.auth_service = AuthService(accounts={
            'alice': {'password': 'alice-password', 'role': ROLE_PATIENT, 'label': '患者 A'},
            'doctor': {'password': 'doctor-password', 'role': 'doctor', 'label': '医生'},
        })
        app_module.patient_uploads = PatientUploadStore(self.tmp.name)
        app_module.app.config.update(TESTING=True, MAX_CONTENT_LENGTH=16 * 1024 * 1024)
        self.client = app_module.app.test_client()
        response = self.client.post('/api/login', json={'username': 'alice', 'password': 'alice-password'})
        self.headers = {'Authorization': 'Bearer ' + response.get_json()['session']['token']}

    def tearDown(self):
        app_module.auth_service = self.old_auth
        if self.old_uploads is not None:
            app_module.patient_uploads = self.old_uploads
        self.tmp.cleanup()

    def test_upload_requires_patient_session_and_returns_metadata(self):
        response = self.client.post('/api/v2/patient/uploads', headers=self.headers, data={
            'files': (BytesIO(b'jpeg-data'), 'report.jpg'),
        }, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 201, response.get_json())
        item = response.get_json()['uploads'][0]
        self.assertEqual(item['content_type'], 'image/jpeg')
        self.assertNotIn('path', item)
        self.assertTrue(item['upload_id'])

    def test_delete_upload_is_owner_bound(self):
        response = self.client.post('/api/v2/patient/uploads', headers=self.headers, data={
            'files': (BytesIO(b'jpeg-data'), 'report.jpg'),
        }, content_type='multipart/form-data')
        upload_id = response.get_json()['uploads'][0]['upload_id']
        deleted = self.client.delete('/api/v2/patient/uploads/' + upload_id, headers=self.headers)
        self.assertEqual(deleted.status_code, 200, deleted.get_json())
        missing = self.client.delete('/api/v2/patient/uploads/' + upload_id, headers=self.headers)
        self.assertEqual(missing.status_code, 404)

    def test_upload_rejects_non_image(self):
        response = self.client.post('/api/v2/patient/uploads', headers=self.headers, data={
            'files': (BytesIO(b'text'), 'notes.txt'),
        }, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(response.get_json()['code'], 'unsupported_type')

    def test_ocr_degrades_without_provider_configuration(self):
        response = self.client.post('/api/v2/patient/uploads', headers=self.headers, data={
            'files': (BytesIO(b'jpeg-data'), 'report.jpg'),
        }, content_type='multipart/form-data')
        upload_id = response.get_json()['uploads'][0]['upload_id']
        ocr = self.client.post('/api/v2/patient/uploads/' + upload_id + '/ocr', headers=self.headers)
        self.assertEqual(ocr.status_code, 503)
        self.assertEqual(ocr.get_json()['code'], 'ocr_unavailable')


if __name__ == '__main__':
    unittest.main()
