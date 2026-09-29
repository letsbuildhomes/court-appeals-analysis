"""Live monitoring uses checkpoints, not stale export totals or inferred completion."""
import fcntl
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import duckdb

from appeal_runtime import save
from appeal_viewer import archive_overview, overview, live_usage


class LiveViewerTests(unittest.TestCase):
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
            self.assertEqual(state['records'],[])
        state=overview(self.folder)
        self.assertFalse(state['summary']['running'])
        self.assertEqual(state['cases'][0]['status'],'interrupted')

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
        self.assertEqual(cases['g-a']['run'],'second')
        self.assertEqual(cases['g-a']['status'],'pending')
        self.assertEqual(cases['g-b']['status'],'not_planned')
        self.assertEqual(cases['g-b']['source_documents'][0]['sha'],'sha-b')
        self.assertEqual(state['records'],[])


if __name__=='__main__':unittest.main()
