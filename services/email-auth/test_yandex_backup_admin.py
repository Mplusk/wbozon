import gc
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from app import LoginApp
from portal import Portal, Vault
import yandex_backup_admin


@unittest.skipIf(yandex_backup_admin.fcntl is None, "Linux settings lock required")
class YandexBackupAdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 100000
        self.login = LoginApp(self.tmp.name + '/auth.sqlite', 'x' * 32, 'https://3dprinta.shop', clock=lambda: self.now)
        self.vault = Vault(self.tmp.name + '/vault.sqlite', secrets.token_bytes(32))
        self.app = Portal(self.login, self.vault, hashlib.sha256(b'synthetic').hexdigest())
        self.sid, self.csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.login.connect() as db:
            db.execute('INSERT INTO sessions VALUES (?,?,?)', (self.login.digest(self.sid), self.now + 43200, self.csrf))

    def tearDown(self):
        gc.collect(); self.tmp.cleanup()

    def request(self, path='/settings', method='GET', data=None, *, cookie=True, origin=True):
        body = urlencode(data or {}).encode()
        env = {'PATH_INFO': path, 'REQUEST_METHOD': method, 'HTTP_HOST': '3dprinta.shop',
               'CONTENT_TYPE': 'application/x-www-form-urlencoded', 'CONTENT_LENGTH': str(len(body)),
               'wsgi.input': io.BytesIO(body)}
        if cookie: env['HTTP_COOKIE'] = '__Host-sid=' + self.sid
        if origin: env['HTTP_ORIGIN'] = 'https://3dprinta.shop'
        response = {}
        def start(status, headers): response.update(status=status, headers=dict(headers))
        response['body'] = b''.join(self.app(env, start)).decode()
        return response

    def save(self, **changes):
        data = {'csrf': self.csrf, 'action': 'save', 'public_url': 'https://disk.yandex.ru/d/example',
                'disk_folder': 'disk:/Backups', 'token': 'synthetic-yandex-token-12345', 'enabled': 'yes'}
        data.update(changes)
        return self.request('/settings/yandex-backup', 'POST', data)

    def test_config_is_encrypted_never_rendered_and_can_be_disabled(self):
        self.assertTrue(self.save()['status'].startswith('303'))
        self.assertTrue(self.vault.read('yandex_backup')['enabled'])
        page = self.request()['body']
        self.assertIn('Резервные копии на Яндекс Диск', page)
        self.assertIn('Отключить резервные копии', page)
        self.assertNotIn('synthetic-yandex-token-12345', page)
        self.assertNotIn(b'synthetic-yandex-token-12345', Path(self.vault.path).read_bytes())
        disabled = self.request('/settings/yandex-backup', 'POST', {'csrf': self.csrf, 'action': 'disable'})
        self.assertTrue(disabled['status'].startswith('303'))
        self.assertFalse(self.vault.read('yandex_backup')['enabled'])

    def test_blank_token_preserves_existing_secret_and_settings_are_editable(self):
        self.save()
        self.assertTrue(self.save(token='', disk_folder='disk:/New folder', enabled='no')['status'].startswith('303'))
        result = self.vault.read('yandex_backup')
        self.assertEqual(result['token'], 'synthetic-yandex-token-12345')
        self.assertEqual(result['disk_folder'], 'disk:/New folder')
        self.assertFalse(result['enabled'])

    def test_settings_page_decrypts_only_display_metadata(self):
        self.save()
        original = self.vault.aead
        class DisplayOnly:
            def decrypt(self, nonce, ciphertext, associated_data):
                if associated_data != b'yandex_backup_metadata':
                    raise AssertionError('settings page tried to decrypt a credential')
                return original.decrypt(nonce, ciphertext, associated_data)
        self.vault.aead = DisplayOnly()
        self.vault.read = lambda *_: (_ for _ in ()).throw(AssertionError('credential read on GET'))
        page = self.request()['body']
        self.assertIn('Включены', page)
        self.assertNotIn('synthetic-yandex-token-12345', page)

    def test_inconsistent_metadata_never_claims_a_state_and_can_be_disabled(self):
        self.save()
        with sqlite3.connect(self.vault.path) as db:
            db.execute('UPDATE vault SET blob=? WHERE provider=?', (b'corrupt', 'yandex_backup_metadata'))
        page = self.request()['body']
        self.assertIn('Состояние не определено', page)
        self.assertIn('Отключить резервные копии', page)
        response = self.request('/settings/yandex-backup', 'POST', {'csrf': self.csrf, 'action': 'disable'})
        self.assertTrue(response['status'].startswith('303'))
        self.assertFalse(self.vault.read('yandex_backup')['enabled'])

    def test_no_session_no_origin_and_bad_csrf_cannot_change_settings(self):
        form = {'csrf': self.csrf, 'action': 'disable'}
        self.assertTrue(self.request('/settings/yandex-backup', 'POST', form, cookie=False)['status'].startswith('401'))
        self.assertTrue(self.request('/settings/yandex-backup', 'POST', form, origin=False)['status'].startswith('403'))
        self.assertTrue(self.request('/settings/yandex-backup', 'POST', dict(form, csrf='bad'))['status'].startswith('403'))
        self.assertIsNone(self.vault.read('yandex_backup'))

    def test_stale_session_can_disable_but_cannot_edit_or_enable(self):
        self.save(); self.now += 301
        self.assertTrue(self.save()['status'].startswith('403'))
        self.assertTrue(self.request('/settings/yandex-backup', 'POST', {'csrf': self.csrf, 'action': 'disable'})['status'].startswith('303'))
        self.assertFalse(self.vault.read('yandex_backup')['enabled'])

    def test_invalid_links_tokens_and_paths_are_rejected(self):
        for changes in ({'public_url': 'https://attacker.test/d/example'}, {'public_url': 'http://disk.yandex.ru/d/example'},
                        {'disk_folder': 'disk:/../Elsewhere'}, {'token': 'injected\r\nheader'}, {'enabled': 'maybe'}):
            with self.subTest(changes=changes):
                self.assertTrue(self.save(**changes)['status'].startswith('400'))
        self.assertIsNone(self.vault.read('yandex_backup'))

    def test_running_backup_prevents_false_disable_confirmation(self):
        self.save()
        lock = Path(self.tmp.name) / '.yandex-backup.lock'
        with lock.open('r+') as held:
            yandex_backup_admin.fcntl.flock(held, yandex_backup_admin.fcntl.LOCK_EX | yandex_backup_admin.fcntl.LOCK_NB)
            response = self.request('/settings/yandex-backup', 'POST', {'csrf': self.csrf, 'action': 'disable'})
        self.assertTrue(response['status'].startswith('409'))
        self.assertTrue(self.vault.read('yandex_backup')['enabled'])

    def test_last_success_and_failed_attempt_are_visible(self):
        self.save()
        status = Path(self.tmp.name) / 'status.json'
        status.write_text(json.dumps({'last_success': '2026-11-24T10:00:00+00:00', 'state': 'failed'}))
        status.chmod(0o644)
        with patch.dict(os.environ, {'YANDEX_BACKUP_STATUS_FILE': str(status)}):
            page = self.request()['body']
        self.assertIn('2026-11-24T10:00:00+00:00', page)
        self.assertIn('Последняя попытка завершилась ошибкой', page)


if __name__ == '__main__':
    unittest.main()
