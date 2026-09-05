"""Child-process entrypoint; receives no hosting or GitHub credentials."""
import contextlib
import json
import os
from pathlib import Path
import sys

# -I excludes the script's directory; include only the installed Bridge package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bridge.core import BridgeError
from bridge.execution import invoke

if __name__ == "__main__":
    try:
        request = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            result = invoke(Path(sys.argv[1]), sys.argv[2], request)
        response = {"ok": True, "result": result}
    except BridgeError as error:
        response = {"ok": False, "error": error.code}
    except BaseException:
        response = {"ok": False, "error": "execution_failed"}
    print(json.dumps(response, ensure_ascii=False, allow_nan=False))
