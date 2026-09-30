"""Read-only viewer of final appeal exports and frozen evidence."""
import html
import fcntl
import json
import math
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from claims_server import ClaimsHandler as Handler, ClaimsServer
from appeal_runtime import read
from appeal_evidence import source_pages
from config import ROOT
from appeals import SAFE
import duckdb
from groups import GROUPS_DB_PATH
from appeal_stats import research_stats
from scout import usage_counts, reasoning_tokens
from group_scout import PRICES_PER_M


def live_usage(folder):
    """Estimate Flash costs per request, including archived retry attempts."""
    totals = dict(requests=0, input=0, cached=0, output=0, reasoning=0,
                  estimated_usd=0.0, reserved_usd=0.0, unreported=0, inferred_times=0)
    for path in (folder/'calls').rglob('*.result.json'):
        call = read(path)
        totals['requests'] += 1
        counts = usage_counts(call.get('usage'))
        if counts is None:
            totals['reserved_usd'] += call.get('charged_or_reserved_usd', 0)
            totals['unreported'] += 1
            continue
        stamp = call.get('requested_at')
        if stamp:
            when = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        else:
            # Frozen request files survive retries alongside their original results.
            request = path.with_name(path.name.replace('.result.json', '.request.json'))
            when = datetime.fromtimestamp((request if request.exists() else path).stat().st_mtime, timezone.utc)
            totals['inferred_times'] += 1
        hour = when.astimezone(timezone.utc).hour
        multiplier = 2 if 1 <= hour < 4 or 6 <= hour < 10 else 1
        prompt, cached, output, _ = counts
        cached = cached or 0
        totals['input'] += prompt
        totals['cached'] += cached
        totals['output'] += output
        totals['reasoning'] += reasoning_tokens(call.get('usage')) or 0
        totals['estimated_usd'] += multiplier * (
            cached * PRICES_PER_M['cache_hit'] + (prompt-cached) * PRICES_PER_M['cache_miss']
            + output * PRICES_PER_M['output']) / 1_000_000
    return totals


def elapsed_time(folder, running, now=None):
    """Wall time from the first saved request; stopped runs end at saved activity."""
    starts, activity = [], []
    for request in (folder/'calls').rglob('*.request.json'):
        try:
            start = request.stat().st_mtime
            result = request.with_name(request.name.replace('.request.json', '.result.json'))
            if result.exists():
                stamp = read(result).get('requested_at')
                if stamp:
                    parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                    start = parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
            starts.append(start)
        except FileNotFoundError:
            continue  # A running retry can move the files into its history directory.
    if not starts:
        return {'seconds':None, 'started_at':None, 'ended_at':None}
    for directory in ('calls', 'activity', 'first_pass', 'results'):
        for path in (folder/directory).rglob('*'):
            try:
                if path.is_file():activity.append(path.stat().st_mtime)
            except FileNotFoundError:
                continue
    start = min(starts)
    end = (now or datetime.now(timezone.utc)).timestamp() if running else max([*starts, *activity])
    return {'seconds':max(0, int(end-start)),
            'started_at':datetime.fromtimestamp(start, timezone.utc).isoformat(),
            'ended_at':None if running else datetime.fromtimestamp(end, timezone.utc).isoformat()}


def completion_estimates(folder, manifest, cases, running, now=None):
    """Central estimates from observed processing rates; no pipeline or API writes."""
    now = now or datetime.now(timezone.utc)
    if not cases:
        return {'first_pass':{'status':'learning'}, 'finalized':{'status':'learning'}}
    intervals, completed = [], {c['group'] for c in cases if c.get('first_pass_complete')}
    samples = {'review':[], 'duration':[]}

    def collect(root, first=False):
        for path in (root/'calls').rglob('*.result.json'):
            try:
                result = read(path)
                seconds = result.get('elapsed_seconds', 0)
                if result.get('status') != 'ok' or not isinstance(seconds, (int,float)) or seconds <= 0:
                    continue
                name = path.name.removesuffix('.result.json')
                request = path.with_name(name+'.request.json')
                if first and name.endswith(('.triage', '.extract')) and name.rsplit('.',1)[0] in completed:
                    start = request.stat().st_mtime
                    if result.get('requested_at'):
                        start = datetime.fromisoformat(result['requested_at'].replace('Z','+00:00')).timestamp()
                    intervals.append((start, start+seconds))
                stage = next((s for s in samples if name.startswith(s+'-') and '.' not in name), None)
                if stage:
                    payload = read(request)
                    members = json.loads(payload['messages'][-1]['content'])['cases']
                    if members:samples[stage].append(seconds/len(members))
            except (FileNotFoundError, KeyError, ValueError, TypeError):
                continue

    collect(folder, first=True)
    historical = set()
    missing = {s for s in samples if not samples[s]}
    if missing and manifest.get('model') and manifest.get('prompt_sha256'):
        for previous in sorted(folder.parent.glob('*/manifest.json')):
            if previous.parent == folder:continue
            old = read(previous)
            if any(old.get(k) != manifest.get(k) for k in ('model','prompt_sha256','review_batch_size','review_batch_chars')):continue
            current = {s:list(v) for s,v in samples.items()}
            collect(previous.parent)
            for stage in samples:
                if stage not in missing:samples[stage] = current[stage]
                elif len(samples[stage]) > len(current[stage]):historical.add(stage)

    busy, end = 0, 0
    for start, stop in sorted(intervals):
        busy += max(0, stop-max(start,end))
        end = max(end, stop)
    # Issues remain visible in progress, but do not represent scheduled work in the ETA.
    issue_statuses = {'incomplete','too_large','interrupted','review_failed','duration_failed','scope_flagged'}
    work = [c for c in cases if c['status'] not in issue_statuses]
    remaining_first = sum(not c.get('first_pass_complete') for c in work)
    first_seconds = (busy/len(completed)*remaining_first
                     if len(completed)>=3 and intervals else None)
    if not remaining_first:first_seconds = 0
    assessed = [c for c in cases if c.get('first_pass_complete')]
    retained_share = (sum(c.get('route',{}).get('route')=='extract' for c in assessed)/len(assessed)
                      if assessed else None)
    review_remaining = sum(c['status'] not in {'reviewed','complete','measuring'} for c in work)
    duration_remaining = sum(c['status']!='complete' and c.get('route',{}).get('route')=='extract'
                             for c in work if c.get('first_pass_complete'))
    if retained_share is not None:duration_remaining += remaining_first*retained_share
    final_seconds = first_seconds
    for stage, count in [('review',review_remaining),('duration',duration_remaining)]:
        if count:
            if not samples[stage] or final_seconds is None:final_seconds = None
            else:final_seconds += sum(samples[stage])/len(samples[stage])*count
    if remaining_first and retained_share is None:final_seconds = None

    def estimate(seconds, complete, finished, history=False):
        if complete:return {'status':'complete'}
        if finished:return {'status':'finished'}
        if seconds is None:return {'status':'learning'}
        seconds = max(1, math.ceil(seconds))
        return {'status':'running' if running else 'paused', 'remaining_seconds':seconds,
                'finish_at':datetime.fromtimestamp(now.timestamp()+seconds,timezone.utc).isoformat() if running else None,
                'historical':history}
    return {'first_pass':estimate(first_seconds, all(c.get('first_pass_complete') for c in cases), not remaining_first),
            'finalized':estimate(final_seconds, bool(cases) and all(c['status']=='complete' for c in cases),
                                 not any(c['status']!='complete' for c in work), bool(historical))}


def lines(path):
    import json
    return [json.loads(s) for s in path.read_text().splitlines() if s.strip()] if path.exists() else []


def run_folder(root, name):
    if not SAFE.fullmatch(name):raise ValueError('Invalid run name')
    folder = root/name
    if not (folder/'manifest.json').is_file():raise ValueError('Run not found')
    return folder


def first_pass_status(record, status):
    """Keep first-pass outcomes independent of subsequent review and duration work."""
    if record.get('status') in {'extracted','reviewed','complete','review_failed','duration_failed','scope_flagged'}:
        route = record.get('route',{}).get('route')
        if route == 'extract':return 'retained'
        if route in {'out_of_scope','no_members'}:return 'excluded'
        return 'issues'
    if status in {'triaging','extracting'}:return 'active'
    if status in {'incomplete','interrupted','too_large'}:return 'issues'
    return 'waiting'


def processing_progress(cases):
    """Count saved milestones, with source PDFs deduplicated by checksum."""
    documents, first_pass, complete = set(), set(), set()
    for case in cases:
        sources = {d['sha'] for d in case['source_documents']}
        documents.update(sources)
        if case.get('first_pass_complete'):
            first_pass.update(sources)
        if case['status'] == 'complete':
            complete.update(sources)
    return {'groups':len(cases),
            'first_pass_groups':sum(c.get('first_pass_complete',False) for c in cases),
            'complete_groups':sum(c['status']=='complete' for c in cases),
            'documents':len(documents),'first_pass_documents':len(first_pass),
            'complete_documents':len(complete)}


def overview(folder):
    manifest = read(folder/'manifest.json')
    running=False
    if (folder/'.lock').exists():
        with (folder/'.lock').open('r') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
            except BlockingIOError:running=True
    cases,records,review=[],[],[]
    for group in manifest['groups']:
        gid=group['group_id']
        paths=[p for p in [folder/'results'/f'{gid}.json',folder/'first_pass'/f'{gid}.json'] if p.exists()]
        path=max(paths,key=lambda p:p.stat().st_mtime_ns) if paths else folder/'results'/f'{gid}.json'
        result=read(path) if path.exists() else {'status':'pending'}
        status=result['status']
        # Later review failures or interruption do not erase a successful first pass.
        first_pass_complete=status in {'extracted','reviewed','complete',
                                      'review_failed','duration_failed','scope_flagged'}
        activity=folder/'activity'/f'{gid}.json'
        if activity.exists() and (not path.exists() or activity.stat().st_mtime_ns>path.stat().st_mtime_ns):
            stage=read(activity)['stage']
            if stage in ('triaging','extracting','reviewing','measuring'):
                status=stage if running else 'interrupted'
        if not group.get('eligible',True):status='too_large'
        first_path=folder/'first_pass'/f'{gid}.json'
        first_record=read(first_path) if first_path.exists() else result
        scope=result.get('triage',{})
        reasons=list(dict.fromkeys(result.get('warnings',[])))
        reasons.extend(f['issue'] for f in result.get('review',{}).get('flags',[]))
        if 'unclear' in [scope.get('housing'),scope.get('municipal_appeal')]:reasons.append('Housing or municipal scope needs confirmation')
        if result.get('error'):reasons.append(result['error'])
        if reasons:review.append({'group':gid,'reasons':reasons})
        docs=group['documents']
        title=next((d.get('title') for d in reversed(docs) if d.get('title')),gid)
        cases.append({'group':gid,'run':manifest['name'],'dockets':group['dockets'],
                      'title':title,'description':scope.get('matter'),
                      'status':status,'route':result.get('route',{}),'scope':scope,
                      'first_pass_complete':first_pass_complete,
                      'first_pass_status':first_pass_status(first_record,status),
                      'scope_review':result.get('review',{}).get('scope',{}),
                      'documents':len(docs),'source_documents':docs,'review_required':bool(reasons)})
        # Read completed checkpoints directly so the dashboard need not wait for end-of-run export.
        if result['status']=='complete':
            bounds={b['docket']:b for b in result.get('duration_bounds',[])}
            for r in (result.get('candidate') or {}).get('dockets',[]):
                b=bounds.get(r['docket'])
                if b is None:continue
                clean={k:v for k,v in r.items() if k not in ('scouting','uncertainties','suggestions')}
                clean.update(duration=b['duration'],duration_bounds={k:v for k,v in b.items() if k not in ('docket','duration')})
                records.append({'group':gid,'run':manifest['name'],'scope_confirmed':all(scope.get(k)=='yes' for k in ('municipal_appeal','housing')),'review_required':bool(reasons),'record':clean})
    calls=[read(p) for p in (folder/'calls').rglob('*.result.json')]
    summary={'name':manifest['name'],'groups':len(cases),'complete':sum(c['status']=='complete' for c in cases),
             'records':len(records),'running':running,'review_groups':len(review),
             'cost_usd':sum(c.get('charged_or_reserved_usd',0) for c in calls),
             'usage':live_usage(folder),
             'progress':processing_progress(cases),
             'elapsed':elapsed_time(folder, running),
             'estimates':completion_estimates(folder, manifest, cases, running),
             'failed':sum(c['status'] in ('incomplete','review_failed','duration_failed','interrupted','too_large','scope_flagged') for c in cases)}
    return {'summary':summary,'cases':cases,'records':records,'review':review,
            'research':research_stats(cases,records),'updated_at':datetime.now(timezone.utc).isoformat()}


def archive_overview(root):
    """All upstream groups, overlaid with the most recent planned run per group."""
    with duckdb.connect(str(GROUPS_DB_PATH),read_only=True) as db:
        rows=db.execute('''SELECT g.group_id,g.dockets,g.doc_count,
            (SELECT listing_title FROM group_documents d WHERE d.group_id=g.group_id
             ORDER BY ordinal DESC LIMIT 1) FROM groups g ORDER BY g.group_id''').fetchall()
        documents={}
        for gid,ordinal,sha,title,pages in db.execute('SELECT group_id,ordinal,pdf_sha256,listing_title,pages FROM group_documents ORDER BY group_id,ordinal').fetchall():
            documents.setdefault(gid,[]).append({'doc':ordinal,'sha':sha,'title':title,'pages':pages})
    cases={gid:{'group':gid,'run':None,'dockets':dockets,'documents':count,'title':title or gid,
                'status':'not_planned','scope':{},'route':{},'review_required':False,
                'first_pass_complete':False,
                'source_documents':documents.get(gid,[])} for gid,dockets,count,title in rows}
    records,review={},{}
    usage = None
    manifests=sorted(root.glob('*/manifest.json'),key=lambda p:read(p)['created_at'])
    for manifest in manifests:
        state=overview(manifest.parent)
        if usage is None:
            usage = dict(state['summary']['usage'])
        else:
            for key, value in state['summary']['usage'].items():usage[key] += value
        grouped={}
        for r in state['records']:grouped.setdefault(r['group'],[]).append(r)
        notes={r['group']:r for r in state['review']}
        for c in state['cases']:
            cases[c['group']]=c
            records[c['group']]=grouped.get(c['group'],[])
            review[c['group']]=notes.get(c['group'])
    values=list(cases.values())
    selected_records=[r for batch in records.values() for r in batch]
    return {'cases':values,'records':selected_records,
            'review':[r for r in review.values() if r],
            'research':research_stats(values,selected_records),
            'summary':{'groups':len(values),'complete':sum(c['status']=='complete' for c in values),
                       'planned':sum(c['run'] is not None for c in values),'running':False,'name':'All groups',
                       'progress':processing_progress(values),
                       'usage':usage},
            'updated_at':datetime.now(timezone.utc).isoformat()}


def serve(root, port):
    root=Path(root)
    class Viewer(Handler):
        def log_message(self,*args):pass
        def do_GET(self):
            url=urlparse(self.path)
            query=parse_qs(url.query)
            try:
                if url.path=='/':
                    return self.send_body(HTTPStatus.OK,'text/html; charset=utf-8',(ROOT/'appeals.html').read_bytes())
                if url.path in ('/appeals.css','/appeals.js','/appeal_controls.js'):
                    content_type='text/css' if url.path.endswith('.css') else 'application/javascript'
                    return self.send_body(HTTPStatus.OK,content_type+'; charset=utf-8',(ROOT/url.path.lstrip('/')).read_bytes())
                if url.path=='/api/runs':
                    runs=[{'name':p.parent.name,'created_at':read(p)['created_at']} for p in root.glob('*/manifest.json')]
                    return self.send_json(HTTPStatus.OK,sorted(runs,key=lambda x:x['created_at'],reverse=True))
                if url.path=='/api/groups':return self.send_json(HTTPStatus.OK,archive_overview(root))
                if url.path in {'/api/run','/api/source'}:
                    folder=run_folder(root,query.get('run',[''])[0])
                    if url.path=='/api/run':return self.send_json(HTTPStatus.OK,overview(folder))
                    gid=query.get('group',[''])[0]
                    group=next((g for g in read(folder/'manifest.json')['groups'] if g['group_id']==gid),None)
                    if group is None:raise ValueError('Group not in this run')
                    doc,page=int(query.get('doc',['0'])[0]),int(query.get('page',['0'])[0])
                    metadata=next((d for d in group['documents'] if d['doc']==doc),None)
                    pages=source_pages((folder/'sources'/f'{gid}.txt').read_text())
                    if metadata is None or page not in pages.get(doc,{}):raise ValueError('Source page not found')
                    return self.send_json(HTTPStatus.OK,{'text':html.unescape(pages[doc][page]),'sha':metadata['sha'],'title':metadata.get('title'),'page':page})
                if url.path in {'/api/pdf','/api/render'}:return super().do_GET()
                self.send_json(HTTPStatus.NOT_FOUND,{'error':'Not found'})
            except (ValueError,KeyError,OSError) as exc:
                self.send_json(HTTPStatus.BAD_REQUEST,{'error':str(exc)})
    print(f'Appeal explorer: http://127.0.0.1:{port}',flush=True)
    ClaimsServer(('127.0.0.1',port),Viewer).serve_forever()
