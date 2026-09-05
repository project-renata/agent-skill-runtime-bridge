"""Ordinary Python run(repository_root, input) invocation over a temporary snapshot.

Only operator-trusted programs are supported. This is NOT an untrusted-code sandbox.
"""
import json
from pathlib import Path
import tempfile

from .core import BridgeError, Execution, MAX_FILE, MAX_FILES, MAX_RESULT, MAX_TOTAL, safe_path


def populate(root, files):
    for name, content in files.items():
        path = root / safe_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def invoke(root, program, request):
    path = root / safe_path(program)
    namespace = {"__name__": "_bridge_skill_", "__file__": str(path)}
    exec(compile(path.read_bytes(), str(path), "exec"), namespace)
    if not callable(namespace.get("run")):
        raise BridgeError("missing_run_function", 422)
    result = namespace["run"](str(root), request)
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > MAX_RESULT:
        raise BridgeError("result_too_large", 413)
    return json.loads(encoded)


def collect_changes(root, baseline):
    import os
    current, size, visited = {}, 0, 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        visited += 1
        if visited > 256:
            raise BridgeError("too_many_snapshot_directories", 413)
        for name in dirs + names:
            if (Path(directory) / name).is_symlink():
                raise BridgeError("unsupported_repository_entry", 422)
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative == ".bridge-input.json":
                continue
            safe_path(relative)
            if not path.is_file():
                raise BridgeError("unsupported_repository_entry", 422)
            if path.stat().st_size > MAX_FILE:
                raise BridgeError("file_too_large", 413)
            if len(current) >= MAX_FILES * 2:
                raise BridgeError("too_many_snapshot_files", 413)
            content = path.read_bytes()
            size += len(content)
            if len(content) > MAX_FILE or size > MAX_TOTAL * 2:
                raise BridgeError("snapshot_too_large", 413)
            current[relative] = content
    changes = {path: content for path, content in current.items() if baseline.get(path) != content}
    changes.update({path: None for path in baseline if path not in current})
    return changes


def execute_inline(files, program, request):
    # Cloudflare has no subprocess: synchronous invocation does not yield between
    # populating, executing and cleaning up this per-request temporary directory.
    with tempfile.TemporaryDirectory(prefix="skill-") as directory:
        root = Path(directory)
        populate(root, files)
        import contextlib
        import os
        try:
            with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                result = invoke(root, program, request)
                return Execution(result, collect_changes(root, files))
        except BridgeError:
            raise
        except BaseException:
            raise BridgeError("execution_failed", 422) from None


def execute_subprocess(files, program, request, timeout=10):
    import os
    import selectors
    import signal
    import subprocess
    import sys
    import time

    with tempfile.TemporaryDirectory(prefix="skill-") as directory:
        root = Path(directory)
        populate(root, files)
        payload = root / ".bridge-input.json"
        if payload.exists():
            raise BridgeError("reserved_path")
        payload.write_text(json.dumps(request), encoding="utf-8")
        runner = Path(__file__).with_name("runner.py")
        process = subprocess.Popen(
            [sys.executable, "-I", str(runner), str(root), program, str(payload)],
            cwd=root, env={"PYTHONIOENCODING": "utf-8"}, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + timeout
        output = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BridgeError("execution_timeout", 504)
                    for key, _ in selector.select(remaining):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            output.extend(chunk)
                            if len(output) > MAX_RESULT + 1024:
                                raise BridgeError("result_too_large", 413)
                try:
                    process.wait(timeout=max(0.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    raise BridgeError("execution_timeout", 504) from None
            if process.returncode:
                raise BridgeError("execution_failed", 422)
            envelope = json.loads(output)
            if not envelope["ok"]:
                raise BridgeError(envelope["error"], 422)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return Execution(envelope["result"], collect_changes(root, files))
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
