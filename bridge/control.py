"""Bounded GitHub control plane; no caller credentials or arbitrary REST surface."""
import hashlib
import json
import re

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from .core import BridgeError, GitHub, immutable_ref, safe_path

REQUEST = 'LOCAL_CODING_DISPATCH_REQUEST_V1'
DISPATCH = 'LOCAL_CODING_DISPATCH_TICKET_V2'
ACCEPT = 'LOCAL_CODING_ACCEPTANCE_V1'
PR = 'LOCAL_AGENT_DISPATCH_PR_V1'
EVIDENCE = 'LOCAL_CODING_DISPATCH_EVIDENCE_V1'
RECEIPT = 'LOCAL_AGENT_DISPATCH_RECEIPT_V1'
RESERVED = (REQUEST, DISPATCH, ACCEPT, PR, EVIDENCE, RECEIPT, 'LOCAL_CODING_TASK_V1')


class StrictInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class DispatchInput(StrictInput):
    task_repository: str
    target_repository: str
    title: str = Field(min_length=1, max_length=128)
    task: str = Field(min_length=1, max_length=12000)
    allowed_paths: list[str] = Field(min_length=1, max_length=200)
    sources: list[str] = Field(default_factory=list, max_length=32)
    validation_profile: Literal['documentation', 'python-tests', 'repository-tests']
    commit_message: str = Field(min_length=1, max_length=300)
    mode: Literal['controlled', 'maintainer'] = 'controlled'
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')


class AcceptInput(StrictInput):
    task_repository: str
    task_issue: int = Field(gt=0)
    target_repository: str
    expected_commit_sha: str = Field(pattern=r'^[0-9a-f]{40}$')
    expected_outcome: Literal['PASS']


def fenced(marker, value):
    return marker + '\n```json\n' + json.dumps(value, ensure_ascii=False, indent=2) + '\n```\n'


def payload(body, marker):
    if not isinstance(body, str) or body.count(marker) != 1:
        raise BridgeError('invalid_marker_payload')
    match = re.match(r'\s*```json\s*(.*?)\s*```', body.split(marker)[1], re.S)
    if not match:
        raise BridgeError('invalid_marker_payload')
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise ValueError('duplicate')
            result[k] = v
        return result
    try:
        value = json.loads(match[1], object_pairs_hook=pairs)
        if not isinstance(value, dict):
            raise ValueError('object required')
        return value
    except ValueError:
        raise BridgeError('invalid_marker_payload') from None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ControlPolicy:
    def __init__(self, value, settings):
        required = {'trusted_user_id', 'trusted_login', 'central_repository', 'repositories'}
        if not isinstance(value, dict) or set(value) != required:
            raise BridgeError('invalid_control_policy', 503)
        if (not isinstance(value['trusted_user_id'], str) or not value['trusted_user_id'].isdigit()
                or not re.fullmatch(r'[A-Za-z0-9-]+', value['trusted_login'])
                or not isinstance(value['repositories'], dict) or not value['repositories']):
            raise BridgeError('invalid_control_policy', 503)
        self.repositories = value['repositories']
        self.login, self.user_id = value['trusted_login'], value['trusted_user_id']
        self.central = value['central_repository']
        for repo, grant in self.repositories.items():
            if repo not in settings.repositories or not isinstance(grant, dict):
                raise BridgeError('invalid_control_policy', 503)
            if set(grant) != {'target_branch', 'labels', 'modes', 'required_sources'}:
                raise BridgeError('invalid_control_policy', 503)
            if grant['target_branch'] != settings.repositories[repo]['ref']:
                raise BridgeError('invalid_control_policy', 503)
            if (not isinstance(grant['labels'], list) or not all(isinstance(v, str) and v for v in grant['labels'])
                    or not isinstance(grant['modes'], list) or not grant['modes']
                    or not set(grant['modes']) <= {'controlled', 'maintainer'}
                    or not isinstance(grant['required_sources'], list) or not grant['required_sources']):
                raise BridgeError('invalid_control_policy', 503)
            for path in grant['required_sources']:
                safe_path(path)
        if self.central not in self.repositories:
            raise BridgeError('invalid_control_policy', 503)

    def repo(self, repo):
        if repo not in self.repositories:
            raise BridgeError('repository_not_allowed', 403)
        return self.repositories[repo]


class RedisJournal:
    """Existing Redis, atomic durable claims. Never expire an uncertain POST claim."""
    def __init__(self, url):
        self.url = url

    async def claim(self, key, fingerprint):
        from redis.asyncio import Redis
        async with Redis.from_url(self.url, decode_responses=True) as redis:
            name = 'runtime-bridge-control:' + key
            if await redis.set(name, fingerprint, nx=True):
                return True
            if await redis.get(name) != fingerprint:
                raise BridgeError('idempotency_conflict', 409)
            return False


class ControlPlane:
    def __init__(self, settings, policy, journal, *, fetch, send):
        self.policy, self.journal, self.send = policy, journal, send
        if not settings.github_token:
            raise BridgeError('control_credential_missing', 503)
        self.github = GitHub(fetch, settings.github_token)

    def base(self, repo):
        self.policy.repo(repo)
        return '/repos/' + repo

    def trusted(self, user):
        return (isinstance(user, dict) and user.get('login') == self.policy.login
                and str(user.get('id')) == self.policy.user_id)

    async def authorize(self, *repos):
        # Validate every requested repository before any network call.
        for repo in repos:
            self.policy.repo(repo)
        user = await self.github.get('/user')
        if not self.trusted(user):
            raise BridgeError('control_identity_not_trusted', 403)
        for repo in set(repos):
            value = await self.github.get(self.base(repo))
            if value.get('full_name') != repo or value.get('private') is not True:
                raise BridgeError('private_repository_required', 403)

    async def write(self, repo, suffix, body):
        # Internal paths are composed only by fixed operations below.
        return await self.send('POST', 'https://api.github.com' + self.base(repo) + suffix,
                               self.github.headers, body)

    async def pages(self, path, collection=None):
        result = []
        for page in range(1, 101):
            value = await self.github.get(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
            batch = value.get(collection) if collection and isinstance(value, dict) else value
            if not isinstance(batch, list):
                raise BridgeError('invalid_upstream_response', 502)
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise BridgeError('github_listing_truncated', 502)

    async def issue(self, repo, number):
        if type(number) is not int or number < 1:
            raise BridgeError('invalid_issue_number')
        result = await self.github.get(self.base(repo) + f'/issues/{number}')
        if (result.get('number') != number or 'pull_request' in result
                or result.get('html_url') != f'https://github.com/{repo}/issues/{number}'
                or result.get('state') not in {'open', 'closed'}):
            raise BridgeError('invalid_issue_response', 502)
        return result

    async def comments(self, repo, number):
        await self.issue(repo, number)
        return await self.pages(self.base(repo) + f'/issues/{number}/comments')

    async def read_issue(self, repo, number):
        await self.authorize(repo)
        return await self.issue(repo, number)

    async def read_comments(self, repo, number):
        await self.authorize(repo)
        return {'comments': await self.comments(repo, number)}

    async def read_pr(self, repo, number):
        await self.authorize(repo)
        return await self.pr(repo, number)

    async def pr(self, repo, number):
        if type(number) is not int or number < 1:
            raise BridgeError('invalid_pr_number')
        result = await self.github.get(self.base(repo) + f'/pulls/{number}')
        if (result.get('number') != number
                or result.get('html_url') != f'https://github.com/{repo}/pull/{number}'
                or result.get('base', {}).get('repo', {}).get('full_name') != repo
                or not immutable_ref(result.get('head', {}).get('sha'))
                or type(result.get('draft')) is not bool
                or result.get('state') not in {'open', 'closed'}):
            raise BridgeError('invalid_pr_response', 502)
        result = dict(result)
        if PR in str(result.get('body')):
            p = payload(result['body'], PR)
            required = {'task_repository', 'task_issue', 'target_repository', 'run_id', 'commit_sha'}
            if (not required <= p.keys() or p['target_repository'] != repo
                    or p['commit_sha'] != result['head']['sha']
                    or type(p['task_issue']) is not int or p['task_issue'] < 1
                    or not isinstance(p['run_id'], str) or not p['run_id']
                    or p['task_repository'] not in self.policy.repositories):
                raise BridgeError('pr_payload_mismatch', 409)
            result['dispatch_payload'] = p
        return result

    async def pr_review(self, repo, number):
        await self.authorize(repo)
        before = await self.pr(repo, number)
        sha = before['head']['sha']
        files = await self.pages(self.base(repo) + f'/pulls/{number}/files')
        # GitHub caps PR files at 3000. Reject missing/truncated review data.
        if len(files) != before.get('changed_files') or len(files) >= 3000:
            raise BridgeError('pr_files_incomplete', 409)
        checks = await self.pages(self.base(repo) + f'/commits/{sha}/check-runs', 'check_runs')
        statuses = await self.pages(self.base(repo) + f'/commits/{sha}/statuses')
        after = await self.pr(repo, number)
        if (after['head']['sha'] != sha or after['base']['sha'] != before['base']['sha']
                or after['draft'] != before['draft'] or after['state'] != before['state']):
            raise BridgeError('pr_changed_during_read', 409)
        return {'pull_request': after, 'files': files, 'check_runs': checks,
                'statuses': statuses, 'reviewed_commit_sha': sha,
                'patches_complete': all(isinstance(f.get('patch'), str) for f in files)}

    def ordinary(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise BridgeError('invalid_issue_text')
        if any(marker in text for marker in RESERVED):
            raise BridgeError('reserved_control_marker', 403)

    async def add_label(self, repo, number, label, internal=False):
        if not internal and label not in self.policy.repo(repo)['labels']:
            raise BridgeError('label_not_allowed', 403)
        if not internal and label in {'local-coding-request', 'local-coding-dispatch'}:
            raise BridgeError('use_high_level_control_tool', 403)
        if not internal:
            await self.authorize(repo)
        issue = await self.issue(repo, number)
        if label not in [v.get('name') for v in issue['labels']]:
            await self.write(repo, f'/issues/{number}/labels', {'labels': [label]})
        current = await self.issue(repo, number)
        if label not in [v.get('name') for v in current['labels']]:
            raise BridgeError('label_verification_failed', 502)
        return current

    async def add_comment(self, repo, number, body):
        self.ordinary(body)
        await self.authorize(repo)
        await self.issue(repo, number)
        return await self.write(repo, f'/issues/{number}/comments', {'body': body})

    async def ensure_issue(self, repo, title, body, key, label=None):
        """Claim before POST; reconcile exact GitHub receipts on every retry."""
        fingerprint = digest({'repo': repo, 'title': title, 'body': body})
        key = digest({'repository': repo, 'key': key})
        claimed = await self.journal.claim(key, fingerprint)
        receipt = {'key': key, 'fingerprint': fingerprint}
        complete = body + '\n' + fenced(RECEIPT, receipt)
        matches = []
        for item in await self.pages(self.base(repo) + '/issues?state=all&sort=created&direction=desc'):
            if 'pull_request' in item or RECEIPT not in str(item.get('body')):
                continue
            try:
                record = payload(item['body'], RECEIPT)
            except BridgeError:
                continue
            if record.get('key') == key:
                if (record != receipt or not self.trusted(item.get('user'))
                        or not item['body'].startswith(body)):
                    raise BridgeError('idempotency_receipt_mismatch', 409)
                matches.append(item)
        if len(matches) > 1:
            raise BridgeError('duplicate_control_receipts', 409)
        status = 'existing' if matches else 'created'
        if matches:
            issue = matches[0]
        elif not claimed:
            # A process may have died after POST. Never guess and create again.
            raise BridgeError('creation_pending_or_indeterminate', 409)
        else:
            issue = await self.write(repo, '/issues', {'title': title, 'body': complete})
        issue = await self.issue(repo, issue['number'])
        # Dispatcher appends a normalized contract. Original request/receipt must survive.
        if (payload(issue.get('body'), RECEIPT) != receipt or not self.trusted(issue.get('user'))
                or not issue.get('body', '').startswith(body) or issue.get('title') != title):
            raise BridgeError('issue_verification_failed', 502)
        if label and issue['state'] == 'open':
            issue = await self.add_label(repo, issue['number'], label, internal=True)
        return {'repository': repo, 'issue_number': issue['number'], 'issue_url': issue['html_url'],
                'created_at': issue['created_at'], 'status': status, 'state': issue['state']}

    async def create_issue(self, repo, title, body, key):
        self.ordinary(title)
        self.ordinary(body)
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', key):
            raise BridgeError('invalid_idempotency_key')
        await self.authorize(repo)
        return await self.ensure_issue(repo, title, body, 'issue:' + key)

    async def dispatch(self, request):
        value = request.model_dump()
        task_repo, target = request.task_repository, request.target_repository
        grant = self.policy.repo(target)
        self.policy.repo(task_repo)
        if request.mode not in grant['modes']:
            raise BridgeError('mode_not_allowed', 403)
        if request.mode == 'maintainer' and request.validation_profile != 'repository-tests':
            raise BridgeError('maintainer_requires_repository_tests')
        if request.mode == 'controlled' and (len(request.allowed_paths) > 20 or any('*' in p or '?' in p for p in request.allowed_paths)):
            raise BridgeError('invalid_controlled_paths')
        if len(set(request.allowed_paths)) != len(request.allowed_paths):
            raise BridgeError('duplicate_paths')
        for path in request.allowed_paths:
            if path == '*' and request.mode == 'maintainer':
                continue
            safe_path(path)
        for path in request.sources:
            safe_path(path)
        for text in (request.task, request.title, request.commit_message):
            self.ordinary(text)
        # Match the existing board's short Traditional Chinese task-title contract.
        from unicodedata import east_asian_width
        if (re.search('[A-Za-z]', request.title)
                or sum(2 if east_asian_width(c) in 'WF' else 1 for c in request.title) > 32):
            raise BridgeError('short_chinese_title_required')
        await self.authorize(task_repo, target, self.policy.central)
        contract = {'target_repository': target, 'task': request.task,
                    'paths': request.allowed_paths,
                    'sources': list(dict.fromkeys(grant['required_sources'] + request.sources)),
                    'profile': request.validation_profile, 'commit_message': request.commit_message,
                    'mode': request.mode}
        task = await self.ensure_issue(task_repo, request.title, fenced(REQUEST, contract),
                                       'dispatch:' + request.idempotency_key, 'local-coding-request')
        # Separate V2 control ticket supports different task and code repositories.
        control = {'task_repository': task_repo, 'task_issue': task['issue_number'], 'target_repository': target}
        ticket = await self.ensure_issue(self.policy.central, '派工：' + request.title,
            fenced(DISPATCH, control), 'dispatch-ticket:' + digest(value), 'local-coding-dispatch')
        return {**task, 'marker': REQUEST, 'contract': contract, 'dispatch_ticket': ticket}

    async def accept(self, request):
        repo, target = request.task_repository, request.target_repository
        await self.authorize(repo, target, self.policy.central)
        task = await self.issue(repo, request.task_issue)
        if not self.trusted(task.get('user')):
            raise BridgeError('task_author_not_trusted', 403)
        # Locate prior ticket first, but validate the request identity by the terminal evidence.
        evidence = None
        for comment in reversed(await self.comments(repo, request.task_issue)):
            if EVIDENCE in str(comment.get('body')):
                if not self.trusted(comment.get('user')):
                    raise BridgeError('evidence_author_not_trusted', 403)
                evidence = payload(comment['body'], EVIDENCE)
                break
        expected = {'repository': repo, 'issue_number': request.task_issue, 'target_repository': target,
                    'outcome': 'PASS', 'stage': 'completed', 'provider_started': True,
                    'commit_sha': request.expected_commit_sha}
        if evidence is None or any(evidence.get(k) != v for k, v in expected.items()):
            raise BridgeError('terminal_evidence_mismatch', 409)
        # Retry after a successful merge returns the original ticket, never a new one.
        candidates = await self.pages(self.base(target) + '/pulls?state=all')
        matching = []
        for item in candidates:
            if PR not in str(item.get('body')):
                continue
            p = payload(item['body'], PR)
            if p.get('task_repository') == repo and p.get('task_issue') == request.task_issue and p.get('commit_sha') == request.expected_commit_sha:
                matching.append(item)
        if len(matching) != 1:
            raise BridgeError('unique_matching_pr_required', 409)
        pr = await self.pr(target, matching[0]['number'])
        p = pr['dispatch_payload']
        if (pr['draft'] is not False or pr['base']['ref'] != self.policy.repo(target)['target_branch']
                or p['run_id'] != evidence.get('run_id')
                or (pr['state'] != 'open' and not pr.get('merged'))):
            raise BridgeError('ready_pr_mismatch', 409)
        control = {'target_repository': target, 'target_issue': request.task_issue,
                   'expected_outcome': 'PASS', 'expected_commit_sha': request.expected_commit_sha}
        if repo != target:
            control['task_repository'] = repo
        body = fenced(ACCEPT, control)
        # A closed Task can only return an already-created matching ticket.
        key = 'accept:' + digest(request.model_dump())
        if task['state'] != 'open' or pr.get('merged'):
            candidates = await self.pages(self.base(self.policy.central) + '/issues?state=all')
            existing = [i for i in candidates if 'pull_request' not in i and i.get('body', '').startswith(body)
                        and self.trusted(i.get('user'))]
            if len(existing) != 1:
                raise BridgeError('task_or_pr_already_terminal', 409)
        ticket = await self.ensure_issue(self.policy.central, '驗收派工結果', body, key, 'local-coding-dispatch')
        return {**ticket, 'marker': ACCEPT, 'expected_commit_sha': request.expected_commit_sha,
                'task_issue': request.task_issue, 'pull_request': pr['html_url']}
