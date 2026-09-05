"""Read Markdown titles from a repository; JSON input and output, no dependencies."""

import json
from pathlib import Path
import sys


def run(repository, request):
    root = Path(repository).resolve(strict=True)
    files = request.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("files must be a non-empty list of repository-relative paths")

    results = []
    for name in files:
        if not isinstance(name, str) or Path(name).is_absolute():
            raise ValueError("Each file must be a repository-relative path")
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError("File is outside the repository")
        lines = path.read_text(encoding="utf-8").splitlines()
        title = next(
            (line[2:].strip() for line in lines
             if line.startswith("# ")),
            None,
        )
        results.append({"path": name, "title": title, "line_count": len(lines)})
    return {"files": results, "count": len(results)}


def main():
    try:
        if len(sys.argv) != 2:
            raise ValueError("Usage: python3 main.py REPOSITORY_ROOT")
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ValueError("Input must be a JSON object")
        result = {"ok": True, "result": run(sys.argv[1], request)}
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
