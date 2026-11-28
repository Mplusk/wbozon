"""Restore regression for owner-controlled model catalogue vault slots."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import restore_transform as transform
import restore_prepare
import test_restore_prepare

OLD = b'o' * 32
CURRENT = b'c' * 32
SLOTS = ('content_factory_catalog_setup', 'content_factory_catalog_openai', 'content_factory_catalog_gemini')


def encrypt(key, provider, value):
    nonce = b'n' * 12
    return nonce + AESGCM(key).encrypt(nonce, value, provider.encode())


def decrypt(key, provider, value):
    return AESGCM(key).decrypt(value[:12], value[12:], provider.encode())


def vault(path, key, values):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE vault(provider TEXT PRIMARY KEY, blob BLOB NOT NULL)')
        db.executemany('INSERT INTO vault VALUES (?,?)', [(p, encrypt(key,p,v)) for p,v in values.items()])


class CatalogRestoreTests(unittest.TestCase):
    def test_archives_before_and_after_feature_preserve_only_current_slots(self):
        for archived_slots in ({}, {slot: b'old-value' for slot in SLOTS}):
            with self.subTest(archive_has_slots=bool(archived_slots)), tempfile.TemporaryDirectory() as d:
                archive, current = Path(d)/'archive.sqlite', Path(d)/'current.sqlite'
                vault(archive, OLD, {'other_provider':b'archived', **archived_slots})
                current_slots = {slot: ('current-' + slot).encode() for slot in SLOTS}
                vault(current, CURRENT, {'yandex_backup':b'current', 'yandex_backup_metadata':b'meta', **current_slots})
                transform.prepare_vault(archive, current, OLD, CURRENT)
                with closing(sqlite3.connect(archive)) as db:
                    rows=dict(db.execute('SELECT provider,blob FROM vault'))
                self.assertEqual(decrypt(CURRENT, 'other_provider', rows['other_provider']), b'archived')
                for slot in SLOTS:
                    self.assertEqual(decrypt(CURRENT,slot,rows[slot]), current_slots[slot])

    def test_archive_does_not_resurrect_catalogue_if_current_is_unconfigured(self):
        with tempfile.TemporaryDirectory() as d:
            archive, current = Path(d)/'archive.sqlite', Path(d)/'current.sqlite'
            vault(archive, OLD, {slot:b'old-value' for slot in SLOTS})
            vault(current, CURRENT, {'yandex_backup':b'current', 'yandex_backup_metadata':b'meta'})
            transform.prepare_vault(archive, current, OLD, CURRENT)
            with closing(sqlite3.connect(archive)) as db:
                names={row[0] for row in db.execute('SELECT provider FROM vault')}
            self.assertFalse(names.intersection(SLOTS))


class CatalogFullPrepareTests(test_restore_prepare.PrepareTests):
    def test_full_prepare_preserves_current_slots_and_stale_archive_cannot_restore_them(self):
        with closing(sqlite3.connect(self.data / 'vault.sqlite')) as db, db:
            for slot in SLOTS:
                db.execute('INSERT INTO vault VALUES(?,?)', (slot,test_restore_prepare.blob(self.new,slot,('current-'+slot).encode())))
        with closing(sqlite3.connect(self.archive / 'vault.sqlite')) as db, db:
            for slot in SLOTS:
                db.execute('INSERT INTO vault VALUES(?,?)', (slot,test_restore_prepare.blob(self.old,slot,b'archive-old')))
        # Refresh snapshot after preparing synthetic current state.
        import restore_files
        before2=self.root_catalog_before()
        records=restore_files.snapshot(self.data,before2)
        prepared=restore_prepare.build(self.files,before2,records,self.new,self.work)
        with closing(sqlite3.connect(prepared['vault.sqlite'])) as db:
            rows=dict(db.execute('SELECT provider,blob FROM vault'))
        for slot in SLOTS:
            self.assertEqual(decrypt(self.new,slot,rows[slot]), ('current-'+slot).encode())

    def root_catalog_before(self):
        path=self.before.parent/'before-catalogue'; path.mkdir(mode=0o700)
        return path


if __name__ == '__main__': unittest.main()
