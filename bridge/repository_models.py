"""Public, provider-neutral repository operation schemas."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class TreeQuery(Strict):
    operation: Literal['tree']
    prefix: str = ''
    max_depth: int = Field(default=16, ge=1, le=128)
    max_entries: int = Field(default=1000, ge=1, le=32768)
    max_bytes: int = Field(default=32768, ge=1024, le=262144)


class SearchQuery(Strict):
    operation: Literal['search']
    cursor: str | None = Field(default=None, max_length=2048, description='Opaque next_cursor from the same search. Continues at its immutable commit; keep other query fields unchanged.')
    pattern: str = Field(min_length=1, max_length=256, description='Literal, case-sensitive, single-line text. No regex or shell syntax.')
    prefix: str = ''
    glob: str = Field(default='*', max_length=256, description='Case-sensitive path glob filter; no filesystem expansion.')
    suffix: str = Field(default='', max_length=128)
    max_results: int = Field(default=100, ge=1, le=1000)
    max_bytes: int = Field(default=32768, ge=1024, le=262144)


class ReadQuery(Strict):
    operation: Literal['read']
    path: str
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    byte_start: int | None = Field(default=None, ge=0)
    byte_count: int | None = Field(default=None, ge=1, le=524288)
    max_bytes: int = Field(default=32768, ge=1024, le=262144)


Query = Annotated[TreeQuery | SearchQuery | ReadQuery, Field(discriminator='operation')]


class ExactEdit(Strict):
    start: int = Field(ge=0, description='Zero-based Unicode code point offset in the original UTF-8 text.')
    end: int = Field(ge=0, description='Exclusive end offset in the same original text.')
    expected: str
    replacement: str


class Change(Strict):
    path: str
    operation: Literal['create', 'update', 'delete']
    expected_sha256: str | None = Field(pattern='^[0-9a-f]{64}$', description='Required SHA256 of the whole base file; null requires absence.')
    content: str | None = Field(default=None, description='Exact replacement text, mutually exclusive with edits; omitted for delete.')
    edits: list[ExactEdit] | None = Field(default=None, min_length=1, max_length=64)


class Candidate(Strict):
    base_commit: str = Field(pattern='^[0-9a-f]{40}$')
    changes: list[Change] = Field(min_length=1, max_length=32)
    fingerprint: str | None = Field(default=None, pattern='^[0-9a-f]{64}$')

    def request(self):
        result = self.model_dump(exclude_none=True)
        # Null is an explicit existence precondition, not an omitted field.
        for raw, model in zip(result['changes'], self.changes):
            raw['expected_sha256'] = model.expected_sha256
        return result
