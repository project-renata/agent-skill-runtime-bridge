"""Stage only runtime sources; never bundle developer environments or secrets."""
from pathlib import Path
import shutil

root = Path(__file__).resolve().parent.parent
output = root / ".cloudflare-build"
if output.is_symlink():
    raise RuntimeError("build output cannot be a symlink")
if output.exists():
    shutil.rmtree(output)
output.mkdir()
(output / "bridge").mkdir(exist_ok=True)
for name in ("worker.py", "bridge/__init__.py", "bridge/core.py", "bridge/execution.py"):
    shutil.copyfile(root / name, output / name)
