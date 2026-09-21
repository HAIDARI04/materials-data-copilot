"""Research context, append-only evidence and reviewed file associations."""
import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

import database

KINDS = {'project', 'substrate', 'device', 'state', 'run', 'region', 'growth'}
PARENTS = {'project': {None}, 'substrate': {'project'}, 'device': {'substrate'},
           'state': {'device'}, 'run': {'device', 'state', 'growth'},
           'region': {'device', 'state', 'run'}, 'growth': {'project', 'substrate'}}
EVIDENCE_KINDS = {'observation', 'correction', 'hypothesis', 'plan', 'interpretation', 'verified_result', 'reference'}


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def initialize(connection):
    statements = [
        '''CREATE TABLE IF NOT EXISTS research_entities (
        entity_id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
        parent_id TEXT REFERENCES research_entities(entity_id), metadata_json TEXT NOT NULL,
        created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS research_aliases (
        alias TEXT NOT NULL, scope TEXT NOT NULL, entity_id TEXT NOT NULL REFERENCES research_entities(entity_id),
        PRIMARY KEY(alias,scope))''',
        '''CREATE TABLE IF NOT EXISTS research_revisions (
        revision_id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES research_entities(entity_id),
        metadata_json TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS research_links (
        link_id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES research_entities(entity_id),
        file_id TEXT NOT NULL REFERENCES imported_files(file_id), selector TEXT NOT NULL,
        role TEXT NOT NULL, source_path TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(entity_id,file_id,selector,role,source_path))''',
        '''CREATE TABLE IF NOT EXISTS research_evidence (
        evidence_id TEXT PRIMARY KEY, entity_id TEXT REFERENCES research_entities(entity_id),
        kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, sha256 TEXT NOT NULL,
        citation_json TEXT NOT NULL, created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS research_import_plans (
        plan_id TEXT PRIMARY KEY, root TEXT NOT NULL, entries_json TEXT NOT NULL, created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS research_sources (
        source_id TEXT PRIMARY KEY, filename TEXT NOT NULL, content_type TEXT NOT NULL,
        payload BLOB NOT NULL, created_at TEXT NOT NULL)''',
        '''CREATE INDEX IF NOT EXISTS research_links_file ON research_links(file_id)''',
    ]
    for statement in statements:
        connection.execute(statement)
    for table in ['research_entities', 'research_aliases', 'research_revisions', 'research_evidence', 'research_links', 'research_import_plans', 'research_sources']:
        for action in ['UPDATE', 'DELETE']:
            connection.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()}_guard
                BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'Research history is append-only'); END''')


def entity(row, connection):
    result = dict(row)
    revision = connection.execute('SELECT metadata_json FROM research_revisions WHERE entity_id=? ORDER BY rowid DESC LIMIT 1', (row['entity_id'],)).fetchone()
    result['metadata'] = json.loads(revision['metadata_json'] if revision else result['metadata_json'])
    result.pop('metadata_json')
    result['aliases'] = [r['alias'] for r in connection.execute('SELECT alias FROM research_aliases WHERE entity_id=?', (row['entity_id'],))]
    return result


def list_entities():
    with database.connect_database() as c:
        return [entity(r, c) for r in c.execute('SELECT * FROM research_entities ORDER BY created_at, name')]


def create_entity(kind, name, parent_id=None, metadata=None, aliases=()):
    if kind not in KINDS or not name.strip() or len(name) > 200:
        raise ValueError('Provide a valid entity kind and name.')
    metadata = metadata or {}
    with database.connect_database() as c:
        parent = c.execute('SELECT * FROM research_entities WHERE entity_id=?', (parent_id,)).fetchone()
        parent_kind = parent['kind'] if parent else None
        if (parent_id and not parent) or parent_kind not in PARENTS[kind]:
            raise ValueError('The selected parent is not valid for this entity kind.')
        identity = str(uuid4())
        c.execute('INSERT INTO research_entities VALUES (?,?,?,?,?,?)', (identity, kind, name.strip(), parent_id, encode(metadata), now()))
        for alias in {value.strip().casefold() for value in [name, *aliases]}:
            if not alias.strip() or len(alias) > 200:
                raise ValueError('Aliases must contain 1 to 200 characters.')
            c.execute('INSERT INTO research_aliases VALUES (?,?,?)', (alias.strip().casefold(), parent_id or '', identity))
        return entity(c.execute('SELECT * FROM research_entities WHERE entity_id=?', (identity,)).fetchone(), c)


def revise(entity_id, metadata, reason):
    if not reason.strip():
        raise ValueError('Record a reason and evidence for this correction.')
    with database.connect_database() as c:
        row = c.execute('SELECT * FROM research_entities WHERE entity_id=?', (entity_id,)).fetchone()
        if not row:
            raise ValueError('Entity not found.')
        if row['kind'] == 'state':
            raise ValueError('Create a new device state to record a material change.')
        c.execute('INSERT INTO research_revisions VALUES (?,?,?,?,?)', (str(uuid4()), entity_id, encode(metadata), reason, now()))
        return entity(row, c)


def add_link(entity_id, file_id, selector='', role='raw', source_path=''):
    if role not in {'raw', 'derived', 'reference', 'preview', 'control', 'protocol'}:
        raise ValueError('Unknown source role.')
    with database.connect_database() as c:
        c.execute('INSERT OR IGNORE INTO research_links VALUES (?,?,?,?,?,?,?)',
                  (str(uuid4()), entity_id, file_id, selector, role, source_path, now()))


def add_evidence(title, body, kind, citation, entity_id=None):
    if kind not in EVIDENCE_KINDS or not body.strip() or len(body) > 2_000_000:
        raise ValueError('Provide evidence text and a valid classification.')
    if not citation:
        raise ValueError('A source citation is required.')
    digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
    identity = hashlib.sha256(encode([entity_id, kind, title, digest, citation]).encode()).hexdigest()
    with database.connect_database() as c:
        c.execute('INSERT OR IGNORE INTO research_evidence VALUES (?,?,?,?,?,?,?,?)',
                  (identity, entity_id, kind, title, body, digest, encode(citation), now()))
    return identity


def search(query, entity_id=None, limit=30):
    stop = {'a','an','and','are','as','at','be','by','can','compare','does','for','from','how','i','in','is','it','me','of','on','or','our','show','that','the','their','these','this','to','was','we','what','which','with','you'}
    terms = [term for term in re.findall(r'[\w-]+', query.casefold()) if term not in stop][:12]
    identifiers = [term for term in terms if re.fullmatch(r'[a-z]{1,4}-?\d+(?:-\d+)?',term)]
    if not terms:
        return []
    with database.connect_database() as c:
        if entity_id:
            rows = c.execute('''WITH RECURSIVE descendants(id) AS (SELECT ? UNION ALL
                SELECT entity_id FROM research_entities JOIN descendants ON parent_id=id)
                SELECT * FROM research_evidence WHERE entity_id IN (SELECT id FROM descendants)''',(entity_id,)).fetchall()
        else:
            rows = c.execute('SELECT * FROM research_evidence').fetchall()
    matches = []
    seen = set()
    for row in rows:
        body = row['body']
        if hashlib.sha256(body.encode()).hexdigest() != row['sha256']:
            raise ValueError('Evidence checksum mismatch.')
        folded = (row['title'] + ' ' + body).casefold()
        if identifiers and not any(term.replace('-','') in folded.replace('-','') for term in identifiers):
            continue
        score = sum(term in folded for term in terms)
        if not score or row['sha256'] in seen:
            continue
        seen.add(row['sha256'])
        start = max(0, min((body.casefold().find(t) for t in terms if t in body.casefold()), default=0) - 150)
        matches.append({**dict(row), 'score': score, 'excerpt': body[start:start+1200], 'citation': json.loads(row['citation_json'])})
        matches[-1].pop('body')
        matches[-1].pop('citation_json')
    return sorted(matches, key=lambda r: (-r['score'], r['created_at']))[:limit]


def detail(identity):
    with database.connect_database() as c:
        row = c.execute('SELECT * FROM research_entities WHERE entity_id=?', (identity,)).fetchone()
        if not row:
            raise ValueError('Entity not found.')
        result = entity(row, c)
        result['children'] = [entity(r, c) for r in c.execute('SELECT * FROM research_entities WHERE parent_id=?', (identity,))]
        result['links'] = [dict(r) for r in c.execute('''WITH RECURSIVE descendants(id) AS
            (SELECT ? UNION ALL SELECT entity_id FROM research_entities JOIN descendants ON parent_id=id)
            SELECT research_links.*, original_filename, sha256, technique FROM research_links
            JOIN imported_files USING(file_id) WHERE entity_id IN (SELECT id FROM descendants)''', (identity,))]
        result['history'] = [dict(r) for r in c.execute('SELECT * FROM research_revisions WHERE entity_id=? ORDER BY rowid', (identity,))]
        result['evidence'] = [dict(r) for r in c.execute('SELECT * FROM research_evidence WHERE entity_id=?', (identity,))]
    return result
