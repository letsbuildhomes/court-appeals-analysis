"""Plan, run, export and browse the municipal housing appeal pipeline.

The steps, providers and batching come from a recipe (see pipeline/recipe.py);
planning freezes the base recipe into the run with its prompts and sources.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

import duckdb
import group_scout as gs
from groups import GROUPS_DB_PATH
from config import ROOT
from listen import DEFAULT_HOST
from appeal_assembly import finalized_dockets
from appeal_runtime import digest, read, save
from pipeline import recipe as recipes
from pipeline.engine import Engine, clients_for

RUNS = ROOT/'data/appeals'
SAFE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')


@contextmanager
def locked(folder):
    with (folder/'.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another process is using this run') from None
        yield


def plan(args):
    folder = args.root/args.name
    if not SAFE.fullmatch(args.name) or folder.exists():
        raise ValueError('Choose a new run name using letters, numbers, dots, dashes or underscores')
    if not args.all and not args.group:
        raise ValueError('Select --group IDs or explicitly request --all')
    composed, prompts = recipes.load(recipes.BASE)
    if args.model:
        composed['providers']['default']['model'] = args.model
    recipes.validate(composed)
    with duckdb.connect(str(GROUPS_DB_PATH),read_only=True) as db:
        ids = [r[0] for r in db.execute('SELECT group_id FROM groups ORDER BY group_id').fetchall()] if args.all else args.group
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate group')
        groups = [gs.load_group(db,g) for g in ids]
    folder.mkdir(parents=True)
    save(folder/'prompts.json',prompts)
    recipes.freeze(folder, composed)
    for group in groups:
        source, _ = gs.render_group(group['group_id'],group['documents'])
        (folder/'sources').mkdir(exist_ok=True)
        (folder/'sources'/f'{group["group_id"]}.txt').write_text(source)
        group['source_sha256'] = digest(source)
        group['eligible'] = len(source) <= args.max_chars
    manifest = {'schema_version':'appeals-1','name':args.name,
                'created_at':datetime.now(timezone.utc).isoformat(),
                'model':composed['providers']['default']['model'],'max_chars':args.max_chars,
                'prompt_sha256':digest(json.dumps(prompts,sort_keys=True)),
                'recipe_sha256':recipes.sha256(composed),'groups':groups}
    save(folder/'manifest.json',manifest)
    print(f'Planned {len(groups)} groups; {sum(not g["eligible"] for g in groups)} oversized; {folder}')


def prepare(folder):
    manifest, prompts = read(folder/'manifest.json'),read(folder/'prompts.json')
    if digest(json.dumps(prompts,sort_keys=True)) != manifest['prompt_sha256']:
        raise ValueError('Frozen prompts changed')
    for group in manifest['groups']:
        source = (folder/'sources'/f'{group["group_id"]}.txt').read_text()
        if digest(source) != group['source_sha256']:
            raise ValueError('Frozen source changed: '+group['group_id'])
        group['source'] = source
    return manifest,prompts


def raise_char_cap(folder, manifest, max_chars):
    """Explicitly increase eligibility without changing frozen model inputs."""
    if max_chars is None:
        return
    previous = manifest['max_chars']
    if max_chars < previous:
        raise ValueError('The existing run character cap may only be increased')
    if max_chars == previous:
        return
    manifest.setdefault('cap_changes', []).append({
        'previous': previous, 'max_chars': max_chars,
        'changed_at': datetime.now(timezone.utc).isoformat()})
    manifest['max_chars'] = max_chars
    for group in manifest['groups']:
        group['eligible'] = len(group['source']) <= max_chars
    # Loaded source text is runtime-only; keep the manifest compact.
    save(folder/'manifest.json', {**manifest, 'groups':[
        {k:v for k,v in group.items() if k != 'source'} for group in manifest['groups']]})


def run(args, inherit=None, rerun=()):
    """Run or resume a planned run.

    ``inherit`` reads through to another run's identical saved calls; steps in
    ``rerun`` call afresh even then.
    """
    folder = args.root/args.name
    with locked(folder):
        manifest,prompts = prepare(folder)
        raise_char_cap(folder, manifest, args.max_chars)
        manifest['workers'] = args.workers
        save(folder/'manifest.json',{**manifest,'groups':[
            {k:v for k,v in group.items() if k != 'source'} for group in manifest['groups']]})
        recipe = recipes.validate(recipes.for_run(folder, manifest, prompts))
        clients = clients_for(folder, recipe, args.max_spend, args.price_multiplier, args.retry_failed,
                              args.retry_max_output_tokens, inherit=inherit,
                              base_url=args.api_base_url, api_url=args.api_url)
        engine = Engine(folder, recipe, prompts, manifest, clients, args.workers, rerun)
        outputs = engine.run_first_pass()
        incomplete = any(r['status']=='incomplete' for r in outputs.values())
        if args.first_pass_only or incomplete:
            for gid,result in outputs.items():
                path = folder/'results'/f'{gid}.json'
                if not path.exists() or read(path)['status'] in {'incomplete','too_large','extracted'}:
                    save(path,result)
            export(folder)
            print('First pass incomplete; later batches not scheduled. Inspect results and resume.'
                  if incomplete else 'First pass finished; stopped before review (--first-pass-only).',flush=True)
            return
        engine.finish(outputs)
        for gid,result in outputs.items():
            path = folder/'results'/f'{gid}.json'
            if not path.exists() or read(path) != result:
                save(path,result)
        export(folder)
        spent = next(iter(clients.values())).spent()
        print(f'Spent/reserved ${spent:.6f}; {sum(r["status"]=="complete" for r in outputs.values())}/{len(outputs)} complete')


def export(folder):
    manifest = read(folder/'manifest.json')
    records,coverage,queue,sources = [],[],[],[]
    for group in manifest['groups']:
        gid = group['group_id']
        path = folder/'results'/f'{gid}.json'
        result = read(path) if path.exists() else {'status':'not_run'}
        triage = result.get('triage',{})
        scope = all(triage.get(k)=='yes' for k in ('housing','municipal_appeal'))
        reasons = list(dict.fromkeys(result.get('warnings',[])))
        if result['status']!='complete': reasons.append(result['status'])
        if result.get('error'):reasons.append(result['error'])
        if 'unclear' in [triage.get('housing'),triage.get('municipal_appeal')]: reasons.append('Uncertain scope')
        reasons.extend(f['issue'] for f in result.get('review',{}).get('flags',[]))
        if reasons: queue.append({'group':gid,'reasons':reasons})
        coverage.append({'group':gid,'status':result['status'],'scope_confirmed':scope,
                         'route':result.get('route',{}),'triage':triage,'review_required':bool(reasons)})
        for doc in group['documents']:
            sources.append({'group':gid,**doc})
        # Partial drafts stay inspectable in results/, never masquerade as final records.
        if result['status']!='complete': continue
        for clean in finalized_dockets((result.get('candidate') or {}).get('dockets',[]),
                                       result.get('duration_bounds',[])):
            records.append({'schema_version':'appeals-1','group':gid,'scope_confirmed':scope,
                            'review_required':bool(reasons),'record':clean})
    out = folder/'export'
    out.mkdir(exist_ok=True)
    for name,values in [('records',records),('coverage',coverage),('review',queue),('sources',sources)]:
        # Export under the run lock; readers use atomic replacements.
        target = out/f'{name}.jsonl'
        with NamedTemporaryFile(mode='w',dir=out,delete=False) as f:
            f.write(''.join(json.dumps(v,ensure_ascii=False)+'\n' for v in values))
            tmp=f.name
        os.replace(tmp,target)
    calls = [read(p) for p in (folder/'calls').rglob('*.result.json')]
    summary = {'name':manifest['name'],'groups':len(coverage),'records':len(records),
               'complete':sum(c['status']=='complete' for c in coverage),'review_groups':len(queue),
               'cost_usd':sum(c['charged_or_reserved_usd'] for c in calls),
               'input_tokens':sum((c.get('usage') or {}).get('prompt_tokens',0) for c in calls),
               'output_tokens':sum((c.get('usage') or {}).get('completion_tokens',0) for c in calls)}
    save(out/'summary.json',summary)
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=RUNS)
    sub = p.add_subparsers(dest='command',required=True)
    planning=sub.add_parser('plan')
    planning.add_argument('--name',required=True)
    selection=planning.add_mutually_exclusive_group(required=True)
    selection.add_argument('--group',action='append')
    selection.add_argument('--all',action='store_true')
    planning.add_argument('--model',default=os.getenv('APPEALS_MODEL'),help='Default provider model (default: APPEALS_MODEL, else the recipe)')
    planning.add_argument('--max-chars',type=int,default=400000)
    running=sub.add_parser('run')
    running.add_argument('--name',required=True)
    running.add_argument('--max-spend',type=float,required=True)
    running.add_argument('--price-multiplier',type=float,default=1)
    running.add_argument('--workers',type=int,default=3,help='Concurrent first-pass groups or finishing batches (default: 3)')
    running.add_argument('--max-chars',type=int,help='Explicitly raise this run\'s frozen source eligibility cap')
    running.add_argument('--first-pass-only',action='store_true',help='Export first-pass checkpoints and stop before review')
    endpoint=running.add_mutually_exclusive_group()
    endpoint.add_argument('--api-base-url')
    endpoint.add_argument('--api-url')
    running.add_argument('--retry-failed',action='store_true',help='Retry failed or interrupted deliveries once; preserve and charge all attempts')
    running.add_argument('--retry-max-output-tokens',type=int,help='Output allowance for retried calls only; provider must support it')
    for command in ('export','status'):
        s=sub.add_parser(command);s.add_argument('--name',required=True)
    checking=sub.add_parser('check')
    checking.add_argument('--name',required=True)
    checking.add_argument('--expectations',type=Path,default=ROOT/'tests/fixtures/appeals/release-expectations.json')
    serving=sub.add_parser('serve');serving.add_argument('--port',type=int,default=8769)
    serving.add_argument('--host',default=DEFAULT_HOST)
    args=p.parse_args()
    if hasattr(args,'name') and not SAFE.fullmatch(args.name):p.error('Invalid run name')
    if args.command=='plan':plan(args)
    elif args.command=='run':
        if args.max_spend<=0 or args.price_multiplier<=0 or args.workers<1:p.error('Positive budget, multiplier and worker count required')
        if args.max_chars is not None and args.max_chars < 1:p.error('Positive --max-chars required')
        if args.retry_max_output_tokens is not None and (not args.retry_failed or args.retry_max_output_tokens<1):p.error('Positive --retry-max-output-tokens requires --retry-failed')
        run(args)
    elif args.command=='export':
        with locked(args.root/args.name):print(export(args.root/args.name))
    elif args.command=='status':print(json.dumps(read(args.root/args.name/'export/summary.json'),indent=2))
    elif args.command=='check':
        expected=read(args.expectations)
        folder=args.root/args.name/'export'
        rows=[json.loads(s) for s in (folder/'records.jsonl').read_text().splitlines()]
        coverage=[json.loads(s) for s in (folder/'coverage.jsonl').read_text().splitlines()]
        actual_scope={c['group']:[c['triage'].get('municipal_appeal'),c['triage'].get('housing')] for c in coverage}
        actual_duration={r['record']['docket']:[r['record']['duration']['kind'],r['record']['duration']['days']] for r in rows}
        failures=[]
        for kind,actual in [('scope',actual_scope),('duration',actual_duration)]:
            for key,value in expected[kind].items():
                if actual.get(key)!=value:failures.append({'field':kind,'key':key,'expected':value,'actual':actual.get(key)})
        if any(c['status']!='complete' for c in coverage):failures.append({'field':'completion','error':'Incomplete groups'})
        print(json.dumps({'passed':not failures,'differences':failures},indent=2))
        if failures:raise SystemExit(1)
    else:
        from appeal_viewer import serve
        serve(args.root,args.port,args.host)


if __name__=='__main__':
    main()
