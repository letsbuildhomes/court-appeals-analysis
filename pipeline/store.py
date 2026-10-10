"""Content-addressed model calls with bounded spending.

A call's key is the hash of the provider identity, the full request and a
sample number. Identical work is reused whatever the pipeline's shape or step
names. Sample 0 is the default; asking for another sample of the same request
measures run-to-run variation.

``CallStore`` keeps calls in one shared directory, and it can read through
to indexes of earlier runs' saved calls, such as the production run's. An
index is read-only: a call made again under an indexed key is stored in the
store, which takes precedence.

``Calls`` is one run's view of the store. It keeps the guarantees of the
earlier per-run client:

- Before each new call it reserves the provider's worst-case cost and refuses
  the call if the run's cap on new spending would be exceeded, so nothing is
  sent.
- A failed or uncertain delivery is never resent automatically. Retrying is
  explicit, at most once per call per execution, and earlier attempts are
  archived with their charges.
- Reused calls are free and do not count toward the cap.
- A dry run records what each new call could cost and raises ``DryRunMiss``
  instead of sending.

The run's ledger (``ledger.jsonl``) records each call it made or reused.
"""
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

from appeal_runtime import read, save
from pipeline.providers import REJECTED_BEFORE_GENERATION

SUFFIXES = ('.request.json', '.result.json', '.parsed.json', '.response.json', '.response.txt', '.sent.json',
            '.format-repair-note.json', '.original-parsed.json', '.recovery.json')
DELIVERED = ('ok',)


class DryRunMiss(Exception):
    """A dry run reached a call it would have to make."""


class BudgetRefused(Exception):
    """A new call was refused before sending because it could exceed the cap."""


class DeliveryFailed(Exception):
    """A saved delivery failed or is uncertain and will not be resent automatically."""


def call_key(provider, request, sample=0):
    identity = {'kind': provider.kind, 'model': provider.model}
    raw = json.dumps({'provider': identity, 'request': request, 'sample': sample}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class CallStore:
    def __init__(self, root, indexes=None):
        self.root = Path(root)
        self.indexes = {}           # index name -> {key: saved call prefix}
        for path in indexes if indexes is not None else sorted((self.root/'index').glob('*.json')):
            self.indexes[Path(path).stem] = read(path)['calls']
        self.lock = threading.RLock()

    def prefix(self, key):
        return self.root/'objects'/key[:2]/key

    def paths(self, key):
        prefix = self.prefix(key)
        return [Path(str(prefix)+s) for s in ('.request.json', '.result.json', '.parsed.json')]

    def indexed(self, key):
        """``(index name, prefix)`` of an indexed delivery, or None."""
        for name, calls in self.indexes.items():
            if key in calls:
                return name, Path(calls[key])
        return None

    def charge(self, prefix):
        path = Path(str(prefix)+'.result.json')
        return read(path).get('charged_or_reserved_usd') or 0 if path.exists() else 0

    def files(self):
        """A cross-process lock for bookkeeping; never held while waiting on a provider."""
        self.root.mkdir(parents=True, exist_ok=True)
        return (self.root/'.lock').open('a')


def build_index(store_root, name, calls_dir, provider):
    """Index a saved run's delivered calls by content key, without modifying them.

    A call counts as delivered when its response was complete and decoded,
    including one a later validation rejected: its answer is the same answer a
    rebuilt request would receive.
    """
    calls_dir, entries, skipped = Path(calls_dir).resolve(), {}, 0
    for result_path in sorted(calls_dir.glob('*.result.json')):
        prefix = Path(str(result_path)[:-len('.result.json')])
        request_path, parsed_path = Path(str(prefix)+'.request.json'), Path(str(prefix)+'.parsed.json')
        state = read(result_path)
        delivered = state.get('status') == 'ok' or state.get('error_type') == 'validation'
        if not delivered or not request_path.exists() or not parsed_path.exists():
            skipped += 1
            continue
        entries[call_key(provider, read(request_path))] = str(prefix)
    index = {'name': name, 'source': str(calls_dir), 'provider': {'kind': provider.kind, 'model': provider.model},
             'created_at': datetime.now(timezone.utc).isoformat(), 'calls': entries}
    path = Path(store_root)/'index'/f'{name}.json'
    if path.exists():
        raise ValueError(f'Index {name} already exists')
    save(path, index)
    return {'indexed': len(entries), 'skipped': skipped, 'path': str(path)}


class Calls:
    def __init__(self, store, ledger=None, run=None, limit=0.0, multiplier=1, retry_failed=False,
                 retry_max_tokens=None, dry_run=False):
        self.store, self.run = store, run
        self.ledger_path = Path(ledger) if ledger else None
        self.limit, self.multiplier = limit, multiplier
        self.retry_failed, self.retry_max_tokens = retry_failed, retry_max_tokens
        self.dry_run, self.retried = dry_run, set()
        self.entries = read_ledger(self.ledger_path) if self.ledger_path and self.ledger_path.exists() else []

    # Ledger and spending --------------------------------------------------
    def log(self, **entry):
        entry['at'] = datetime.now(timezone.utc).isoformat()
        self.entries.append(entry)
        if self.ledger_path and not self.dry_run:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open('a') as f:
                f.write(json.dumps(entry, ensure_ascii=False)+'\n')

    def spent(self):
        """New spending and reservations by this run, including archived attempts."""
        total, seen = 0.0, set()
        for entry in self.entries:
            if entry['event'] == 'new' and entry['key'] not in seen:
                seen.add(entry['key'])
                path = Path(str(self.store.prefix(entry['key']))+'.result.json')
                if path.exists() and read(path).get('run') == self.run:
                    total += read(path).get('charged_or_reserved_usd') or 0
            elif entry['event'] == 'archived':
                path = Path(entry['path'])
                state = read(path) if path.exists() else {}
                if state.get('run') == self.run:
                    total += state.get('charged_or_reserved_usd') or 0
        return total

    def bound(self, provider, request):
        return provider.bound(request)*self.multiplier

    def would_spend(self):
        return sum(e['usd'] for e in self.entries if e['event'] == 'would_send')

    # Calls ----------------------------------------------------------------
    def call(self, provider, request, sample=0, retry=False, allow_repair=True):
        """Return ``(value, key, kind)``; reuse identical work, otherwise send under the cap.

        ``retry`` resends even a delivered call, for an answer validation rejected.
        """
        key = call_key(provider, request, sample)
        if retry and key in self.retried:
            raise DeliveryFailed(f'{key}: already retried in this execution')
        prefix = self.store.prefix(key)
        req, result, parsed = self.store.paths(key)
        with self.store.lock, self.store.files() as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            resending, repair = retry, None
            if not req.exists():
                found = self.store.indexed(key)
                if found and not retry:
                    name, saved = found
                    self.log(event='reused', key=key, source='index:'+name, usd=self.store.charge(saved))
                    return read(Path(str(saved)+'.parsed.json')), key, 'reused'
            else:
                if read(req) != request:
                    raise ValueError(f'{key}: stored request differs from its key')
                state = read(result) if result.exists() else {}
                if state.get('status') in DELIVERED and not retry:
                    self.log(event='reused', key=key, source='store', usd=state.get('charged_or_reserved_usd') or 0)
                    return read(parsed), key, 'reused'
                if not retry and self.retry_failed and key not in self.retried:
                    resending = True
                elif not retry and state.get('error') == 'Invalid JSON object' and allow_repair \
                        and Path(str(prefix)+'.response.json').exists():
                    repair = state
                elif not retry:
                    raise DeliveryFailed('Previous delivery incomplete or failed; no automatic resend; use --retry-failed')
            if repair is None:
                sent = copy.deepcopy(request)
                if resending and self.retry_max_tokens and 'max_tokens' in sent:
                    sent['max_tokens'] = self.retry_max_tokens
                bound = self.bound(provider, sent)
                if self.dry_run:
                    self.log(event='would_send', key=key, usd=bound)
                    raise DryRunMiss(key)
                if self.spent()+bound > self.limit:
                    raise BudgetRefused('Run spending cap reached; raise --max-spend to resume')
                if resending:
                    self.retried.add(key)
                    if req.exists():
                        # Keep every prior attempt, including its charge or uncertain-delivery reservation.
                        archive = self.store.root/'attempts'/uuid.uuid4().hex
                        archive.mkdir(parents=True)
                        for suffix in SUFFIXES:
                            old = Path(str(prefix)+suffix)
                            if old.exists():
                                old.rename(archive/old.name)
                        self.log(event='archived', key=key, path=str(archive/f'{key}.result.json'))
                state = {'status': 'pending', 'charged_or_reserved_usd': bound, 'run': self.run,
                         'requested_at': datetime.now(timezone.utc).isoformat(),
                         'price_multiplier': self.multiplier, 'provider': provider.identity(), 'sample': sample}
                save(result, state)
                save(req, request)
                save(Path(str(prefix)+'.sent.json'), sent)
                self.log(event='new', key=key, usd=bound)
        if repair is not None:
            return self.repair(provider, key, request, repair), key, 'repaired'
        return self.send(provider, key, request, sent, state, allow_repair), key, 'retried' if resending else 'new'

    def send(self, provider, key, request, sent, state, allow_repair):
        prefix = self.store.prefix(key)
        result, parsed = Path(str(prefix)+'.result.json'), Path(str(prefix)+'.parsed.json')
        began = time.monotonic()
        try:
            response = provider.post(sent)
            state['http_status'] = response.status_code
            # Preserve non-JSON error bodies as well as normal provider responses.
            if isinstance(response.text, str):
                Path(str(prefix)+'.response.txt').write_text(response.text)
            body = response.json()
            save(Path(str(prefix)+'.response.json'), body)
            usage = provider.usage(body)
            state['usage'] = usage
            charge = provider.price(usage)
            if charge is not None:
                state['charged_or_reserved_usd'] = charge*self.multiplier
            elif response.status_code in REJECTED_BEFORE_GENERATION:
                state['charged_or_reserved_usd'] = 0
                state['cost_note'] = 'Request rejected before generation; no usage reported'
            if response.status_code != 200:
                raise ValueError(f'HTTP {response.status_code}: {body.get("error", {})}')
            outcome = provider.outcome(body)
            state['finish_reason'] = outcome.finish_reason
            if not outcome.complete:
                state['error_type'] = outcome.error_type
                raise ValueError('Incomplete model response; use --retry-failed (and optionally --retry-max-output-tokens)')
            value, repairs = provider.decode(outcome.content)
            if value is None:
                if not allow_repair or 'json_repair' not in provider.capabilities:
                    raise ValueError('Invalid JSON object')
                state['json_repair'] = True
                # Persist the completed delivery before any bounded format repair.
                state.update(status='failed', error='Invalid JSON object')
                save(result, state)
                value, repair_key, _ = self.call(provider, provider.repair_request(request, outcome.content), allow_repair=False)
                state['json_repair_key'] = repair_key
            save(parsed, value)
            state.update(status='ok', json_repairs=repairs)
            state.pop('error', None)
            return value
        except Exception as exc:
            import httpx
            state.setdefault('error_type', type(exc).__name__)
            if state.get('json_repair') and state.get('error') == 'Invalid JSON object':
                state.update(status='failed', repair_error=str(exc))
            else:
                state.update(status='failed', error=str(exc))
            if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
                state['charged_or_reserved_usd'] = 0
            raise
        finally:
            state['elapsed_seconds'] = time.monotonic()-began
            save(result, state)

    def repair(self, provider, key, request, state):
        """Repair a completed delivery whose JSON was invalid, as a separate call."""
        prefix = self.store.prefix(key)
        raw = provider.outcome(read(Path(str(prefix)+'.response.json'))).content
        value, repair_key, _ = self.call(provider, provider.repair_request(request, raw), allow_repair=False)
        save(Path(str(prefix)+'.format-repair-note.json'), state)
        save(Path(str(prefix)+'.parsed.json'), value)
        state.update(status='ok', error=None, json_repair_key=repair_key)
        save(Path(str(prefix)+'.result.json'), state)
        return value


def read_ledger(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
