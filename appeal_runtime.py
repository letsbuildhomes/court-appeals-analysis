"""Atomic checkpoints, bounded API spending, and immutable replay."""
import hashlib
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid
from urllib.parse import urlparse

import httpx
from group_scout import cost_usd, request_cost_bound
from json_repair import repair_json


def api_config(base_url=None, api_url=None, key=None):
    """Use a complete chat-completions URL or append it to a provider base URL."""
    endpoint = api_url or os.getenv('APPEALS_API_URL')
    base = base_url or os.getenv('APPEALS_API_BASE_URL')
    if endpoint and base:
        raise ValueError('Set API URL or base URL, not both')
    endpoint = endpoint or (base.rstrip('/')+'/chat/completions' if base else None)
    key = key or os.getenv('APPEALS_API_KEY')
    if not endpoint or not key:
        raise ValueError('Require APPEALS_API_BASE_URL (or APPEALS_API_URL) and APPEALS_API_KEY')
    parsed = urlparse(endpoint)
    if parsed.scheme not in ('http','https') or not parsed.netloc or parsed.username or parsed.password or parsed.query:
        raise ValueError('API URL must be an HTTP(S) URL without embedded credentials or query parameters')
    return endpoint,key


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


def payload(model, prompt, data):
    return {'model': model, 'stream': False, 'reasoning_effort': 'high',
            'max_tokens': 65536, 'response_format': {'type': 'json_object'},
            'messages': [{'role': 'system', 'content': prompt},
                         {'role': 'user', 'content': json.dumps(data, ensure_ascii=False)}]}


class Client:
    def __init__(self, root, endpoint, key, limit, multiplier=1, retry_failed=False, retry_max_tokens=None):
        self.root, self.endpoint, self.key = Path(root), endpoint, key
        self.limit, self.multiplier = limit, multiplier
        self.lock = threading.RLock()
        self.retry_failed, self.retry_max_tokens = retry_failed, retry_max_tokens
        self.retried = set()

    def reject(self, name, error):
        """Persist downstream schema/validation failure so it can be retried."""
        if 'frozen request changed' in str(error):
            return  # The saved result still belongs to a valid, different request.
        path = self.root/(name+'.result.json')
        if path.exists():
            state = read(path)
            if state.get('status') == 'ok':
                state.update(status='failed',error=str(error),error_type='validation')
                save(path,state)

    def spent(self):
        return sum(read(p)['charged_or_reserved_usd'] for p in self.root.rglob('*.result.json'))

    def recover_validation(self, name, request, recover):
        """Revalidate a saved schema failure before considering a paid retry."""
        prefix = self.root/name
        req, result, parsed = [Path(str(prefix)+suffix) for suffix in
                               ('.request.json','.result.json','.parsed.json')]
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
        repair = copy.deepcopy(request)
        repair['messages'] += [{'role':'assistant','content':raw},
                               {'role':'user','content':'The response has invalid JSON syntax. Return the same research content as a valid JSON object using the requested schema. Repair brackets, nesting and syntax only; do not add facts or change research judgments. Return only JSON.'}]
        return self.call(name+'.json-repair',repair,allow_repair=False)

    def call(self, name, request, allow_repair=True):
        prefix = self.root/name
        req, result, parsed = [Path(str(prefix)+suffix) for suffix in
                               ('.request.json','.result.json','.parsed.json')]
        with self.lock:
            retrying = False
            if req.exists():
                if read(req) != request:
                    raise ValueError(f'{name}: frozen request changed; create a new run')
                if result.exists() and read(result).get('status') == 'ok':
                    return read(parsed)
                if self.retry_failed and name not in self.retried:
                    proposed = {**request, 'max_tokens':self.retry_max_tokens or request['max_tokens']}
                    if self.spent()+request_cost_bound(proposed,self.multiplier)>self.limit:
                        raise ValueError('Run spending cap reached; raise --max-spend to resume')
                    # Keep every prior attempt, including its charge or uncertain-delivery reservation.
                    archive = self.root/'attempts'/uuid.uuid4().hex
                    archive.mkdir(parents=True)
                    for suffix in ('.request.json','.result.json','.parsed.json','.response.json','.response.txt','.sent.json','.format-repair-note.json','.original-parsed.json','.recovery.json'):
                        old = Path(str(prefix)+suffix)
                        if old.exists():old.rename(archive/old.name)
                    self.retried.add(name)
                    retrying = True
                elif result.exists() and read(result).get('error') == 'Invalid JSON object' and allow_repair:
                    previous=read(result)
                    raw=read(Path(str(prefix)+'.response.json'))['choices'][0]['message']['content']
                    value=self.fix_json(name,request,raw)
                    save(Path(str(prefix)+'.format-repair-note.json'),previous)
                    save(parsed,value)
                    previous.update(status='ok',error=None,json_repair_stage=name+'.json-repair')
                    save(result,previous)
                    return value
                else:
                    raise ValueError(f'{name}: previous delivery incomplete or failed; no automatic resend; use --retry-failed')
            sent = copy.deepcopy(request)
            if retrying and self.retry_max_tokens:sent['max_tokens'] = self.retry_max_tokens
            bound = request_cost_bound(sent, self.multiplier)
            if self.spent()+bound > self.limit:
                raise ValueError('Run spending cap reached; raise --max-spend to resume')
            state = {'status':'pending','charged_or_reserved_usd':bound,
                     'requested_at':datetime.now(timezone.utc).isoformat(),
                     'price_multiplier':self.multiplier}
            save(result,state)
            save(req,request)
            save(Path(str(prefix)+'.sent.json'),sent)
        began = time.monotonic()
        try:
            with httpx.Client(timeout=httpx.Timeout(650,connect=30)) as connection:
                response = connection.post(self.endpoint, headers={'Authorization':f'Bearer {self.key}'}, json=sent)
            state['http_status'] = response.status_code
            # Preserve non-JSON error bodies as well as normal provider responses.
            if isinstance(response.text,str):Path(str(prefix)+'.response.txt').write_text(response.text)
            body = response.json()
            save(Path(str(prefix)+'.response.json'),body)
            usage = body.get('usage')
            state['usage'] = usage
            charge = cost_usd(usage)
            if charge is not None:
                state['charged_or_reserved_usd'] = charge*self.multiplier
            elif response.status_code in (400,401,403,404,413,422):
                state['charged_or_reserved_usd'] = 0
                state['cost_note'] = 'Request rejected before generation; no usage reported'
            if response.status_code != 200:
                raise ValueError(f'HTTP {response.status_code}: {body.get("error",{})}')
            choice = body['choices'][0]
            state['finish_reason'] = choice.get('finish_reason')
            if choice.get('finish_reason') != 'stop':
                state['error_type'] = 'output_limit' if choice.get('finish_reason')=='length' else 'incomplete_response'
                raise ValueError('Incomplete model response; use --retry-failed (and optionally --retry-max-output-tokens)')
            value, repairs = repair_json(choice['message']['content'])
            if not isinstance(value,dict):
                if not allow_repair:raise ValueError('Invalid JSON object')
                state['json_repair_stage']=name+'.json-repair'
                # Persist the completed delivery before any bounded format repair.
                state.update(status='failed',error='Invalid JSON object')
                save(result,state)
                value=self.fix_json(name,request,choice['message']['content'])
            save(parsed,value)
            state.update(status='ok',json_repairs=repairs)
            state.pop('error',None)
            return value
        except Exception as exc:
            state.setdefault('error_type',type(exc).__name__)
            if state.get('json_repair_stage') and state.get('error')=='Invalid JSON object':
                state.update(status='failed',repair_error=str(exc))
            else:
                state.update(status='failed',error=str(exc))
            if isinstance(exc,(httpx.ConnectError,httpx.ConnectTimeout)):
                state['charged_or_reserved_usd'] = 0
            raise
        finally:
            state['elapsed_seconds'] = time.monotonic()-began
            save(result,state)
