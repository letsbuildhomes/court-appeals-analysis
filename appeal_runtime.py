"""Atomic checkpoints, bounded API spending, and immutable replay.

``Client`` is the only path to a model. Before each call it reserves the
provider's worst-case cost and refuses the call if the run's cap would be
exceeded, so nothing is sent. Every request, response, usage and charge is
saved under the call's name, and an identical request reuses its saved answer
for free. Failed or uncertain deliveries are never resent automatically.

With ``inherit`` a client reads through to another run's calls: an identical,
successful saved call is copied in and marked inherited, which costs nothing
and does not count toward the cap. With ``dry_run`` it records what each new
call could cost and raises ``DryRunMiss`` instead of sending.
"""
import copy
from datetime import datetime, timezone
import glob
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import uuid

import httpx
from pipeline.providers import REJECTED_BEFORE_GENERATION

CALL_SUFFIXES = ('.request.json', '.result.json', '.parsed.json', '.response.json', '.response.txt', '.sent.json',
                 '.format-repair-note.json', '.original-parsed.json', '.recovery.json')


class DryRunMiss(Exception):
    """A dry run reached a call it would have to make."""


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
        json.dump(value, f, indent=2, ensure_ascii=False)
        f.write('\n')
        tmp = f.name
    os.replace(tmp, path)


def read(path):
    return json.loads(Path(path).read_text())


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Client:
    def __init__(self, root, provider, limit, multiplier=1, retry_failed=False, retry_max_tokens=None,
                 inherit=None, lock=None, dry_run=False):
        self.root, self.provider = Path(root), provider
        self.limit, self.multiplier = limit, multiplier
        # Clients for different providers in one run share a lock, so their
        # reservations are checked against the same cap one at a time.
        self.lock = lock or threading.RLock()
        self.retry_failed, self.retry_max_tokens = retry_failed, retry_max_tokens
        self.inherit = Path(inherit) if inherit else None
        self.dry_run, self.ledger = dry_run, []
        self.retried = set()

    def reject(self, name, error):
        """Persist downstream schema/validation failure so it can be retried."""
        if 'frozen request changed' in str(error) or isinstance(error, DryRunMiss):
            return  # The saved result still belongs to a valid, different request.
        path = self.root/(name+'.result.json')
        if path.exists():
            state = read(path)
            if state.get('status') == 'ok':
                state.update(status='failed', error=str(error), error_type='validation')
                save(path, state)

    def spent(self):
        """New spending and reservations; inherited calls were paid by their own run."""
        total = 0
        for path in self.root.rglob('*.result.json'):
            state = read(path)
            if not state.get('inherited_from'):
                total += state['charged_or_reserved_usd']
        return total

    def bound(self, request):
        return self.provider.bound(request)*self.multiplier

    def inherit_call(self, name, request, aliases=()):
        """Copy an identical successful call from the inherited run; True if copied.

        ``aliases`` are earlier names for the same call; a match is stored
        under the current name.
        """
        for saved in (name, *aliases):
            source, result = self.inherit/f'{saved}.request.json', self.inherit/f'{saved}.result.json'
            if source.exists() and result.exists() and read(source) == request and read(result).get('status') == 'ok':
                break
        else:
            return False
        self.root.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.inherit.glob(glob.escape(saved)+'.*')):
            if path.is_file():
                target = self.root/(name+path.name[len(saved):])
                shutil.copy2(path, target)
                if target.name.endswith('.result.json'):
                    state = read(target)
                    state['inherited_from'] = str(self.inherit/saved)
                    save(target, state)
        self.ledger.append({'name': name, 'kind': 'inherited',
                            'usd': read(self.root/f'{name}.result.json').get('charged_or_reserved_usd') or 0})
        return True

    def recover_validation(self, name, request, recover):
        """Revalidate a saved schema failure before considering a paid retry."""
        prefix = self.root/name
        req, result, parsed = [Path(str(prefix)+suffix) for suffix in
                               ('.request.json', '.result.json', '.parsed.json')]
        with self.lock:
            if not all(p.exists() for p in (req, result, parsed)):
                return False
            state = read(result)
            if read(req) != request or state.get('error_type') != 'validation' or state.get('status') != 'failed':
                return False
            try:
                value, notes = recover(read(parsed))
            except ValueError:
                return False
            # Preserve both the original parsed output and the failed state.
            original = Path(str(prefix)+'.original-parsed.json')
            if not original.exists():
                save(original, read(parsed))
            save(Path(str(prefix)+'.recovery.json'), {
                'recovered_at': datetime.now(timezone.utc).isoformat(),
                'previous_result': state, 'corrections': notes})
            save(parsed, value)
            state.update(status='ok', validation_recovered=True)
            state.pop('error', None)
            state.pop('error_type', None)
            save(result, state)
            return True

    def fix_json(self, name, request, raw):
        return self.call(name+'.json-repair', self.provider.repair_request(request, raw), allow_repair=False)

    def call(self, name, request, allow_repair=True, aliases=()):
        prefix = self.root/name
        req, result, parsed = [Path(str(prefix)+suffix) for suffix in
                               ('.request.json', '.result.json', '.parsed.json')]
        with self.lock:
            if not req.exists() and self.inherit is not None and self.inherit_call(name, request, aliases):
                return read(parsed)
            retrying = False
            if req.exists():
                if read(req) != request:
                    raise ValueError(f'{name}: frozen request changed; create a new run')
                if result.exists() and read(result).get('status') == 'ok':
                    return read(parsed)
                if self.retry_failed and name not in self.retried:
                    proposed = request
                    if 'max_tokens' in request:
                        proposed = {**request, 'max_tokens': self.retry_max_tokens or request['max_tokens']}
                    if self.spent()+self.bound(proposed) > self.limit:
                        raise ValueError('Run spending cap reached; raise --max-spend to resume')
                    # Keep every prior attempt, including its charge or uncertain-delivery reservation.
                    archive = self.root/'attempts'/uuid.uuid4().hex
                    archive.mkdir(parents=True)
                    for suffix in CALL_SUFFIXES:
                        old = Path(str(prefix)+suffix)
                        if old.exists():
                            old.rename(archive/old.name)
                    self.retried.add(name)
                    retrying = True
                elif result.exists() and read(result).get('error') == 'Invalid JSON object' and allow_repair:
                    previous = read(result)
                    raw = self.provider.outcome(read(Path(str(prefix)+'.response.json'))).content
                    value = self.fix_json(name, request, raw)
                    save(Path(str(prefix)+'.format-repair-note.json'), previous)
                    save(parsed, value)
                    previous.update(status='ok', error=None, json_repair_stage=name+'.json-repair')
                    save(result, previous)
                    return value
                else:
                    raise ValueError(f'{name}: previous delivery incomplete or failed; no automatic resend; use --retry-failed')
            sent = copy.deepcopy(request)
            if retrying and self.retry_max_tokens and 'max_tokens' in sent:
                sent['max_tokens'] = self.retry_max_tokens
            bound = self.bound(sent)
            if self.dry_run:
                self.ledger.append({'name': name, 'kind': 'new', 'usd': bound})
                raise DryRunMiss(name)
            if self.spent()+bound > self.limit:
                raise ValueError('Run spending cap reached; raise --max-spend to resume')
            state = {'status': 'pending', 'charged_or_reserved_usd': bound,
                     'requested_at': datetime.now(timezone.utc).isoformat(),
                     'price_multiplier': self.multiplier, 'provider': self.provider.identity()}
            save(result, state)
            save(req, request)
            save(Path(str(prefix)+'.sent.json'), sent)
        began = time.monotonic()
        try:
            response = self.provider.post(sent)
            state['http_status'] = response.status_code
            # Preserve non-JSON error bodies as well as normal provider responses.
            if isinstance(response.text, str):
                Path(str(prefix)+'.response.txt').write_text(response.text)
            body = response.json()
            save(Path(str(prefix)+'.response.json'), body)
            usage = self.provider.usage(body)
            state['usage'] = usage
            charge = self.provider.price(usage)
            if charge is not None:
                state['charged_or_reserved_usd'] = charge*self.multiplier
            elif response.status_code in REJECTED_BEFORE_GENERATION:
                state['charged_or_reserved_usd'] = 0
                state['cost_note'] = 'Request rejected before generation; no usage reported'
            if response.status_code != 200:
                raise ValueError(f'HTTP {response.status_code}: {body.get("error", {})}')
            outcome = self.provider.outcome(body)
            state['finish_reason'] = outcome.finish_reason
            if not outcome.complete:
                state['error_type'] = outcome.error_type
                raise ValueError('Incomplete model response; use --retry-failed (and optionally --retry-max-output-tokens)')
            value, repairs = self.provider.decode(outcome.content)
            if value is None:
                if not allow_repair or 'json_repair' not in self.provider.capabilities:
                    raise ValueError('Invalid JSON object')
                state['json_repair_stage'] = name+'.json-repair'
                # Persist the completed delivery before any bounded format repair.
                state.update(status='failed', error='Invalid JSON object')
                save(result, state)
                value = self.fix_json(name, request, outcome.content)
            save(parsed, value)
            state.update(status='ok', json_repairs=repairs)
            state.pop('error', None)
            return value
        except Exception as exc:
            state.setdefault('error_type', type(exc).__name__)
            if state.get('json_repair_stage') and state.get('error') == 'Invalid JSON object':
                state.update(status='failed', repair_error=str(exc))
            else:
                state.update(status='failed', error=str(exc))
            if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
                state['charged_or_reserved_usd'] = 0
            raise
        finally:
            state['elapsed_seconds'] = time.monotonic()-began
            save(result, state)
