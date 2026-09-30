"""Repeat finishing on a fresh copy of a saved first pass; report usage and quality."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

from appeal_runtime import Client, api_config, read, save
from appeals import RUNS, SAFE, export, finish, locked, prepare
from scout import usage_counts, reasoning_tokens


def usage(folder):
    stages = {}
    for path in (folder/'calls').glob('*.result.json'):
        stage = next((s for s in ('review','duration') if path.name.startswith(s+'-')),None)
        if stage is None:
            continue
        totals = stages.setdefault(stage,dict(calls=0,input=0,cached=0,output=0,
                      reasoning=0,cost_usd=0,elapsed_seconds=0,failures=0))
        result = read(path)
        totals['calls'] += 1
        totals['failures'] += result.get('status') != 'ok'
        totals['cost_usd'] += result.get('charged_or_reserved_usd',0)
        totals['elapsed_seconds'] += result.get('elapsed_seconds',0)
        counts = usage_counts(result.get('usage'))
        if counts:
            prompt,cached,output,_ = counts
            totals['input'] += prompt
            totals['cached'] += cached or 0
            totals['output'] += output
            totals['reasoning'] += reasoning_tokens(result.get('usage')) or 0
    for totals in stages.values():
        totals['cached_fraction'] = totals['cached']/totals['input'] if totals['input'] else None
    return stages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-run',default='release-check')
    parser.add_argument('--name',default='sanity-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S'))
    parser.add_argument('--max-spend',type=float,default=1)
    parser.add_argument('--workers',type=int,default=3)
    args = parser.parse_args()
    if not all(SAFE.fullmatch(n) for n in (args.from_run,args.name)):
        parser.error('Invalid run name')
    if args.workers < 1 or args.max_spend <= 0:
        parser.error('Positive worker count and spending cap required')
    original,folder = RUNS/args.from_run,RUNS/args.name
    if folder.exists():
        parser.error('Choose a new sanity run name')
    manifest,prompts = prepare(original)
    outputs = {g['group_id']:read(original/'first_pass'/f'{g["group_id"]}.json') for g in manifest['groups']}
    if any(r['status'] not in {'extracted','complete'} for r in outputs.values()):
        parser.error('Sanity input must have a successful saved first pass')
    target_dockets = {d['docket'] for r in outputs.values()
                      for d in (r.get('candidate') or {}).get('dockets',[])}
    folder.mkdir()
    for name in ('sources','first_pass'):
        shutil.copytree(original/name,folder/name)
    shutil.copyfile(original/'prompts.json',folder/'prompts.json')
    (folder/'calls').mkdir()
    # Keep identical first-pass calls as a cache, without copying finishing calls.
    for path in (original/'calls').glob('*'):
        if path.is_file() and any(path.name.startswith(gid+'.') for gid in outputs):
            shutil.copyfile(path,folder/'calls'/path.name)
    manifest.update(name=args.name,review_batch_size=2,workers=args.workers,
                    finishing='review-duration-followup')
    save(folder/'manifest.json',{**manifest,'groups':[
        {k:v for k,v in g.items() if k!='source'} for g in manifest['groups']]})
    endpoint,key = api_config()
    client = Client(folder/'calls',endpoint,key,args.max_spend)
    started = datetime.now(timezone.utc)
    with locked(folder):
        finish(folder,manifest,prompts,client,outputs,args.workers)
        summary = export(folder)
    expected = read(Path(__file__).parent/'tests/fixtures/appeals/release-expectations.json')
    mismatches = []
    measured = set()
    details = []
    for gid,result in outputs.items():
        actual_scope = [result.get('triage',{}).get(k) for k in ('municipal_appeal','housing')]
        if gid in expected['scope'] and actual_scope != expected['scope'][gid]:
            mismatches.append({'group':gid,'scope':actual_scope,'expected':expected['scope'][gid]})
        bounds = result.get('duration_bounds',[])
        for bound in bounds:
            measured.add(bound['docket'])
            actual = [bound['duration']['kind'],bound['duration']['days']]
            if bound['docket'] in expected['duration'] and actual != expected['duration'][bound['docket']]:
                mismatches.append({'group':gid,'docket':bound['docket'],'duration':actual,
                                   'expected':expected['duration'][bound['docket']]})
        details.append({'group':gid,'status':result['status'],'error':result.get('error'),
                        'warnings':result.get('warnings',[]),'review':result.get('review'),
                        'duration_bounds':bounds})
    for docket in sorted(target_dockets-measured):
        if docket in expected['duration']:
            mismatches.append({'docket':docket,'duration':None,
                               'expected':expected['duration'][docket],'issue':'Missing duration output'})
    report = {'run':args.name,'baseline':args.from_run,
              'wall_seconds':(datetime.now(timezone.utc)-started).total_seconds(),
              'status_counts':dict(Counter(r['status'] for r in outputs.values())),
              'new_finishing_usage':usage(folder),'baseline_finishing_usage':usage(original),
              'fixture_durations_checked':len(measured & expected['duration'].keys()),
              'fixture_mismatches':mismatches,'groups':details,'summary':summary}
    save(folder/'sanity-report.json',report)
    print(json.dumps({k:v for k,v in report.items() if k!='groups'},indent=2),flush=True)
    print(f'Full quality report: {folder / "sanity-report.json"}',flush=True)


if __name__ == '__main__':
    main()
