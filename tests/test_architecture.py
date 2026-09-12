"""The supported surface is infrastructure, independent of application workflows."""
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.testclient import TestClient

from bridge.execution import execute_subprocess
from bridge.gmail import GmailTransport
from bridge.google_journal import MemoryGoogleJournal
from bridge.google_services import GoogleServices
from bridge.mcp_server import create_server
from test_github_service import fixture
from test_mcp import AUTH, rpc

ROOT = Path(__file__).resolve().parents[1]
TOOLS = {
    'list_runtime_targets', 'run_readonly_skill', 'run_write_skill',
    'create_github_issue', 'read_github_issue', 'add_github_issue_label',
    'add_github_issue_comment', 'read_github_issue_comments', 'read_github_pr',
    'read_github_pr_review', 'list_github_issues', 'list_github_prs',
    'gmail_get_profile', 'gmail_list_labels', 'gmail_search_messages', 'gmail_read_messages',
    'google_services_catalog', 'google_services_read', 'google_services_prepare',
    'google_services_execute', 'google_mail_compose', 'google_read_document',
    'query_repository', 'evaluate_repository_candidate', 'commit_repository_candidate',
}
FORBIDDEN = re.compile(
    r'dispatch_local_agent|accept_local_agent_result|local[ _-](?:runner|codex|coding)|'
    r'LOCAL_AGENT_DISPATCH|dispatch_payload|google_workflow_prepare|'
    r'followup_(?:put|close)|registration_(?:track|confirm)|mail_to_calendar|'
    r'\bRenata\b|\bRecall\b|\bRemember\b|\bDream\b|Current[ _]Self|Sync workflow|'
    r'central_repository|trusted_login|required_sources|ControlPlane|BRIDGE_GITHUB_CONTROL|'
    r'repo_files_usage|authoring_usage|requires_local_agent|should_dispatch|fallback_to_codex|'
    r'local_escalation|executor_policy|Attention Budget|Story Writer', re.I)


class ArchitectureTests(unittest.TestCase):
    def full_server(self):
        cfg, fake, github = fixture()
        mail = GmailTransport('client', 'secret', 'refresh', 'owner@example.com')
        google = GoogleServices(mail, MemoryGoogleJournal())
        return create_server(cfg, StaticTokenVerifier(tokens={AUTH: {'client_id': 'test', 'scopes': []}}),
                             github=github, gmail=mail, google=google), fake

    def test_exact_public_surface_descriptions_schemas_and_instructions(self):
        server, fake = self.full_server()
        with TestClient(server.http_app(path='/mcp', stateless_http=True, json_response=True)) as client:
            tools = rpc(client, 'tools/list').json()['result']['tools']
            self.assertEqual({t['name'] for t in tools}, TOOLS)
            self.assertIsNone(FORBIDDEN.search(json.dumps(tools)))
            initialized = rpc(client, 'initialize', {'protocolVersion': '2025-11-25',
                'capabilities': {}, 'clientInfo': {'name': 'boundary-test', 'version': '1'}}).json()['result']
            self.assertIsNone(FORBIDDEN.search(initialized['instructions']))
            metadata = rpc(client, 'tools/call', {'name': 'list_runtime_targets', 'arguments': {}}).json()['result']['structuredContent']
            self.assertIsNone(FORBIDDEN.search(json.dumps(metadata)))
            self.assertEqual(set(metadata), {'runtime_version', 'repositories', 'github_transport',
                'github_api', 'gmail_transport', 'google_services', 'snapshot_usage',
                'repository_operations', 'runtime_source_commit', 'public_tools'})
            self.assertEqual(set(metadata['public_tools']), TOOLS)
            self.assertEqual(fake.calls, [])
            for name in ('dispatch_local_agent', 'accept_local_agent_result', 'google_workflow_prepare'):
                result = rpc(client, 'tools/call', {'name': name, 'arguments': {}}).json()
                self.assertTrue(result.get('error') or result.get('result', {}).get('isError'), result)

    def test_no_application_symbols_in_shipped_runtime_or_configuration(self):
        files = [p for folder in ('bridge', 'api', 'cloudflare') for p in (ROOT / folder).rglob('*.py')]
        files += [ROOT / p for p in ('worker.py', 'pyproject.toml', 'wrangler.jsonc', 'vercel.json')]
        for path in files:
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertIsNone(FORBIDDEN.search(path.read_text()), str(path))

    def test_deployment_keeps_secret_and_developer_environment_exclusions(self):
        ignored = set((ROOT / '.vercelignore').read_text().splitlines())
        self.assertTrue({'.env*', '.dev.vars*', '.venv', '.venv-workers', 'node_modules',
                         'migrations', 'tests', '.git'} <= ignored)

    def test_cloudflare_stage_contains_only_current_portable_runtime(self):
        import subprocess
        import sys
        output = ROOT / '.cloudflare-build'
        output.mkdir(exist_ok=True)
        stale = output / 'stale_module.py'
        stale.write_text('retired = True')
        subprocess.run([sys.executable, 'cloudflare/build.py'], cwd=ROOT, check=True)
        self.assertEqual({str(p.relative_to(output)) for p in output.rglob('*') if p.is_file()},
                         {'worker.py', 'bridge/__init__.py', 'bridge/core.py', 'bridge/execution.py'})

    def test_all_host_credentials_absent_from_canonical_child(self):
        names = ('BRIDGE_GITHUB_TOKEN', 'GH_TOKEN', 'BRIDGE_GOOGLE_CREDENTIALS',
                 'BRIDGE_GMAIL_CREDENTIALS', 'BRIDGE_GOOGLE_DOCUMENT_SECRETS',
                 'BRIDGE_OAUTH_ENCRYPTION_KEY', 'REDIS_URL')
        program = ('import os\ndef run(root, value):\n'
                   ' return {k:os.environ.get(k) for k in value["names"]}\n').encode()
        with patch.dict('os.environ', {name: 'host-secret' for name in names}):
            result = execute_subprocess({'program.py': program}, 'program.py', {'names': names})
        self.assertEqual(result.result, dict.fromkeys(names))

    def test_new_canonical_program_and_dependency_need_no_server_change(self):
        # Two unrelated callers can evolve their semantics while this interpreter
        # and its transport contract stay fixed. Dependency closure itself is
        # exercised through immutable Git snapshots in test_dependencies.py.
        first = b'def run(root, value):\n return {"result": value["x"] + 1}\n'
        second = b'def run(root, value):\n return {"result": value["x"] * 7}\n'
        for source, expected in ((first, 4), (second, 21)):
            result = execute_subprocess({'new/path/program.py': source}, 'new/path/program.py', {'x': 3})
            self.assertEqual(result.result, {'result': expected})
