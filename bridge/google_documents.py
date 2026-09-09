"""Read bounded attachment/Drive document text without accepting plaintext secrets."""
import base64
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from .core import BridgeError


def extract_document(content, mime, *, max_chars=20000, password=None, page_start=1, page_count=20):
    if type(max_chars) is not int or not 1 <= max_chars <= 100000:
        raise BridgeError('google_document_character_limit', 400)
    if type(page_start) is not int or page_start < 1 or type(page_count) is not int or not 1 <= page_count <= 50:
        raise BridgeError('google_document_page_limit', 400)
    pages, scanned = None, False
    if mime.split(';')[0] == 'application/pdf' or content.startswith(b'%PDF-'):
        from pypdf import PdfReader
        try:
            pdf = PdfReader(io.BytesIO(content))
            if pdf.is_encrypted and (not password or not pdf.decrypt(password)):
                raise BridgeError('google_document_password_reference_required_or_invalid', 403)
            pages = len(pdf.pages)
            selected = pdf.pages[page_start - 1:page_start - 1 + page_count]
            chunks = [(page.extract_text() or '') for page in selected]
            scanned = any(not text.strip() for text in chunks)
            text = '\n\n'.join(chunks)
        except BridgeError:
            raise
        except Exception:
            raise BridgeError('google_document_pdf_failed', 422) from None
    elif mime.startswith(('text/', 'application/json', 'application/xml')):
        text = content.decode('utf-8', errors='replace')
    else:
        raise BridgeError('google_document_requires_export_or_local_ocr', 422)
    return {'text': text[:max_chars], 'total_chars_in_selected_pages': len(text),
            'truncated': len(text) > max_chars, 'page_start': page_start if pages else None,
            'page_count': min(page_count, max(0, pages - page_start + 1)) if pages else None,
            'total_pages': pages, 'more_pages': bool(pages and pages >= page_start + page_count),
            'needs_local_ocr': scanned, 'sha256': hashlib.sha256(content).hexdigest(),
            'trust': 'external-untrusted'}


def read_document(services, source, max_chars=20000, secret_ref=None, secrets=None, page_start=1, page_count=20, ocr=False):
    if not isinstance(source, dict) or set(source) - {'file_id', 'message_id', 'attachment_id', 'part_id', 'mime_type', 'sha256'}:
        raise BridgeError('google_invalid_document_source', 400)
    if bool(source.get('file_id')) == bool(source.get('message_id')):
        raise BridgeError('google_document_requires_one_source', 400)
    if source.get('file_id'):
        metadata = services._read({'operation': 'drive.files.get', 'params': {
            'fileId': source['file_id'], 'fields': 'id,name,mimeType,modifiedTime,version,md5Checksum,size'}})
        mime = metadata['data']['mimeType']
        if mime.startswith('application/vnd.google-apps.'):
            if mime == 'application/vnd.google-apps.document':
                export_mime = 'text/plain'
            elif mime == 'application/vnd.google-apps.spreadsheet':
                # PDF includes all exported sheets; values API is preferable for calculations.
                export_mime = 'application/pdf'
            elif mime == 'application/vnd.google-apps.presentation':
                export_mime = 'text/plain'
            else:
                raise BridgeError('google_document_export_type_unsupported', 422)
            data = services._read({'operation': 'drive.files.export', 'params': {
                'fileId': source['file_id'], 'mimeType': export_mime}})['data']
            mime = export_mime
        else:
            data = services._read({'operation': 'drive.files.get', 'params': {
                'fileId': source['file_id'], 'alt': 'media'}})['data']
        content = base64.b64decode(data['data_base64'])
    else:
        metadata = services._read({'operation': 'gmail.users.messages.get', 'params': {
            'id': source['message_id'], 'format': 'full'}})
        found = []
        def walk(part, depth=0):
            if depth > 30:
                raise BridgeError('google_document_mime_depth', 422)
            by_part = source.get('part_id') is not None and part.get('partId') == source['part_id']
            by_handle = source.get('attachment_id') and part.get('body', {}).get('attachmentId') == source['attachment_id']
            if (by_part or by_handle) and part.get('body', {}).get('attachmentId'):
                found.append(part)
            for child in part.get('parts', []):
                walk(child, depth + 1)
        walk(metadata['data'].get('payload', {}))
        if len(found) != 1:
            raise BridgeError('google_attachment_not_in_message', 404)
        mime = found[0].get('mimeType', 'application/octet-stream')
        data = services._read({'operation': 'gmail.users.messages.attachments.get', 'params': {
            'messageId': source['message_id'], 'id': found[0]['body']['attachmentId']}})['data']
        raw = data['data']
        content = base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))
    sha = hashlib.sha256(content).hexdigest()
    if source.get('sha256') and source['sha256'] != sha:
        raise BridgeError('google_document_source_changed', 409)
    password = None
    if secret_ref:
        if not source.get('sha256'):
            raise BridgeError('google_document_secret_requires_source_sha256', 400)
        binding = (secrets or {}).get(secret_ref)
        if not isinstance(binding, dict) or binding.get('sha256') != sha or not binding.get('password'):
            raise BridgeError('google_document_secret_not_bound_to_source', 403)
        password = binding['password']
    result = extract_document(content, mime, max_chars=max_chars, password=password,
                              page_start=page_start, page_count=page_count)
    if ocr and result['needs_local_ocr']:
        if not all(shutil.which(name) for name in ('pdftoppm', 'tesseract')):
            raise BridgeError('google_local_ocr_dependencies_missing', 422)
        # Only selected pages are decrypted into a private disposable directory.
        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            reader.decrypt(password)
        writer = PdfWriter()
        for page in reader.pages[page_start - 1:page_start - 1 + page_count]:
            writer.add_page(page)
        try:
            with tempfile.TemporaryDirectory(prefix='renata-google-document-') as directory:
                target = Path(directory)
                writer.write(target / 'selected.pdf')
                subprocess.run(['pdftoppm', '-scale-to', '2200', '-png', str(target / 'selected.pdf'),
                                str(target / 'page')], check=True, capture_output=True, timeout=90)
                langs = subprocess.run(['tesseract', '--list-langs'], check=True, capture_output=True,
                                       text=True, timeout=15).stdout
                language = 'chi_tra+eng' if 'chi_tra' in langs else 'eng'
                chunks = [subprocess.run(['tesseract', str(p), 'stdout', '-l', language], check=True,
                    capture_output=True, text=True, timeout=45).stdout for p in sorted(target.glob('page-*.png'))]
            text = '\n\n'.join(chunks)
            result.update(text=text[:max_chars], total_chars_in_selected_pages=len(text),
                          truncated=len(text) > max_chars, needs_local_ocr=False, ocr_language=language,
                          extraction='local-ocr', ocr_may_contain_errors=True)
        except (OSError, subprocess.SubprocessError):
            raise BridgeError('google_local_ocr_failed', 422) from None
    result.update(account=services.account, source=source, source_fingerprint=metadata['fingerprint'], mime_type=mime)
    return result


def document_secrets(env):
    try:
        return json.loads(env.get('BRIDGE_GOOGLE_DOCUMENT_SECRETS', '{}'))
    except ValueError:
        raise ValueError('Invalid BRIDGE_GOOGLE_DOCUMENT_SECRETS') from None
