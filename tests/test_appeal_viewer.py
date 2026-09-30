"""Live monitoring uses checkpoints, not stale export totals or inferred completion."""
import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import duckdb

from appeal_runtime import save
from appeal_viewer import archive_overview, overview, live_usage, elapsed_time, completion_estimates, first_pass_status


class LiveViewerTests(unittest.TestCase):
    def test_first_pass_categories(self):
        for route,expected in [('extract','retained'),('out_of_scope','excluded'),('no_members','excluded')]:
            for later in ['reviewing','measuring','interrupted','review_failed','complete']:
                self.assertEqual(first_pass_status({'status':'extracted','route':{'route':route}},later),expected)
        for status,expected in [('triaging','active'),('extracting','active'),('incomplete','issues'),
                                ('interrupted','issues'),('too_large','issues'),('pending','waiting')]:
            self.assertEqual(first_pass_status({'status':'pending'},status),expected)

    def test_saved_first_pass_outcome_survives_later_result(self):
        save(self.folder/'first_pass/g-a.json',{'status':'extracted','route':{'route':'out_of_scope'}})
        save(self.folder/'results/g-a.json',{'status':'review_failed','route':{'route':'extract'}})
        self.assertEqual(overview(self.folder)['cases'][0]['first_pass_status'],'excluded')

    def test_estimates_exclude_idle_gaps_and_add_sequential_stages(self):
        cases=[]
        for i in range(6):
            cases.append({'group':f'g-{i}','status':'extracted' if i<3 else 'pending',
                          'first_pass_complete':i<3,'route':{'route':'extract'} if i<3 else {}})
        for i in range(3):
            # Long idle gaps do not change the 60-second-per-group rate.
            save(self.folder/f'calls/g-{i}.triage.request.json',{})
            save(self.folder/f'calls/g-{i}.triage.result.json',{
                'status':'ok','elapsed_seconds':60,'requested_at':f'2026-09-29T0{i}:00:00Z'})
        for stage,seconds in [('review',120),('duration',60)]:
            save(self.folder/f'calls/{stage}-sample.request.json',{
                'messages':[{'content':json.dumps({'cases':[{},{}]})}]})
            save(self.folder/f'calls/{stage}-sample.result.json',{'status':'ok','elapsed_seconds':seconds})
        result=completion_estimates(self.folder,{},cases,True)
        self.assertEqual(result['first_pass']['remaining_seconds'],180)
        self.assertEqual(result['finalized']['remaining_seconds'],720)
        self.assertIsNotNone(result['finalized']['finish_at'])
        paused=completion_estimates(self.folder,{},cases,False)
        self.assertEqual(paused['first_pass']['status'],'paused')
        self.assertIsNone(paused['first_pass']['finish_at'])
        cases[0]['status']='review_failed'
        now=datetime(2026,9,29,tzinfo=timezone.utc)
        updated=completion_estimates(self.folder,{},cases,True,now)
        self.assertEqual(updated['finalized']['status'],'running')
        self.assertEqual(updated['finalized']['remaining_seconds'],630)
        self.assertEqual(updated['first_pass']['remaining_seconds'],180)
        finish=datetime.fromisoformat(updated['finalized']['finish_at'])
        self.assertEqual((finish-now).total_seconds(),630)
        for status in ('incomplete','too_large','interrupted','review_failed','duration_failed','scope_flagged'):
            cases.append({'group':status,'status':status,'first_pass_complete':False})
        self.assertEqual(completion_estimates(self.folder,{},cases,True,now),updated)

    def test_estimates_finish_when_only_issues_remain(self):
        cases=[{'group':'g','status':'incomplete','first_pass_complete':False}]
        for running in (True,False):
            self.assertEqual(completion_estimates(self.folder,{},cases,running),{
                'first_pass':{'status':'finished'},'finalized':{'status':'finished'}})
        cases[0]['status']='triaging'
        self.assertEqual(completion_estimates(self.folder,{},cases,True)['first_pass']['status'],'learning')

    def test_estimates_learning_and_complete(self):
        cases=[{'group':'g','status':'pending','first_pass_complete':False}]
        self.assertEqual(completion_estimates(self.folder,{},cases,True)['first_pass']['status'],'learning')
        cases[0].update(status='complete',first_pass_complete=True)
        result=completion_estimates(self.folder,{},cases,False)
        self.assertEqual(result,{'first_pass':{'status':'complete'},'finalized':{'status':'complete'}})

    def test_estimates_only_use_matching_history(self):
        manifest={'model':'flash','prompt_sha256':'same','review_batch_size':6,'review_batch_chars':200000}
        old=self.root/'previous'
        save(old/'manifest.json',manifest)
        for stage in ('review','duration'):
            save(old/f'calls/{stage}-sample.request.json',{'messages':[{'content':'{"cases":[{}]}'}]})
            save(old/f'calls/{stage}-sample.result.json',{'status':'ok','elapsed_seconds':60})
        cases=[{'group':'g','status':'extracted','first_pass_complete':True,'route':{'route':'extract'}}]
        result=completion_estimates(self.folder,manifest,cases,True)['finalized']
        self.assertEqual(result['remaining_seconds'],120)
        self.assertTrue(result['historical'])
        self.assertEqual(completion_estimates(self.folder,{**manifest,'model':'other'},cases,True)['finalized']['status'],'learning')

    def test_elapsed_not_started_even_with_planning_or_activity(self):
        save(self.folder/'activity/g-a.json',{'stage':'triaging'})
        self.assertIsNone(elapsed_time(self.folder, True)['seconds'])

    def test_elapsed_legacy_retry_history_freezes_and_resumes(self):
        def saved(relative, timestamp, value):
            path=self.folder/relative
            save(path,value)
            os.utime(path,(timestamp,timestamp))
        saved('calls/attempts/old/g-a.request.json',100,{})
        saved('calls/attempts/old/g-a.result.json',150,{'status':'failed'})
        saved('calls/g-a.request.json',300,{})
        saved('calls/g-a.result.json',400,{'status':'ok'})
        saved('activity/g-a.json',420,{'stage':'idle'})
        saved('results/g-a.json',430,{'status':'complete'})
        saved('export/summary.json',900,{})  # Reading/exporting later is not processing.
        now=datetime.fromtimestamp(1000,timezone.utc)
        self.assertEqual(elapsed_time(self.folder,False,now)['seconds'],330)
        self.assertEqual(elapsed_time(self.folder,True,now)['seconds'],900)
        self.assertIsNone(elapsed_time(self.folder,True,now)['ended_at'])
        self.assertEqual(elapsed_time(self.folder,False,now)['seconds'],330)

    def test_elapsed_prefers_explicit_request_time(self):
        request=self.folder/'calls/g-a.request.json'
        result=self.folder/'calls/g-a.result.json'
        save(request,{})
        save(result,{'requested_at':'1970-01-01T01:01:40+01:00'})
        os.utime(request,(200,200))
        os.utime(result,(300,300))
        self.assertEqual(elapsed_time(self.folder,False)['seconds'],200)

    def test_live_cost_peak_boundaries_and_retries(self):
        usage={'prompt_tokens':1000000,'prompt_cache_hit_tokens':400000,
               'completion_tokens':100000,'completion_tokens_details':{'reasoning_tokens':10}}
        for hour, multiplier in [(0,1),(1,2),(3,2),(4,1),(6,2),(9,2),(10,1)]:
            with self.subTest(hour=hour), TemporaryDirectory() as temp:
                folder=Path(temp)
                save(folder/'calls'/'a.result.json',{'usage':usage,'requested_at':f'2026-09-29T{hour:02}:00:00Z'})
                save(folder/'calls'/'attempts'/'retry'/'a.result.json',{'usage':usage,'requested_at':'2026-09-29T12:00:00Z'})
                save(folder/'calls'/'pending.result.json',{'status':'pending','charged_or_reserved_usd':0.5})
                totals=live_usage(folder)
                self.assertAlmostEqual(totals['estimated_usd'],0.1512*(multiplier+1))
                self.assertEqual(totals['reserved_usd'],0.5)
                self.assertEqual(totals['requests'],3)
                self.assertEqual(totals['input'],2000000)
                self.assertEqual(totals['cached'],800000)
                self.assertEqual(totals['output'],200000)
                self.assertEqual(totals['reasoning'],20)

    def test_live_cost_legacy_request_timestamp(self):
        save(self.folder/'calls'/'a.result.json',{'usage':{'prompt_tokens':1000000,'completion_tokens':0}})
        request=self.folder/'calls'/'a.request.json'
        save(request,{})
        from datetime import datetime, timezone
        stamp=datetime(2026,9,29,2,tzinfo=timezone.utc).timestamp()
        os.utime(request,(stamp,stamp))
        totals=live_usage(self.folder)
        self.assertAlmostEqual(totals['estimated_usd'],0.3)
        self.assertEqual(totals['inferred_times'],1)

    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.folder=self.root/'first'
        self.group={'group_id':'g-a','dockets':['A'],'eligible':True,
                    'documents':[{'doc':1,'title':'Named project','sha':'pdf','pages':2}]}
        save(self.folder/'manifest.json',{'name':'first','created_at':'2026-09-01','groups':[self.group]})

    def completed(self):
        return {'status':'complete','triage':{'municipal_appeal':'yes','housing':'yes'},'route':{'route':'extract'},
                'candidate':{'dockets':[{'docket':'A','project':{'description':'Project'},'scouting':{}}]},
                'duration_bounds':[{'docket':'A','start':None,'end':None,'note':'Unknown',
                                    'duration':{'kind':'unknown','days':None}}]}

    def test_completion_and_records_update_before_export(self):
        save(self.folder/'export/summary.json',{'complete':0,'records':0})
        save(self.folder/'results/g-a.json',self.completed())
        state=overview(self.folder)
        self.assertEqual(state['summary']['complete'],1)
        self.assertEqual(state['summary']['records'],1)
        self.assertEqual(state['summary']['progress'],{
            'groups':1,'first_pass_groups':1,'complete_groups':1,
            'documents':1,'first_pass_documents':1,'complete_documents':1})
        self.assertEqual(state['cases'][0]['title'],'Named project')
        self.assertNotIn('scouting',state['records'][0]['record'])
        self.assertEqual(state['records'][0]['record']['duration']['kind'],'unknown')
        self.assertEqual(len(state['research']['appeals']),1)
        self.assertIsNone(state['research']['duration']['mean_supported_days'])

    def test_live_stage_and_interruption_do_not_masquerade_as_completion(self):
        save(self.folder/'results/g-a.json',{'status':'extracted','triage':{},'candidate':{'dockets':[{'docket':'A'}]}})
        save(self.folder/'activity/g-a.json',{'stage':'reviewing'})
        os.utime(self.folder/'results/g-a.json',(100,100))
        os.utime(self.folder/'activity/g-a.json',(200,200))
        with (self.folder/'.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            state=overview(self.folder)
            self.assertTrue(state['summary']['running'])
            self.assertEqual(state['cases'][0]['status'],'reviewing')
            self.assertEqual(state['summary']['progress']['first_pass_documents'],1)
            self.assertEqual(state['summary']['progress']['complete_documents'],0)
            self.assertEqual(state['records'],[])
        state=overview(self.folder)
        self.assertFalse(state['summary']['running'])
        self.assertEqual(state['cases'][0]['status'],'interrupted')
        self.assertEqual(state['summary']['progress']['first_pass_groups'],1)

    def test_first_pass_coverage_counts_documents_and_keeps_failures_separate(self):
        statuses=['extracted','review_failed','duration_failed','scope_flagged',
                  'incomplete','too_large','pending']
        groups=[]
        for index,status in enumerate(statuses):
            gid=f'g-{index}'
            groups.append({**self.group,'group_id':gid,
                           'documents':[{'doc':n+1,'sha':f'{index}-{n}'} for n in range(index+1)]})
            if status!='pending':save(self.folder/'first_pass'/f'{gid}.json',{'status':status})
        save(self.folder/'manifest.json',{'name':'first','groups':groups})
        progress=overview(self.folder)['summary']['progress']
        self.assertEqual(progress,{'groups':7,'first_pass_groups':4,'complete_groups':0,
                                   'documents':28,'first_pass_documents':10,'complete_documents':0})

    def test_newest_checkpoint_wins_and_exclusions_complete_without_records(self):
        save(self.folder/'results/g-a.json',{'status':'review_failed','error':'Old error'})
        save(self.folder/'first_pass/g-a.json',{'status':'extracted','triage':{'housing':'no'}})
        os.utime(self.folder/'results/g-a.json',(100,100))
        os.utime(self.folder/'first_pass/g-a.json',(200,200))
        self.assertEqual(overview(self.folder)['cases'][0]['status'],'extracted')
        scope_review={'verdict':'agree','reason':'Not new housing.',
                      'evidence':[{'doc':1,'page':1,'quote':'Existing home'}]}
        save(self.folder/'results/g-a.json',{'status':'complete','triage':{'housing':'no','municipal_appeal':'yes'},
                                           'route':{'route':'out_of_scope'},'candidate':None,
                                           'review':{'scope':scope_review}})
        state=overview(self.folder)
        self.assertEqual(state['summary']['complete'],1)
        self.assertEqual(state['summary']['progress']['first_pass_documents'],1)
        self.assertEqual(state['summary']['progress']['complete_documents'],1)
        self.assertEqual(state['records'],[])
        self.assertEqual(state['research']['appeals'],[])
        self.assertEqual(state['cases'][0]['scope_review'],scope_review)

    def test_all_archive_groups_and_latest_run_are_explicit(self):
        database=self.root/'groups.duckdb'
        with duckdb.connect(str(database)) as db:
            db.execute('CREATE TABLE groups(group_id VARCHAR,dockets VARCHAR[],doc_count INT)')
            db.execute("INSERT INTO groups VALUES ('g-a',['A'],1),('g-b',['B'],1)")
            db.execute('CREATE TABLE group_documents(group_id VARCHAR,ordinal INT,pdf_sha256 VARCHAR,listing_title VARCHAR,pages INT)')
            db.execute("INSERT INTO group_documents VALUES ('g-a',1,'sha-a','Project A',1),('g-b',1,'sha-b','Project B',2)")
        save(self.folder/'results/g-a.json',self.completed())
        later=self.root/'second'
        save(later/'manifest.json',{'name':'second','created_at':'2026-09-02','groups':[self.group]})
        with patch('appeal_viewer.GROUPS_DB_PATH',database):state=archive_overview(self.root)
        cases={c['group']:c for c in state['cases']}
        self.assertEqual(state['summary']['groups'],2)
        self.assertEqual(state['summary']['planned'],1)
        self.assertEqual(state['summary']['progress'],{
            'groups':2,'first_pass_groups':0,'complete_groups':0,
            'documents':2,'first_pass_documents':0,'complete_documents':0})
        self.assertEqual(cases['g-a']['run'],'second')
        self.assertEqual(cases['g-a']['status'],'pending')
        self.assertEqual(cases['g-b']['status'],'not_planned')
        self.assertEqual(cases['g-b']['source_documents'][0]['sha'],'sha-b')
        self.assertEqual(state['records'],[])

        save(later/'first_pass/g-a.json',{'status':'extracted'})
        with patch('appeal_viewer.GROUPS_DB_PATH',database):state=archive_overview(self.root)
        self.assertEqual(state['summary']['progress']['first_pass_documents'],1)
        self.assertEqual(state['summary']['progress']['complete_documents'],0)


if __name__=='__main__':unittest.main()
