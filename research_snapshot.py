"""Freeze structured appeal facts; query them without access to pipeline files."""
from contextlib import contextmanager
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time

VERSION = 'appeals-research-1'
SAFE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
ENUMS = {
    'scope': ('yes', 'no', 'unclear'),
    'route': ('extract', 'out_of_scope', 'no_members', 'unclear'),
    'status': ('complete', 'not_run', 'pending', 'incomplete', 'too_large', 'interrupted',
               'extracted', 'reviewed', 'review_failed', 'duration_failed', 'scope_flagged',
               'triaging', 'extracting', 'reviewing', 'measuring'),
    'decision': ('approved', 'approved_with_conditions', 'denied', 'other', 'unclear'),
    'relationship': ('developer', 'neighbor', 'organization', 'other', 'unclear'),
    'seeks': ('overturn_approval', 'overturn_denial', 'change_conditions', 'other', 'unclear'),
    'representation': ('represented', 'pro_se', 'unclear'),
    'central': ('yes', 'no', 'unclear', 'not_applicable'),
    'event': ('filing', 'judgment', 'further_appeal', 'reopening', 'remand', 'resolution'),
    'litigation': ('resolved', 'ongoing', 'unclear'),
    'permission': ('allowed', 'allowed_with_conditions', 'denied', 'unclear'),
}

# Only these columns ever reach SQLite. Identity strings are deliberately kept;
# all prose, quotations, evidence, source links and model/error responses are absent.
TABLES = {
    'coverage': (
        ('group_id', 'TEXT PRIMARY KEY', 'Run-local source group identity.'),
        ('status', 'TEXT NOT NULL', 'Pipeline processing status, never an appeal outcome.'),
        ('route', 'TEXT NOT NULL', 'Extraction routing decision.'),
        ('municipal_appeal', 'TEXT NOT NULL', 'Scope classification: yes/no/unclear.'),
        ('housing', 'TEXT NOT NULL', 'Scope classification: yes/no/unclear.'),
        ('scope_category', 'TEXT NOT NULL', 'confirmed/uncertain/excluded; only complete groups produce facts.'),
        ('review_required', 'INTEGER NOT NULL', '1 if warnings, review/scope flags, uncertain scope or errors remain.'),
        ('warning_count', 'INTEGER NOT NULL', 'Number of distinct checkpoint validation warnings.'),
        ('review_flag_count', 'INTEGER NOT NULL', 'Number of unresolved review flags; their prose is omitted.'),
        ('scope_review', 'TEXT NOT NULL', 'agree/flag/not_reviewed; flags do not become new scope classifications.'),
        ('error_present', 'INTEGER NOT NULL', 'Pipeline error presence; message omitted.'),
        ('document_count', 'INTEGER NOT NULL', 'Number of source documents in the frozen run group.'),
        ('finalized_docket_count', 'INTEGER NOT NULL', 'Number of finalized docket records, including excluded scope.'),
    ),
    'all_appeals': (
        ('appeal_id', 'TEXT PRIMARY KEY', 'Stable join key from group ID and docket, including null docket.'),
        ('group_id', 'TEXT NOT NULL REFERENCES coverage(group_id)', 'Run-local source group; distinct from project identity.'),
        ('docket', 'TEXT', 'Extracted proceeding identifier; null means unavailable.'),
        ('scope_category', 'TEXT NOT NULL', 'confirmed/uncertain/excluded; default appeals view selects confirmed.'),
        ('review_required', 'INTEGER NOT NULL', 'Group review flag; flagged records remain in analytical counts.'),
        ('review_flag_count', 'INTEGER NOT NULL', 'Flags targeting this docket or all dockets in its group.'),
        ('municipality', 'TEXT', 'Literal extracted municipality, not canonicalized.'),
        ('proposed_units', 'INTEGER', 'Proposed new/replacement/additionally authorized dwellings; not net additions or built homes.'),
        ('proposed_lots', 'INTEGER', 'Proposed new residential building lots; not all subdivision lots or dwelling units.'),
        ('municipal_decision_date', 'TEXT', 'Challenged municipal decision date; YYYY-MM-DD, YYYY-MM, YYYY, or null.'),
        ('municipal_decision_outcome', 'TEXT NOT NULL', 'Challenged municipal decision: approved/approved_with_conditions/denied/other/unclear.'),
        ('discretion_involved', 'TEXT NOT NULL', 'Established evaluative municipal contention: yes/no/unclear.'),
        ('discretion_central', 'TEXT NOT NULL', 'Principal discretionary contention: yes/no/unclear/not_applicable.'),
        ('litigation_status', 'TEXT NOT NULL', 'Latest observed linked adjudication: resolved/ongoing/unclear.'),
        ('permission', 'TEXT NOT NULL', 'Latest observed proposal authorization: allowed/allowed_with_conditions/denied/unclear; not appellant success.'),
        ('representation', 'TEXT NOT NULL', 'All appellants: represented/pro_se/mixed/unclear. Any unknown or empty/missing list gives unclear.'),
        ('appellants_state', 'TEXT NOT NULL', 'documented/none_documented/unknown; empty list does not establish no appellants.'),
        ('appellant_count', 'INTEGER', 'Listed appellants; null when list unavailable.'),
        ('related_dockets_state', 'TEXT NOT NULL', 'documented/none_documented/unknown; empty means no established observation.'),
        ('related_docket_count', 'INTEGER', 'Listed related proceedings, not separate inferred appeals.'),
        ('timeline_state', 'TEXT NOT NULL', 'documented/none_documented/unknown.'),
        ('timeline_count', 'INTEGER', 'Listed material events, not a complete chronology.'),
        ('restrictions_state', 'TEXT NOT NULL', 'documented/none_documented/unknown; only explicit stop orders, not routine conditions/remand.'),
        ('restriction_count', 'INTEGER', 'Listed explicit restrictions; not days of construction delay.'),
        ('housing_changes_state', 'TEXT NOT NULL', 'documented/none_documented/unknown; no quantitative before/after coding exists.'),
        ('housing_change_count', 'INTEGER', 'Listed explicit imposed changes; no inferred lost homes.'),
        ('duration_kind', 'TEXT NOT NULL', 'exact/at_least/unknown. at_least is a minimum, never a complete duration.'),
        ('duration_days', 'INTEGER', 'Validated end minus start in days; null for unknown/reversed/zero-day lower bound.'),
        ('duration_start_date', 'TEXT', 'Full evidenced start date; null if unavailable.'),
        ('duration_start_relation', 'TEXT', 'exact or on_or_before; independent from end relation.'),
        ('duration_end_date', 'TEXT', 'Full evidenced end date; never extrapolated to today.'),
        ('duration_end_relation', 'TEXT', 'exact or on_or_after; exact only for closure of whole linked chain.'),
    ),
    'appellants': (
        ('appeal_id', 'TEXT NOT NULL REFERENCES all_appeals(appeal_id)', 'Join to one appeal.'),
        ('ordinal', 'INTEGER NOT NULL', '1-based position within the extracted list, not rank.'),
        ('name', 'TEXT NOT NULL', 'Literal extracted appellant identity; not deduplicated across cases.'),
        ('relationship', 'TEXT NOT NULL', 'developer/neighbor/organization/other/unclear.'),
        ('seeks', 'TEXT NOT NULL', 'overturn_approval/overturn_denial/change_conditions/other/unclear; requested relief, not success.'),
        ('representation', 'TEXT NOT NULL', 'represented/pro_se/unclear for this appellant.'),
    ),
    'related_dockets': (
        ('appeal_id', 'TEXT NOT NULL REFERENCES all_appeals(appeal_id)', 'Join to originating appeal.'),
        ('ordinal', 'INTEGER NOT NULL', '1-based list position.'),
        ('docket', 'TEXT', 'Extracted related proceeding identifier; may be null or outside this snapshot.'),
    ),
    'timeline_events': (
        ('appeal_id', 'TEXT NOT NULL REFERENCES all_appeals(appeal_id)', 'Join to one appeal.'),
        ('ordinal', 'INTEGER NOT NULL', '1-based list position; not chronological ordering.'),
        ('kind', 'TEXT NOT NULL', 'filing/judgment/further_appeal/reopening/remand/resolution.'),
        ('event_date', 'TEXT', 'Event date YYYY-MM-DD, YYYY-MM, YYYY, or null; partial dates are not precise endpoints.'),
        ('dockets_state', 'TEXT NOT NULL', 'documented/none_documented/unknown for event docket links.'),
    ),
    'timeline_dockets': (
        ('appeal_id', 'TEXT NOT NULL REFERENCES all_appeals(appeal_id)', 'Originating appeal, not the linked docket.'),
        ('event_ordinal', 'INTEGER NOT NULL', 'Join to timeline_events on appeal_id AND ordinal.'),
        ('ordinal', 'INTEGER NOT NULL', '1-based position among this event’s docket links.'),
        ('docket', 'TEXT NOT NULL', 'Literal linked proceeding identifier.'),
    ),
    'restrictions': (
        ('appeal_id', 'TEXT NOT NULL REFERENCES all_appeals(appeal_id)', 'Join to one appeal.'),
        ('ordinal', 'INTEGER NOT NULL', '1-based list position; type/description not classified here.'),
        ('start_date', 'TEXT', 'Explicit restriction start: full/partial date or null.'),
        ('end_date', 'TEXT', 'Explicit restriction end: full/partial date or null; null does not mean ongoing.'),
    ),
    'review_flags': (
        ('group_id', 'TEXT NOT NULL REFERENCES coverage(group_id)', 'Run-local group.'),
        ('ordinal', 'INTEGER NOT NULL', '1-based list position.'),
        ('docket', 'TEXT', 'Target docket; null denotes a group-wide flag.'),
    ),
}
VIEWS = {
    'appeals': "SELECT * FROM all_appeals WHERE scope_category = 'confirmed'",
    'uncertain_appeals': "SELECT * FROM all_appeals WHERE scope_category = 'uncertain'",
}
SQL_SCHEMA = {
    **{name: 'CREATE TABLE '+name+' ('+', '.join(c+' '+t for c, t, _ in fields)+
       (', PRIMARY KEY ('+('appeal_id, event_ordinal, ordinal' if name == 'timeline_dockets'
                           else 'group_id, ordinal' if name == 'review_flags' else 'appeal_id, ordinal')+')'
        if name not in ('coverage', 'all_appeals') else '')+')'
       for name, fields in TABLES.items()},
    **{name: 'CREATE VIEW '+name+' AS '+query for name, query in VIEWS.items()},
}
LIMITATIONS = [
    'Default appeals view includes only finalized, confirmed municipal housing appeals; uncertain_appeals and coverage expose uncertainty and exclusions.',
    'Observed permission is not appeal success/failure, construction, abandonment, or causal construction delay. No failed-appeal outcome is coded.',
    'Duration minima must stay separate from exact durations; unknown is not zero and ongoing is not extended to today.',
    'One record per (group_id,docket); related appeals may concern the same proposal. Summing units/lots does not count distinct homes or net additions.',
    'Child-table joins multiply appeals. Filter with EXISTS or aggregate at appeal_id before counting/averaging. Municipalities and names are literal, not canonical.',
    'Empty lists mean no established observation, not absence. Missing lists are unknown. Housing changes have no quantitative impact coding.',
    'Review flags remain included; filter review_required explicitly if desired. Snapshot does not reclassify source evidence or expose any narrative text.',
    'SQL supports ordinary SELECT/CTE/window queries with an allowed function set. Results are bounded; a truncated result is not a complete distribution.',
]
FUNCTIONS = frozenset(('abs avg ceil ceiling char coalesce concat concat_ws count date datetime '
    'dense_rank exp first_value floor format glob hex ifnull iif instr julianday lag last_value '
    'lead length like likelihood likely ln log log10 log2 lower ltrim max min mod nth_value ntile '
    'nullif percent_rank pow power printf quote rank replace round row_number rtrim sign '
    'sqrt strftime substr substring sum time total trim trunc typeof unicode unixepoch '
    'unlikely upper json json_array json_array_length json_extract json_object json_quote '
    'json_type json_valid json_group_array json_group_object group_concat string_agg').split())
MAX_SQL = 16000
MAX_ROWS = 1000
MAX_BYTES = 256000
MAX_SECONDS = 2.0


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def enum(value, kind, default='unclear'):
    if value is None:
        value = default
    if value not in ENUMS[kind]:
        raise ValueError('Invalid structured category: '+kind)
    return value


def identity(value, nullable=True):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 1000 or '\x00' in value:
        raise ValueError('Invalid structured identity')
    return value


def count(value):
    if value is not None and (type(value) is not int or value < 0 or value > 2**63-1):
        raise ValueError('Invalid structured count')
    return value


def fact_date(value, full=False):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}(?:-\d{2}(?:-\d{2})?)?', value):
        raise ValueError('Invalid structured date')
    if full and len(value) != 10:
        raise ValueError('Duration endpoints must be full dates')
    date.fromisoformat(value + ('-01-01' if len(value) == 4 else '-01' if len(value) == 7 else ''))
    return value


def observations(value):
    if value is None:
        return 'unknown', None
    if not isinstance(value, list):
        raise ValueError('Invalid structured observation list')
    return ('documented' if value else 'none_documented'), len(value)


def scope_category(scope, route):
    values = [enum(scope.get(k), 'scope') for k in ('municipal_appeal', 'housing')]
    if route in ('out_of_scope', 'no_members') or 'no' in values:
        return 'excluded'
    return 'confirmed' if values == ['yes', 'yes'] else 'uncertain'


def duration(bound):
    points = []
    for side, allowed in [('start', ('exact', 'on_or_before')), ('end', ('exact', 'on_or_after'))]:
        point = bound.get(side)
        if point is None:
            points.extend([None, None])
        else:
            if not isinstance(point, dict) or point.get('relation') not in allowed:
                raise ValueError('Invalid duration endpoint')
            points.extend([fact_date(point.get('date'), full=True), point['relation']])
            if points[-2] is None:
                raise ValueError('Duration endpoint missing date')
    start, sr, end, er = points
    kind, days = 'unknown', None
    if start and end:
        difference = (date.fromisoformat(end)-date.fromisoformat(start)).days
        if difference >= 0:
            kind = 'exact' if sr == er == 'exact' else 'at_least'
            days = difference
            if days == 0 and kind == 'at_least':
                kind, days = 'unknown', None
    if bound.get('duration') != {'kind': kind, 'days': days}:
        raise ValueError('Checkpoint duration disagrees with endpoint arithmetic')
    return [kind, days, *points]


def structured_appeal(gid, record, bound, category, required, flags):
    docket = identity(record.get('docket'))
    aid = sha(json.dumps([gid, docket], ensure_ascii=False).encode())[:32]
    project = record.get('project') or {}
    decision = record.get('municipal_decision') or {}
    discretion = record.get('discretion') or {}
    status = record.get('observed_status') or {}
    parties = record.get('appellants')
    statuses = {enum(p.get('representation'), 'representation') for p in parties or []}
    representation = ('unclear' if not statuses or 'unclear' in statuses else
                      'mixed' if len(statuses) == 2 else next(iter(statuses)))
    row = [aid, gid, docket, category, required,
           sum(f.get('docket') in (None, docket) for f in flags), identity(project.get('municipality')),
           count(project.get('proposed_units')), count(project.get('proposed_lots')),
           fact_date(decision.get('date')), enum(decision.get('outcome'), 'decision'),
           enum(discretion.get('involved'), 'scope'), enum(discretion.get('central'), 'central'),
           enum(status.get('litigation'), 'litigation'), enum(status.get('permission'), 'permission'),
           representation]
    for key in ('appellants', 'related_dockets', 'timeline', 'restrictions', 'housing_changes'):
        row.extend(observations(record.get(key)))
    row.extend(duration(bound))
    children = {name: [] for name in TABLES if name not in ('coverage', 'all_appeals', 'review_flags')}
    for n, p in enumerate(parties or [], 1):
        children['appellants'].append([aid, n, identity(p.get('name'), False),
            enum(p.get('relationship'), 'relationship'), enum(p.get('seeks'), 'seeks'),
            enum(p.get('representation'), 'representation')])
    for n, related in enumerate(record.get('related_dockets') or [], 1):
        children['related_dockets'].append([aid, n, identity(related.get('docket'))])
    for n, event in enumerate(record.get('timeline') or [], 1):
        children['timeline_events'].append([aid, n, enum(event.get('kind'), 'event'),
            fact_date(event.get('date')), observations(event.get('dockets'))[0]])
        for j, linked in enumerate(event.get('dockets') or [], 1):
            children['timeline_dockets'].append([aid, n, j, identity(linked, False)])
    for n, restriction in enumerate(record.get('restrictions') or [], 1):
        children['restrictions'].append([aid, n, fact_date(restriction.get('start')),
                                         fact_date(restriction.get('end'))])
    return row, children


@contextmanager
def run_lock(folder):
    # Never create or change a pipeline lock/file. A missing lock means this run
    # has never established the pipeline's writer protocol.
    try:
        lock = (folder/'.lock').open('rb')
    except FileNotFoundError:
        raise ValueError('Selected run has no pipeline lock; choose a previously run plan') from None
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Selected run is being written; build after it stops') from None
        yield


def selected_paths(folder, manifest):
    paths = [folder/'manifest.json']
    for group in manifest['groups']:
        gid = group['group_id']
        if not isinstance(gid, str) or not SAFE_NAME.fullmatch(gid):
            raise ValueError('Invalid group identity')
        for sub in ('results', 'first_pass', 'activity'):
            p = folder/sub/(gid+'.json')
            if p.exists():
                paths.append(p)
    return paths


def load_run(folder):
    manifest_raw = (folder/'manifest.json').read_bytes()
    manifest = json.loads(manifest_raw)
    if manifest.get('schema_version') != 'appeals-1':
        raise ValueError('Unsupported pipeline manifest version')
    paths = selected_paths(folder, manifest)
    raw = {p: p.read_bytes() for p in paths}
    if raw[folder/'manifest.json'] != manifest_raw:
        raise ValueError('Manifest changed while loading snapshot')
    # Include mtime in stability checks: dashboard selects newest first-pass/result.
    times = {p: p.stat().st_mtime_ns for p in paths}
    data = {name: [] for name in TABLES}
    seen = set()
    for group in manifest['groups']:
        gid = group['group_id']
        if gid in seen:
            raise ValueError('Duplicate group in manifest')
        seen.add(gid)
        candidates = [folder/sub/(gid+'.json') for sub in ('results', 'first_pass')]
        available = [p for p in candidates if p in raw]
        p = max(available, key=lambda path: times[path]) if available else None
        result = json.loads(raw[p]) if p else {'status': 'not_run'}
        status = enum(result.get('status'), 'status', 'not_run')
        activity = folder/'activity'/(gid+'.json')
        if activity in raw and (p is None or times[activity] > times[p]):
            if json.loads(raw[activity]).get('stage') in ('triaging', 'extracting', 'reviewing', 'measuring'):
                status = 'interrupted'
        if not group.get('eligible', True):
            status = 'too_large'
        scope = result.get('triage') or {}
        route = enum((result.get('route') or {}).get('route'), 'route')
        category = scope_category(scope, route)
        review = result.get('review') or {}
        flags = review.get('flags') or []
        warnings = result.get('warnings') or []
        verdict = (review.get('scope') or {}).get('verdict', 'not_reviewed')
        if verdict not in ('agree', 'flag', 'not_reviewed'):
            raise ValueError('Invalid review verdict')
        error = int(bool(result.get('error')))
        required = int(bool(warnings or flags or error or verdict == 'flag' or
                            category == 'uncertain'))
        candidate = result.get('candidate') or {}
        records = candidate.get('dockets', []) if status == 'complete' else []
        if records and candidate.get('schema_version') != 'v6':
            raise ValueError('Only finalized v6 extraction records are supported')
        bounds = result.get('duration_bounds') or []
        by_docket = {b.get('docket'): b for b in bounds}
        if len(by_docket) != len(bounds):
            raise ValueError('Duplicate duration docket')
        docket_ids = [r.get('docket') for r in records]
        if len(set(docket_ids)) != len(docket_ids):
            raise ValueError('Duplicate docket in completed group')
        if records and set(by_docket) != set(docket_ids):
            raise ValueError('Completed dockets require exactly matching finalized duration bounds')
        data['coverage'].append([gid, status, route, enum(scope.get('municipal_appeal'), 'scope'),
            enum(scope.get('housing'), 'scope'), category, required, len(set(warnings)), len(flags),
            verdict, error, len(group.get('documents', [])), len(records)])
        for n, flag in enumerate(flags, 1):
            data['review_flags'].append([gid, n, identity(flag.get('docket'))])
        for record in records:
            row, children = structured_appeal(gid, record, by_docket[record.get('docket')], category, required, flags)
            data['all_appeals'].append(row)
            for name, rows in children.items():
                data[name].extend(rows)
    if selected_paths(folder, manifest) != paths or any(
            p.read_bytes() != raw[p] or p.stat().st_mtime_ns != times[p] for p in paths):
        raise ValueError('Run inputs changed while building snapshot')
    inputs = [{'file': str(p.relative_to(folder)), 'sha256': sha(raw[p]), 'mtime_ns': times[p]} for p in paths]
    provenance = {'run_name': identity(manifest.get('name'), False), 'pipeline_schema': 'appeals-1',
        'extraction_schema': 'v6', 'prompt_sha256': manifest.get('prompt_sha256'),
        'source_hashes': [{'group_id': g['group_id'], 'sha256': g.get('source_sha256')} for g in manifest['groups']],
        'input_hashes': inputs}
    return data, provenance


def build_snapshot(run_dir, output):
    run_dir, output = Path(run_dir).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('Snapshot already exists; choose a new output directory')
    if output == run_dir or run_dir in output.parents:
        raise ValueError('Snapshot must be outside the selected pipeline run')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.'+output.name+'-', dir=output.parent))
    try:
        with run_lock(run_dir):
            data, provenance = load_run(run_dir)
            db_path = temporary/'facts.sqlite'
            with sqlite3.connect(db_path) as db:
                db.execute('PRAGMA foreign_keys=ON')
                for sql in SQL_SCHEMA.values():
                    db.execute(sql)
                for name, rows in data.items():
                    db.executemany('INSERT INTO '+name+' VALUES ('+','.join('?' for _ in TABLES[name])+')', rows)
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchall():
                    raise ValueError('Snapshot integrity check failed')
            database_hash = sha(db_path.read_bytes())
            manifest = {'snapshot_version': VERSION,
                'snapshot_id': sha(json.dumps([VERSION, provenance, database_hash], sort_keys=True).encode()),
                'created_at': datetime.now(timezone.utc).isoformat(), **provenance,
                'database_sha256': database_hash,
                'table_counts': {name: len(rows) for name, rows in data.items()},
                'scope_counts': {scope: sum(r[3] == scope for r in data['all_appeals'])
                                 for scope in ('confirmed', 'uncertain', 'excluded')}}
            (temporary/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
            original_paths = [run_dir/item['file'] for item in provenance['input_hashes']]
            for item, path in zip(provenance['input_hashes'], original_paths):
                if sha(path.read_bytes()) != item['sha256'] or path.stat().st_mtime_ns != item['mtime_ns']:
                    raise ValueError('Run inputs changed while building snapshot')
            if selected_paths(run_dir, json.loads((run_dir/'manifest.json').read_bytes())) != original_paths:
                raise ValueError('Run checkpoints changed while building snapshot')
            db_path.chmod(0o444)
            (temporary/'manifest.json').chmod(0o444)
            # mkdir is an exclusive reservation; os.replace cannot silently
            # overwrite another snapshot built concurrently at this destination.
            output.mkdir()
            try:
                os.replace(temporary, output)
            except BaseException:
                output.rmdir()
                raise
        return manifest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


class ResearchSnapshot:
    """Pinned memory image. Each SQL call gets an isolated read-only connection."""
    def __init__(self, folder):
        folder = Path(folder).resolve()
        self.manifest = json.loads((folder/'manifest.json').read_bytes())
        self.image = (folder/'facts.sqlite').read_bytes()
        if self.manifest.get('snapshot_version') != VERSION or sha(self.image) != self.manifest.get('database_sha256'):
            raise ValueError('Unsupported or altered snapshot')
        provenance = {key: self.manifest[key] for key in ('run_name', 'pipeline_schema', 'extraction_schema',
                     'prompt_sha256', 'source_hashes', 'input_hashes')}
        expected_id = sha(json.dumps([VERSION, provenance, sha(self.image)], sort_keys=True).encode())
        if self.manifest.get('snapshot_id') != expected_id:
            raise ValueError('Altered snapshot identity')
        db = sqlite3.connect(':memory:')
        try:
            db.deserialize(self.image)
            actual = dict(db.execute("SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
            if actual != SQL_SCHEMA:
                raise ValueError('Unexpected snapshot schema')
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Snapshot integrity check failed')
            self.functions = FUNCTIONS & {row[0].lower() for row in db.execute('PRAGMA function_list')}
        finally:
            db.close()

    def describe(self):
        schemas = {name: [{'name': c, 'type': t.split()[0], 'meaning': meaning}
                          for c, t, meaning in fields] for name, fields in TABLES.items()}
        for name in VIEWS:
            schemas[name] = schemas['all_appeals']
        header = {key: self.manifest[key] for key in ('snapshot_version', 'snapshot_id', 'created_at',
                  'run_name', 'pipeline_schema', 'extraction_schema', 'prompt_sha256',
                  'database_sha256', 'table_counts', 'scope_counts')}
        return {'snapshot': header, 'tables': schemas, 'views': VIEWS,
            'categories': ENUMS, 'allowed_functions': sorted(self.functions), 'sqlite_version': sqlite3.sqlite_version,
            'joins': ['Child tables join appeals on appeal_id; review_flags joins coverage on group_id.',
                      'timeline_dockets joins timeline_events on appeal_id AND event_ordinal=ordinal.',
                      'related_dockets.docket is not a foreign key and may refer to records outside this snapshot.'],
            'limits': {'sql_characters': MAX_SQL, 'rows': MAX_ROWS, 'response_bytes': MAX_BYTES, 'seconds': MAX_SECONDS},
            'limitations': LIMITATIONS}

    def authorize(self, action, first, second, database, source):
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and (second or '').lower() in self.functions:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ and database in ('main', None):
            fields = TABLES.get(first) or (TABLES['all_appeals'] if first in VIEWS else None)
            if fields and (second == '' or second in {c for c, _, _ in fields}):
                return sqlite3.SQLITE_OK
            # SQLite emits a READ with no database/column for row counting in a
            # compiler CTE. No values are read. Exact schema validation means
            # every physical table is already covered above; block metadata and
            # pragma virtual tables even for these empty-column callbacks.
            if database is None and second == '' and first and not first.lower().startswith(('sqlite_', 'pragma_')):
                return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def query(self, sql, parameters=None, max_rows=200):
        if not isinstance(sql, str) or not sql.strip() or len(sql) > MAX_SQL:
            raise ValueError('SQL must be nonempty and at most 16000 characters')
        if type(max_rows) is not int or not 1 <= max_rows <= MAX_ROWS:
            raise ValueError('max_rows must be between 1 and 1000')
        if parameters is None:
            parameters = []
        if not isinstance(parameters, (list, dict)) or len(parameters) > 1000:
            raise ValueError('Parameters must be a list or object with at most 1000 values')
        if isinstance(parameters, dict) and any(not isinstance(k, str) or len(k) > 100 for k in parameters):
            raise ValueError('Invalid parameter name')
        for value in parameters.values() if isinstance(parameters, dict) else parameters:
            if value is not None and type(value) not in (str, int, float, bool):
                raise ValueError('Parameters must be scalar JSON values')
            if type(value) is float and not math.isfinite(value):
                raise ValueError('Parameters must be finite')
            if isinstance(value, str) and len(value) > MAX_SQL:
                raise ValueError('Parameter too long')
            if type(value) is int and not -2**63 <= value < 2**63:
                raise ValueError('Parameter integer out of range')
        if len(json.dumps(parameters, ensure_ascii=False).encode()) > MAX_SQL:
            raise ValueError('Parameters too large')
        result = {'snapshot_id': self.manifest['snapshot_id'], 'run_name': self.manifest['run_name'],
                  'sql': sql, 'parameters': parameters, 'columns': [], 'rows': [], 'row_count': 0,
                  'truncated': False, 'truncation_reason': None, 'limitations': LIMITATIONS}
        started = time.monotonic()
        db = sqlite3.connect(':memory:')
        try:
            db.deserialize(self.image)
            db.execute('PRAGMA query_only=ON')
            db.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
            db.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DDL, False)
            db.enable_load_extension(False)
            for category, limit in [(sqlite3.SQLITE_LIMIT_LENGTH, MAX_BYTES), (sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL),
                    (sqlite3.SQLITE_LIMIT_COLUMN, 100), (sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 100),
                    (sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 20), (sqlite3.SQLITE_LIMIT_VDBE_OP, 200000),
                    (sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 1000), (sqlite3.SQLITE_LIMIT_ATTACHED, 0),
                    (sqlite3.SQLITE_LIMIT_FUNCTION_ARG, 32)]:
                db.setlimit(category, limit)
            db.set_authorizer(self.authorize)
            # Both a deadline and an instruction budget bound recursive/join work.
            steps = 0
            def progress():
                nonlocal steps
                steps += 1000
                return int(steps > 5_000_000 or time.monotonic()-started > MAX_SECONDS)
            db.set_progress_handler(progress, 1000)
            cursor = db.execute(sql, parameters)
            if cursor.description is None:
                raise ValueError('Only result-producing SELECT queries are allowed')
            result['columns'] = [col[0] for col in cursor.description]
            base_size = len(json.dumps(result, ensure_ascii=False).encode())
            used = base_size
            for row in cursor:
                if len(result['rows']) >= max_rows:
                    result.update(truncated=True, truncation_reason='row_limit')
                    break
                if any(isinstance(v, bytes) for v in row):
                    raise ValueError('Binary result values are not supported')
                if any(type(v) is float and not math.isfinite(v) for v in row):
                    raise ValueError('Nonfinite result values are not supported')
                row_size = len(json.dumps(row, ensure_ascii=False).encode())+2
                if used+row_size > MAX_BYTES-200:
                    result.update(truncated=True, truncation_reason='byte_limit')
                    break
                result['rows'].append(list(row))
                used += row_size
                if time.monotonic()-started > MAX_SECONDS:
                    raise ValueError('Query execution limit reached')
            result['row_count'] = len(result['rows'])
            return result
        except (sqlite3.Error, MemoryError, OverflowError) as exc:
            # No record, source content, filepath, or underlying DB detail leaks.
            raise ValueError('SQL rejected, invalid, or execution limit reached; check describe_dataset for allowed columns and functions') from None
        finally:
            db.close()
