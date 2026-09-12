"""Opt-in, real-account scratch CRUD check. No sends, shares or attendee invitations."""
import argparse
import asyncio
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.google_local import local_services
from bridge.google_documents import read_document


async def check(account, report_path, native_only=False, slides_only=False):
    google = local_services(account)
    run_id = 'bridge-google-check-' + uuid.uuid4().hex[:12]
    report = {'run_id': run_id, 'account': account, 'started_at': datetime.now(timezone.utc).isoformat(), 'checks': [], 'cleanup': []}
    cleanup = []
    counter = 0
    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        report_path.chmod(0o600)
    async def change(operation, params=None, body=None, media=None, verification=None):
        nonlocal counter
        counter += 1
        call = {'operation': operation, 'params': params or {}}
        if body is not None:
            call['body'] = body
        if media is not None:
            call['media'] = media
        if verification is not None:
            call['verify'] = {'require_readback': True, 'expected': verification}
        for attempt in range(3):
            plan = await google.prepare([call], run_id + '-' + str(counter) + '-' + str(attempt))
            result = await google.execute(plan['plan_id'], plan['plan_hash'])
            if result['status'] != 'conflict' or result['completed_changes']:
                break
            # Only disposable resources created by THIS check. Drive can increment
            # its version after server-side indexing. No mutation occurred; re-read
            # and prepare the same intended scratch effect against current state.
            report.setdefault('reconciled_conflicts', []).append({'operation': operation, 'plan_id': plan['plan_id']})
        record = {'operation': operation, 'status': result['status'], 'plan_id': plan['plan_id'], 'effects': [e['status'] for e in result['effects']]}
        report['checks'].append(record)
        save()
        print(json.dumps(record), flush=True)
        if result['status'] != 'applied':
            raise RuntimeError(json.dumps(result))
        # Repeated execute must be a receipt lookup, never a duplicate effect.
        assert await google.execute(plan['plan_id'], plan['plan_hash']) == result
        return result['effects'][0]['data']
    async def read(op, params):
        return (await google.read(op, params))['data']
    async def remove(op, params):
        try:
            await change(op, params)
            report['cleanup'].append({'operation': op, 'status': 'removed'})
        except Exception as error:
            report['cleanup'].append({'operation': op, 'params': params, 'status': 'failed', 'error': str(error)[:300]})
        save()
    try:
        if not native_only and not slides_only:
            profile = await read('gmail.users.getProfile', {})
            assert profile['emailAddress'].lower() == account.lower()
            label = await change('gmail.users.labels.create', body={'name': run_id})
            cleanup.append(('gmail.users.labels.delete', {'id': label['id']}))
            await change('gmail.users.labels.patch', {'id': label['id']}, {'name': run_id + '-updated'}, verification={'name': run_id + '-updated'})
            composed = google.compose([account], run_id, 'Bridge 完整服務驗證草稿；不寄送。', attachments=[
                {'filename': 'scratch.txt', 'mime_type': 'text/plain', 'data_base64': base64.b64encode('附件驗證'.encode()).decode()}])
            draft = await change('gmail.users.drafts.create', body={'message': composed['message']})
            cleanup.append(('gmail.users.drafts.delete', {'id': draft['id']}))
            source = await read('gmail.users.drafts.get', {'id': draft['id'], 'format': 'full'})
            assert source['message']['id']
            attachment = next(p for p in source['message']['payload']['parts'] if p.get('filename') == 'scratch.txt')
            doc = read_document(google, {'message_id': source['message']['id'], 'part_id': attachment['partId']})
            assert '附件驗證' in doc['text']
            composed = google.compose([account], run_id + '-updated', '更新草稿驗證。')
            await change('gmail.users.drafts.update', {'id': draft['id']}, {'message': composed['message']})
            calendar = await change('calendar.calendars.insert', body={'summary': run_id, 'timeZone': 'Asia/Taipei'})
            cleanup.append(('calendar.calendars.delete', {'calendarId': calendar['id']}))
            event = await change('calendar.events.insert', {'calendarId': calendar['id'], 'sendUpdates': 'none'}, {
                'summary': run_id, 'start': {'dateTime': '2026-09-11T09:00:00+08:00', 'timeZone': 'Asia/Taipei'},
                'end': {'dateTime': '2026-09-11T09:15:00+08:00', 'timeZone': 'Asia/Taipei'}, 'reminders': {'useDefault': False}},
                verification={'summary': run_id, 'status': 'confirmed'})
            cleanup.append(('calendar.events.delete', {'calendarId': calendar['id'], 'eventId': event['id'], 'sendUpdates': 'none'}))
            await change('calendar.events.patch', {'calendarId': calendar['id'], 'eventId': event['id'], 'sendUpdates': 'none'}, {'summary': run_id + '-updated'}, verification={'summary': run_id + '-updated'})
            busy = await google.read('calendar.freebusy.query', {}, {'timeMin': '2026-09-11T00:00:00Z', 'timeMax': '2026-09-12T00:00:00Z', 'items': [{'id': calendar['id']}]})
            assert calendar['id'] in busy['data']['calendars']
            tasklist = await change('tasks.tasklists.insert', body={'title': run_id})
            cleanup.append(('tasks.tasklists.delete', {'tasklist': tasklist['id']}))
            task = await change('tasks.tasks.insert', {'tasklist': tasklist['id']}, {'title': 'scratch task', 'notes': run_id})
            cleanup.append(('tasks.tasks.delete', {'tasklist': tasklist['id'], 'task': task['id']}))
            await change('tasks.tasks.patch', {'tasklist': tasklist['id'], 'task': task['id']}, {'status': 'completed'}, verification={'status': 'completed'})
            await change('tasks.tasks.patch', {'tasklist': tasklist['id'], 'task': task['id']}, {'status': 'needsAction', 'title': 'reopened'}, verification={'status': 'needsAction', 'title': 'reopened'})
            folder = await change('drive.files.create', body={'name': run_id, 'mimeType': 'application/vnd.google-apps.folder'})
            cleanup.append(('drive.files.delete', {'fileId': folder['id']}))
            content = 'Bridge 測試檔案'.encode()
            file = await change('drive.files.create', {'fields': 'id,name,md5Checksum'}, {'name': 'scratch.txt', 'parents': [folder['id']]}, {'mime_type': 'text/plain', 'data_base64': base64.b64encode(content).decode()})
            cleanup.append(('drive.files.delete', {'fileId': file['id']}))
            downloaded = await read('drive.files.get', {'fileId': file['id'], 'alt': 'media'})
            assert base64.b64decode(downloaded['data_base64']) == content
            await change('drive.files.update', {'fileId': file['id']}, {'name': 'updated.txt'}, {'mime_type': 'text/plain', 'data_base64': base64.b64encode(b'updated').decode()}, verification={'name': 'updated.txt'})
            copied = await change('drive.files.copy', {'fileId': file['id']}, {'name': 'copy.txt'})
            cleanup.append(('drive.files.delete', {'fileId': copied['id']}))
            await change('drive.files.update', {'fileId': copied['id']}, {'trashed': True}, verification={'trashed': True})
            await change('drive.files.update', {'fileId': copied['id']}, {'trashed': False}, verification={'trashed': False})
        if not slides_only:
            doc = await change('docs.documents.create', body={'title': run_id})
            cleanup.append(('drive.files.delete', {'fileId': doc['documentId']}))
            await change('docs.documents.batchUpdate', {'documentId': doc['documentId']}, {'requests': [{'insertText': {'endOfSegmentLocation': {}, 'text': 'Bridge 文件編輯成功。\n'}}]})
            extracted = read_document(google, {'file_id': doc['documentId']})
            assert '文件編輯成功' in extracted['text']
            sheet = await change('sheets.spreadsheets.create', body={'properties': {'title': run_id}})
            cleanup.append(('drive.files.delete', {'fileId': sheet['spreadsheetId']}))
            await change('sheets.spreadsheets.values.update', {'spreadsheetId': sheet['spreadsheetId'], 'range': 'A1:B2', 'valueInputOption': 'USER_ENTERED'}, {'values': [['Bridge', '測試'], [1, '=A2+1']]})
            values = await read('sheets.spreadsheets.values.get', {'spreadsheetId': sheet['spreadsheetId'], 'range': 'A1:B2', 'valueRenderOption': 'UNFORMATTED_VALUE'})
            assert values['values'][1][1] == 2
        slides = await change('slides.presentations.create', body={'title': run_id})
        cleanup.append(('drive.files.delete', {'fileId': slides['presentationId']}))
        await change('slides.presentations.batchUpdate', {'presentationId': slides['presentationId']}, {'requests': [
            {'createSlide': {'objectId': 'bridge_slide'}},
            {'createShape': {'objectId': 'bridge_shape', 'shapeType': 'TEXT_BOX', 'elementProperties': {'pageObjectId': 'bridge_slide', 'size': {'width': {'magnitude': 300, 'unit': 'PT'}, 'height': {'magnitude': 100, 'unit': 'PT'}}, 'transform': {'scaleX': 1, 'scaleY': 1, 'translateX': 40, 'translateY': 40, 'unit': 'PT'}}}},
            {'insertText': {'objectId': 'bridge_shape', 'text': 'Bridge 簡報編輯成功'}}]})
        presentation = await read('slides.presentations.get', {'presentationId': slides['presentationId']})
        assert '簡報編輯成功' in json.dumps(presentation, ensure_ascii=False)
        report['result'] = 'pass'
    except Exception as error:
        report['result'] = 'failed'
        report['error'] = str(error)[:1000]
        print('CHECK FAILED:', report['error'], flush=True)
    finally:
        for op, params in reversed(cleanup):
            await remove(op, params)
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        save()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--account', required=True)
    parser.add_argument('--execute-scratch', action='store_true', required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--native-only', action='store_true')
    parser.add_argument('--slides-only', action='store_true')
    args = parser.parse_args()
    result = asyncio.run(check(args.account, args.report, args.native_only, args.slides_only))
    print(json.dumps({'result': result['result'], 'check_count': len(result['checks']), 'cleanup': result['cleanup'], 'report': str(args.report)}))
    raise SystemExit(0 if result['result'] == 'pass' and all(x['status'] == 'removed' for x in result['cleanup']) else 1)
