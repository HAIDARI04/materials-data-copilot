"""Read-only folder review and source-backed evidence snapshots."""
import hashlib
import json
import mimetypes
import os
import re
import zipfile
from pathlib import Path
from uuid import uuid4

import database
import research_store as store


def digest(path):
    checksum = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest()


def media_type(path, prefix):
    for signature, mime in [(b'\x89PNG\r\n\x1a\n', 'image/png'), (b'\xff\xd8\xff', 'image/jpeg'),
                            (b'II*\x00', 'image/tiff'), (b'MM\x00*', 'image/tiff'), (b'%PDF-', 'application/pdf')]:
        if prefix.startswith(signature):
            return mime
    return mimetypes.guess_type(path.name)[0] or 'application/octet-stream'


def scan(root):
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Choose a folder.')
    entries = []
    seen = {}
    with database.connect_database() as connection:
        imported = {r['sha256']: dict(r) for r in connection.execute('SELECT * FROM imported_files')}
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not Path(folder, d).is_symlink() and d not in {'.git', '.venv', '__pycache__'})
        for name in sorted(files):
            if len(entries) >= 5000:
                raise ValueError('Review at most 5,000 files at a time; select a smaller folder.')
            path = Path(folder, name)
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                continue
            relative = path.relative_to(root).as_posix()
            lower = relative.casefold()
            technique = next((label for token, label in [('raman', 'Raman spectroscopy'), ('probe', 'Transport'),
                ('afm', 'AFM'), ('xrd', 'XRD'), ('sem', 'SEM'), ('pl-data', 'PL'), ('cvd', 'CVD')]
                if token in (root.name + '/' + lower).casefold()), 'Unknown')
            role = 'reference' if any(t in lower for t in ['reference', 'pdf_card']) else (
                'derived' if any(t in lower for t in ['processed', 'output', 'result', 'plot']) else 'raw')
            entry = {'entry_id': len(entries), 'relative_path': relative, 'technique': technique,
                     'role': role, 'flags': ['Device and worksheet assignments require review.']}
            try:
                before = path.stat()
                checksum = digest(path)
                with path.open('rb') as source:
                    prefix = source.read(64)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError('File changed during review.')
                entry.update(size_bytes=after.st_size, sha256=checksum, content_type=media_type(path, prefix))
                if path.suffix.casefold()=='.xlsx' and not zipfile.is_zipfile(path):
                    entry['flags'].append('Partial or invalid workbook archive; recovered data may be incomplete.')
                if path.suffix.casefold()=='.wdf':
                    import wdf_reader
                    try:
                        measurement = wdf_reader.read_wdf(path)
                        entry['technique'] = measurement['technique']
                        entry['axis_unit'] = measurement['x_axis']['unit']
                    except wdf_reader.WdfError:
                        entry['flags'].append('WDF container could not be decoded; preserve for review.')
                if entry['content_type'] != (mimetypes.guess_type(path.name)[0] or 'application/octet-stream'):
                    entry['flags'].append('File signature differs from filename extension.')
                if checksum in imported:
                    entry['existing_file_id'] = imported[checksum]['file_id']
                    entry['existing_metadata'] = {k: imported[checksum][k] for k in ['sample_id', 'technique', 'material_system']}
                    entry['flags'].append('Identical bytes already stored; retain a separate source association.')
                elif checksum in seen:
                    entry['duplicate_of_entry'] = seen[checksum]
                seen[checksum] = entry['entry_id']
                if after.st_size > 100 * 1024 * 1024:
                    entry['error'] = 'Exceeds the 100 MiB per-file import limit.'
                if name.startswith('~$'):
                    entry['error'] = 'Office lock file; not experimental data.'
                stem = path.with_suffix('')
                entry['companions'] = [str(p.relative_to(root).as_posix()) for p in path.parent.glob(stem.name + '.*') if p != path and p.is_file()]
                match = re.search(r'(?:20)?\d{2}[._-]\d{2}[._-]\d{2}', relative)
                entry['folder_date_hint'] = match.group() if match else None
            except (OSError, ValueError) as error:
                entry['error'] = str(error)
            entries.append(entry)
    identity = str(uuid4())
    with database.connect_database() as connection:
        connection.execute('INSERT INTO research_import_plans VALUES (?,?,?,?)',
                           (identity, str(root), store.encode(entries), store.now()))
    return {'plan_id': identity, 'root': str(root), 'entries': entries}


def plan_source(plan_id, entry_id):
    with database.connect_database() as connection:
        row = connection.execute('SELECT * FROM research_import_plans WHERE plan_id=?', (plan_id,)).fetchone()
    if not row:
        raise ValueError('Folder review not found.')
    entries = json.loads(row['entries_json'])
    if entry_id < 0 or entry_id >= len(entries):
        raise ValueError('Unknown reviewed file.')
    entry = entries[entry_id]
    if entry.get('error'):
        raise ValueError(entry['error'])
    root = Path(row['root']).resolve(strict=True)
    path = (root / entry['relative_path']).resolve(strict=True)
    if not path.is_relative_to(root) or path.is_symlink():
        raise ValueError('Source is outside the reviewed folder.')
    return path, entry


def import_chats(folder):
    """Import extracted snapshots; historical messages never become instructions."""
    paths = sorted(Path(folder).resolve(strict=True).glob('*.json'))
    if len(paths) > 500:
        raise ValueError('Import at most 500 chat snapshots per request.')
    records = []
    sources = []
    for path in paths:
        if path.name == 'index.json':
            continue
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError('Chat snapshot exceeds 20 MiB.')
        content = path.read_bytes()
        document = json.loads(content)
        if not isinstance(document, dict) or 'messages' not in document:
            continue
        source_hash = hashlib.sha256(content).hexdigest()
        sources.append((source_hash,path.name,'application/json',content,store.now()))
        for message in document['messages']:
            if message.get('role') not in {'user', 'assistant'} or not message.get('text', '').strip():
                continue
            citation = {'url': document.get('url'), 'source_id':source_hash, 'snapshot_filename': path.name, 'snapshot_sha256': source_hash,
                        'message_id': message.get('id'), 'parent_id': message.get('parent'), 'role': message['role'],
                        'message_time': message.get('time'), 'attachments_verified': False}
            records.append((document.get('title', path.stem), message['text'],
                            'observation' if message['role'] == 'user' else 'interpretation', citation))
    # Validate every source before writes. Each retained body is independently checksummed.
    if any(len(record[1])>2_000_000 for record in records):
        raise ValueError('A source message exceeds the supported evidence size.')
    with database.connect_database() as connection:
        connection.executemany('INSERT OR IGNORE INTO research_sources VALUES (?,?,?,?,?)',sources)
    for title, body, kind, citation in records:
        store.add_evidence(title, body, kind, citation)
    return {'message_occurrences': len(records), 'snapshots': len({r[3]['snapshot_sha256'] for r in records}),
            'note': 'Snapshots only. User statements and assistant interpretations retain their roles; attachments are unverified.'}


def index_document(record, path, kind='reference', entity_id=None):
    """Extract searchable text from a verified source; never execute embedded content."""
    import xml.etree.ElementTree as ET
    import transport
    extension = path.suffix.casefold()
    if extension in {'.txt','.md','.csv'}:
        sections = [('text',path.read_text(encoding='utf-8-sig'))]
    elif extension=='.pdf':
        from pypdf import PdfReader
        reader = PdfReader(path)
        if len(reader.pages)>1000:
            raise ValueError('Index at most 1,000 PDF pages per file.')
        sections = [(f'page {i+1}',page.extract_text() or '') for i,page in enumerate(reader.pages)]
    elif extension in {'.docx','.pptx'}:
        members,_ = transport._read_zip_entries(path)
        selected = ['word/document.xml'] if extension=='.docx' else sorted(
            (name for name in members if re.fullmatch(r'ppt/slides/slide\d+\.xml',name)),
            key=lambda name:int(re.search(r'(\d+)\.xml',name).group(1)))
        sections=[]
        for name in selected:
            if name not in members:
                continue
            root = ET.fromstring(members[name])
            paragraphs = [''.join(node.text or '' for node in paragraph.iter() if node.tag.endswith('}t'))
                          for paragraph in root.iter() if paragraph.tag.endswith('}p')]
            sections.append((name,'\n'.join(paragraphs)))
    else:
        raise ValueError('Searchable notes support UTF-8 TXT/MD/CSV, PDF, DOCX and PPTX. Use measurement analysis for binary experiments.')
    if sum(len(text) for _,text in sections)>10_000_000:
        raise ValueError('Extracted document text exceeds 10 million characters.')
    count=0
    for section,text in sections:
        for offset in range(0,len(text),32000):
            body=text[offset:offset+32000]
            if not body.strip():
                continue
            store.add_evidence(record['original_filename'],body,kind,{'file_id':record['file_id'],'source_sha256':record['sha256'],
                 'section':section,'text_offset':offset,'extraction':'stored_text_only; embedded images and attachments not interpreted'},entity_id)
            count+=1
    return {'passages':count,'file_id':record['file_id'],'source_sha256':record['sha256']}
