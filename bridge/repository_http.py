"""HTTP transport for the same repository operations exposed by MCP."""
import hmac
import json
from typing import Literal

from pydantic import Field, ValidationError

from .core import BridgeError, Settings
from .github_service import RedisJournal
from .http import fetch_json, send_json
from .repository import Evidence, MAX_INPUT, RepositoryService
from .repository_models import Candidate, Query, Strict
from .validation import execute_validation


class QueryCall(Strict):
    repository: str
    ref: str
    query: Query


class EvaluateCall(Strict):
    repository: str
    ref: str
    candidate: Candidate
    operation: Literal['inspect', 'validate']
    manifest: str | None = None
    profile: str | None = None
    max_bytes: int = Field(default=65536, ge=1024, le=262144)


class CommitCall(Strict):
    repository: str
    ref: str
    candidate: Candidate
    message: str = Field(min_length=1, max_length=500)
    inspection_receipt: str
    validation_receipt: str


def service_from_env(env):
    settings = Settings.from_env(env)
    url = env.get('BRIDGE_OAUTH_REDIS_URL') or env.get('REDIS_URL')
    if not url:
        raise BridgeError('repository_journal_unavailable', 503)
    if url.startswith('redis://'):
        url = 'rediss://' + url[len('redis://'):]
    return RepositoryService(settings, fetch=fetch_json, send=send_json,
        journal=RedisJournal(url), evidence=Evidence(settings.key), validator=execute_validation)


async def handle_repository(raw, authorization, service):
    try:
        if not isinstance(authorization, str) or not hmac.compare_digest(
                authorization.encode(), ('Bearer ' + service.settings.key).encode()):
            raise BridgeError('unauthorized', 401)
        if len(raw) > MAX_INPUT:
            raise BridgeError('repository_request_too_large', 413)
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {'operation', 'arguments'}:
            raise BridgeError('invalid_repository_request')
        operation = value['operation']
        models = {'query_repository': QueryCall, 'evaluate_repository_candidate': EvaluateCall,
                  'commit_repository_candidate': CommitCall}
        if operation not in models:
            raise BridgeError('unsupported_repository_operation')
        call = models[operation].model_validate(value['arguments'])
        if operation == 'query_repository':
            result = await service.query(call.repository, call.ref, call.query.model_dump(exclude_none=True))
        elif operation == 'evaluate_repository_candidate':
            result = await service.evaluate(call.repository, call.ref, call.candidate.request(), call.operation,
                                            call.manifest, call.profile, call.max_bytes)
        else:
            result = await service.persist(call.repository, call.ref, call.candidate.request(), call.message,
                                            call.inspection_receipt, call.validation_receipt)
        return 200, {'ok': True, 'result': result}
    except BridgeError as error:
        return error.status, {'ok': False, 'error': {'code': error.code, **error.details}}
    except (ValueError, TypeError, ValidationError):
        return 400, {'ok': False, 'error': {'code': 'invalid_repository_request'}}
    except Exception:
        return 500, {'ok': False, 'error': {'code': 'repository_operation_failed'}}
