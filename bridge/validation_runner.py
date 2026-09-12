"""Packaged microVM supervisor. Never invoked on the Bridge credential host.

Repository Python runs after chroot, uid/gid drop, resource limits and seccomp.
Only stdout/stderr/exit observations cross back; there are no host credentials.
"""
import ctypes
import errno
import json
import os
from pathlib import Path
import platform
import pkgutil  # Preload runpy's script loader before candidate paths enter sys.path.
import resource
import runpy
import selectors
import shutil
import signal
import sys
import sysconfig
import tempfile
import time


def restrict_syscalls():
    # chroot/uid drop provide filesystem isolation; deny launching processes,
    # networking, tracing, signals to others and privilege/kernel interfaces.
    if platform.machine() != 'x86_64':
        raise RuntimeError('unsupported sandbox architecture')
    denied = [41, 42, 43, 44, 45, 46, 47, 49, 50, 53, 56, 57, 58, 59, 62, 101,
              155, 161, 165, 166, 167, 168, 169, 172, 173, 175, 176, 200, 234,
              246, 248, 249, 250, 272, 288, 298, 303, 304, 308, 310, 311,
              313, 321, 322, 323, 425, 426, 427, 434, 435, 438, 442]
    class Filter(ctypes.Structure):
        _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                    ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint32)]
    class Program(ctypes.Structure):
        _fields_ = [('len', ctypes.c_ushort), ('filter', ctypes.POINTER(Filter))]
    rules = [(0x20, 0, 0, 4), (0x15, 1, 0, 0xC000003E), (0x06, 0, 0, 0x80000000),
             (0x20, 0, 0, 0), (0x35, 0, 1, 0x40000000), (0x06, 0, 0, 0x80000000)]
    for number in denied:
        rules.extend([(0x15, 0, 1, number), (0x06, 0, 0, 0x00050000 | errno.EPERM)])
    rules.append((0x06, 0, 0, 0x7FFF0000))
    data = (Filter * len(rules))(*(Filter(*r) for r in rules))
    program = Program(len(rules), data)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:
        raise RuntimeError('seccomp unavailable')


def child(jail, command, stdout_fd, stderr_fd, timeout):
    try:
        os.setsid()
        os.dup2(stdout_fd, 1); os.dup2(stderr_fd, 2)
        os.closerange(3, 1024)
        # Preload optional stdlib shared-library dependencies before chroot.
        import bz2, hashlib, lzma, sqlite3, ssl, zlib  # noqa: F401
        os.chroot(jail)
        os.chdir('/work' + ('/' + command.get('cwd', '') if command.get('cwd') else ''))
        os.setgroups([]); os.setgid(65534); os.setuid(65534)
        os.environ.clear()
        os.environ.update({'LANG': 'C.UTF-8', 'HOME': '/tmp', 'TMPDIR': '/tmp', 'PYTHONIOENCODING': 'utf-8'})
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 1))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024 * 1024,) * 2)
        sys.dont_write_bytecode = True
        stdlib = sysconfig.get_path('stdlib')
        sys.path = [os.getcwd(), '/work', stdlib, stdlib + '/lib-dynload']
        restrict_syscalls()
        argv = command['argv']
        if argv[0] == '-m':
            sys.argv = [argv[1], *argv[2:]]
            runpy.run_module(argv[1], run_name='__main__', alter_sys=True)
        else:
            sys.argv = argv
            runpy.run_path(argv[0], run_name='__main__')
        code = 0
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else (0 if error.code is None else 1)
    except BaseException:
        import traceback
        traceback.print_exc()
        code = 1
    try:
        sys.stdout.flush(); sys.stderr.flush()
    finally:
        os._exit(max(0, min(255, code)))


def command_result(jail, command, timeout, output_limit):
    pipes = [os.pipe(), os.pipe()]
    started = time.monotonic()
    pid = os.fork()
    if pid == 0:
        child(str(jail), command, pipes[0][1], pipes[1][1], timeout)
    output = [bytearray(), bytearray()]
    timed_out = truncated = False
    for _, write_fd in pipes:
        os.close(write_fd)
    try:
        with selectors.DefaultSelector() as selector:
            for index, (read_fd, _) in enumerate(pipes):
                selector.register(read_fd, selectors.EVENT_READ, index)
            while selector.get_map():
                left = started + timeout - time.monotonic()
                if left <= 0:
                    timed_out = True; break
                for key, _ in selector.select(min(left, .2)):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fd)
                    else:
                        remaining = output_limit - sum(map(len, output))
                        output[key.data].extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            truncated = True; break
                if truncated:
                    break
    finally:
        status = None
        # Closing both output pipes does not mean the process exited.
        while not (timed_out or truncated):
            finished, status = os.waitpid(pid, os.WNOHANG)
            if finished:
                break
            if time.monotonic() >= started + timeout:
                timed_out = True
                break
            time.sleep(.01)
        if timed_out or truncated:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _, status = os.waitpid(pid, 0)
        for read_fd, _ in pipes:
            os.close(read_fd)
    return {'command': command, 'exit_status': os.waitstatus_to_exitcode(status),
        'duration_seconds': round(time.monotonic() - started, 4), 'timed_out': timed_out,
        'stdout': output[0].decode('utf-8', errors='replace'),
        'stderr': output[1].decode('utf-8', errors='replace'),
        'truncated': truncated, 'complete': not truncated and not timed_out}


def supervise(jail, profile):
    jail.chmod(0o755)
    shutil.copytree('repository', jail / 'work')
    stdlib = Path(sysconfig.get_path('stdlib'))
    shutil.copytree(stdlib, jail / str(stdlib).lstrip('/'),
        ignore=shutil.ignore_patterns('__pycache__', 'site-packages', 'test', 'tests', 'idlelib', 'tkinter', 'ensurepip'))
    (jail / 'tmp').mkdir(mode=0o777)
    (jail / 'tmp').chmod(0o777)
    for path in (jail / 'work').rglob('*'):
        if path.is_symlink():
            raise RuntimeError('unexpected symlink')
        path.chmod(0o555 if path.is_dir() else 0o444)
    (jail / 'work').chmod(0o555)
    results = []
    started = time.monotonic()
    for command in profile['commands']:
        left = profile['timeout_seconds'] - (time.monotonic() - started)
        if left <= 0:
            break
        result = command_result(jail, command, max(1, int(left)), profile['output_bytes'])
        results.append(result)
        if result['exit_status'] != 0 or not result['complete']:
            break
    passed = len(results) == len(profile['commands']) and all(r['exit_status'] == 0 and r['complete'] for r in results)
    return {'passed': passed, 'commands': results,
        'duration_seconds': round(time.monotonic() - started, 4),
        'complete': len(results) == len(profile['commands']) and all(r['complete'] for r in results),
        'truncated': any(r['truncated'] for r in results)}


def main():
    if os.geteuid() != 0:
        raise RuntimeError('isolated supervisor requires root')
    profile = json.loads(Path(sys.argv[1]).read_text())
    with tempfile.TemporaryDirectory(prefix='bridge-validation-jail-') as directory:
        print(json.dumps(supervise(Path(directory), profile)))


if __name__ == '__main__':
    main()
