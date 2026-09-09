"""Source-bound mail/calendar/follow-up workflows using the same durable write engine."""
from copy import deepcopy

from .core import BridgeError
from .gmail import summarize
from .google_services import fingerprint, matches_expected


def all_tasks(google, tasklist):
    tasks, token = [], None
    for _ in range(10):
        params = {'tasklist': tasklist, 'maxResults': 100, 'showCompleted': True, 'showHidden': True}
        if token:
            params['pageToken'] = token
        page = google._read({'operation': 'tasks.tasks.list', 'params': params})
        tasks.extend(t for t in page['data'].get('items', []) if not t.get('deleted'))
        token = page['next_page_token']
        if not token:
            return tasks
    raise BridgeError('google_followup_inventory_limit', 400)


def prepare_workflow(google, workflow, input, idempotency_key):
    if workflow not in {'followup_put', 'followup_close', 'registration_track', 'mail_to_calendar', 'registration_confirm'}:
        raise BridgeError('google_unknown_workflow', 400)
    allowed = {'tracking_key', 'message_id', 'tasklist_id', 'task_id', 'task_fingerprint',
               'title', 'notes', 'due', 'calendar_id', 'event', 'confirmation_quote'}
    if not isinstance(input, dict) or set(input) - allowed:
        raise BridgeError('google_invalid_workflow_input', 400)
    journal_key = 'workflow:' + fingerprint({'account': google.account, 'key': idempotency_key})
    request_hash = fingerprint({'workflow': workflow, 'input': input})
    previous = google.journal.get(journal_key)
    if previous:
        if previous['request_hash'] != request_hash:
            raise BridgeError('google_idempotency_conflict', 409)
        return previous['result']
    key = input.get('tracking_key')
    if not isinstance(key, str) or not 1 <= len(key) <= 200:
        raise BridgeError('google_tracking_key_required', 400)
    key_hash = fingerprint({'account': google.account, 'key': key})[:40]
    marker = '[renata-followup:' + key_hash + ']'
    changes, checks = [], []
    if workflow in ('registration_track', 'mail_to_calendar', 'registration_confirm'):
        if not input.get('message_id'):
            raise BridgeError('google_workflow_message_required', 400)
        source_call = {'operation': 'gmail.users.messages.get', 'params': {'id': input['message_id'], 'format': 'full'}}
        source = google._read(source_call)
        checks.append({'call': source_call, 'fingerprint': source['fingerprint']})
        if workflow == 'registration_confirm':
            quote = input.get('confirmation_quote')
            if not isinstance(quote, str) or len(quote) < 4 or quote not in summarize(source['data'], 20000)['body']:
                raise BridgeError('google_confirmation_source_quote_required', 400)
    task = None
    if workflow != 'mail_to_calendar':
        if not input.get('tasklist_id'):
            raise BridgeError('google_workflow_tasklist_required', 400)
        matches = [t for t in all_tasks(google, input['tasklist_id']) if marker in t.get('notes', '')]
        if len(matches) > 1:
            raise BridgeError('google_followup_ambiguous', 409)
        task = matches[0] if matches else None
    if workflow in ('followup_put', 'registration_track'):
        if not input.get('title'):
            raise BridgeError('google_followup_title_required', 400)
        notes = input.get('notes', '') + '\n\n' + marker
        if input.get('message_id'):
            notes += '\nGmail: https://mail.google.com/mail/u/0/#all/' + input['message_id']
        body = {'title': input['title'], 'notes': notes, 'status': 'needsAction'}
        if input.get('due'):
            body['due'] = input['due']
        if task and matches_expected(task, body):
            return {'status': 'unchanged', 'task_id': task['id'], 'tracking_key': key}
        params = {'tasklist': input['tasklist_id']}
        if task:
            params['task'] = task['id']
        changes.append({'operation': 'tasks.tasks.patch' if task else 'tasks.tasks.insert',
                        'params': params, 'body': body, 'checks': checks,
                        'verify': {'require_readback': True, 'expected': body}})
    elif workflow in ('mail_to_calendar', 'registration_confirm'):
        event = deepcopy(input.get('event', {}))
        if not all(event.get(k) for k in ('summary', 'start', 'end')) or not input.get('calendar_id'):
            raise BridgeError('google_workflow_complete_event_required', 400)
        event['id'] = 'r' + key_hash
        event.setdefault('extendedProperties', {}).setdefault('private', {})['renataKey'] = key_hash
        event_call = {'operation': 'calendar.events.get', 'params': {
            'calendarId': input['calendar_id'], 'eventId': event['id']}}
        expected = {k: event[k] for k in ('id', 'summary', 'start', 'end', 'extendedProperties')}
        expected['status'] = 'confirmed'
        try:
            existing = google._read(event_call)
            if not matches_expected(existing['data'], expected):
                raise BridgeError('google_calendar_key_conflict', 409)
            checks.append({'call': event_call, 'fingerprint': existing['fingerprint']})
        except BridgeError as error:
            if error.code != 'google_not_found':
                raise
            changes.append({'operation': 'calendar.events.insert', 'params': {
                'calendarId': input['calendar_id'], 'sendUpdates': 'all' if event.get('attendees') else 'none'},
                'body': event, 'checks': deepcopy(checks),
                'verify': {'require_readback': True, 'expected': expected}})
    if workflow in ('followup_close', 'registration_confirm') and task:
        if workflow == 'followup_close' and (input.get('task_id') != task['id'] or not input.get('task_fingerprint')):
            raise BridgeError('google_followup_exact_task_and_fingerprint_required', 400)
        task_call = {'operation': 'tasks.tasks.get', 'params': {'tasklist': input['tasklist_id'], 'task': task['id']}}
        observed = google._read(task_call)
        if input.get('task_fingerprint') and observed['fingerprint'] != input['task_fingerprint']:
            raise BridgeError('google_stale_source', 409)
        task_checks = deepcopy(checks) + [{'call': task_call, 'fingerprint': observed['fingerprint']}]
        changes.append({'operation': 'tasks.tasks.delete', 'params': task_call['params'], 'checks': task_checks})
    if not changes:
        return {'status': 'unchanged', 'tracking_key': key, 'task_id': task['id'] if task else None}
    result = google._prepare(changes, idempotency_key)
    result['workflow'] = workflow
    result['tracking_key'] = key
    result['ordering'] = 'Calendar must pass read-back and expected-state verification before the tracking task is removed.'
    google.journal.put(journal_key, {'request_hash': request_hash, 'result': result}, once=True)
    return result
