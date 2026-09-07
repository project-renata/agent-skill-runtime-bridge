"""Canonical dependencies are hydrated at the code revision, without caller lists."""
import asyncio
import unittest
from unittest.mock import patch
from bridge.core import handle, canonical_dependencies, BridgeError
from bridge.execution import execute_subprocess
from test_bridge import KEY, request
from test_snapshots import SnapshotGitHub, config, HEAD, OLD

class DependencyTests(unittest.TestCase):
    def call(self,current,program,input,files=None,execute=execute_subprocess,old=None,**extra):
        current={'docs/a.md':b'fixture',**current}
        if old is not None:old={'docs/a.md':b'old',**old}
        fake=SnapshotGitHub(current=current,old=old)
        policy={'ref':'main','read_all':True,'program_prefixes':['memory/skill','program'],'write_refs':['main'],'write_all_refs':['main']}
        status,body=asyncio.run(handle(request(program=program,input=input,files=files or [],**extra),'Bearer '+KEY,config(policy),fake.fetch,execute,fake.send))
        return status,body,fake



    def test_dependencies_use_code_revision_over_explicit_old_file(self):
        p='program/main.py';dep='program/helper.py'
        program=b'CANONICAL_DEPENDENCIES=["program/helper.py"]\nfrom pathlib import Path\ndef run(root,input):\n return (Path(root)/"program/helper.py").read_text()\n'
        status,body,_=self.call({p:program,dep:b'value="new"'},p,{},old={p:program,dep:b'value="old"'},ref=OLD,program_ref='main',files=[dep])
        self.assertEqual(status,200,body);self.assertEqual(body['result'],'value="new"')
        self.assertEqual(body['source']['program_commit'],HEAD);self.assertEqual(body['source']['commit'],OLD)

    def test_missing_denied_cycles_and_limits_never_execute(self):
        p='program/main.py';helper='program/helper.py'
        cases=[({p:b'CANONICAL_DEPENDENCIES=["program/absent.py"]\n'},'dependency_missing'),({p:b'CANONICAL_DEPENDENCIES=["outside/run.py"]\n'},'dependency_not_allowed'),({p:b'CANONICAL_DEPENDENCIES=["../escape.py"]\n'},'invalid_path'),({p:b'CANONICAL_DEPENDENCIES=["program/helper.py"]\n',helper:b'CANONICAL_DEPENDENCIES=["program/main.py"]\n'},'dependency_cycle'),({p:b'CANONICAL_DEPENDENCIES=make_list()\n'},'invalid_dependency_declaration')]
        for current,code in cases:
            called=[];status,body,fake=self.call(current,p,{},execute=lambda *a:called.append(True))
            self.assertEqual(body['error']['code'],code,(status,body));self.assertEqual(called,[]);self.assertEqual(fake.writes,[])
        with patch('bridge.core.MAX_FILES',1):
            _,body,_=self.call({p:b'CANONICAL_DEPENDENCIES=["program/helper.py"]\n',helper:b'pass'},p,{})
            self.assertEqual(body['error']['code'],'too_many_snapshot_files')

    def test_literal_unique_declarations(self):
        for text in [b'CANONICAL_DEPENDENCIES=["x.py","x.py"]',b'CANONICAL_DEPENDENCIES=[1]',b'CANONICAL_DEPENDENCIES="x.py"']:
            with self.assertRaises(BridgeError):canonical_dependencies(text)
        self.assertEqual(canonical_dependencies(b'def run(root,input): return {}'),[])



class TransitiveClosureTests(unittest.TestCase):
    call = DependencyTests.call

    def test_real_transitive_python_and_support_file_code_overlay(self):
        p, child, leaf, support = 'program/main.py', 'program/child.py', 'program/leaf.py', 'support/profiles.json'
        program = b'''CANONICAL_DEPENDENCIES=["program/child.py"]
import importlib.util
from pathlib import Path
def run(root, input):
    p = Path(root) / "program/child.py"
    spec = importlib.util.spec_from_file_location("child", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.run(root, input)
'''
        child_code = program.replace(b'program/child.py', b'program/leaf.py').replace(b'"child"', b'"leaf"')
        leaf_code = b'''CANONICAL_DEPENDENCIES=["support/profiles.json"]
import json
from pathlib import Path
def run(root, input):
    return {"profile": json.loads((Path(root) / "support/profiles.json").read_text()),
            "data": (Path(root) / "docs/a.md").read_text()}
'''
        current = {p: program, child: child_code, leaf: leaf_code, support: b'{"version":"new"}'}
        old = {**current, support: b'{"version":"old"}'}
        status, body, fake = self.call(current, p, {}, files=['docs/a.md', support], old=old, ref=OLD, program_ref='main')
        self.assertEqual(status, 200, body)
        self.assertEqual(body['result'], {'profile': {'version': 'new'}, 'data': 'old'})
        self.assertEqual(set(body['source']['snapshot']['canonical_dependencies']), {child, leaf, support})
        self.assertEqual(fake.writes, [])

    def test_support_files_retain_read_policy(self):
        from bridge.core import Settings
        p = 'program/main.py'
        fake = SnapshotGitHub(current={p: b'CANONICAL_DEPENDENCIES=["private/config.json"]',
                                     'docs/a.md': b'data', 'private/config.json': b'{}'})
        policy = {'ref': 'main', 'program_prefixes': ['program'], 'data_prefixes': ['docs']}
        calls = []
        status, body = asyncio.run(handle(request(program=p, files=[], input={}), 'Bearer ' + KEY,
            Settings(KEY, {'owner/private': policy}, 'private-credential'), fake.fetch,
            lambda *args: calls.append(True)))
        self.assertEqual(body['error']['code'], 'dependency_not_allowed')
        self.assertEqual(calls, [])

    def test_dependency_symlinks_submodules_and_size_limits_fail_closed(self):
        p, helper = 'program/main.py', 'program/helper.py'
        source = {p: b'CANONICAL_DEPENDENCIES=["program/helper.py"]', helper: b'pass', 'docs/a.md': b'data'}
        for mode, kind, size in [('120000', 'blob', 4), ('160000', 'commit', 4), ('100644', 'blob', 524289)]:
            fake = SnapshotGitHub(current=source)
            for value in fake.responses.values():
                for entry in value.get('tree', []) if isinstance(value.get('tree'), list) else []:
                    if entry['path'] == 'helper.py':
                        entry.update(mode=mode, type=kind, size=size)
            calls = []
            policy = {'ref': 'main', 'read_all': True, 'program_prefixes': ['program']}
            status, body = asyncio.run(handle(request(program=p, files=[], input={}), 'Bearer ' + KEY,
                config(policy), fake.fetch, lambda *args: calls.append(True)))
            self.assertIn(body['error']['code'], ('unsupported_repository_entry', 'file_too_large'))
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
