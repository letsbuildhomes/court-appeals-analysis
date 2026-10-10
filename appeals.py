"""Plan, run, export and browse the municipal housing appeal pipeline."""
import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re

import duckdb
import group_scout as gs
from groups import GROUPS_DB_PATH
from config import ROOT
from listen import DEFAULT_HOST
from appeal_evidence import apply_review, interval, source_pages
from appeal_assembly import finalized_dockets
from appeal_runtime import Client, api_config, digest, payload, read, save

RUNS = ROOT/'data/appeals'
PROMPTS = ROOT/'prompts/appeals'
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
    with duckdb.connect(str(GROUPS_DB_PATH),read_only=True) as db:
        ids = [r[0] for r in db.execute('SELECT group_id FROM groups ORDER BY group_id').fetchall()] if args.all else args.group
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate group')
        groups = [gs.load_group(db,g) for g in ids]
    folder.mkdir(parents=True)
    prompts = {p.stem:p.read_text() for p in sorted(PROMPTS.glob('*.md'))}
    save(folder/'prompts.json',prompts)
    for group in groups:
        source, _ = gs.render_group(group['group_id'],group['documents'])
        (folder/'sources').mkdir(exist_ok=True)
        (folder/'sources'/f'{group["group_id"]}.txt').write_text(source)
        group['source_sha256'] = digest(source)
        group['eligible'] = len(source) <= args.max_chars
    manifest = {'schema_version':'appeals-1','name':args.name,
                'created_at':datetime.now(timezone.utc).isoformat(),
                'model':args.model,'reasoning':'high','max_output_tokens':65536,
                'max_chars':args.max_chars,'review_batch_size':2,'review_batch_chars':200000,
                'prompt_sha256':digest(json.dumps(prompts,sort_keys=True)), 'groups':groups}
    save(folder/'manifest.json',manifest)
    print(f'Planned {len(groups)} groups; {sum(not g["eligible"] for g in groups)} oversized; {folder}')


def packs(groups, size=6, chars=200000):
    batch, total = [],0
    for group in groups:
        amount = len(group['source'])+len(json.dumps(group.get('candidate')))
        if batch and (len(batch) >= size or total+amount > chars):
            yield batch
            batch,total = [],0
        batch.append(group)
        total += amount
    if batch:
        yield batch


def exact_members(value, key, expected):
    if len(set(expected))!=len(expected) or not isinstance(value,list) or len(value)!=len(expected) or {x.get(key) for x in value} != set(expected):
        raise ValueError(f'Response must cover every {key} exactly once')


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


def normalize_extract(value):
    """Relocate fields only when ownership and destination are unambiguous."""
    value = copy.deepcopy(value)
    notes = []
    dockets = value.get('dockets') if isinstance(value, dict) else None
    if not isinstance(dockets, list) or not all(isinstance(d, dict) for d in dockets):
        return value, notes
    def move(source, destination, field, origin, target):
        if field in source and field not in destination:
            destination[field] = source.pop(field)
            notes.append(f'Moved {origin}.{field} to {target}.{field}')
    if len(dockets) == 1:
        for field in ('timeline', 'observed_status', 'restrictions', 'housing_changes'):
            move(value, dockets[0], field, 'extract', 'extract.dockets[0]')
    for index, docket in enumerate(dockets):
        discretion = docket.get('discretion')
        if not isinstance(discretion, dict):
            continue
        target = f'extract.dockets[{index}].discretion'
        for field in ('involved', 'central', 'central_evidence'):
            move(docket, discretion, field, f'extract.dockets[{index}]', target)
            if len(dockets) == 1:
                move(value, discretion, field, 'extract', target)
    return value, notes


def recover_extract(value, pages):
    normalized, notes = normalize_extract(value)
    candidate, errors = gs.validate_extract(json.dumps(normalized), pages, version='v6')
    if candidate is None:
        raise ValueError(str(errors))
    return normalized, notes


def duration_followup(request, reviews, targets, prompt):
    """Continue the exact review prefix; sources already appear in that history."""
    followup = copy.deepcopy(request)
    followup['messages'] += [
        {'role':'assistant','content':json.dumps(reviews,ensure_ascii=False)},
        {'role':'user','content':json.dumps({'instructions':prompt+'\n\nUse the source documents in the preceding review request. '
         'Assess only the targets below; omit other groups from the earlier batch. '
         'The corrected records below supersede the earlier drafts.',
         'cases':targets},ensure_ascii=False)}]
    return followup


def finish(folder, manifest, prompts, client, outputs, workers):
    """Finish independent two-group batches, each with a focused duration follow-up."""
    def checkpoint(gid, stage=None):
        save(folder/'results'/f'{gid}.json',outputs[gid])
        save(folder/'activity'/f'{gid}.json',{
            'stage':stage or 'idle','updated_at':datetime.now(timezone.utc).isoformat()})
    retained = []
    for group in manifest['groups']:
        gid = group['group_id']
        result = outputs[gid]
        # Pack from frozen first-pass drafts, including completed members, so
        # unfinished neighbors retain their identical review request and batch.
        if result['status'] == 'extracted' and result['candidate'] is not None:
            retained.append({**group,**result})
        path = folder/'results'/f'{gid}.json'
        if path.exists() and (saved := read(path))['status'] == 'complete':
            outputs[gid] = saved
            continue
        if result['status'] != 'extracted':
            checkpoint(gid)
        elif result['candidate'] is None:
            result['status'] = 'complete'
            checkpoint(gid)
    prefix = prompts['primer']+'\n\nEXTRACTION CONTRACT\n'+prompts['extract']
    prefix += '\n\nSCOPE CONTRACT\n'+prompts['triage']+'\n\nEDITING CONTRACT\n'+prompts['review']
    joint = '''Review this batch as one research editor. Apply the same definitions consistently,
but never transfer facts, dates, people or citations between groups. Doc/page numbering
is local to each group. Return JSON {"reviews":[{"group":"exact group id","review":{...}}]}.
Include every supplied group exactly once, with a complete v6-review-1 patch.'''

    def batch_finish(batch):
        active = [c for c in batch if outputs[c['group']]['status'] != 'complete']
        if not active:
            return
        identity = digest('|'.join(c['group'] for c in batch))[:16]
        name = 'review-'+identity
        data = {'cases':[{'group':c['group'],'source_documents':c['source'],
                         'triage':c['triage'],'candidate':c['candidate']} for c in batch]}
        request = payload(manifest['model'],prefix+'\n\nBATCH EDITING\n'+joint,data)
        # Saved runs record these values; experiments may vary them.
        request.update(reasoning_effort=manifest.get('reasoning','high'),
                       max_tokens=manifest.get('review_max_output_tokens',manifest.get('max_output_tokens',65536)))
        print(f'{name}: reviewing {len(active)} unfinished groups ({len(batch)} in context)',flush=True)
        for c in active:checkpoint(c['group'],'reviewing')
        try:
            response = client.call(name,request)
            reviews = response['reviews']
            exact_members(reviews,'group',[c['group'] for c in batch])
            by_group = {r['group']:r['review'] for r in reviews}
        except Exception as exc:
            client.reject(name,exc)
            for c in active:
                outputs[c['group']].update(status='review_failed',error=str(exc))
                checkpoint(c['group'])
            print(f'{name}: failed: {exc}',flush=True)
            return
        successful = []
        for c in active:
            result = outputs[c['group']]
            try:
                review = by_group[c['group']]
                candidate,warnings = apply_review(c['candidate'],review,source_pages(c['source']))
                result['review'] = review
                result['warnings'].extend(warnings)
                if review['scope']['verdict'] == 'flag':
                    result['status'] = 'scope_flagged'
                else:
                    result.update(candidate=candidate,status='reviewed')
                    successful.append(c)
            except Exception as exc:
                client.reject(name,exc)
                result.update(status='review_failed',error=str(exc))
            checkpoint(c['group'])
        if not successful:
            print(f'{name}: no groups eligible for duration',flush=True)
            return
        targets = [{'group':c['group'],'candidate':outputs[c['group']]['candidate']} for c in successful]
        request = duration_followup(request,response,targets,prompts['duration'])
        # A changed review answer or target set is a distinct, billable request.
        # Keep prior calls intact rather than relaxing frozen-request checks.
        name = 'duration-'+identity+'-'+digest(json.dumps(request,sort_keys=True,ensure_ascii=False))
        print(f'{name}: measuring {len(successful)} groups',flush=True)
        for c in successful:checkpoint(c['group'],'measuring')
        try:
            durations = client.call(name,request)['groups']
            exact_members(durations,'group',[c['group'] for c in successful])
            by_group = {r['group']:r for r in durations}
        except Exception as exc:
            client.reject(name,exc)
            for c in successful:
                outputs[c['group']].update(status='duration_failed',error=str(exc))
                checkpoint(c['group'])
            print(f'{name}: failed: {exc}',flush=True)
            return
        for c in successful:
            result = outputs[c['group']]
            try:
                records = by_group[c['group']]['dockets']
                exact_members(records,'docket',[d['docket'] for d in result['candidate']['dockets']])
                bounds = []
                for record in records:
                    validated,warnings = interval(record,source_pages(c['source']))
                    bounds.append(validated)
                    result['warnings'].extend(warnings)
                result.update(duration_bounds=bounds,status='complete')
            except Exception as exc:
                client.reject(name,exc)
                result.update(status='duration_failed',error=str(exc))
            checkpoint(c['group'])
        print(f'{name}: '+', '.join(f'{c["group"]} {outputs[c["group"]]["status"]}' for c in batch),flush=True)

    batches = list(packs(retained,2,manifest['review_batch_chars']))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(batch_finish,batches))


def run(args):
    folder = args.root/args.name
    with locked(folder):
        manifest,prompts = prepare(folder)
        raise_char_cap(folder, manifest, args.max_chars)
        manifest.update(review_batch_size=2,workers=args.workers,finishing='review-duration-followup')
        save(folder/'manifest.json',{**manifest,'groups':[
            {k:v for k,v in group.items() if k != 'source'} for group in manifest['groups']]})
        endpoint,key = api_config(args.api_base_url,args.api_url)
        client = Client(folder/'calls',endpoint,key,args.max_spend,args.price_multiplier,args.retry_failed,args.retry_max_output_tokens)
        model = manifest['model']
        outputs = {}
        def activity(gid,stage):
            save(folder/'activity'/f'{gid}.json',{'stage':stage,'updated_at':datetime.now(timezone.utc).isoformat()})
        def first(group):
            gid = group['group_id']
            path = folder/'first_pass'/f'{gid}.json'
            if path.exists() and (cached := read(path))['status'] in {'extracted','complete'}:
                return cached
            result = {'group':gid,'status':'incomplete','warnings':[]}
            call_name = f'{gid}.triage'
            try:
                if not group['eligible']:
                    result.update(status='too_large',reason='Separate route required; source not truncated')
                    return result
                pages = source_pages(group['source'])
                activity(gid,'triaging')
                request = gs.triage_payload(model,prompts,group['source'],manifest.get('reasoning','high'))
                request['max_tokens'] = manifest['max_output_tokens']
                triage = client.call(f'{gid}.triage',request)
                triage,warnings = gs.validate_triage(json.dumps(triage),pages,version='v5-triage')
                if triage is None:
                    raise ValueError(str(warnings))
                result.update(triage=triage,warnings=warnings,route=gs.route_triage(triage,list(pages)))
                result['candidate'] = None
                if result['route']['route'] == 'extract':
                    activity(gid,'extracting')
                    call_name = f'{gid}.extract'
                    extract_request = gs.extract_payload(request,json.dumps(triage),prompts,manifest.get('reasoning','high'))
                    client.recover_validation(call_name, extract_request,
                                              lambda value: recover_extract(value, pages))
                    extracted = client.call(call_name,extract_request)
                    candidate,warnings = gs.validate_extract(json.dumps(extracted),pages,version='v6')
                    if candidate is None:
                        raise ValueError(str(warnings))
                    candidate.pop('scouting',None)
                    result['candidate'] = candidate
                    result['warnings'].extend(warnings)
                result['status'] = 'complete' if result['candidate'] is None else 'extracted'
            except Exception as exc:
                client.reject(call_name,exc)
                result['error'] = str(exc)
                state_path = folder/'calls'/f'{call_name}.result.json'
                state = read(state_path) if state_path.exists() else {}
                result['error_type'] = ('budget' if 'Run spending cap reached' in str(exc)
                                        else state.get('error_type', 'unknown'))
                result['failed_call'] = call_name
            finally:
                save(folder/'first_pass'/f'{gid}.json',result)
                activity(gid,'idle')
                print(gid,result['status'],flush=True)
            return result
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for result in pool.map(first,manifest['groups']):
                outputs[result['group']] = result
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
        finish(folder,manifest,prompts,client,outputs,args.workers)
        for gid,result in outputs.items():
            path = folder/'results'/f'{gid}.json'
            if not path.exists() or read(path) != result:
                save(path,result)
        export(folder)
        print(f'Spent/reserved ${client.spent():.6f}; {sum(r["status"]=="complete" for r in outputs.values())}/{len(outputs)} complete')


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
        from tempfile import NamedTemporaryFile
        import os
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
    planning.add_argument('--model',default=os.getenv('APPEALS_MODEL'),required=not os.getenv('APPEALS_MODEL'))
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
