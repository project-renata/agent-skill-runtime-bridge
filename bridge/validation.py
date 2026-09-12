"""Bounded declarative Python validation in disposable, network-denied microVMs."""
import asyncio
import json
from pathlib import Path

from .core import BridgeError, safe_path

IMAGE = 'vercel/sandbox/universal@sha256:0e3e3617e824397f170fc7c43ccaa565dd7ac36518e83ead3d41e077cd9f6ec7'
MAX_SECONDS = 30
MAX_OUTPUT = 65536
MAX_COMMANDS = 4


def validation_profile(manifest, name):
    from .repository import fields, integer
    fields(manifest, ['version', 'profiles'])
    if manifest['version'] != 1 or not isinstance(manifest['profiles'], dict) or name not in manifest['profiles']:
        raise BridgeError('validation_profile_not_found')
    profile = manifest['profiles'][name]
    fields(profile, ['files', 'commands'], ['timeout_seconds', 'output_bytes'])
    files = profile['files']
    if (not isinstance(files, list) or not 1 <= len(files) <= 32
            or any(not isinstance(p, str) for p in files) or len(set(files)) != len(files)):
        raise BridgeError('invalid_validation_selectors')
    for path in files:
        if path != '':
            safe_path(path[:-1] if path.endswith('/') else path)
    commands = profile['commands']
    if not isinstance(commands, list) or not 1 <= len(commands) <= MAX_COMMANDS:
        raise BridgeError('invalid_validation_commands')
    for command in commands:
        fields(command, ['executable', 'argv'], ['cwd'])
        if command['executable'] != 'python':
            raise BridgeError('forbidden_executable', 403)
        argv = command['argv']
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 64
                or any(not isinstance(a, str) or len(a) > 1024 or '\0' in a or '\n' in a for a in argv)):
            raise BridgeError('structured_argv_required')
        if argv[0] == '-m':
            import re
            if len(argv) < 2 or not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', argv[1]):
                raise BridgeError('invalid_python_module')
        elif not argv[0].endswith('.py') or argv[0].startswith('-'):
            raise BridgeError('python_file_or_module_required')
        else:
            safe_path(argv[0])
        for arg in argv:
            value = arg.split('=', 1)[-1]
            if value.startswith('/') or '\\' in value or '..' in value.split('/'):
                raise BridgeError('argument_path_escape', 403)
        cwd = command.get('cwd', '')
        if cwd:
            safe_path(cwd)
    return {**profile,
        'timeout_seconds': integer(profile.get('timeout_seconds', 10), 1, MAX_SECONDS),
        'output_bytes': integer(profile.get('output_bytes', 16384), 1024, MAX_OUTPUT)}


async def execute_validation(files, profile):
    """No local fallback. Unsupported authentication/isolation fails closed."""
    from vercel import sandbox
    from vercel.api import session
    from vercel.sandbox import NetworkPolicy, SandboxResources

    for command in profile['commands']:
        cwd = command.get('cwd', '')
        if cwd and not any(path.startswith(cwd + '/') for path in files):
            raise BridgeError('invalid_validation_cwd')
        if command['argv'][0] != '-m':
            script = '/'.join(p for p in (cwd, command['argv'][0]) if p)
            if script not in files:
                raise BridgeError('validation_program_missing')
    try:
        async with asyncio.timeout(150):
            # An SDK session owns the HTTP client for this request/event loop.
            async with session():
                async with sandbox.create_sandbox(image=IMAGE, persistent=False, ports=[], env={},
                        execution_time_limit=120, resources=SandboxResources(vcpus=2, memory=4096),
                        network_policy=NetworkPolicy.deny_all()) as box:
                    if str(box.image) != IMAGE:
                        raise BridgeError('validation_image_mismatch', 503)
                    async with box.fs.batch() as batch:
                        batch.write_bytes('.bridge/runner.py', Path(__file__).with_name('validation_runner.py').read_bytes())
                        batch.write_text('.bridge/request.json', json.dumps(profile))
                        for path, content in files.items():
                            batch.write_bytes('repository/' + safe_path(path), content)
                    # Only this packaged supervisor uses sudo, to create a chroot
                    # and drop to an unprivileged uid before repository execution.
                    process = await box.run_process('python3', ['-I', '.bridge/runner.py', '.bridge/request.json'],
                        sudo=True, kill_after=100, capture_output=True)
                    if process.returncode != 0 or len(process.stdout.encode()) > MAX_OUTPUT * 4:
                        raise BridgeError('validation_supervisor_failed', 502)
                    result = json.loads(process.stdout)
                    result['environment'] = {'provider': 'vercel-sandbox', 'image': str(box.image),
                        'network': 'deny-all', 'filesystem': 'readonly chroot snapshot and private temporary directory',
                        'executable': 'python', 'children': 'denied by seccomp',
                        'memory_bytes': 512 * 1024 * 1024, 'vm_vcpus': 2}
                    return result
    except BridgeError:
        raise
    except Exception:
        # Provider errors may contain account details, tokens or request bodies.
        raise BridgeError('validation_environment_unavailable', 503) from None
