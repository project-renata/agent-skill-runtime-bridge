"""Retired grants cannot pass deployment or regain writes through broad grants."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from bridge.core import BridgeError, Settings, parse_request, writable
from test_bridge import request
from test_repository import fixture, REPO

ROOT = Path(__file__).resolve().parents[1]

def module(path):
    spec = importlib.util.spec_from_file_location(Path(path).stem, ROOT/path)
    value = importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value

retirement = module('maintenance/check_deployment.py')


def legacy():
    return {retirement.REPOSITORY: {'ref':'main','read_all':True,
        'program_prefixes':['memory','runtime-workspace/programs'],
        'additional_refs':sorted(retirement.RETIRED_REFS),
        'write_refs':['main',*sorted(retirement.RETIRED_REFS)], 'write_all_refs':['main'],
        'write_prefixes_by_ref':{r:['memory/story/projects/Agent Skill Runtime Bridge/probe-data',
            'runtime-workspace/programs','runtime-workspace/data'] for r in retirement.RETIRED_REFS}}}


class DeploymentPolicyTests(unittest.TestCase):
    def test_retirement_is_idempotent_and_preserves_unrelated_grants(self):
        old=legacy();old['owner/other']={'ref':'trunk','program_prefixes':['src'],'read_all':True}
        before=deepcopy(old);updated=retirement.retire(old)
        self.assertEqual(old,before)
        self.assertEqual(updated['owner/other'],old['owner/other'])
        self.assertEqual(retirement.retire(updated),updated)
        retirement.validate(updated)
        policy=updated[retirement.REPOSITORY]
        Settings('k'*40,updated,'host-secret')
        self.assertEqual(policy['program_prefixes'],['memory'])
        self.assertEqual(policy['write_refs'],['main'])
        self.assertNotIn('additional_refs',policy)
        self.assertNotIn('write_prefixes_by_ref',policy)
        self.assertTrue(writable('memory/example.md',policy,'main'))
        for path in ('runtime-workspace','runtime-workspace/programs/new.py','runtime-workspace/data/new.json'):
            self.assertFalse(writable(path,policy,'main'))

    def test_restored_legacy_grants_or_removed_denial_fail_build_check(self):
        clean=retirement.retire(legacy())
        cases=[legacy()]
        for field in retirement.PATH_FIELDS:
            value=deepcopy(clean);value[retirement.REPOSITORY][field]=['runtime-workspace/programs'];cases.append(value)
        for field in retirement.REF_FIELDS:
            value=deepcopy(clean);value[retirement.REPOSITORY].setdefault(field,[]).append('runtime-bridge/web-workspace');cases.append(value)
        for field,value in [('write_denied_paths',[]),('authoring',{}),('repo_files',{}),
                            ('write_prefixes_by_ref',{'runtime-bridge/validation-20260905':['memory']})]:
            item=deepcopy(clean);item[retirement.REPOSITORY][field]=value;cases.append(item)
        for value in cases:
            with self.assertRaises(ValueError):retirement.validate(value)

    def test_old_migration_also_retires_all_grants(self):
        migration=module('migrations/v0_9_0.py')
        value,_=migration.configuration({'BRIDGE_REPOSITORIES':json.dumps(legacy()),
            'BRIDGE_GITHUB_CONTROL':json.dumps({'trusted_user_id':'1','repositories':[retirement.REPOSITORY]})})
        retirement.validate(json.loads(value['BRIDGE_REPOSITORIES']))

    def test_cli_rejects_stale_or_missing_config_without_printing_secrets(self):
        for config,success in [(legacy(),False),(retirement.retire(legacy()),True),(None,False)]:
            env={'PATH':os.environ['PATH'],'BRIDGE_API_KEY':'do-not-print-secret'}
            if config is not None:env['BRIDGE_REPOSITORIES']=json.dumps(config)
            result=subprocess.run([sys.executable,'maintenance/check_deployment.py'],cwd=ROOT,
                env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode==0,success,result.stderr)
            self.assertNotIn('do-not-print-secret',result.stdout+result.stderr)
            self.assertNotIn('program_prefixes',result.stdout+result.stderr)
        vercel=json.loads((ROOT/'vercel.json').read_text())
        self.assertEqual(vercel['buildCommand'],'python3 maintenance/check_deployment.py')
        self.assertIn('!maintenance/check_deployment.py',(ROOT/'.vercelignore').read_text())

    def test_retired_refs_and_programs_reject_before_execution(self):
        settings=Settings('k'*40,retirement.retire(legacy()),'host-secret')
        for ref in retirement.RETIRED_REFS:
            with self.assertRaisesRegex(BridgeError,'ref_not_allowed'):
                parse_request(request(repository=retirement.REPOSITORY,ref=ref,program='memory/probe.py'),
                              'Bearer '+settings.key,settings)
        with self.assertRaisesRegex(BridgeError,'program_not_allowed'):
            parse_request(request(repository=retirement.REPOSITORY,program='runtime-workspace/programs/new.py'),
                          'Bearer '+settings.key,settings)


class StatelessSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_helper_never_creates_remote_programs_or_commits(self):
        git,settings,service,_=fixture()
        git.head=git.add_commit({**git.files,'src/calc.py':b'def add(a, b):\n    return a + b\n'},[git.head],'working baseline')
        before=git.head
        async def call(name,**args):
            if name=='query_repository':return await service.query(args['repository'],args['ref'],args['query'])
            self.assertEqual(name,'evaluate_repository_candidate')
            return await service.evaluate(args['repository'],args['ref'],args['candidate'],args['operation'],
                                          args.get('manifest'),args.get('profile'))
        result=await module('scripts/check_repository_live.py').check(call,REPO,'main','src/calc.py','validation.json','tests')
        self.assertTrue(result['repository_unchanged'])
        self.assertEqual(git.head,before)
        self.assertEqual(git.writes,[])
