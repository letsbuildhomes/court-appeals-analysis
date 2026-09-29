import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from appeal_evidence import interval, apply_review
from appeal_runtime import Client, api_config, payload, read, save
from appeals import exact_members, export, packs
from appeal_viewer import run_folder


class PipelineTests(unittest.TestCase):
    def test_direct_api_configuration(self):
        with patch.dict('os.environ',{},clear=True):
            self.assertEqual(api_config('https://provider.example/v1/',key='secret'),('https://provider.example/v1/chat/completions','secret'))
            self.assertEqual(api_config(api_url='https://provider.example/chat/completions',key='secret')[0],'https://provider.example/chat/completions')
            with self.assertRaises(ValueError):api_config()
            with self.assertRaises(ValueError):api_config('https://x',api_url='https://y',key='k')

    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_resume_is_free_and_refuses_changed_requests(self):
        client=Client(self.root,'http://localhost','unused',1)
        req=payload('model','Return JSON',{})
        response=Mock(status_code=200)
        response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}],
                                    'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}}
        with patch('appeal_runtime.httpx.Client') as transport:
            transport.return_value.__enter__.return_value.post.return_value=response
            self.assertEqual(client.call('case',req),{'ok':True})
            spent=client.spent()
            self.assertEqual(client.call('case',req),{'ok':True})
            self.assertEqual(transport.call_count,1)
            self.assertEqual(client.spent(),spent)
            with self.assertRaisesRegex(ValueError,'changed'):
                client.call('case',payload('other','Return JSON',{}))

    def test_budget_and_uncertain_delivery_do_not_send(self):
        req=payload('model','JSON',{})
        client=Client(self.root,'http://localhost','unused',0)
        with patch('appeal_runtime.httpx.Client') as transport:
            with self.assertRaisesRegex(ValueError,'cap'):client.call('case',req)
            transport.assert_not_called()
        save(self.root/'case.request.json',req)
        save(self.root/'case.result.json',{'status':'pending','charged_or_reserved_usd':0.1})
        client.limit=1
        with self.assertRaisesRegex(ValueError,'no automatic resend'):client.call('case',req)

    def test_completed_malformed_response_has_one_format_repair(self):
        req=payload('model','JSON',{})
        save(self.root/'case.request.json',req)
        save(self.root/'case.result.json',{'status':'failed','error':'Invalid JSON object','charged_or_reserved_usd':0.02})
        save(self.root/'case.response.json',{'choices':[{'message':{'content':'broken'}}]})
        response=Mock(status_code=200)
        response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':'{"fixed":true}'}}],
                                    'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}}
        client=Client(self.root,'http://localhost','unused',1)
        with patch('appeal_runtime.httpx.Client') as transport:
            transport.return_value.__enter__.return_value.post.return_value=response
            self.assertEqual(client.call('case',req),{'fixed':True})
            self.assertEqual(client.call('case',req),{'fixed':True})
            self.assertEqual(transport.call_count,1)
        self.assertEqual(read(self.root/'case.response.json')['choices'][0]['message']['content'],'broken')
        self.assertGreater(client.spent(),0.02)

    def test_batch_boundaries_and_membership(self):
        items=[{'source':'x'*10,'candidate':None} for _ in range(7)]
        self.assertEqual([len(b) for b in packs(items,6,10000)],[6,1])
        self.assertEqual([len(b) for b in packs(items,6,20)],[1]*7)
        with self.assertRaises(ValueError):exact_members([{'group':'a'},{'group':'a'}],'group',['a','b'])
        with self.assertRaises(ValueError):exact_members([{'docket':None},{'docket':None}],'docket',[None,None])

    def test_output_limit_retry_preserves_cost_and_success_cache(self):
        req=payload('model','JSON',{})
        limited=Mock(status_code=200,text='partial')
        limited.json.return_value={'choices':[{'finish_reason':'length','message':{'content':'{"partial":'}}], 'usage':{'prompt_tokens':10,'completion_tokens':20}}
        complete=Mock(status_code=200,text='complete')
        complete.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}], 'usage':{'prompt_tokens':10,'completion_tokens':30}}
        with patch('appeal_runtime.httpx.Client') as transport:
            post=transport.return_value.__enter__.return_value.post
            post.side_effect=[limited,complete]
            client=Client(self.root,'https://provider','secret',1)
            with self.assertRaisesRegex(ValueError,'Incomplete'):client.call('case',req)
            state=read(self.root/'case.result.json')
            self.assertEqual(state['error_type'],'output_limit')
            self.assertEqual(state['finish_reason'],'length')
            retry=Client(self.root,'https://provider','secret',1,retry_failed=True,retry_max_tokens=131072)
            self.assertEqual(retry.call('case',req),{'ok':True})
            self.assertEqual(post.call_args.kwargs['json']['max_tokens'],131072)
            self.assertEqual(retry.call('case',req),{'ok':True})
            self.assertEqual(post.call_count,2)
            self.assertGreater(retry.spent(),state['charged_or_reserved_usd'])
            self.assertEqual(len(list((self.root/'attempts').rglob('*.response.json'))),1)

    def test_schema_failure_and_non_json_http_are_recorded(self):
        req=payload('model','JSON',{})
        client=Client(self.root,'https://provider','secret',1)
        save(self.root/'case.result.json',{'status':'ok','charged_or_reserved_usd':0.01})
        client.reject('case','Missing required dockets')
        self.assertEqual(read(self.root/'case.result.json')['error_type'],'validation')
        response=Mock(status_code=503,text='Service unavailable')
        response.json.side_effect=ValueError('not JSON')
        with patch('appeal_runtime.httpx.Client') as transport:
            transport.return_value.__enter__.return_value.post.return_value=response
            with self.assertRaises(ValueError):client.call('other',req)
        self.assertEqual((self.root/'other.response.txt').read_text(),'Service unavailable')
        self.assertEqual(read(self.root/'other.result.json')['http_status'],503)

    def test_partial_drafts_are_not_exported_and_feedback_removed(self):
        save(self.root/'manifest.json',{'name':'test','groups':[{'group_id':'a','documents':[]},{'group_id':'b','documents':[]}]})
        draft={'dockets':[{'docket':'A','scouting':{'suggestions':['x']}}]}
        result={'status':'duration_failed','candidate':draft,'triage':{'housing':'yes','municipal_appeal':'yes'}}
        save(self.root/'results/a.json',result)
        result.update(status='complete',duration_bounds=[{'docket':'A','start':None,'end':None,'note':'Unknown',
                      'duration':{'kind':'unknown','days':None}}])
        save(self.root/'results/b.json',result)
        summary=export(self.root)
        self.assertEqual(summary['records'],1)
        row=json.loads((self.root/'export/records.jsonl').read_text())
        self.assertEqual(row['group'],'b')
        self.assertNotIn('scouting',row['record'])
        self.assertEqual(row['record']['duration']['kind'],'unknown')

    def test_viewer_disallows_path_traversal(self):
        for name in ['../secrets','a/b','/tmp','']:
            with self.assertRaises(ValueError):run_folder(self.root,name)

    def test_citation_correction_provenance_does_not_break_review(self):
        old={'dockets':[{'docket':'A','project':{'evidence':[{'doc':1,'page':2,'quote':'Fact','cited_as':{'doc':1,'page':1}}]}}]}
        review={'schema_version':'v6-review-1','scope':{'verdict':'agree'},'changes':[],'flags':[]}
        def validate(raw,*args,**kwargs):
            value=json.loads(raw)
            self.assertNotIn('cited_as',value['dockets'][0]['project']['evidence'][0])
            return value,[]
        with patch('appeal_evidence.gs.validate_extract',side_effect=validate):
            candidate,warnings=apply_review(old,review,{1:{2:'Fact'}})
        self.assertIn('cited_as',old['dockets'][0]['project']['evidence'][0])
        self.assertFalse(warnings)

    def test_bounds_are_conservative_and_do_not_mutate_input(self):
        pages={1:{1:'A real event'}}
        def point(day,relation,quote='A real event'):
            return {'date':day,'relation':relation,'description':'Event','evidence':[{'doc':1,'page':1,'quote':quote}]}
        record={'start':point('2020-01-01','on_or_before'),'end':point('2020-01-11','exact')}
        before=copy.deepcopy(record)
        value,warnings=interval(record,pages)
        self.assertEqual(value['duration'],{'kind':'at_least','days':10})
        self.assertEqual(record,before)
        self.assertFalse(warnings)
        record['start']['relation']='exact'
        self.assertEqual(interval(record,pages)[0]['duration']['kind'],'exact')
        record['start']=point('2020-01-11','on_or_before')
        self.assertEqual(interval(record,pages)[0]['duration'],{'kind':'unknown','days':None})
        record['start']=point('2020-02-01','exact')
        self.assertIsNone(interval(record,pages)[0]['duration']['days'])
        record['start']=point('2020-01-01','exact','Invented')
        self.assertIsNone(interval(record,pages)[0]['start'])


if __name__=='__main__':unittest.main()
