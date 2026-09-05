"""Bootstrap UTF-8 file access for an operator-trusted Python authoring workspace.

The Bridge supplies only explicitly loaded files and enforces persisted writes.
This helper is ordinary canonical Python; it does not hold repository credentials.
"""
from pathlib import Path


def run(root, request):
    root = Path(root).resolve(strict=True)
    reads = request.get("read", [])
    changes = request.get("changes", {})
    if (set(request) - {"read", "changes"} or not isinstance(reads, list)
            or not isinstance(changes, dict) or len(reads) + len(changes) > 31):
        raise ValueError("Expected read list and/or changes object, at most 31 entries")

    def target(name):
        if (not isinstance(name, str) or not name or Path(name).is_absolute()
                or "\\" in name or any(p in ("", ".", "..", ".git") for p in name.split("/"))):
            raise ValueError("Expected repository-relative path")
        path = (root / name).resolve()
        if not path.is_relative_to(root) or path == root:
            raise ValueError("Path must stay inside the supplied snapshot")
        return path

    # Return the pre-edit text so an author can inspect exactly what was loaded.
    contents = {name: target(name).read_text(encoding="utf-8") for name in reads}
    pending = []
    for name, content in changes.items():
        path = target(name)
        if content is not None and not isinstance(content, str):
            raise ValueError("Content must be UTF-8 text or null to delete")
        if content is None and not path.is_file():
            raise ValueError("Delete target must be loaded first")
        pending.append((path, content))
    for path, content in pending:
        if content is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    return {"files": contents, "changed": sorted(changes)}
