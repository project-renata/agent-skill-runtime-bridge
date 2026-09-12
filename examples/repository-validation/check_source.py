"""Repository-owned syntax/format checks; freely replace with other Python checks."""
import ast
from pathlib import Path

for path in Path('.').rglob('*.py'):
    source = path.read_text()
    ast.parse(source, filename=str(path))
    assert source.endswith('\n'), f'{path}: final newline required'
    assert all(line == line.rstrip() for line in source.splitlines()), f'{path}: trailing whitespace'
print('syntax and whitespace checks passed')
