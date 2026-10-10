"""Export, check and browse legacy appeals-1 runs, such as the production run.

New runs are planned and executed with ``appeal_runner.py`` over pipeline
modules (see docs/PIPELINE.md). This tool only reads legacy run directories.
"""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from config import ROOT
from listen import DEFAULT_HOST
from appeal_assembly import finalized_dockets
from appeal_runtime import read, save

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
    for command in ('export','status'):
        s=sub.add_parser(command);s.add_argument('--name',required=True)
    checking=sub.add_parser('check')
    checking.add_argument('--name',required=True)
    checking.add_argument('--expectations',type=Path,default=ROOT/'tests/fixtures/appeals/release-expectations.json')
    serving=sub.add_parser('serve');serving.add_argument('--port',type=int,default=8769)
    serving.add_argument('--host',default=DEFAULT_HOST)
    args=p.parse_args()
    if hasattr(args,'name') and not SAFE.fullmatch(args.name):p.error('Invalid run name')
    if args.command=='export':
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
