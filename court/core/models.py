"""The models service: the only way a step reaches a language model.

Providers render requests, send them, read usage, price them and bound
their worst-case cost. Above them, one policy governs every call:

- **Cache.** A call's key is the hash of the provider kind and model, the
  exact request and a sample number. A delivered answer is stored under its
  key (``data/calls/objects``) and an identical call is answered from the store,
  or from a read-only index of an earlier run's calls, for free.
- **Spending cap.** Before a new call is sent, its worst-case cost is
  reserved against the run's cap; a call that could exceed it is refused
  without sending. A run's spending is read from its own ledger, so each run
  is charged exactly for the attempts it made.
- **No automatic resending.** A failed or uncertain delivery is never resent
  unless the run allows it (``resend_failed``), and then once per key per run,
  counted in the ledger so a resumed run doesn't resend again. Earlier attempts
  are archived, never overwritten.
- **One sender per call.** A per-key lock is held while sending; a call
  another process is sending is reported as in flight, not sent twice.
- **Rejected answers.** A delivered answer a step rejects is final for that
  request. ``ask`` may try further samples (new keys) up to the run's
  ``resamples`` limit; because samples are cached too, a resumed run never
  pays for them again.
- **Dry runs** record what each new call could cost and raise ``NotSent``.
"""
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid
from urllib.parse import urlparse

from court.config import paths
from court.core.hashing import read_json, save_json

REJECTED_BEFORE_GENERATION = (400, 401, 403, 404, 413, 422)
SUFFIXES = ('.request.json', '.result.json', '.parsed.json', '.response.json', '.response.txt', '.sent.json',
            '.format-repair-note.json', '.original-parsed.json', '.recovery.json')
REPAIR_PROMPT = ('The response has invalid JSON syntax. Return the same research content as a valid JSON object '
                 'using the requested schema. Repair brackets, nesting and syntax only; do not add facts or change '
                 'research judgments. Return only JSON.')


class Unavailable(Exception):
    """No answer is available for a call in this run; ``kind`` names why."""
    kind = 'unavailable'


class NotSent(Unavailable):
    """A dry run reached a call it would have to send."""
    kind = 'not_sent'


class BudgetRefused(Unavailable):
    """Refused before sending because it could exceed the run's spending cap."""
    kind = 'budget'


class DeliveryFailed(Unavailable):
    """A delivery failed, is uncertain or is in flight elsewhere, and was not resent."""
    kind = 'delivery'


class Rejected(Exception):
    """Every allowed sample of a request was delivered and rejected by the step."""
    kind = 'validation'


# Conversations ------------------------------------------------------------
@dataclass(frozen=True)
class Conversation:
    """An immutable transcript bound to one provider; continuing keeps a byte-identical prefix."""
    provider: str
    messages: tuple = ()

    @classmethod
    def start(cls, provider, system, user):
        return cls(provider, (('system', system), ('user', user)))

    def extend(self, *messages):
        return Conversation(self.provider, self.messages + tuple(messages))

    def answer(self, content):
        return self.extend(('assistant', content))

    def ask(self, content):
        return self.extend(('user', content))

    def payload(self):
        return [{'role': role, 'content': content} for role, content in self.messages]


# Providers ------------------------------------------------------------------
@dataclass(frozen=True)
class Rates:
    """USD per million tokens."""
    input: float
    output: float = 0.0
    cached_input: float | None = None

    def cost(self, prompt, cached, output):
        hit = cached or 0
        cached_rate = self.input if self.cached_input is None else self.cached_input
        return (hit*cached_rate + (prompt-hit)*self.input + output*self.output)/1_000_000

    def times(self, factor):
        return Rates(self.input*factor, self.output*factor,
                     None if self.cached_input is None else self.cached_input*factor)


# Price functions: a model's rates at a moment. A catalog entry names one under
# "pricing": {"function": NAME, "usd_per_million": {...}, ...its settings}.
class FlatPrice:
    """The same rates at every hour."""

    def __init__(self, usd_per_million):
        self.base = Rates(**usd_per_million)

    def factor(self, at=None):
        return 1

    def rates(self, at=None):
        return self.base.times(self.factor(at))

    def cost(self, prompt, cached, output, at=None):
        return self.rates(at).cost(prompt, cached, output)


class PeakHoursPrice(FlatPrice):
    """Base rates times a multiplier during listed hours on listed weekdays.

    ``peak``: ``{"multiplier": 2, "timezone": "UTC", "weekdays": ["mon", ...],
    "hours": [["01:00", "04:00"], ...]}``; each window includes its start and
    excludes its end. A call is priced at the time it was sent.
    """
    DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')

    def __init__(self, usd_per_million, peak):
        super().__init__(usd_per_million)
        self.multiplier = float(peak['multiplier'])
        self.days = {self.DAYS.index(d) for d in peak['weekdays']}
        self.windows = [(_minutes(start), _minutes(end)) for start, end in peak['hours']]
        zone = peak.get('timezone', 'UTC')
        if zone == 'UTC':
            self.zone = timezone.utc
        else:
            from zoneinfo import ZoneInfo
            self.zone = ZoneInfo(zone)

    def factor(self, at=None):
        local = (at or datetime.now(timezone.utc)).astimezone(self.zone)
        minute = local.hour*60 + local.minute
        peak = local.weekday() in self.days and any(start <= minute < end for start, end in self.windows)
        return self.multiplier if peak else 1


PRICE_FUNCTIONS = {'flat': FlatPrice, 'peak-hours': PeakHoursPrice}


def _minutes(text):
    hours, minutes = text.split(':')
    return int(hours)*60 + int(minutes)


def pricing_from(entry):
    """A catalog entry's price function; ``None`` when it has no price."""
    if 'pricing' in entry:
        settings = dict(entry['pricing'])
        function = settings.pop('function', 'flat')
        if function not in PRICE_FUNCTIONS:
            raise ValueError(f'Unknown price function {function!r}')
        return PRICE_FUNCTIONS[function](**settings)
    if 'pricing_usd_per_million' in entry:
        return FlatPrice(entry['pricing_usd_per_million'])
    return None


@dataclass
class Outcome:
    complete: bool
    content: object
    finish_reason: str | None = None
    error_type: str | None = None


def catalog(path=None):
    return json.loads(Path(path or paths().models).read_text())['models']


class Provider:
    kind = None
    capabilities = frozenset()
    path = ''

    def __init__(self, name, model, pricing, env='APPEALS'):
        self.name, self.model, self.pricing, self.env = name, model, pricing, env
        self.endpoint = self.key = None

    def identity(self):
        return {'provider': self.name, 'kind': self.kind, 'model': self.model}

    def connect(self, base_url=None, api_url=None, key=None):
        """Read credentials; only sending needs them."""
        endpoint = api_url or os.getenv(f'{self.env}_API_URL')
        base = base_url or os.getenv(f'{self.env}_API_BASE_URL')
        if endpoint and base:
            raise ValueError('Set API URL or base URL, not both')
        key = key or os.getenv(f'{self.env}_API_KEY')
        endpoint = endpoint or (base.rstrip('/')+self.path if base else None)
        if not endpoint or not key:
            raise ValueError(f'Require {self.env}_API_BASE_URL (or {self.env}_API_URL) and {self.env}_API_KEY')
        parsed = urlparse(endpoint)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password \
                or parsed.query:
            raise ValueError('API URL must be an HTTP(S) URL without embedded credentials or query parameters')
        self.endpoint, self.key = endpoint, key

    def post(self, request):
        import httpx
        with httpx.Client(timeout=httpx.Timeout(650, connect=30)) as connection:
            return connection.post(self.endpoint, headers={'Authorization': f'Bearer {self.key}'}, json=request)

    def usage(self, body):
        return body.get('usage') if isinstance(body, dict) else None

    def tokens(self, usage):
        """``{input, cached, output}`` token counts from a usage report, when it has them."""
        return None

    def decode(self, content):
        return (content if isinstance(content, dict) else None), []


class ChatCompletionsProvider(Provider):
    """OpenAI-compatible chat completions with JSON object output."""
    kind = 'chat'
    capabilities = frozenset({'conversation', 'json_repair'})
    path = '/chat/completions'

    def render(self, conversation, settings):
        request = {'model': self.model, 'stream': False, 'response_format': {'type': 'json_object'},
                   'messages': conversation.payload()}
        if settings.get('reasoning') is not None:
            request['reasoning_effort'] = settings['reasoning']
        request['max_tokens'] = settings['max_output_tokens']
        return request

    def estimate(self, known_bytes, messages, unknown_tokens, max_tokens, at=None):
        # A byte bounds a token; every message adds framing; output is capped.
        rates = self.pricing.rates(at)
        tokens = known_bytes + 256*messages + 1024 + unknown_tokens
        return (tokens*rates.input + max_tokens*rates.output)/1_000_000

    def bound(self, request, at=None):
        known = sum(len(m['content'].encode('utf-8')) for m in request['messages'])
        return self.estimate(known, len(request['messages']), 0, request['max_tokens'], at)

    def tokens(self, usage):
        if not isinstance(usage, dict) or not isinstance(usage.get('prompt_tokens'), int):
            return None
        details = usage.get('prompt_tokens_details') or {}
        cached = usage.get('prompt_cache_hit_tokens', details.get('cached_tokens') if isinstance(details, dict) else 0)
        return {'input': usage['prompt_tokens'], 'cached': cached if isinstance(cached, int) else 0,
                'output': usage.get('completion_tokens') or 0}

    def price(self, usage, at=None):
        if not isinstance(usage, dict):
            return None
        prompt, output = usage.get('prompt_tokens'), usage.get('completion_tokens')
        if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (prompt, output)):
            return None
        details = usage.get('prompt_tokens_details') or {}
        cached = usage.get('prompt_cache_hit_tokens', details.get('cached_tokens') if isinstance(details, dict) else None)
        if not isinstance(cached, int) or isinstance(cached, bool) or not 0 <= cached <= prompt:
            cached = None
        return self.pricing.cost(prompt, cached, output, at)

    def outcome(self, body):
        choice = body['choices'][0]
        reason = choice.get('finish_reason')
        if reason != 'stop':
            return Outcome(False, choice.get('message', {}).get('content'), reason,
                           'output_limit' if reason == 'length' else 'incomplete_response')
        return Outcome(True, choice['message']['content'], reason)

    def decode(self, content):
        from court.core.json_repair import repair_json
        value, repairs = repair_json(content)
        return (value if isinstance(value, dict) else None), repairs

    def repair_request(self, request, raw):
        repair = json.loads(json.dumps(request))
        repair['messages'] += [{'role': 'assistant', 'content': raw}, {'role': 'user', 'content': REPAIR_PROMPT}]
        return repair


class DecisionProvider(Provider):
    """Stateless typed questions about one state, answered with structured values."""
    kind = 'decision'
    capabilities = frozenset({'typed_questions'})

    def render(self, state, questions):
        return {'model': self.model, 'state': state, 'questions': questions}

    def estimate(self, known_bytes, messages, unknown_tokens, max_tokens, at=None):
        rates = self.pricing.rates(at)
        return ((known_bytes + unknown_tokens + 1024)*rates.input + max_tokens*rates.output)/1_000_000

    def bound(self, request, at=None):
        known = len(json.dumps({k: request[k] for k in ('state', 'questions')}, ensure_ascii=False).encode('utf-8'))
        return self.estimate(known, 1, 0, 0, at)

    def price(self, usage, at=None):
        tokens = (usage or {}).get('input_tokens')
        if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
            return None
        return self.pricing.cost(tokens, None, (usage or {}).get('output_tokens') or 0, at)

    def outcome(self, body):
        answers = body.get('answers')
        ok = isinstance(answers, dict)
        return Outcome(ok, answers, 'answered' if ok else None, None if ok else 'incomplete_response')


PROVIDERS = {'chat-completions': ChatCompletionsProvider, 'decision': DecisionProvider}


def provider_for(model, catalog_path=None):
    entry = catalog(catalog_path).get(model)
    if entry is None:
        raise ValueError(f'Unknown model {model!r}; add it to pipelines/models.json')
    if entry.get('provider') not in PROVIDERS:
        raise ValueError(f'Unknown provider type {entry.get("provider")!r} for model {model!r}')
    pricing = pricing_from(entry)
    if pricing is None:
        raise ValueError(f'No price for model {model!r}, so its spending cannot be capped')
    return PROVIDERS[entry['provider']](model, model, pricing, entry.get('env', 'APPEALS'))


# The call store -------------------------------------------------------------
def call_key(provider, request, sample=0):
    identity = {'kind': provider.kind, 'model': provider.model}
    raw = json.dumps({'provider': identity, 'request': request, 'sample': sample}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class CallStore:
    """Delivered calls by key, plus read-only indexes of earlier runs' calls."""

    def __init__(self, root=None):
        self.root = Path(root or paths().calls)
        self.indexes = {Path(p).stem: read_json(p)['calls'] for p in sorted((self.root/'index').glob('*.json'))}

    def prefix(self, key):
        return self.root/'objects'/key[:2]/key

    def file(self, key, suffix):
        return Path(str(self.prefix(key))+suffix)

    def indexed(self, key):
        """``(index name, saved call prefix)``; index paths are relative to the repository."""
        for name, calls in self.indexes.items():
            if key in calls:
                return name, paths().root/calls[key]
        return None

    def bookkeeping(self):
        """A short cross-process lock for store bookkeeping; never held while sending."""
        self.root.mkdir(parents=True, exist_ok=True)
        return (self.root/'.lock').open('a')


def build_index(store_root, name, calls_dir, provider):
    """Index a saved run's delivered calls by key, without modifying them.

    Paths are stored relative to the repository, so the index works wherever
    the repository is, such as mounted in a container.
    """
    calls_dir, entries, skipped = Path(calls_dir).resolve(), {}, 0
    root = paths().root.resolve()
    for result_path in sorted(calls_dir.glob('*.result.json')):
        prefix = Path(str(result_path)[:-len('.result.json')])
        request_path, parsed_path = Path(str(prefix)+'.request.json'), Path(str(prefix)+'.parsed.json')
        state = read_json(result_path)
        delivered = state.get('status') == 'ok' or state.get('error_type') == 'validation'
        if not delivered or not request_path.exists() or not parsed_path.exists():
            skipped += 1
            continue
        entries[call_key(provider, read_json(request_path))] = str(prefix.relative_to(root))
    path = Path(store_root)/'index'/f'{name}.json'
    if path.exists():
        raise ValueError(f'Index {name} already exists')
    save_json(path, {'name': name, 'source': str(calls_dir.relative_to(root)),
                     'provider': {'kind': provider.kind, 'model': provider.model},
                     'created_at': datetime.now(timezone.utc).isoformat(), 'calls': entries})
    return {'indexed': len(entries), 'skipped': skipped, 'path': str(path)}


# The service ----------------------------------------------------------------
class Models:
    """One run's access to models. ``mode`` is send, dry (estimate only) or cached (never send)."""

    def __init__(self, run_id, ledger=None, store=None, limit=0.0, mode='cached', resend_failed=False,
                 resend_max_tokens=None, resamples=0, catalog_path=None, transport=None, clock=None):
        self.run_id, self.store = run_id, store or CallStore()
        self.ledger_path = Path(ledger) if ledger else None
        self.limit, self.mode = limit, mode
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.resend_failed, self.resend_max_tokens, self.resamples = resend_failed, resend_max_tokens, resamples
        self.catalog_path, self.transport = catalog_path, transport
        self.entries = [json.loads(line) for line in self.ledger_path.read_text().splitlines() if line.strip()] \
            if self.ledger_path and self.ledger_path.exists() else []
        self._providers, self._lock = {}, threading.RLock()

    # Providers -------------------------------------------------------------
    def provider(self, settings):
        """The provider for a step's model settings (``model`` names a catalog entry)."""
        model = settings['model']
        with self._lock:
            if model not in self._providers:
                self._providers[model] = provider_for(model, self.catalog_path)
            return self._providers[model]

    # Ledger ----------------------------------------------------------------
    def log(self, **entry):
        entry = {k: v for k, v in entry.items() if v is not None}
        entry['at'] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.entries.append(entry)
            if self.ledger_path and self.mode != 'dry':
                self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
                with self.ledger_path.open('a') as f:
                    f.write(json.dumps(entry, ensure_ascii=False)+'\n')

    def spent(self):
        """This run's new spending: final charges, or reservations still outstanding."""
        reserved, charged = {}, {}
        for e in self.entries:
            if e['event'] == 'sent':
                reserved[e['attempt']] = e['usd']
            elif e['event'] == 'charged':
                charged[e['attempt']] = e['usd']
        return sum(charged.get(a, usd) for a, usd in reserved.items())

    def would_spend(self):
        return sum(e['usd'] for e in self.entries if e['event'] == 'would_send')

    def summary(self):
        events = {}
        for e in self.entries:
            events[e['event']] = events.get(e['event'], 0)+1
        # Each distinct call reused counts once, however many times the run was continued.
        reused = sum({e['key']: e.get('usd') or 0 for e in self.entries if e['event'] == 'reused'}.values())
        return {'spent_usd': round(self.spent(), 6), 'reused_value_usd': round(reused, 6),
                'would_spend_usd': round(self.would_spend(), 6), 'events': events}

    def _resent(self, key):
        return any(e['event'] == 'sent' and e['key'] == key and e.get('resend') for e in self.entries)

    # Calls -----------------------------------------------------------------
    def ask(self, provider, request, accept, on_attempt=None, step=None):
        """Call, then ``accept(value)``; on rejection try further samples within the run's limit.

        Returns ``(accepted, value, key)``. ``accept`` raises to reject.
        ``on_attempt(key, kind, error)`` is told about every attempt; ``step`` labels its ledger entries.
        """
        errors = []
        for sample in range(self.resamples+1):
            value, key, kind = self.call(provider, request, sample, step=step)
            try:
                accepted = accept(value)
            except Exception as exc:
                errors.append(str(exc))
                if on_attempt:
                    on_attempt(key, kind, str(exc))
                continue
            if on_attempt:
                on_attempt(key, kind, None)
            return accepted, value, key
        raise Rejected('; '.join(errors[-3:]))

    def call(self, provider, request, sample=0, allow_repair=True, step=None):
        """Return ``(value, key, kind)``; kind is reused, new, resent or repaired."""
        key = call_key(provider, request, sample)
        request_file, result_file = self.store.file(key, '.request.json'), self.store.file(key, '.result.json')
        resending = False
        with self._lock, self.store.bookkeeping() as books:
            fcntl.flock(books, fcntl.LOCK_EX)
            if request_file.exists():
                if read_json(request_file) != request:
                    raise ValueError(f'{key}: stored request differs from its key')
                state = read_json(result_file) if result_file.exists() else {}
                if state.get('status') == 'ok':
                    self.log(event='reused', key=key, source='store', usd=state.get('charged_or_reserved_usd') or 0,
                             step=step)
                    return read_json(self.store.file(key, '.parsed.json')), key, 'reused'
                if state.get('status') == 'pending' and _in_flight(self.store, key):
                    raise DeliveryFailed('This call is being sent by another run')
                if state.get('error') == 'Invalid JSON object' and allow_repair \
                        and self.store.file(key, '.response.json').exists() and self.mode == 'send':
                    repair = state
                elif self.mode == 'send' and self.resend_failed and not self._resent(key):
                    repair, resending = None, True
                else:
                    if self.mode == 'dry':
                        self.log(event='would_send', key=key, usd=provider.bound(request, self.clock()),
                                 note='a failed delivery; needs resending', step=step)
                        raise NotSent('An earlier delivery failed; a dry run does not resend it')
                    raise DeliveryFailed('An earlier delivery failed or is uncertain ('+str(state.get('error'))
                                         + '); it is not resent automatically')
            else:
                found = self.store.indexed(key)
                if found:
                    name, saved = found
                    state = read_json(Path(str(saved)+'.result.json'))
                    self.log(event='reused', key=key, source='index:'+name, usd=state.get('charged_or_reserved_usd') or 0,
                             step=step)
                    return read_json(Path(str(saved)+'.parsed.json')), key, 'reused'
                repair = None
            if repair is None:
                sent = copy.deepcopy(request)
                if resending and self.resend_max_tokens and 'max_tokens' in sent:
                    sent['max_tokens'] = self.resend_max_tokens
                now = self.clock()
                bound = provider.bound(sent, now)
                if self.mode == 'dry':
                    self.log(event='would_send', key=key, usd=bound, step=step)
                    raise NotSent('A new call; a dry run does not send it')
                if self.mode != 'send':
                    raise NotSent('Not in the call cache; this run does not send new calls')
                if self.spent()+bound > self.limit:
                    raise BudgetRefused('The run\'s spending cap would be exceeded; raise it to continue')
                if resending:
                    self._archive(key)
                attempt = uuid.uuid4().hex
                state = {'status': 'pending', 'charged_or_reserved_usd': bound, 'run': self.run_id, 'attempt': attempt,
                         'requested_at': now.isoformat(), 'price_factor': provider.pricing.factor(now),
                         'provider': provider.identity(), 'sample': sample}
                # Hold the call's send lock before anyone can see it pending.
                self.store.prefix(key).parent.mkdir(parents=True, exist_ok=True)
                lock = self.store.file(key, '.lock').open('a')
                fcntl.flock(lock, fcntl.LOCK_EX)
                save_json(result_file, state)
                save_json(request_file, request)
                save_json(self.store.file(key, '.sent.json'), sent)
                self.log(event='sent', key=key, attempt=attempt, usd=bound, resend=resending or None, step=step)
        if repair is not None:
            return self._repair(provider, key, request, repair, step), key, 'repaired'
        return self._send(provider, key, request, sent, state, allow_repair, lock, step), key, \
            'resent' if resending else 'new'

    def _archive(self, key):
        archive = self.store.root/'attempts'/uuid.uuid4().hex
        archive.mkdir(parents=True)
        for suffix in SUFFIXES:
            old = self.store.file(key, suffix)
            if old.exists():
                old.rename(archive/old.name)
        self.log(event='archived', key=key, path=str(archive))

    def _send(self, provider, key, request, sent, state, allow_repair, lock, step=None):
        result, parsed = self.store.file(key, '.result.json'), self.store.file(key, '.parsed.json')
        began = time.monotonic()
        try:
            if self.transport is None and provider.endpoint is None:
                provider.connect()
            response = (self.transport or provider.post)(sent)
            state['http_status'] = response.status_code
            if isinstance(response.text, str):
                self.store.file(key, '.response.txt').write_text(response.text)
            body = response.json()
            save_json(self.store.file(key, '.response.json'), body)
            usage = provider.usage(body)
            state['usage'] = usage
            # Priced at the time it was sent, as the provider bills it.
            charge = provider.price(usage, datetime.fromisoformat(state['requested_at']))
            if charge is not None:
                state['charged_or_reserved_usd'] = charge
            elif response.status_code in REJECTED_BEFORE_GENERATION:
                state['charged_or_reserved_usd'] = 0
                state['cost_note'] = 'Request rejected before generation; no usage reported'
            if response.status_code != 200:
                raise ValueError(f'HTTP {response.status_code}: {body.get("error", {})}')
            outcome = provider.outcome(body)
            state['finish_reason'] = outcome.finish_reason
            if not outcome.complete:
                state['error_type'] = outcome.error_type
                raise ValueError('Incomplete model response ('+str(outcome.error_type)+')')
            value, repairs = provider.decode(outcome.content)
            if value is None:
                if not allow_repair or 'json_repair' not in provider.capabilities:
                    raise ValueError('Invalid JSON object')
                state.update(json_repair=True, status='failed', error='Invalid JSON object')
                save_json(result, state)
                value, repair_key, _ = self.call(provider, provider.repair_request(request, outcome.content),
                                                 allow_repair=False, step=step)
                state['json_repair_key'] = repair_key
            save_json(parsed, value)
            state.update(status='ok', json_repairs=repairs)
            state.pop('error', None)
            return value
        except Unavailable:
            raise
        except Exception as exc:
            import httpx
            state.setdefault('error_type', type(exc).__name__)
            if state.get('json_repair') and state.get('error') == 'Invalid JSON object':
                state.update(status='failed', repair_error=str(exc))
            else:
                state.update(status='failed', error=str(exc))
            if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
                state['charged_or_reserved_usd'] = 0
            raise DeliveryFailed(str(exc)) from exc
        finally:
            state['elapsed_seconds'] = time.monotonic()-began
            save_json(result, state)
            self.log(event='charged', key=key, attempt=state['attempt'], usd=state['charged_or_reserved_usd'],
                     status=state['status'], step=step, tokens=provider.tokens(state.get('usage')),
                     seconds=round(state['elapsed_seconds'], 2))
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    def _repair(self, provider, key, request, state, step=None):
        """Repair a completed delivery whose JSON was invalid, as a separate call."""
        raw = provider.outcome(read_json(self.store.file(key, '.response.json'))).content
        value, repair_key, _ = self.call(provider, provider.repair_request(request, raw), allow_repair=False, step=step)
        save_json(self.store.file(key, '.format-repair-note.json'), state)
        save_json(self.store.file(key, '.parsed.json'), value)
        state.update(status='ok', error=None, json_repair_key=repair_key)
        save_json(self.store.file(key, '.result.json'), state)
        return value


def _in_flight(store, key):
    """Whether another process holds this call's send lock."""
    path = store.file(key, '.lock')
    if not path.exists():
        return False
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
    return False


def ledger_summary(entries, since=None, points=240):
    """Spending, calls and tokens from a run's ledger: totals, per step, and spending over time.

    Spending counts each attempt's final charge, or its reservation while it is
    still outstanding. ``since`` (an ISO time) also reports the part of the run
    after it, such as the current execution.
    """
    reserved, charged, steps = {}, {}, {}
    totals = {'spent_usd': 0.0, 'outstanding_usd': 0.0, 'reused_value_usd': 0.0, 'would_spend_usd': 0.0,
              'calls': {'reused': 0, 'sent': 0, 'failed': 0, 'would_send': 0},
              'tokens': {'input': 0, 'cached': 0, 'output': 0}, 'since_usd': 0.0}
    reused_keys, timeline, running = {}, [], 0.0

    def step_of(entry):
        name = entry.get('step') or '(unlabeled)'
        return steps.setdefault(name, {'step': name, 'spent_usd': 0.0, 'calls': {'reused': 0, 'sent': 0, 'failed': 0,
                                                                                 'would_send': 0},
                                       'tokens': {'input': 0, 'cached': 0, 'output': 0}, 'seconds': 0.0})
    for e in entries:
        event = e['event']
        if event == 'sent':
            reserved[e['attempt']] = e
            totals['calls']['sent'] += 1
            step_of(e)['calls']['sent'] += 1
        elif event == 'charged':
            charged[e['attempt']] = e
            step = step_of(e)
            step['spent_usd'] += e['usd']
            step['seconds'] += e.get('seconds') or 0
            running += e['usd']
            timeline.append([e['at'], round(running, 6)])
            if e.get('status') != 'ok':
                totals['calls']['failed'] += 1
                step['calls']['failed'] += 1
            for kind, count in (e.get('tokens') or {}).items():
                totals['tokens'][kind] = totals['tokens'].get(kind, 0)+(count or 0)
                step['tokens'][kind] = step['tokens'].get(kind, 0)+(count or 0)
            if since and e['at'] >= since:
                totals['since_usd'] += e['usd']
        elif event == 'reused':
            totals['calls']['reused'] += 1
            step_of(e)['calls']['reused'] += 1
            reused_keys[e['key']] = e.get('usd') or 0
        elif event == 'would_send':
            totals['calls']['would_send'] += 1
            totals['would_spend_usd'] += e['usd']
            step_of(e)['calls']['would_send'] += 1
    outstanding = {a: e for a, e in reserved.items() if a not in charged}
    totals['outstanding_usd'] = sum(e['usd'] for e in outstanding.values())
    totals['spent_usd'] = sum(e['usd'] for e in charged.values()) + totals['outstanding_usd']
    totals['reused_value_usd'] = sum(reused_keys.values())
    for e in outstanding.values():
        step_of(e)['spent_usd'] += e['usd']
    if len(timeline) > points:
        stride = len(timeline)/points
        timeline = [timeline[int(i*stride)] for i in range(points)] + [timeline[-1]]
    rounded = {k: round(v, 6) if isinstance(v, float) else v for k, v in totals.items()}
    return {**rounded, 'steps': sorted(({**s, 'spent_usd': round(s['spent_usd'], 6)} for s in steps.values()),
                                       key=lambda s: s['step']), 'timeline': timeline}
