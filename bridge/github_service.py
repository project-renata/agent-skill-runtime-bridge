"""Bounded GitHub API primitives with host credentials and durable write receipts."""
import hashlib
import json
import re
from urllib.parse import quote

from .core import BridgeError, GitHub, immutable_ref

RECEIPT = 'BRIDGE_GITHUB_ISSUE_RECEIPT_V1'


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


class GitHubPolicy:
    """Repository/API permissions and a pinned host credential identity only."""
    def __init__(self, value, settings):
        if (not isinstance(value, dict) or set(value) != {'credential_user_id', 'repositories'}
                or not isinstance(value['credential_user_id'], str)
                or not value['credential_user_id'].isdigit()
                or not isinstance(value['repositories'], dict) or not value['repositories']):
            raise BridgeError('invalid_github_policy', 503)
        self.user_id = value['credential_user_id']
        self.repositories = value['repositories']
        for repo, grant in self.repositories.items():
            if (repo not in settings.repositories or not isinstance(grant, dict)
                    or set(grant) != {'permissions', 'private_only'}
                    or type(grant['private_only']) is not bool
                    or not isinstance(grant['permissions'], list) or not grant['permissions']
                    or any(p not in ('read', 'issues_write') for p in grant['permissions'])):
                raise BridgeError('invalid_github_policy', 503)

    def repo(self, repo, permission='read'):
        if not isinstance(repo, str) or repo not in self.repositories:
            raise BridgeError('repository_not_allowed', 403)
        grant = self.repositories[repo]
        if permission not in grant['permissions']:
            raise BridgeError('github_permission_denied', 403)
        return grant


class RedisJournal:
    """Existing Redis, atomic durable claims. Never expire an uncertain POST claim."""
    def __init__(self, url):
        self.url = url

    async def claim(self, key, fingerprint):
        from redis.asyncio import Redis
        async with Redis.from_url(self.url, decode_responses=True) as redis:
            name = 'runtime-bridge-github:' + key
            if await redis.set(name, fingerprint, nx=True):
                return True
            if await redis.get(name) != fingerprint:
                raise BridgeError('idempotency_conflict', 409)
            return False


class GitHubService:
    def __init__(self, settings, policy, journal, *, fetch, send):
        self.policy, self.journal, self.send = policy, journal, send
        if not settings.github_token:
            raise BridgeError('github_credential_missing', 503)
        self.github = GitHub(fetch, settings.github_token)

    def base(self, repo):
        self.policy.repo(repo)
        return '/repos/' + repo

    def credential_author(self, user):
        return isinstance(user, dict) and str(user.get('id')) == self.policy.user_id

    async def authorize(self, repo, permission='read'):
        # Validate the complete permission boundary before using the host token.
        grant = self.policy.repo(repo, permission)
        user = await self.github.get('/user')
        if not self.credential_author(user):
            raise BridgeError('github_credential_identity_mismatch', 403)
        value = await self.github.get(self.base(repo))
        if value.get('full_name') != repo:
            raise BridgeError('invalid_repository_response', 502)
        if grant['private_only'] and value.get('private') is not True:
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
        return result

    async def pr_review(self, repo, number):
        await self.authorize(repo)
        before = await self.pr(repo, number)
        sha = before['head']['sha']
        files = await self.pages(self.base(repo) + f'/pulls/{number}/files')
        # GitHub caps PR files at 3000. Reject missing/truncated review data.
        if len(files) != before.get('changed_files') or len(files) >= 3000:
            raise BridgeError('pr_files_incomplete', 409)
        checks_error = None
        try:
            checks = await self.pages(self.base(repo) + f'/commits/{sha}/check-runs', 'check_runs')
        except BridgeError as error:
            if error.code != 'github_forbidden':
                raise
            checks, checks_error = [], 'github_forbidden'
        statuses = await self.pages(self.base(repo) + f'/commits/{sha}/statuses')
        runs = await self.pages(self.base(repo) + f'/actions/runs?head_sha={sha}', 'workflow_runs')
        branch = await self.github.get(self.base(repo) + '/branches/' + quote(before['base']['ref'], safe=''))
        if type(branch.get('protected')) is not bool:
            raise BridgeError('invalid_branch_protection', 502)
        required, rules = [], []
        if branch['protected']:
            rules = await self.github.get(self.base(repo) + '/rules/branches/' + quote(before['base']['ref'], safe=''))
            if not isinstance(rules, list):
                raise BridgeError('invalid_branch_rules', 502)
            protection = branch.get('protection', {}).get('required_status_checks')
            if not isinstance(protection, dict):
                raise BridgeError('branch_check_requirements_unavailable', 409)
            required.extend({'context': c} for c in protection.get('contexts', []))
            required.extend({'context': c['context'], 'integration_id': c.get('app_id')} for c in protection.get('checks', []))
        for rule in rules:
            if rule.get('type') == 'required_status_checks':
                required.extend(rule.get('parameters', {}).get('required_status_checks', []))
        satisfied = True
        for requirement in required:
            name = requirement.get('context')
            integration = requirement.get('integration_id')
            check = next((c for c in checks if c.get('name') == name and c.get('head_sha') == sha
                          and (integration is None or c.get('app', {}).get('id') == integration)), None)
            status = next((s for s in statuses if s.get('context') == name), None)
            passed = (check is not None and check.get('status') == 'completed'
                      and check.get('conclusion') in {'success', 'neutral', 'skipped'})
            # A status cannot prove an integration-specific check requirement.
            passed = passed or (integration is None and status is not None and status.get('state') == 'success')
            satisfied = satisfied and passed

        after = await self.pr(repo, number)
        if (after['head']['sha'] != sha or after['base']['sha'] != before['base']['sha']
                or after['draft'] != before['draft'] or after['state'] != before['state']):
            raise BridgeError('pr_changed_during_read', 409)
        return {'pull_request': after, 'files': files, 'check_runs': checks,
                'statuses': statuses, 'workflow_runs': runs, 'check_runs_error': checks_error,
                'branch_rules': rules, 'required_checks': required, 'required_checks_satisfied': satisfied,
                'reviewed_commit_sha': sha,
                'patches_complete': all(isinstance(f.get('patch'), str) for f in files)}

    def text(self, value, *, limit=20000):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise BridgeError('invalid_issue_text')
        # Only this service's own idempotency receipt is protected from forgery.
        if RECEIPT in value:
            raise BridgeError('reserved_receipt_marker', 403)

    async def add_label(self, repo, number, label):
        if (not isinstance(label, str) or not label.strip() or len(label) > 50
                or any(ord(c) < 32 for c in label)):
            raise BridgeError('invalid_label')
        await self.authorize(repo, 'issues_write')
        issue = await self.issue(repo, number)
        if label not in [v.get('name') for v in issue['labels']]:
            await self.write(repo, f'/issues/{number}/labels', {'labels': [label]})
        current = await self.issue(repo, number)
        if label not in [v.get('name') for v in current['labels']]:
            raise BridgeError('label_verification_failed', 502)
        return current

    async def add_comment(self, repo, number, body):
        self.text(body)
        await self.authorize(repo, 'issues_write')
        await self.issue(repo, number)
        return await self.write(repo, f'/issues/{number}/comments', {'body': body})

    async def ensure_issue(self, repo, title, body, key):
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
                if (record != receipt or not self.credential_author(item.get('user'))
                        or not item['body'].startswith(body)):
                    raise BridgeError('idempotency_receipt_mismatch', 409)
                matches.append(item)
        if len(matches) > 1:
            raise BridgeError('duplicate_issue_receipts', 409)
        status = 'existing' if matches else 'created'
        if matches:
            issue = matches[0]
        elif not claimed:
            # A process may have died after POST. Never guess and create again.
            raise BridgeError('creation_pending_or_indeterminate', 409)
        else:
            issue = await self.write(repo, '/issues', {'title': title, 'body': complete})
        issue = await self.issue(repo, issue['number'])
        # Preserve caller text and verify the exact host receipt and author.
        if (payload(issue.get('body'), RECEIPT) != receipt or not self.credential_author(issue.get('user'))
                or not issue.get('body', '').startswith(body) or issue.get('title') != title):
            raise BridgeError('issue_verification_failed', 502)
        return {'repository': repo, 'issue_number': issue['number'], 'issue_url': issue['html_url'],
                'created_at': issue['created_at'], 'status': status, 'state': issue['state']}

    async def create_issue(self, repo, title, body, key):
        self.text(title, limit=256)
        self.text(body)
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', key):
            raise BridgeError('invalid_idempotency_key')
        await self.authorize(repo, 'issues_write')
        return await self.ensure_issue(repo, title, body, 'issue:' + key)

    async def list_issues(self, repo, state='all'):
        if state not in ('open', 'closed', 'all'):
            raise BridgeError('invalid_issue_state')
        await self.authorize(repo)
        items = await self.pages(self.base(repo) + '/issues?state=' + state)
        return {'issues': [item for item in items if 'pull_request' not in item], 'complete': True}

    async def list_prs(self, repo, state='all'):
        if state not in ('open', 'closed', 'all'):
            raise BridgeError('invalid_pr_state')
        await self.authorize(repo)
        return {'pull_requests': await self.pages(self.base(repo) + '/pulls?state=' + state), 'complete': True}
