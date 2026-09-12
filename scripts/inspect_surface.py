"""Export the fully enabled MCP contract without external network or credentials."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.testclient import TestClient
from bridge import __version__
from bridge.core import Settings
from bridge.github_service import GitHubPolicy, GitHubService
from bridge.gmail import GmailTransport
from bridge.google_journal import MemoryGoogleJournal
from bridge.google_services import GoogleServices
from bridge.mcp_server import create_server


def inspect_surface():
    settings = Settings('x' * 32, {'example/programs': {
        'ref': 'main', 'program_prefixes': ['programs'], 'read_all': True}}, 'inspection-only')
    policy = GitHubPolicy({'credential_user_id': '123', 'repositories': {
        'example/programs': {'permissions': ['read', 'issues_write'], 'private_only': True}}}, settings)
    github = GitHubService(settings, policy, None, fetch=None, send=None)
    mail = GmailTransport('inspection', 'unused', 'unused', 'example@example.com')
    google = GoogleServices(mail, MemoryGoogleJournal())
    server = create_server(settings, StaticTokenVerifier(tokens={
        'inspection-only': {'client_id': 'inspection', 'scopes': []}}), github=github, gmail=mail, google=google)
    with TestClient(server.http_app(path='/mcp', stateless_http=True, json_response=True)) as client:
        def rpc(method, params=None):
            response = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}},
                headers={'Authorization': 'Bearer inspection-only', 'Accept': 'application/json, text/event-stream'})
            response.raise_for_status()
            return response.json()['result']
        tools = rpc('tools/list')['tools']
        metadata = rpc('tools/call', {'name': 'list_runtime_targets', 'arguments': {}})['structuredContent']
    return {'version': __version__, 'tool_count': len(tools), 'tools': tools, 'runtime_metadata': metadata}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = inspect_surface()
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'version': result['version'], 'tool_count': result['tool_count'], 'output': str(args.output)}))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
