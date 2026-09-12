"""One-time operator migration from 0.8; never imported or deployed as runtime.

Input/output JSON may contain secrets and must remain in private host files.
Run --apply once before promotion and once afterward to catch in-flight claims.
No Issue bodies or external messages are changed by this migration.
"""
import argparse
import json
from pathlib import Path
import shutil
import sqlite3


def configuration(env):
    updated = dict(env)
    old = json.loads(updated.pop('BRIDGE_GITHUB_CONTROL'))
    updated['BRIDGE_GITHUB_API'] = json.dumps({
        'credential_user_id': old['trusted_user_id'], 'repositories': {
            repo: {'permissions': ['read', 'issues_write'], 'private_only': True}
            for repo in old['repositories']}})
    repositories = json.loads(updated['BRIDGE_REPOSITORIES'])
    for repo, grant in repositories.items():
        grant.pop('repo_files', None)
        grant.pop('authoring', None)
        if repo == 'project-renata/project-renata':
            # One canonical trust boundary replaces individual capability paths.
            grant['program_prefixes'] = ['memory', 'runtime-workspace/programs']
    updated['BRIDGE_REPOSITORIES'] = json.dumps(repositories)
    if not updated.get('BRIDGE_GOOGLE_CREDENTIALS') and updated.get('BRIDGE_GMAIL_CREDENTIALS'):
        updated['BRIDGE_GOOGLE_CREDENTIALS'] = updated['BRIDGE_GMAIL_CREDENTIALS']
    updated.pop('BRIDGE_GMAIL_CREDENTIALS', None)
    return updated, old


def migrate_claims(redis, *, apply=False):
    count = 0
    for raw in redis.scan_iter(match='runtime-bridge-control:*', count=100):
        if count >= 10000:
            raise RuntimeError('claim migration limit exceeded')
        key = raw.decode() if isinstance(raw, bytes) else raw
        value = redis.get(key)
        if value is None:
            continue
        target = key.replace('runtime-bridge-control:', 'runtime-bridge-github:', 1)
        if apply:
            redis.set(target, value, nx=True)
            if redis.get(target) != value:
                raise RuntimeError('claim conflict; source retained')
            # Compare-and-delete so a changed source is never lost.
            redis.eval("if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('DEL',KEYS[1]) else return 0 end", 1, key, value)
        count += 1
    return count


def migrate_local_journal(home, *, apply=False):
    old = Path(home) / 'Library/Application Support/Renata/google-services'
    new = Path(home) / 'Library/Application Support/AgentSkillRuntimeBridge/google-services'
    if not old.exists():
        return 'already_migrated_or_absent'
    if old.is_symlink() or new.is_symlink():
        raise RuntimeError('journal path conflict; no files moved')
    if new.exists():
        # The new host may already have written receipts. Merge, preserving both
        # keys' decrypted values and every permanent claim; never reset a journal.
        from cryptography.fernet import Fernet
        for source in old.iterdir():
            if not source.is_dir() or source.is_symlink():
                raise RuntimeError('unexpected source journal entry')
            target = new / source.name
            if not target.exists():
                if apply:
                    source.rename(target)
                continue
            if target.is_symlink():
                raise RuntimeError('unexpected target journal entry')
            source_cipher = Fernet((source / 'journal.key').read_bytes())
            target_cipher = Fernet((target / 'journal.key').read_bytes())
            with sqlite3.connect(source / 'journal.sqlite3') as before, sqlite3.connect(target / 'journal.sqlite3') as after:
                records = before.execute('SELECT key,value FROM records').fetchall()
                claims = before.execute('SELECT key FROM claims').fetchall()
                for key, raw in records:
                    value = source_cipher.decrypt(raw)
                    existing = after.execute('SELECT value FROM records WHERE key=?', (key,)).fetchone()
                    if existing and json.loads(target_cipher.decrypt(existing[0])) != json.loads(value):
                        raise RuntimeError('journal record conflict; source retained')
                    if apply and not existing:
                        after.execute('INSERT INTO records VALUES (?,?)', (key, target_cipher.encrypt(value)))
                if apply:
                    after.executemany('INSERT OR IGNORE INTO claims VALUES (?)', claims)
            if apply:
                # Transaction committed; verify every plaintext value and claim
                # before removing the old encrypted copy.
                with sqlite3.connect(target / 'journal.sqlite3') as verified:
                    for key, raw in records:
                        actual = verified.execute('SELECT value FROM records WHERE key=?', (key,)).fetchone()
                        if json.loads(target_cipher.decrypt(actual[0])) != json.loads(source_cipher.decrypt(raw)):
                            raise RuntimeError('journal verification failed; source retained')
                    if any(not verified.execute('SELECT 1 FROM claims WHERE key=?', row).fetchone() for row in claims):
                        raise RuntimeError('claim verification failed; source retained')
                shutil.rmtree(source)
        if apply:
            old.rmdir()
        return 'merged' if apply else 'would_merge'
    if apply:
        new.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        old.rename(new)
    return 'moved' if apply else 'would_move'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--environment-json', type=Path, required=True)
    parser.add_argument('--output-json', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    env = json.loads(args.environment_json.read_text())
    updated, _ = configuration(env)
    args.output_json.touch(mode=0o600, exist_ok=True)
    args.output_json.chmod(0o600)
    args.output_json.write_text(json.dumps(updated))
    from redis import Redis
    url = env.get('BRIDGE_OAUTH_REDIS_URL') or env['REDIS_URL']
    if url.startswith('redis://'):
        url = 'rediss://' + url[len('redis://'):]
    redis = Redis.from_url(url, socket_timeout=10, socket_connect_timeout=10)
    count = migrate_claims(redis, apply=args.apply)
    journal = migrate_local_journal(Path.home(), apply=args.apply)
    print(json.dumps({'applied': args.apply, 'claims': count, 'local_journal': journal,
                      'environment_output': str(args.output_json)}))
