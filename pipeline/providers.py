"""Inference providers behind one interface.

A provider renders a step's request, sends it, reads usage, prices it, and
bounds its worst-case cost before sending. Chat models take conversations and
return generated JSON. Decision models, such as stateless typed-question
models, take a state plus questions and return structured answers; they have
no conversation to continue. The spending cap, call log and retries live above
providers in ``appeal_runtime.Client``, so every provider is governed alike.

Prices come from ``pipeline/models.json``. A model without a price cannot make
paid calls, because its spending could not be capped.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
from urllib.parse import urlparse

MODELS_PATH = Path(__file__).with_name('models.json')
REJECTED_BEFORE_GENERATION = (400, 401, 403, 404, 413, 422)


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens."""
    input: float
    output: float = 0.0
    cached_input: float | None = None

    def cost(self, prompt, cached, output):
        hit = cached or 0
        cached_rate = self.input if self.cached_input is None else self.cached_input
        return (hit*cached_rate + (prompt-hit)*self.input + output*self.output)/1_000_000


@dataclass
class Outcome:
    """How a delivered response ended, independent of the provider's format."""
    complete: bool
    content: object
    finish_reason: str | None = None
    error_type: str | None = None


def catalog(path=None):
    return json.loads(Path(path or MODELS_PATH).read_text())['models']


def pricing_for(model, path=None):
    entry = catalog(path).get(model)
    if not entry or 'pricing_usd_per_million' not in entry:
        raise ValueError(f'No price for model {model!r}; add it to pipeline/models.json before making paid calls')
    return Pricing(**entry['pricing_usd_per_million'])


def endpoint_config(prefix, base_url=None, api_url=None, key=None):
    """A complete endpoint URL, or a base URL plus the provider's path, and a key."""
    endpoint = api_url or os.getenv(f'{prefix}_API_URL')
    base = base_url or os.getenv(f'{prefix}_API_BASE_URL')
    if endpoint and base:
        raise ValueError('Set API URL or base URL, not both')
    key = key or os.getenv(f'{prefix}_API_KEY')
    return endpoint, base, key


def _check_url(endpoint):
    parsed = urlparse(endpoint)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query:
        raise ValueError('API URL must be an HTTP(S) URL without embedded credentials or query parameters')


class Provider:
    """Base class. Subclasses set ``kind``, ``capabilities`` and ``path``."""
    kind = None
    capabilities = frozenset()
    path = ''

    def __init__(self, name, model, pricing, endpoint=None, key=None):
        self.name, self.model, self.pricing = name, model, pricing
        self.endpoint, self.key = endpoint, key

    @classmethod
    def from_spec(cls, name, spec, base_url=None, api_url=None, key=None, offline=False):
        """Build from a recipe provider spec; ``offline`` skips credentials for planning."""
        pricing = pricing_for(spec['model'])
        if offline:
            return cls(name, spec['model'], pricing)
        endpoint, base, key = endpoint_config(spec.get('env', 'APPEALS'), base_url, api_url, key)
        endpoint = endpoint or (base.rstrip('/')+cls.path if base else None)
        if not endpoint or not key:
            prefix = spec.get('env', 'APPEALS')
            raise ValueError(f'Require {prefix}_API_BASE_URL (or {prefix}_API_URL) and {prefix}_API_KEY')
        _check_url(endpoint)
        return cls(name, spec['model'], pricing, endpoint, key)

    def identity(self):
        return {'provider': self.name, 'kind': self.kind, 'model': self.model}

    def post(self, request):
        import httpx  # Only sending needs it; describing recipes stays standard-library.
        with httpx.Client(timeout=httpx.Timeout(650, connect=30)) as connection:
            return connection.post(self.endpoint, headers={'Authorization': f'Bearer {self.key}'}, json=request)

    def bound(self, request):
        raise NotImplementedError

    def estimate(self, known_bytes, messages, unknown_tokens, max_tokens):
        """Worst-case cost of a request not yet built; unknown text counts as tokens."""
        raise NotImplementedError

    def usage(self, body):
        return body.get('usage') if isinstance(body, dict) else None

    def price(self, usage):
        raise NotImplementedError

    def outcome(self, body):
        raise NotImplementedError

    def decode(self, content):
        """Return (value, repair notes); value is None when the content is not an object."""
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

    def estimate(self, known_bytes, messages, unknown_tokens, max_tokens):
        # A byte bounds a token; every message adds framing; output is capped.
        tokens = known_bytes + 256*messages + 1024 + unknown_tokens
        return (tokens*self.pricing.input + max_tokens*self.pricing.output)/1_000_000

    def bound(self, request):
        known = sum(len(m['content'].encode('utf-8')) for m in request['messages'])
        return self.estimate(known, len(request['messages']), 0, request['max_tokens'])

    def price(self, usage):
        if not isinstance(usage, dict):
            return None
        prompt, output = usage.get('prompt_tokens'), usage.get('completion_tokens')
        if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (prompt, output)):
            return None
        details = usage.get('prompt_tokens_details') or {}
        cached = usage.get('prompt_cache_hit_tokens', details.get('cached_tokens') if isinstance(details, dict) else None)
        if not isinstance(cached, int) or isinstance(cached, bool) or not 0 <= cached <= prompt:
            cached = None
        return self.pricing.cost(prompt, cached, output)

    def outcome(self, body):
        choice = body['choices'][0]
        reason = choice.get('finish_reason')
        if reason != 'stop':
            return Outcome(False, choice.get('message', {}).get('content'), reason,
                           'output_limit' if reason == 'length' else 'incomplete_response')
        return Outcome(True, choice['message']['content'], reason)

    def decode(self, content):
        from json_repair import repair_json
        value, repairs = repair_json(content)
        return (value if isinstance(value, dict) else None), repairs

    def repair_request(self, request, raw):
        """Ask for the same content as valid JSON, continuing the conversation."""
        repair = json.loads(json.dumps(request))
        repair['messages'] += [{'role': 'assistant', 'content': raw},
                               {'role': 'user', 'content': 'The response has invalid JSON syntax. Return the same research content as a valid JSON object using the requested schema. Repair brackets, nesting and syntax only; do not add facts or change research judgments. Return only JSON.'}]
        return repair


class DecisionProvider(Provider):
    """Stateless typed questions about one state, answered with structured values.

    Requests are ``{"model", "state", "questions"}``; responses carry ``answers``
    and ``usage.input_tokens``. Pricing is per input token, so the bound before a
    call is exact up to tokenization. There is no conversation to continue.
    This is the interface a decision model such as Jev plugs into; no model is
    configured for it yet.
    """
    kind = 'decision'
    capabilities = frozenset({'typed_questions'})
    path = ''

    def render(self, state, questions):
        return {'model': self.model, 'state': state, 'questions': questions}

    def estimate(self, known_bytes, messages, unknown_tokens, max_tokens):
        return ((known_bytes + unknown_tokens + 1024)*self.pricing.input + max_tokens*self.pricing.output)/1_000_000

    def bound(self, request):
        known = len(json.dumps({k: request[k] for k in ('state', 'questions')}, ensure_ascii=False).encode('utf-8'))
        return self.estimate(known, 1, 0, 0)

    def usage(self, body):
        usage = body.get('usage') if isinstance(body, dict) else None
        return usage if isinstance(usage, dict) else None

    def price(self, usage):
        tokens = (usage or {}).get('input_tokens')
        if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
            return None
        output = (usage or {}).get('output_tokens') or 0
        return self.pricing.cost(tokens, None, output)

    def outcome(self, body):
        answers = body.get('answers')
        return Outcome(isinstance(answers, dict), answers, 'answered' if isinstance(answers, dict) else None,
                       None if isinstance(answers, dict) else 'incomplete_response')


PROVIDERS = {'chat-completions': ChatCompletionsProvider, 'decision': DecisionProvider}


def build(name, spec, offline=False, **endpoint):
    """Build a provider from a recipe's provider spec."""
    kind = catalog().get(spec['model'], {}).get('provider', spec.get('type'))
    if spec.get('type') and kind != spec['type']:
        raise ValueError(f'Model {spec["model"]!r} is a {kind} model, not {spec["type"]}')
    if kind not in PROVIDERS:
        raise ValueError(f'Unknown provider type {kind!r} for model {spec["model"]!r}')
    return PROVIDERS[kind].from_spec(name, spec, offline=offline, **endpoint)
