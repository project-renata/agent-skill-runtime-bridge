import json
from pathlib import Path
import tempfile
import unittest

from v0_9_0 import configuration, migrate_claims, migrate_local_journal


class Redis:
    def __init__(self):
        self.values = {'runtime-bridge-control:opaque': b'fingerprint'}

    def scan_iter(self, **kwargs):
        return list(k for k in self.values if k.startswith('runtime-bridge-control:'))

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, nx=False):
        if key not in self.values or not nx:
            self.values[key] = value

    def eval(self, script, count, key, value):
        if self.values.get(key) == value:
            del self.values[key]


class MigrationTests(unittest.TestCase):
    def test_claims_preserved_idempotently_without_receipt_interpretation(self):
        redis = Redis()
        self.assertEqual(migrate_claims(redis), 1)
        self.assertIn('runtime-bridge-control:opaque', redis.values)
        self.assertEqual(migrate_claims(redis, apply=True), 1)
        self.assertEqual(redis.values, {'runtime-bridge-github:opaque': b'fingerprint'})
        self.assertEqual(migrate_claims(redis, apply=True), 0)

    def test_conflict_preserves_source(self):
        redis = Redis()
        redis.values['runtime-bridge-github:opaque'] = b'other'
        with self.assertRaises(RuntimeError):
            migrate_claims(redis, apply=True)
        self.assertIn('runtime-bridge-control:opaque', redis.values)

    def test_local_journal_moves_with_encryption_key_and_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory) / 'Library/Application Support/Renata/google-services/account'
            old.mkdir(parents=True)
            (old / 'journal.key').write_bytes(b'private-key')
            (old / 'journal.sqlite3').write_bytes(b'encrypted-data')
            self.assertEqual(migrate_local_journal(directory), 'would_move')
            self.assertEqual(migrate_local_journal(directory, apply=True), 'moved')
            new = Path(directory) / 'Library/Application Support/AgentSkillRuntimeBridge/google-services/account'
            self.assertEqual((new / 'journal.sqlite3').read_bytes(), b'encrypted-data')
            self.assertEqual((new / 'journal.key').read_bytes(), b'private-key')

    def test_configuration_keeps_credentials_and_moves_only_semantics(self):
        env = {'BRIDGE_GITHUB_CONTROL': json.dumps({'trusted_user_id': '123', 'repositories': {'owner/programs': {}}}),
               'BRIDGE_REPOSITORIES': json.dumps({'owner/programs': {'ref': 'main', 'program_prefixes': ['programs'], 'authoring': {}}}),
               'BRIDGE_GOOGLE_CREDENTIALS': 'host-secret', 'BRIDGE_GMAIL_CREDENTIALS': 'old',
               'BRIDGE_OAUTH_ENCRYPTION_KEY': 'keep-key'}
        updated, old = configuration(env)
        self.assertNotIn('BRIDGE_GITHUB_CONTROL', updated)
        self.assertNotIn('BRIDGE_GMAIL_CREDENTIALS', updated)
        self.assertEqual(updated['BRIDGE_GOOGLE_CREDENTIALS'], 'host-secret')
        self.assertEqual(updated['BRIDGE_OAUTH_ENCRYPTION_KEY'], 'keep-key')
        self.assertNotIn('authoring', json.loads(updated['BRIDGE_REPOSITORIES'])['owner/programs'])

    def test_merge_preserves_existing_new_and_old_encrypted_receipts_and_claims(self):
        from bridge.google_journal import LocalGoogleJournal
        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory) / 'Library/Application Support/Renata/google-services/account'
            new = Path(directory) / 'Library/Application Support/AgentSkillRuntimeBridge/google-services/account'
            a, b = LocalGoogleJournal(old), LocalGoogleJournal(new)
            a.put('old', {'receipt': 'old'}); a.claim('old-claim')
            b.put('new', {'receipt': 'new'}); b.claim('new-claim')
            self.assertEqual(migrate_local_journal(directory, apply=True), 'merged')
            self.assertFalse(old.exists())
            self.assertEqual(b.get('old'), {'receipt': 'old'})
            self.assertEqual(b.get('new'), {'receipt': 'new'})
            self.assertFalse(b.claim('old-claim'))
            self.assertFalse(b.claim('new-claim'))
