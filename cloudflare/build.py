"""Stage only runtime sources; never bundle developer environments or secrets."""
from pathlib import Path
import shutil

root = Path(__file__).resolve().parent.parent
output = root / ".cloudflare-build"
output.mkdir(exist_ok=True)
(output / "bridge").mkdir(exist_ok=True)
for name in ("worker.py", "bridge/__init__.py", "bridge/core.py", "bridge/execution.py"):
    shutil.copyfile(root / name, output / name)
