"""Execute a recipe over a run directory.

A recipe has one per-case phase followed by one batched phase. The
per-case phase writes ``first_pass/<case>.json``; a case it finishes without a
candidate record is complete, and one with a candidate goes on to the batched
phase, which writes ``results/<case>.json``. Activity files show which step is
running. Call names stay stable across recipes: ``<case>.<step>`` for per-case
steps and ``<step>-<batch id>`` for batched steps, with the request hash added
for components whose requests depend on earlier answers.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from pipeline import batching
from pipeline.components import COMPONENTS
from pipeline.recipe import step_spec

DONE_FIRST_PASS = ('extracted', 'complete')


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def save(path, value):
    from appeal_runtime import save as atomic_save
    atomic_save(path, value)


def read(path):
    return json.loads(Path(path).read_text())


class Engine:
    def __init__(self, folder, recipe, prompts, manifest, clients, workers=3, rerun=()):
        self.folder, self.recipe, self.manifest = Path(folder), recipe, manifest
        self.clients, self.workers = clients, workers
        # Steps to call afresh even when an identical saved call could be inherited.
        self.rerun, self.fresh = set(rerun), {}
        kinds = [recipe['phases'][p]['kind'] for p in recipe['order']]
        if kinds != ['per_case', 'batched']:
            # Finalized records need the batched phase's duration bounds.
            raise ValueError('A recipe runs one per-case phase followed by one batched phase')
        self.first, self.batched = recipe['order']
        self.steps = {}
        for phase in recipe['order']:
            for step_id in recipe['phases'][phase]['steps']:
                spec = step_spec(recipe, step_id)
                roles = {role: prompts[name] for role, name in spec.get('prompts', {}).items()}
                provider = clients[spec['provider']].provider
                self.steps[step_id] = (COMPONENTS[spec['component']](step_id, spec, provider, roles), spec)

    def client(self, step_id):
        client = self.clients[self.steps[step_id][1]['provider']]
        if step_id in self.rerun and client.inherit is not None:
            if step_id not in self.fresh:
                # Shares the run's lock and ledger, without reading through.
                self.fresh[step_id] = copy.copy(client)
                self.fresh[step_id].inherit = None
            return self.fresh[step_id]
        return client

    def all_clients(self):
        return list({id(c): c for c in self.clients.values()}.values())

    def activity(self, gid, stage):
        save(self.folder/'activity'/f'{gid}.json', {'stage': stage, 'updated_at': datetime.now(timezone.utc).isoformat()})

    def continued(self, step_id, conversation):
        return conversation if self.steps[step_id][1]['context'] == 'continue' else None

    def follow(self, component, conversation, unit, value):
        """The conversation after this step, if its provider keeps one."""
        if 'conversation' not in component.provider.capabilities:
            return None
        return conversation.answer(component.transcript(unit, value))

    # Per-case phase --------------------------------------------------------
    def first_pass(self, group):
        from appeal_evidence import source_pages
        gid = group['group_id']
        path = self.folder/'first_pass'/f'{gid}.json'
        if path.exists() and (cached := read(path))['status'] in DONE_FIRST_PASS:
            return cached
        steps = self.recipe['phases'][self.first]['steps']
        result = {'group': gid, 'status': 'incomplete', 'warnings': []}
        step_id = steps[0]
        call_name = f'{gid}.{step_id}'
        try:
            if not group['eligible']:
                result.update(status='too_large', reason='Separate route required; source not truncated')
                return result
            unit = {'case': group, 'result': result, 'pages': source_pages(group['source'])}
            conversation = None
            for step_id in steps:
                component, _ = self.steps[step_id]
                if not component.applies(unit):
                    continue
                call_name, client = f'{gid}.{step_id}', self.client(step_id)
                self.activity(gid, component.activity)
                request, used = component.request(unit, self.continued(step_id, conversation))
                component.before_call(client, call_name, request, unit)
                value = client.call(call_name, request)
                component.accept(unit, value)
                conversation = self.follow(component, used, unit, value)
            result['status'] = 'complete' if result.get('candidate') is None else 'extracted'
        except Exception as exc:
            self.client(step_id).reject(call_name, exc)
            result['error'] = str(exc)
            state_path = self.folder/'calls'/f'{call_name}.result.json'
            state = read(state_path) if state_path.exists() else {}
            result['error_type'] = ('budget' if 'Run spending cap reached' in str(exc)
                                    else state.get('error_type', 'unknown'))
            result['failed_call'] = call_name
        finally:
            save(path, result)
            self.activity(gid, 'idle')
            print(gid, result['status'], flush=True)
        return result

    def run_first_pass(self):
        outputs = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for result in pool.map(self.first_pass, self.manifest['groups']):
                outputs[result['group']] = result
        return outputs

    # Batched phase ---------------------------------------------------------
    def finish(self, outputs):
        """Batch cases with drafts and run the batched steps; completed cases are kept."""
        phase = self.recipe['phases'][self.batched]
        retained = []
        for group in self.manifest['groups']:
            gid = group['group_id']
            result = outputs[gid]
            # Batch from first-pass drafts, including completed members, so an
            # unfinished neighbor keeps its identical request and batch.
            if result['status'] == 'extracted' and result['candidate'] is not None:
                retained.append({**group, **result})
            path = self.folder/'results'/f'{gid}.json'
            if path.exists() and (saved := read(path))['status'] == 'complete':
                outputs[gid] = saved
                continue
            if result['status'] != 'extracted':
                self.checkpoint(outputs, gid)
            elif result['candidate'] is None:
                result['status'] = 'complete'
                self.checkpoint(outputs, gid)
        units = batching.batches(retained, phase.get('batch', {}))
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            list(pool.map(lambda batch: self.run_batch(phase, batch, outputs), units))

    def checkpoint(self, outputs, gid, stage=None):
        save(self.folder/'results'/f'{gid}.json', outputs[gid])
        self.activity(gid, stage or 'idle')

    def run_batch(self, phase, batch, outputs):
        active = [c['group'] for c in batch if outputs[c['group']]['status'] != 'complete']
        if not active:
            return
        identity = digest('|'.join(c['group'] for c in batch))[:16]
        unit = {'batch': batch, 'targets': active, 'outputs': outputs}
        cases = {c['group']: c for c in batch}
        conversation, name = None, None
        for step_id in phase['steps']:
            component, _ = self.steps[step_id]
            client = self.client(step_id)
            if not unit['targets']:
                print(f'{name}: no groups eligible for {step_id}', flush=True)
                return
            request, used = component.request(unit, self.continued(step_id, conversation))
            name, aliases = f'{step_id}-{identity}', ()
            if component.versioned_names:
                # A changed earlier answer or target set is a distinct, billable request.
                aliases = (name,) if component.legacy_names else ()
                name += '-'+digest(json.dumps(request, sort_keys=True, ensure_ascii=False))
            print(f'{name}: {component.activity} {len(unit["targets"])} groups ({len(batch)} in context)', flush=True)
            for gid in unit['targets']:
                self.checkpoint(outputs, gid, component.activity)
            try:
                value = client.call(name, request, aliases=aliases) if aliases else client.call(name, request)
                component.check(unit, value)
            except Exception as exc:
                client.reject(name, exc)
                for gid in unit['targets']:
                    outputs[gid].update(status=component.failure_status, error=str(exc))
                    self.checkpoint(outputs, gid)
                print(f'{name}: failed: {exc}', flush=True)
                return
            continuing = []
            for gid in unit['targets']:
                try:
                    if component.apply(unit, cases[gid], outputs[gid], value):
                        continuing.append(gid)
                except Exception as exc:
                    client.reject(name, exc)
                    outputs[gid].update(status=component.failure_status, error=str(exc))
                self.checkpoint(outputs, gid)
            unit['targets'] = continuing
            conversation = self.follow(component, used, unit, value)
        print(f'{name}: '+', '.join(f'{c["group"]} {outputs[c["group"]]["status"]}' for c in batch), flush=True)


def clients_for(folder, recipe, limit, multiplier=1, retry_failed=False, retry_max_tokens=None,
                inherit=None, dry_run=False, base_url=None, api_url=None):
    """One client per recipe provider, sharing the run's calls, cap and lock.

    Endpoint overrides apply to the provider named ``default``. A dry run needs
    no credentials.
    """
    import threading
    from appeal_runtime import Client
    from pipeline import providers
    lock, clients = threading.RLock(), {}
    for name, spec in recipe['providers'].items():
        endpoint = {'base_url': base_url, 'api_url': api_url} if name == 'default' else {}
        provider = providers.build(name, spec, offline=dry_run, **endpoint)
        clients[name] = Client(Path(folder)/'calls', provider, limit, multiplier, retry_failed, retry_max_tokens,
                               inherit=inherit, lock=lock, dry_run=dry_run)
    return clients
