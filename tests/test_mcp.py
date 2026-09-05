import json
import unittest
from unittest.mock import AsyncMock, patch

from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.testclient import TestClient

from bridge.mcp_server import OwnerGitHubProvider, create_server, production_app, NoStoreMiddleware
from bridge.execution import execute_inline
from test_bridge import FakeGitHub, request, settings
from test_writes import WritableGitHub

AUTH = 'mcp-test-token'
HEADERS = {'Authorization': 'Bearer ' + AUTH, 'Accept': 'application/json, text/event-stream',
           'MCP-Protocol-Version': '2025-11-25'}


def app(fake=None, writes=False):
    fake = fake or FakeGitHub()
    cfg = settings()
    if writes:
        cfg.repositories['owner/private'].update(write_refs=['main'], write_prefixes=['docs'])
    server = create_server(cfg, StaticTokenVerifier(tokens={AUTH: {'client_id': 'test', 'scopes': []}}),
                           fetch=fake.fetch, send=getattr(fake, 'send', None), execute=execute_inline)
    app = server.http_app(path='/mcp', stateless_http=True, json_response=True, host_origin_protection=True,
                          allowed_hosts=['testserver'], allowed_origins=['https://chatgpt.com'])
    app.add_middleware(NoStoreMiddleware)
    return app


def rpc(client, method, params=None, headers=None):
    return client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}},
                       headers=HEADERS if headers is None else headers)


class MCPTests(unittest.TestCase):
    def test_initialization_tools_annotations_and_read(self):
        with TestClient(app()) as client:
            r=rpc(client, 'initialize', {'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'test','version':'1'}})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.headers['Cache-Control'],'no-store')
            self.assertIn('instructions',r.json()['result'])
            tools=rpc(client,'tools/list').json()['result']['tools']
            self.assertEqual(len(tools),3)
            self.assertEqual({t['name']:t['annotations']['readOnlyHint'] for t in tools},
                {'list_runtime_targets':True,'run_readonly_skill':True,'run_write_skill':False})
            r=rpc(client,'tools/call',{'name':'run_readonly_skill','arguments':json.loads(request())}).json()['result']
            self.assertFalse(r.get('isError'));self.assertEqual(r['structuredContent']['result']['count'],1)

    def test_auth_before_tool_and_repository_network(self):
        fake=FakeGitHub()
        with TestClient(app(fake)) as client:
            r=rpc(client,'tools/call',{'name':'run_readonly_skill','arguments':json.loads(request())},headers={'Accept':'application/json, text/event-stream'})
            self.assertEqual(r.status_code,401);self.assertEqual(fake.calls,[])

    def test_origin_rejected(self):
        with TestClient(app()) as client:
            r=rpc(client,'tools/list',headers={**HEADERS,'Origin':'https://attacker.example'})
            self.assertEqual(r.status_code,403)

    def test_read_tool_cannot_smuggle_write(self):
        fake=FakeGitHub()
        with TestClient(app(fake)) as client:
            args=json.loads(request());args['write']={'expected_commit':'a'*40,'message':'not allowed'}
            result=rpc(client,'tools/call',{'name':'run_readonly_skill','arguments':args}).json()['result']
            self.assertTrue(result['isError']);self.assertEqual(fake.calls,[])

    def test_policy_failure_is_tool_error(self):
        with TestClient(app()) as client:
            args=json.loads(request());args['ref']='other'
            result=rpc(client,'tools/call',{'name':'run_readonly_skill','arguments':args}).json()['result']
            self.assertTrue(result['isError']);self.assertIn('ref_not_allowed',result['content'][0]['text'])

    def test_write_and_replay_conflict(self):
        fake=WritableGitHub()
        with TestClient(app(fake,writes=True)) as client:
            args=json.loads(request());args.update(input={},write={'expected_commit':'a'*40,'message':'MCP test'})
            result=rpc(client,'tools/call',{'name':'run_write_skill','arguments':args}).json()['result']
            self.assertFalse(result.get('isError'),result)
            self.assertEqual(len(result['structuredContent']['write']['changed']),2)
            fake.responses['/git/ref/heads/main']={'object':{'sha':'f'*40}}
            replay=rpc(client,'tools/call',{'name':'run_write_skill','arguments':args}).json()['result']
            self.assertTrue(replay['isError']);self.assertIn('branch_conflict',replay['content'][0]['text'])

    def test_missing_oauth_configuration_stays_closed(self):
        with TestClient(production_app({})) as client:
            for path in ['/mcp','/.well-known/oauth-protected-resource','/auth/callback']:
                r=client.get(path);self.assertEqual(r.status_code,503);self.assertEqual(r.json(),{'error':'mcp_oauth_not_configured'})

    def test_server_cannot_be_created_anonymous(self):
        with self.assertRaises(ValueError):create_server(settings(),None)


class OAuthWiringTests(unittest.TestCase):
    def test_discovery_auth_challenge_and_registration_survive_app_recreation(self):
        from cryptography.fernet import Fernet
        from key_value.aio.stores.memory import MemoryStore
        cfg=settings()
        env={'BRIDGE_API_KEY':cfg.key,'BRIDGE_REPOSITORIES':json.dumps(cfg.repositories),
             'BRIDGE_MCP_BASE_URL':'https://bridge.example',
             'BRIDGE_OAUTH_CLIENT_ID':'test-client','BRIDGE_OAUTH_CLIENT_SECRET':'test-secret',
             'BRIDGE_OAUTH_ALLOWED_USER_IDS':'["123"]','BRIDGE_OAUTH_REDIS_URL':'rediss://redis.example',
             'BRIDGE_OAUTH_SIGNING_KEY':'s'*40,'BRIDGE_OAUTH_ENCRYPTION_KEY':Fernet.generate_key().decode()}
        shared=MemoryStore()
        with patch('bridge.mcp_server.RedisStore',return_value=shared):
            first=production_app(env)
            with TestClient(first,base_url='https://bridge.example') as client:
                metadata=client.get('/.well-known/oauth-authorization-server').json()
                self.assertIn('S256',metadata['code_challenge_methods_supported'])
                self.assertEqual(metadata['issuer'],'https://bridge.example/')
                denied=client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/list'},headers={'Accept':'application/json, text/event-stream'})
                self.assertEqual(denied.status_code,401)
                self.assertIn('resource_metadata=',denied.headers['www-authenticate'])
                registered=client.post('/register',json={'client_name':'ChatGPT test','redirect_uris':['https://chatgpt.com/connector_platform_oauth_redirect'],'token_endpoint_auth_method':'none','grant_types':['authorization_code','refresh_token'],'response_types':['code']})
                self.assertEqual(registered.status_code,201,registered.text)
                client_id=registered.json()['client_id']
            second=production_app(env)
            with TestClient(second,base_url='https://bridge.example',follow_redirects=False) as client:
                result=client.get('/authorize',params={'client_id':client_id,'redirect_uri':'https://chatgpt.com/connector_platform_oauth_redirect','response_type':'code','code_challenge':'a'*43,'code_challenge_method':'S256','state':'test-state','scope':'read:user','resource':'https://bridge.example/mcp'})
                self.assertEqual(result.status_code,302,result.text)
                self.assertTrue(result.headers['location'].startswith('https://bridge.example/'),result.headers['location'])
                result=client.get(result.headers['location'])
                self.assertEqual(result.status_code,200,result.text)
                self.assertIn('text/html',result.headers['content-type'])
                self.assertEqual(result.headers['cache-control'],'no-store')


class OwnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_configured_github_id_is_accepted(self):
        # Only the owner check is under test; the library validates OAuth tokens.
        provider=object.__new__(OwnerGitHubProvider);provider.allowed_user_ids=frozenset(['123'])
        for user,expected in [('123',True),('456',False)]:
            token=AccessToken(token='upstream',client_id='client',scopes=['read:user'],claims={'sub':user})
            with patch('fastmcp.server.auth.providers.github.GitHubProvider.verify_token',AsyncMock(return_value=token)):
                self.assertEqual(await provider.verify_token('opaque') is not None,expected)
        with patch('fastmcp.server.auth.providers.github.GitHubProvider.verify_token',AsyncMock(return_value=None)):
            self.assertIsNone(await provider.verify_token('invalid'))
