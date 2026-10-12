"""Research snapshots: one results map, frozen into a read-only SQLite database of facts.

A snapshot is a directory with ``facts.sqlite`` and ``manifest.json``. It
holds only structured facts from the results contract: no prose, quotations,
evidence or source text. Its identity is the hash of its provenance (the
results and case-set dataset IDs and the producing pipeline) and its database.

``ResearchSnapshot`` serves bounded, read-only SQL over a snapshot, in memory,
with an authorizer that admits only the snapshot's own tables and an allowed
function set. It reads nothing but the snapshot.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time

VERSION = 'research-2'
U = 'unknown'
ENUMS = {
    'status': ('complete', 'failed'),
    'disposition': ('in_scope', 'uncertain', 'excluded'),
    'scope': ('yes', 'no', U),
    'decision': ('approved', 'approved_with_conditions', 'denied', 'other', U),
    'relationship': ('developer', 'neighbor', 'organization', 'other', U),
    'seeks': ('overturn_approval', 'overturn_denial', 'change_conditions', 'other', U),
    'representation': ('represented', 'pro_se', U),
    'central': ('yes', 'no', 'not_applicable', U),
    'event': ('filing', 'judgment', 'further_appeal', 'reopening', 'remand', 'resolution'),
    'litigation': ('resolved', 'ongoing', U),
    'permission': ('allowed', 'allowed_with_conditions', 'denied', U),
    'duration': ('exact', 'at_least', U),
}

# Only these columns ever reach SQLite. Identity strings are kept; all prose,
# quotations, evidence, source links and model or error text are absent.
TABLES = {
    'cases': (
        ('case_id', 'TEXT PRIMARY KEY', 'Case identity: one docket group from the case set.'),
        ('status', 'TEXT NOT NULL', 'complete, or failed when the pipeline produced no result for the case.'),
        ('failure_kind', 'TEXT', 'Why a failed case has no result (for example validation or too_large); null when complete.'),
        ('disposition', 'TEXT', 'in_scope/uncertain/excluded; null for a failed case.'),
        ('municipal_appeal', 'TEXT', 'Scope question: yes/no/unknown; null for a failed case.'),
        ('housing', 'TEXT', 'Scope question: yes/no/unknown; null for a failed case.'),
        ('document_count', 'INTEGER NOT NULL', 'Source documents in the case.'),
        ('docket_count', 'INTEGER NOT NULL', 'Docket numbers grouped into the case.'),
        ('record_count', 'INTEGER NOT NULL', 'Appeal records extracted for the case.'),
    ),
    'all_appeals': (
        ('appeal_id', 'TEXT PRIMARY KEY', 'Stable join key from case ID and docket.'),
        ('case_id', 'TEXT NOT NULL REFERENCES cases(case_id)', 'The case the record belongs to.'),
        ('docket', 'TEXT', 'Extracted proceeding identifier; null when unknown.'),
        ('disposition', 'TEXT NOT NULL', 'The case disposition: in_scope or uncertain. The appeals view selects in_scope.'),
        ('municipality', 'TEXT', 'Literal extracted municipality, not canonicalized; null when unknown.'),
        ('proposed_units', 'INTEGER', 'Proposed new/replacement/additionally authorized dwellings; not net additions or built homes.'),
        ('proposed_lots', 'INTEGER', 'Proposed new residential building lots; not all subdivision lots or dwelling units.'),
        ('municipal_decision_date', 'TEXT', 'Challenged municipal decision date; YYYY-MM-DD, YYYY-MM, YYYY, or null.'),
        ('municipal_decision_outcome', 'TEXT NOT NULL', 'approved/approved_with_conditions/denied/other/unknown.'),
        ('discretion_involved', 'TEXT NOT NULL', 'Established evaluative municipal contention: yes/no/unknown.'),
        ('discretion_central', 'TEXT NOT NULL', 'Principal discretionary contention: yes/no/not_applicable/unknown.'),
        ('litigation_status', 'TEXT NOT NULL', 'Latest observed linked adjudication: resolved/ongoing/unknown.'),
        ('permission', 'TEXT NOT NULL', 'Latest observed proposal authorization: allowed/allowed_with_conditions/denied/unknown; not appellant success.'),
        ('representation', 'TEXT NOT NULL', 'All appellants: represented/pro_se/mixed/unknown. Any unknown appellant or none listed gives unknown.'),
        ('appellant_count', 'INTEGER NOT NULL', 'Listed appellants; zero means none was established, not that there were none.'),
        ('related_docket_count', 'INTEGER NOT NULL', 'Listed related proceedings.'),
        ('timeline_count', 'INTEGER NOT NULL', 'Listed material events, not a complete chronology.'),
        ('restriction_count', 'INTEGER NOT NULL', 'Listed explicit restrictions; not days of construction delay.'),
        ('housing_change_count', 'INTEGER NOT NULL', 'Listed explicit imposed changes; no inferred lost homes.'),
        ('duration_kind', 'TEXT NOT NULL', 'exact/at_least/unknown. at_least is a minimum, never a complete duration.'),
        ('duration_days', 'INTEGER', 'End minus start in days; null when unknown.'),
        ('duration_start_date', 'TEXT', 'Full evidenced start date; null if unknown.'),
        ('duration_start_relation', 'TEXT', 'exact or on_or_before.'),
        ('duration_end_date', 'TEXT', 'Full evidenced end date; never extrapolated to today.'),
        ('duration_end_relation', 'TEXT', 'exact or on_or_after.'),
    ),
    'appellants': (
        ('appeal_id', 'TEXT NOT NULL REFERENCES all_appeals(appeal_id)', 'Join to one appeal.'),
        ('ordinal', 'INTEGER NOT NULL', '1-based position within the extracted list, not rank.'),
        ('name', 'TEXT', 'Literal extracted appellant identity; not deduplicated across cases; null when unknown.'),
        ('relationship', 'TEXT NOT NULL', 'developer/neighbor/organization/other/unknown.'),
        ('seeks', 'TEXT NOT NULL', 'overturn_approval/overturn_denial/change_conditions/other/unknown; requested relief, not success.'),
        ('representation', 'TEXT NOT NULL', 'represented/pro_se/unknown for this appellant.'),
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
    ),
    'timeline_dockets': (
        ('appeal_id', 'TEXT NOT NULL REFERENCES all_appeals(appeal_id)', 'Originating appeal, not the linked docket.'),
        ('event_ordinal', 'INTEGER NOT NULL', 'Join to timeline_events on appeal_id AND ordinal.'),
        ('ordinal', 'INTEGER NOT NULL', '1-based position among this event’s docket links.'),
        ('docket', 'TEXT NOT NULL', 'Literal linked proceeding identifier.'),
    ),
    'restrictions': (
        ('appeal_id', 'TEXT NOT NULL REFERENCES all_appeals(appeal_id)', 'Join to one appeal.'),
        ('ordinal', 'INTEGER NOT NULL', '1-based list position.'),
        ('start_date', 'TEXT', 'Explicit restriction start: full/partial date or null.'),
        ('end_date', 'TEXT', 'Explicit restriction end: full/partial date or null; null does not mean ongoing.'),
    ),
}
VIEWS = {
    'appeals': "SELECT * FROM all_appeals WHERE disposition = 'in_scope'",
    'uncertain_appeals': "SELECT * FROM all_appeals WHERE disposition = 'uncertain'",
}
KEYS = {'timeline_dockets': 'appeal_id, event_ordinal, ordinal'}
SQL_SCHEMA = {
    **{name: 'CREATE TABLE '+name+' ('+', '.join(c+' '+t for c, t, _ in fields)+
       (', PRIMARY KEY ('+KEYS.get(name, 'appeal_id, ordinal')+')' if name not in ('cases', 'all_appeals') else '')+')'
       for name, fields in TABLES.items()},
    **{name: 'CREATE VIEW '+name+' AS '+query for name, query in VIEWS.items()},
}
LIMITATIONS = [
    'The appeals view includes only cases whose disposition is in scope; uncertain_appeals and cases expose uncertainty, exclusions and failures.',
    'Observed permission is not appeal success/failure, construction, abandonment, or causal construction delay. No failed-appeal outcome is coded.',
    'Duration minima must stay separate from exact durations; unknown is not zero and ongoing is not extended to today.',
    'One record per (case_id, docket); related appeals may concern the same proposal. Summing units/lots does not count distinct homes or net additions.',
    'Child-table joins multiply appeals. Filter with EXISTS or aggregate at appeal_id before counting/averaging. Municipalities and names are literal, not canonical.',
    'Empty lists mean no established observation, not absence. Housing changes have no quantitative impact coding.',
    'The snapshot does not reclassify source evidence or expose any narrative text.',
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


def known(value):
    return None if value == U else value


def rows(results, cases):
    """Table rows from a results map and its case set."""
    data = {name: [] for name in TABLES}
    for case_id in sorted(results):
        value, case = results[case_id], cases.get(case_id) or {'documents': [], 'dockets': []}
        failed = not isinstance(value, dict)
        disposition = None if failed else value['disposition']
        records = [] if failed else value['records']
        data['cases'].append([case_id, 'failed' if failed else 'complete', value.kind if failed else None,
                              (disposition or {}).get('value'), (disposition or {}).get('municipal_appeal'),
                              (disposition or {}).get('housing'), len(case['documents']), len(case['dockets']),
                              len(records)])
        for record in records:
            docket = known(record['docket'])
            aid = sha(json.dumps([case_id, docket], ensure_ascii=False).encode())[:32]
            project, decision = record['project'], record['municipal_decision']
            discretion, status, duration = record['discretion'], record['observed_status'], record['duration']
            reps = {a['representation'] for a in record['appellants']}
            representation = U if not reps or U in reps else 'mixed' if len(reps) == 2 else next(iter(reps))
            points = []
            for side in ('start', 'end'):
                point = duration[side]
                points += [None, None] if point == U else [point['date'], point['relation']]
            data['all_appeals'].append([
                aid, case_id, docket, disposition['value'], known(project['municipality']),
                known(project['proposed_units']), known(project['proposed_lots']), known(decision['date']),
                decision['outcome'], discretion['involved'], discretion['central'], status['litigation'],
                status['permission'], representation, len(record['appellants']), len(record['related_dockets']),
                len(record['timeline']), len(record['restrictions']), len(record['housing_changes']),
                duration['kind'], known(duration['days']), *points])
            for n, a in enumerate(record['appellants'], 1):
                data['appellants'].append([aid, n, known(a['name']), a['relationship'], a['seeks'], a['representation']])
            for n, r in enumerate(record['related_dockets'], 1):
                data['related_dockets'].append([aid, n, known(r['docket'])])
            for n, e in enumerate(record['timeline'], 1):
                data['timeline_events'].append([aid, n, e['kind'], known(e['date'])])
                for j, linked in enumerate(e['dockets'], 1):
                    data['timeline_dockets'].append([aid, n, j, linked])
            for n, r in enumerate(record['restrictions'], 1):
                data['restrictions'].append([aid, n, known(r['start']), known(r['end'])])
    return data


def build(results, cases, output, provenance):
    """Write a new snapshot directory from a results map and its case set; never overwrite."""
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Snapshot already exists; choose a new output directory')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.'+output.name+'-', dir=output.parent))
    try:
        data = rows(results, cases)
        db_path = temporary/'facts.sqlite'
        with sqlite3.connect(db_path) as db:
            db.execute('PRAGMA foreign_keys=ON')
            for sql in SQL_SCHEMA.values():
                db.execute(sql)
            for name, values in data.items():
                db.executemany('INSERT INTO '+name+' VALUES ('+','.join('?' for _ in TABLES[name])+')', values)
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('Snapshot integrity check failed')
        db.close()
        database_hash = sha(db_path.read_bytes())
        manifest = {'snapshot_version': VERSION,
                    'snapshot_id': sha(json.dumps([VERSION, provenance, database_hash], sort_keys=True).encode()),
                    'created_at': datetime.now(timezone.utc).isoformat(), 'provenance': provenance,
                    'database_sha256': database_hash, 'table_counts': {n: len(v) for n, v in data.items()},
                    'disposition_counts': {d: sum(r[3] == d for r in data['cases'])
                                           for d in ('in_scope', 'uncertain', 'excluded')},
                    'failed_cases': sum(r[1] == 'failed' for r in data['cases'])}
        (temporary/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
        db_path.chmod(0o444)
        (temporary/'manifest.json').chmod(0o444)
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
        expected_id = sha(json.dumps([VERSION, self.manifest.get('provenance'), sha(self.image)],
                                     sort_keys=True).encode())
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
        header = {key: self.manifest[key] for key in ('snapshot_version', 'snapshot_id', 'created_at', 'provenance',
                  'database_sha256', 'table_counts', 'disposition_counts', 'failed_cases')}
        return {'snapshot': header, 'tables': schemas, 'views': VIEWS,
            'categories': ENUMS, 'allowed_functions': sorted(self.functions), 'sqlite_version': sqlite3.sqlite_version,
            'joins': ['Child tables join appeals on appeal_id; all_appeals joins cases on case_id.',
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
        result = {'snapshot_id': self.manifest['snapshot_id'], 'results': self.manifest['provenance']['results'],
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
