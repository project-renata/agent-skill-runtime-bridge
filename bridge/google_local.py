"""Local CLI: gog/Keychain credentials feed the same GoogleServices implementation."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from .core import BridgeError
from .gmail import GmailTransport
from .google_services import GoogleServices
from .google_journal import LocalGoogleJournal
from .google_documents import read_document


def local_services(account):
    config = Path.home() / 'Library/Application Support/gogcli'
    try:
        client = json.loads((config / 'credentials.json').read_text())
        secret = subprocess.run(['security', 'find-generic-password', '-s', 'gogcli',
            '-a', 'client/default/client-secret', '-w'], capture_output=True, text=True,
            check=True, timeout=15).stdout.strip()
        with tempfile.TemporaryDirectory(prefix='bridge-google-auth-') as directory:
            path = Path(directory) / 'token.json'
            subprocess.run(['gog', 'auth', 'tokens', 'export', account, '--out', str(path)],
                           capture_output=True, check=True, timeout=30)
            token = json.loads(path.read_text())
        if token.get('email', '').lower() != account.lower() or token.get('client') != 'default':
            raise ValueError()
        gmail = GmailTransport(client['client_id'], secret, token['refresh_token'], account)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        raise BridgeError('google_local_gog_credentials_unavailable', 401) from None
    directory = Path.home() / 'Library/Application Support/AgentSkillRuntimeBridge/google-services' / hashlib.sha256(account.encode()).hexdigest()[:16]
    return GoogleServices(gmail, LocalGoogleJournal(directory))


async def execute_request(services, request):
    args = dict(request)
    action = args.pop('action')
    if action == 'catalog':
        return services.catalog.describe(**args)
    if action == 'read':
        return await services.read(**args)
    if action == 'prepare':
        return await services.prepare(**args)
    if action == 'execute':
        return await services.execute(**args)
    if action == 'compose':
        return await asyncio.to_thread(services.compose, **args)
    if action == 'document':
        # Local secrets can be referenced by macOS Keychain service/account; the
        # canonical caller passes only the reference and expected source SHA.
        reference = args.pop('keychain_reference', None)
        secrets = None
        if reference:
            if set(reference) != {'service', 'account'} or not args.get('source', {}).get('sha256'):
                raise BridgeError('google_invalid_keychain_reference', 400)
            try:
                password = subprocess.run(['security', 'find-generic-password', '-s', reference['service'],
                    '-a', reference['account'], '-w'], capture_output=True, text=True, check=True, timeout=15).stdout.rstrip('\n')
            except subprocess.SubprocessError:
                raise BridgeError('google_document_keychain_secret_unavailable', 403) from None
            secrets = {'local': {'sha256': args['source']['sha256'], 'password': password}}
            args['secret_ref'] = 'local'
        return await asyncio.to_thread(read_document, services, secrets=secrets, **args)
    raise BridgeError('google_unknown_action', 400)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--account', required=True)
    args = parser.parse_args()
    try:
        request = json.load(sys.stdin)
        result = asyncio.run(execute_request(local_services(args.account), request))
        print(json.dumps(result, ensure_ascii=False))
    except BridgeError as error:
        print(json.dumps({'ok': False, 'error': error.code}))
        raise SystemExit(1)
    except (ValueError, TypeError, KeyError):
        print(json.dumps({'ok': False, 'error': 'google_invalid_local_request'}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
