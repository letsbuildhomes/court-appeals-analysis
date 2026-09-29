"""Read-only viewer of final appeal exports and frozen evidence."""
import html
import fcntl
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


def lines(path):
    import json
    return [json.loads(s) for s in path.read_text().splitlines() if s.strip()] if path.exists() else []


def run_folder(root, name):
    if not SAFE.fullmatch(name):raise ValueError('Invalid run name')
    folder = root/name
    if not (folder/'manifest.json').is_file():raise ValueError('Run not found')
    return folder


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
        activity=folder/'activity'/f'{gid}.json'
        if activity.exists() and (not path.exists() or activity.stat().st_mtime_ns>path.stat().st_mtime_ns):
            stage=read(activity)['stage']
            if stage in ('triaging','extracting','reviewing','measuring'):
                status=stage if running else 'interrupted'
        if not group.get('eligible',True):status='too_large'
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
                'source_documents':documents.get(gid,[])} for gid,dockets,count,title in rows}
    records,review={},{}
    manifests=sorted(root.glob('*/manifest.json'),key=lambda p:read(p)['created_at'])
    for manifest in manifests:
        state=overview(manifest.parent)
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
                       'planned':sum(c['run'] is not None for c in values),'running':False,'name':'All groups'},
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
