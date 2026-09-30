import copy
import json
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from appeal_evidence import interval, apply_review
from appeal_runtime import Client, api_config, digest, payload, read, save
from appeals import exact_members, export, packs, normalize_extract, recover_extract, raise_char_cap, run, finish, duration_followup
import group_scout as gs
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

    def extract_fixture(self):
        return {'schema_version':'v6','dockets':[{
            'docket':'A','related_dockets':[],
            'project':{'description':None,'municipality':None,'proposed_units':None,'proposed_lots':None,'evidence':[]},
            'municipal_decision':{'description':None,'date':None,'outcome':'unclear','evidence':[]},
            'appellants':[],
            'discretion':{'description':None,'evidence':[],'involved':'unclear','central':'unclear','central_evidence':[]},
            'timeline':[],
            'observed_status':{'litigation':'unclear','permission':'unclear','description':None,'evidence':[]},
            'restrictions':[],'housing_changes':[]}]}

    def test_concurrent_finishing_isolates_flags_and_errors_and_keeps_prefix(self):
        groups=[{'group_id':gid,'source':f'SOURCE-{gid}'} for gid in ('flag','good-a','bad','good-b')]
        outputs={g['group_id']:{'group':g['group_id'],'status':'extracted','warnings':[],
                 'triage':{},'candidate':self.extract_fixture()} for g in groups}
        outputs['excluded']={'group':'excluded','status':'extracted','candidate':None,'warnings':[]}
        groups.append({'group_id':'excluded','source':'EXCLUDED SOURCE'})
        manifest={'model':'model','groups':groups,'review_batch_chars':200000}
        prompts={k:k for k in ('primer','extract','triage','review','duration')}
        barrier=threading.Barrier(2)
        requests={}
        responses={}
        def call(name,request):
            requests[name]=copy.deepcopy(request)
            cases=json.loads(request['messages'][-1]['content'])['cases']
            if name.startswith('review-'):
                self.assertEqual(len(cases),2)
                barrier.wait(timeout=5)
                response={'reviews':[{'group':c['group'],'review':{
                    'schema_version':'invalid' if c['group']=='bad' else 'v6-review-1',
                    'scope':{'verdict':'flag' if c['group']=='flag' else 'agree'},
                    'changes':[],'flags':[]}} for c in cases]}
                responses[name]=response
                return response
            self.assertEqual(len(cases),1)
            self.assertNotIn('source_documents',cases[0])
            return {'groups':[{'group':c['group'],'dockets':[
                {'docket':'A','start':None,'end':None,'note':'No supported bounds'}]} for c in cases]}
        client=Mock()
        client.call.side_effect=call
        finish(self.root,manifest,prompts,client,outputs,2)
        self.assertEqual(outputs['flag']['status'],'scope_flagged')
        self.assertEqual(outputs['bad']['status'],'review_failed')
        for gid in ('good-a','good-b','excluded'):
            self.assertEqual(outputs[gid]['status'],'complete')
            self.assertEqual(read(self.root/f'results/{gid}.json')['status'],'complete')
        self.assertEqual(len(requests),4)
        for name,request in requests.items():
            if name.startswith('duration-'):
                review_name=name.replace('duration-','review-',1)
                self.assertEqual(request['messages'][:2],requests[review_name]['messages'])
                self.assertEqual(json.loads(request['messages'][2]['content']),responses[review_name])
                self.assertEqual(len(request['messages']),4)

    def test_all_exclusions_finish_without_model_calls(self):
        result={'group':'excluded','status':'extracted','candidate':None,'warnings':[]}
        client=Mock()
        prompts={k:k for k in ('primer','extract','triage','review','duration')}
        finish(self.root,{'model':'model','review_batch_chars':200000,
               'groups':[{'group_id':'excluded'}]},prompts,client,{'excluded':result},3)
        self.assertEqual(result['status'],'complete')
        client.call.assert_not_called()

    def test_recovery_is_validated_free_and_preserves_original(self):
        malformed=self.extract_fixture()
        docket=malformed['dockets'][0]
        for field in ('timeline','observed_status','restrictions','housing_changes'):
            malformed[field]=docket.pop(field)
        docket['central']=docket['discretion'].pop('central')
        original=copy.deepcopy(malformed)
        req=payload('model','JSON',{})
        state={'status':'failed','error_type':'validation','error':'misplaced fields','charged_or_reserved_usd':0.03}
        save(self.root/'case.request.json',req)
        save(self.root/'case.result.json',state)
        save(self.root/'case.parsed.json',malformed)
        save(self.root/'case.response.json',{'original':malformed})
        client=Client(self.root,'http://localhost','unused',0,retry_failed=True)
        with patch('appeal_runtime.httpx.Client') as transport:
            self.assertFalse(client.recover_validation('case',{**req,'model':'other'},lambda v:recover_extract(v,{})))
            self.assertTrue(client.recover_validation('case',req,lambda v:recover_extract(v,{})))
            self.assertEqual(client.call('case',req),self.extract_fixture())
            transport.assert_not_called()
        self.assertEqual(read(self.root/'case.original-parsed.json'),original)
        self.assertEqual(read(self.root/'case.response.json'),{'original':original})
        self.assertEqual(read(self.root/'case.recovery.json')['previous_result'],state)
        self.assertEqual(len(read(self.root/'case.recovery.json')['corrections']),5)
        self.assertEqual(client.spent(),0.03)

    def test_relocation_refuses_ambiguous_or_conflicting_ownership(self):
        value=self.extract_fixture()
        value['housing_changes']=[{'description':'Root observation','evidence':[]}]
        normalized,notes=normalize_extract(value)
        self.assertEqual(normalized,value)
        self.assertFalse(notes)
        with self.assertRaises(ValueError):recover_extract(value,{})
        value['dockets'][0].pop('housing_changes')
        value['dockets'].append(copy.deepcopy(value['dockets'][0]))
        self.assertEqual(normalize_extract(value),(value,[]))
        with self.assertRaises(ValueError):recover_extract(value,{})

    def test_relocation_does_not_invent_evidence_or_enum_values(self):
        for damage in ('evidence','kind'):
            value=self.extract_fixture()
            if damage=='evidence':
                value['dockets'][0]['observed_status'].pop('evidence')
            else:
                value['dockets'][0]['timeline']=[{'kind':'unsupported'}]
            with self.assertRaises(ValueError):recover_extract(value,{})

    def test_char_cap_raise_is_audited_and_keeps_sources_out_of_manifest(self):
        manifest={'max_chars':4,'groups':[{'group_id':'a','source':'abc','eligible':True},
                                         {'group_id':'b','source':'abcdef','eligible':False}]}
        raise_char_cap(self.root,manifest,6)
        stored=read(self.root/'manifest.json')
        self.assertTrue(stored['groups'][1]['eligible'])
        self.assertNotIn('source',stored['groups'][0])
        self.assertEqual(stored['cap_changes'][0]['previous'],4)
        raise_char_cap(self.root,manifest,6)
        self.assertEqual(len(manifest['cap_changes']),1)
        with self.assertRaises(ValueError):raise_char_cap(self.root,manifest,5)

    def test_first_pass_resume_only_sends_failed_and_newly_eligible_calls(self):
        folder=self.root/'resume'
        prompts={'system':'System','triage':'Triage'}
        save(folder/'prompts.json',prompts)
        groups=[]
        for gid in ('cached','failed','oversized'):
            groups.append({'group_id':gid,'source_sha256':digest('abc'),
                           'eligible':gid!='oversized','documents':[],'dockets':[]})
            source=folder/'sources'/f'{gid}.txt'
            source.parent.mkdir(exist_ok=True)
            source.write_text('abc')
        save(folder/'manifest.json',{'name':'resume','model':'model','max_chars':2,
             'max_output_tokens':65536,'prompt_sha256':digest(json.dumps(prompts,sort_keys=True)),
             'groups':groups})
        triage={'schema_version':'v5-triage','documents':[{'doc':1,'belongs':'yes','caption_dockets':[]}],
                'municipal_appeal':'yes','housing':'no','matter':'Not housing','reason':'Not housing','evidence':[]}
        req=gs.triage_payload('model',prompts,'abc','high')
        req['max_tokens']=65536
        for gid,status in [('cached','ok'),('failed','failed')]:
            save(folder/f'calls/{gid}.triage.request.json',req)
            save(folder/f'calls/{gid}.triage.result.json',{'status':status,'error_type':'validation','charged_or_reserved_usd':0.01})
            save(folder/f'calls/{gid}.triage.parsed.json',triage)
        response=Mock(status_code=200,text='response')
        response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(triage)}}],
                                    'usage':{'prompt_tokens':10,'completion_tokens':10}}
        args=SimpleNamespace(root=self.root,name='resume',max_chars=600000,first_pass_only=True,
                             max_spend=1,price_multiplier=1,retry_failed=True,retry_max_output_tokens=None,
                             api_base_url=None,api_url=None,workers=1)
        with patch('appeals.api_config',return_value=('http://localhost','unused')), \
             patch('appeals.source_pages',return_value={1:{1:'abc'}}), \
             patch('appeal_runtime.httpx.Client') as transport:
            transport.return_value.__enter__.return_value.post.return_value=response
            run(args)
            self.assertEqual(transport.return_value.__enter__.return_value.post.call_count,2)
            transport.reset_mock()
            # Preserve already finalized exclusions when inspecting the first pass again.
            save(folder/'results/cached.json',{'status':'complete','candidate':None})
            run(args)
            transport.assert_not_called()
        self.assertEqual(read(folder/'results/cached.json')['status'],'complete')
        self.assertEqual(read(folder/'results/oversized.json')['status'],'complete')
        self.assertFalse(list((folder/'calls').glob('review-*')))
        self.assertEqual(len(list((folder/'calls/attempts').rglob('*.result.json'))),1)

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
