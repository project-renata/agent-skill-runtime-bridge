"""Apply a small UTF-8 note edit batch using ordinary Python file operations."""
import json
from pathlib import Path
import sys


def run(repository, request):
    root = Path(repository).resolve(strict=True)
    changes = request.get("changes")
    if not isinstance(changes, dict) or not changes or len(changes) > 32:
        raise ValueError("changes must be a non-empty object of at most 32 paths")
    targets = []
    for name, content in changes.items():
        if not isinstance(name, str) or not name or Path(name).is_absolute():
            raise ValueError("expected a repository-relative path")
        path = (root / name).resolve()
        if not path.is_relative_to(root) or path == root:
            raise ValueError("path must stay inside the repository")
        if content is not None and not isinstance(content, str):
            raise ValueError("content must be UTF-8 text, or null to delete")
        if content is None and not path.is_file():
            raise ValueError("delete target must exist in the snapshot")
        targets.append((path, content))
    for path, content in targets:
        if content is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    return {"changed": sorted(changes), "count": len(changes)}


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1], json.load(sys.stdin)), ensure_ascii=False))
