'use strict';

const $ = id => document.getElementById(id);
const node = (tag, text, cls) => {
  const el = document.createElement(tag);
  if (text != null) el.textContent = String(text);
  if (cls) el.className = cls;
  return el;
};
const pretty = value => value === undefined ? 'Not recorded' : JSON.stringify(value, null, 2);
const plain = value => String(value ?? 'Unknown').replaceAll('_', ' ');
const names = {corrected:'Improved', regressed:'Regressed', unchanged_error:'Still incorrect',
  unchanged_correct:'Still correct', changed_unscored:'Changed · not scored', unchanged_unscored:'Unchanged · not scored',
  unscored:'Not scored', missing:'Missing records', extra:'Additional records', ambiguous:'Unclear identity',
  failures:'Processing issues', incompatible:'Different sources'};
const views = {Results:'Review', Design:'Pipeline', Cases:'Inputs', Runs:'Runs & costs'};
const state = {dataset:'demo', view:'Results', overview:null, epoch:0, list:0, detail:0, source:0,
  classification:'', q:'', status:'', scope:'', offset:0, selected:null, returnFocus:null};
const label = key => names[key] || plain(key);
const badge = (text, cls='') => node('span', text, 'badge ' + cls);
function button(text, action, cls='') {
  const el = node('button', text, cls); el.type = 'button'; el.onclick = action; return el;
}
function disclosure(parent, title, value) {
  const el = node('details', null, 'technical');
  el.append(node('summary', title), node('pre', pretty(value))); parent.append(el); return el;
}
function heading(text, sub, eyebrow='') {
  const el = node('div', null, 'page-heading');
  if (eyebrow) el.append(node('p', eyebrow, 'eyebrow'));
  el.append(node('h1', text), node('p', sub, 'lede'));
  $('content').replaceChildren(el);
}
function section(title, sub, cls='panel') {
  const el = node('section', null, cls), head = node('div', null, 'section-heading');
  head.append(node('h2', title)); if (sub) head.append(node('p', sub, 'muted'));
  el.append(head); return el;
}
function error(err, parent=$('content')) { parent.append(node('p', err.message, 'message danger')); }
async function api(path, params={}) {
  const query = path === 'catalog' ? '' : '?' + new URLSearchParams({dataset:state.dataset, ...params});
  const response = await fetch('/api/' + path + query), value = await response.json();
  if (!response.ok || value.error) throw Error(value.error || 'Unable to load this view.');
  return value;
}
function resetFilters() { Object.assign(state, {classification:'', q:'', status:'', scope:'', offset:0}); }
function closeSource() {
  state.source++; $('source').hidden = true; $('source-scrim').hidden = true;
  state.returnFocus?.focus();
}
function navigate(view) {
  state.detail++; state.list++; closeSource(); state.selected = null; state.view = view;
  resetFilters(); render(); window.scrollTo(0, 0);
}
async function loadDataset() {
  const epoch = ++state.epoch; state.detail++; state.list++; closeSource(); resetFilters();
  state.selected = null; heading('Opening workspace…', 'Loading saved results.');
  try {
    const value = await api('overview'); if (epoch !== state.epoch) return;
    state.overview = value;
    const runs = value.runs || [];
    $('context').replaceChildren(node('span', runs[0]?.name || 'Baseline', 'context-name'));
    if (runs[1]) $('context').append(node('span', '→', 'context-arrow'), node('span', runs[1].name, 'context-name'));
    else $('context').append(badge('Baseline only'));
    $('notice').textContent = value.synthetic ? 'Demo · invented cases and costs' : 'Saved snapshot · read only';
    $('notice').className = value.synthetic ? 'demo-note' : 'snapshot-note';
    $('notice').title = value.notice || '';
    render();
  } catch (err) { if (epoch === state.epoch) error(err); }
}
function render() {
  state.list++; state.detail++; closeSource();
  for (const el of $('tabs').children) {
    el.classList.toggle('active', el.dataset.view === state.view);
    if (el.dataset.view === state.view) el.setAttribute('aria-current', 'page'); else el.removeAttribute('aria-current');
  }
  if (!state.overview) return;
  ({Results:review, Design:design, Cases:inputs, Runs:runs})[state.view]();
  enhanceSelects();
}
function filterBy(key) {
  resetFilters(); state.classification = key; state.offset = 0; casesList();
  document.querySelectorAll('[data-result-filter]').forEach(el => el.classList.toggle('selected', el.dataset.resultFilter === key));
}
function review() {
  const o = state.overview, s = o.summary;
  heading(s ? 'What changed?' : 'Your starting point', s
    ? 'Start with the differences. Open a case to compare the findings and check the source.'
    : 'Explore the original extraction. A future candidate can be compared against this frozen baseline.');
  if (s) {
    const counts = s.classifications || {}, tiles = node('div', null, 'review-priorities');
    for (const [key, explanation] of [['regressed','Previously correct fields that now disagree with the reference.'],
      ['corrected','Fields that now agree with the reviewed reference.'], ['changed_unscored','Differences that still need a correctness judgment.']]) {
      const tile = button('', () => filterBy(key), 'priority ' + key);
      tile.dataset.resultFilter = key; tile.classList.toggle('selected', state.classification === key);
      const count=counts[key]||0, amount=node('strong',count);
      amount.append(node('small',count===1?' field':' fields'));
      tile.append(node('span', label(key), 'priority-label'), amount, node('p', explanation), node('span','Review cases →','priority-action'));
      tiles.append(tile);
    }
    $('content').append(tiles);
    const context = node('div', null, 'evaluation-context');
    if (s.comparable_fields) {
      context.append(node('strong', s.baseline_correct + ' → ' + s.candidate_correct + ' correct'),
        node('span', 'on the same ' + s.comparable_fields + ' comparable fields'),
        node('span', s.scored_labels + ' of ' + s.eligible_labels + ' eligible labels could be compared.', 'muted'));
    } else context.append(node('strong','Correctness has not been scored'), node('span','A reviewed reference is needed to assess these differences.'));
    $('content').append(context);
    const details = node('details', null, 'method-note'); details.append(node('summary','How to read this comparison'));
    details.append(node('p', o.synthetic ? 'These are invented software fixtures. Their results demonstrate the review workflow, not the quality of the production extraction.' : 'Both outputs are assessed against the same reference release. Unreviewed or incompatible fields are not scored.'));
    details.append(node('p','Missing records, processing issues, and source mismatches remain separate from field accuracy. A higher agreement rate alone does not establish better coverage.'));
    if (o.reference) disclosure(details,'Reference release',o.reference);
    $('content').append(details);
  } else {
    const run = o.runs[0], intro = node('section', null, 'baseline-summary');
    for (const [value, text] of [[run.case_count,'source groups'],[run.record_count,'finalized docket records']]) {
      const metric = node('div'); metric.append(node('strong',value.toLocaleString()),node('span',text)); intro.append(metric);
    }
    const note = node('div', null, 'baseline-next'); note.append(node('strong','No comparison yet'),node('p','This is the preserved first run. No candidate or reviewed reference has been supplied.'));
    intro.append(note); $('content').append(intro);
  }
  casesList();
}
function inputs() {
  heading('Which inputs are included?', 'Inspect the frozen source groups behind these outputs. Their source identities remain fixed across a comparison.');
  const note = node('p', state.overview.synthetic ? 'Demo inputs are invented to exercise improvements, regressions, missing records, and failures.' : 'All groups in the supplied artifact are shown. No production reference cohort has been selected.', 'context-note');
  $('content').append(note); casesList();
}
function selectFilter(parent, text, key, options) {
  const wrap = node('div', null, 'filter-control'), id = 'filter-' + key;
  const lab = node('label', text); lab.htmlFor = id;
  const select = node('select'); select.id = id; select.setAttribute('aria-label', text);
  if(state[key]&&!options.some(([value])=>value===state[key]))options=[...options,[state[key],plain(state[key])]];
  for (const [value, title] of [['','All'], ...options]) { const opt = node('option',title); opt.value = value; select.append(opt); }
  select.value = state[key];
  select.onchange = () => {
    state[key] = select.value; state.offset = 0; fetchCases($('case-browser'));
    document.querySelectorAll('[data-result-filter]').forEach(el => el.classList.toggle('selected', el.dataset.resultFilter === state.classification));
  };
  wrap.append(lab, select); parent.append(wrap);
}
async function casesList() {
  $('case-browser')?.remove();
  const root = section(state.view === 'Results' ? 'Case review' : 'Source groups', '', 'case-browser'); root.id = 'case-browser';
  $('content').append(root);
  const toolbar = node('div', null, 'case-toolbar'), input = node('input'); input.type = 'search'; input.placeholder = 'Find a case or docket…'; input.value = state.q;
  input.setAttribute('aria-label','Search cases');
  let timer;
  input.oninput = () => { clearTimeout(timer); timer = setTimeout(() => {state.q=input.value; state.offset=0; if(root.isConnected)fetchCases(root);},200); };
  toolbar.append(input);
  if (state.overview.summary) selectFilter(toolbar,'Show','classification',Object.entries(names).map(([k,v])=>[k,v]));
  const extra = node('details', null, 'more-filters'); extra.open = Boolean(state.status || state.scope);
  extra.append(node('summary','More filters')); const extraBody=node('div',null,'extra-filter-body');
  selectFilter(extraBody,'Processing','status',[['complete','Complete'],['failed','Failed'],['interrupted','Interrupted'],['running','In progress']]);
  selectFilter(extraBody,'Baseline scope','scope',[['confirmed','Confirmed'],['uncertain','Uncertain'],['excluded','Excluded']]);
  extraBody.append(button('Clear filters',()=>{resetFilters();casesList();document.querySelectorAll('[data-result-filter]').forEach(el=>el.classList.remove('selected'));},'text-button'));
  extra.append(extraBody); toolbar.append(extra); root.append(toolbar); enhanceSelects(root);
  await fetchCases(root);
}
function caseSignal(c) {
  const signals = [];
  for (const flag of ['incompatible','failures','missing','extra','ambiguous']) if (c.flags.includes(flag)) signals.push([label(flag),'attention']);
  for (const key of ['regressed','corrected','changed_unscored','unchanged_error']) if(c.classifications[key]) signals.push([c.classifications[key]+' '+label(key).toLowerCase(),key]);
  if(Object.values(c.status).some(s=>['running','reviewing','triaging','extracting','measuring'].includes(s)))signals.push(['In progress','attention']);
  if (!signals.length) signals.push([state.overview.summary?'No change to review':'Not yet evaluated','neutral']);
  return signals;
}
async function fetchCases(root) {
  if(!root) return;
  root.querySelector('.case-output')?.remove(); const output=node('div',null,'case-output');root.append(output);
  output.append(node('p','Loading cases…','empty'));
  const seq=++state.list,epoch=state.epoch;
  try {
    const d=await api('cases',{q:state.q,status:state.status,scope:state.scope,classification:state.classification,offset:state.offset,limit:25});
    if(seq!==state.list||epoch!==state.epoch||!root.isConnected)return;
    output.replaceChildren();
    const summary=node('div',null,'list-summary'); summary.append(node('span',d.total.toLocaleString()+(d.total===1?' case':' cases')+(state.classification?' · '+label(state.classification):'')));
    if(state.classification)summary.append(button('Show all',()=>filterBy(''),'text-button')); output.append(summary);
    if(!d.cases.length){output.append(node('div','No cases match these filters. Try another search or clear the filters.','empty'));return;}
    const wrap=node('div',null,'table-wrap'),table=node('table'); table.setAttribute('aria-label','Cases');
    const head=node('thead'),tr=node('tr');
    for(const title of ['Case','Review focus','Processing','Records'])tr.append(node('th',title));head.append(tr);table.append(head);
    const body=node('tbody');
    for(const c of d.cases){
      const row=node('tr'),identity=node('td'); const name=c.dockets.length?c.dockets[0]:c.case_id;
      identity.append(button(name,()=>detail(c.case_id),'case-link'));
      identity.append(node('span',c.dockets.length>1?'+'+(c.dockets.length-1)+' related dockets · '+plain(c.scope_category):plain(c.scope_category)+' scope','cell-sub'));
      if(name!==c.case_id)identity.append(node('span',c.case_id,'case-id'));row.append(identity);
      const focus=node('td');for(const [text,cls]of caseSignal(c))focus.append(badge(text,cls));row.append(focus);
      const status=node('td');
      if(c.status.candidate!=null){status.append(node('span',plain(c.status.baseline)+' → '+plain(c.status.candidate)));}
      else status.append(node('span',plain(c.status.baseline)));
      row.append(status,node('td',c.record_count.candidate==null?c.record_count.baseline:c.record_count.baseline+' → '+c.record_count.candidate,'numeric'));
      body.append(row);
    }
    table.append(body);wrap.append(table);output.append(wrap);
    const paging=node('div',null,'pagination');paging.append(node('span',(state.offset+1)+'–'+Math.min(state.offset+25,d.total)+' of '+d.total.toLocaleString()));
    const actions=node('div');const prev=button('← Previous',()=>{state.offset-=25;fetchCases(root);});prev.disabled=state.offset===0;
    const next=button('Next →',()=>{state.offset+=25;fetchCases(root);});next.disabled=state.offset+25>=d.total;
    actions.append(prev,next);paging.append(actions);output.append(paging);
  }catch(err){if(seq===state.list&&epoch===state.epoch){output.replaceChildren();error(err,output);}}
}

function componentChanges(id) {
  const designs=state.overview.designs,b=designs.baseline?.recipe?.components?.find(c=>c.id===id),c=designs.candidate?.recipe?.components?.find(c=>c.id===id);
  if(!designs.candidate)return {b,c,reasons:[],tone:''};
  if(!b||!c)return {b,c,reasons:[b?'Removed':'Added'],tone:'changed'};
  const reasons=[];
  if(b.prompt_sha256!==c.prompt_sha256)reasons.push('Instructions changed');
  const deps=x=>Object.fromEntries(Object.entries(x.prompt_dependencies||{}).filter(([key])=>key!==id));
  if(pretty(deps(b))!==pretty(deps(c)))reasons.push('Upstream context changed');
  if(pretty(b.config)!==pretty(c.config))reasons.push('Settings changed');
  return {b,c,reasons,tone:reasons.length?'changed':''};
}
function design() {
  const o=state.overview;
  heading('How do the pipelines differ?',o.designs.candidate?'Follow the flow, then select a component to inspect its instructions and settings.':'The recorded extraction flow for this baseline. Select a component to inspect it.');
  const flow=section('Pipeline at a glance','Highlighted components have changed. Connections come from the recorded recipe.');
  const ids=[...new Set(['baseline','candidate'].flatMap(side=>o.designs[side]?.recipe?.components?.map(c=>c.id)||[]))];
  let selected=ids.find(id=>componentChanges(id).reasons.length)||ids[0];
  for(const side of ['baseline','candidate']){
    const d=o.designs[side];if(!d)continue;
    const lane=node('div',null,'pipeline-lane');lane.append(node('span',plain(side),'lane-label'));
    const stages=node('div',null,'stages');
    for(const comp of d.recipe.components||[]){
      const info=componentChanges(comp.id),el=button('',()=>show(comp.id),'stage '+info.tone);el.dataset.component=comp.id;
      el.append(node('strong',comp.label||plain(comp.id)),node('span',info.reasons.join(' · ')|| (o.designs.candidate?'Unchanged':'Recorded component')));stages.append(el);
    }
    lane.append(stages);
    const connections=node('p',null,'connections');connections.append(node('span','Connections: '));
    connections.append(document.createTextNode((d.recipe.edges||[]).map(e=>plain(e.from)+' → '+plain(e.to)).join('   ·   ')));
    lane.append(connections);flow.append(lane);
  }
  $('content').append(flow);
  const inspector=section('Component details','');$('content').append(inspector);
  function show(id){
    selected=id;document.querySelectorAll('[data-component]').forEach(el=>{el.classList.toggle('selected',el.dataset.component===id);el.setAttribute('aria-pressed',el.dataset.component===id?'true':'false');});
    const {b,c,reasons}=componentChanges(id);inspector.replaceChildren(node('h2',plain(id)),node('p',reasons.join(' · ')||'No recorded change','muted'));
    const columns=node('div',null,'compare-grid two');
    for(const side of ['baseline','candidate']){
      if(!o.designs[side])continue;
      const part=node('section',null,'compare-cell');part.append(node('h3',plain(side)));
      const comp=side==='baseline'?b:c;
      if(!comp){part.append(node('p','This component is not in this pipeline.','muted'));columns.append(part);continue;}
      const prompt=o.designs[side].prompts?.[id];part.append(node('pre',typeof prompt==='string'?prompt:'No separate prompt text was recorded.','prompt-text'));
      const settings=comp.config||{};const dl=node('dl',null,'settings');
      for(const [key,value]of Object.entries(settings)){if(value==null)continue;dl.append(node('dt',plain(key)),node('dd',typeof value==='object'?pretty(value):String(value)));}
      part.append(dl);disclosure(part,'Prompt hashes and dependencies',comp);columns.append(part);
    }
    inspector.append(columns);
    if(reasons.includes('Upstream context changed')){
      const deps=node('details',null,'method-note');deps.append(node('summary','Inspect changed upstream instructions'));
      const keys=new Set([...Object.keys(b?.prompt_dependencies||{}),...Object.keys(c?.prompt_dependencies||{})]);
      for(const key of keys){if(key===id||b?.prompt_dependencies?.[key]===c?.prompt_dependencies?.[key])continue;
        const grid=node('div',null,'compare-grid two');
        for(const side of ['baseline','candidate']){const col=node('div');col.append(node('h3',plain(side)+' · '+key),node('pre',o.designs[side]?.prompts?.[key]||'Not recorded','prompt-text'));grid.append(col);}deps.append(grid);
      }inspector.append(deps);
    }
  }
  if(selected)show(selected);
  const metadata=node('details',null,'method-note');metadata.append(node('summary','Full recipes and provenance'));
  for(const side of ['baseline','candidate'])if(o.designs[side])disclosure(metadata,plain(side),o.designs[side].recipe);
  $('content').append(metadata);
}
function money(value){return typeof value==='number'?'$'+value.toFixed(3):'Not recorded';}
function runs(){
  const o=state.overview;heading('Run progress & costs','Saved execution snapshots. This view does not launch or monitor active model calls.');
  if(o.synthetic)$('content').append(node('p','All progress and dollar amounts below are simulated. No model calls were made.','context-note'));
  for(const run of o.runs){
    const panel=section(plain(run.side),run.name),counts=run.status_counts||{},complete=counts.complete||0;
    const progress=node('div',null,'run-progress');progress.append(node('strong',complete+' / '+run.case_count),node('span','groups complete'));
    const bar=node('progress');bar.max=run.case_count||1;bar.value=complete;bar.setAttribute('aria-label',plain(run.side)+' completion');progress.append(bar);panel.append(progress);
    const statuses=node('div',null,'run-statuses');
    for(const [key,value]of Object.entries(counts)){
      const el=button(value+' '+(key==='reviewing'?'in review':plain(key)),()=>{navigate('Cases');state.status=key;casesList();},'status-link');statuses.append(el);
    }panel.append(statuses);
    const costs=node('div',null,'cost-grid');
    for(const [key,text,description]of [['new_usd','New spending','This experiment'],['inherited_usd','Inherited cost','Previously recorded work'],['reserved_usd','Reserved','Potential pending charges'],['estimated_usd','Estimated','Projected total']]){
      const value=run.cost?.[key]??(key==='inherited_usd'&&run.cost?.kind==='inherited'?run.cost.recorded_usd:key==='new_usd'?run.execution?.new_model_cost_usd:null);
      const cell=node('div');cell.append(node('span',text),node('strong',money(value)),node('small',description));costs.append(cell);
    }panel.append(costs);
    panel.append(node('p',run.record_count+' finalized docket records · '+(run.cost?.completeness==='partial'?'Cost reporting is incomplete.':'Unreported amounts remain unknown.'),'muted small'));
    disclosure(panel,'Accounting and execution metadata',{cost:run.cost,execution:run.execution});$('content').append(panel);
  }
}

function displayValue(value){
  if(!value?.present)return node('span','Not present','value absent');
  if(value.value===null)return node('span','Unknown (null)','value absent');
  if(typeof value.value==='object')return node('pre',pretty(value.value),'structured-value');
  return node('span',String(value.value),'value');
}
async function detail(id){
  const epoch=state.epoch,seq=++state.detail;state.list++;closeSource();state.selected=id;
  const scroll=window.scrollY;heading('Opening case…','Loading the saved findings.');window.scrollTo(0,0);
  try{
    const d=await api('case',{id});if(epoch!==state.epoch||seq!==state.detail)return;
    const docket=d.baseline?.records?.[0]?.docket||d.candidate?.records?.[0]?.docket||id;
    heading(docket,'Compare the findings, then inspect the evidence for the fields that matter.',id===docket?'Case review':id);
    const back=button('← '+views[state.view],()=>{state.selected=null;render();window.scrollTo(0,scroll);},'back-link');$('content').prepend(back);
    const meta=node('div',null,'case-summary');
    for(const side of ['baseline','candidate']){
      const c=d[side];if(!c)continue;const row=node('div');row.append(node('span',plain(side),'eyebrow'),node('strong',plain(c.scope_category)+' scope'),node('span',plain(c.execution?.status)+' · '+(c.records?.length||0)+' records','muted'));
      row.append(button('Read source ↗',()=>source(id,side),'text-button'));meta.append(row);
    }$('content').append(meta);
    const e=d.evaluation;
    if(e){
      const concerns=[];
      if(!e.compatibility.compatible)concerns.push('The source inputs differ or are unavailable. These fields cannot be scored as a paired comparison.');
      for(const side of ['baseline','candidate']){
        const rec=e.records[side];if(rec.missing.length)concerns.push(plain(side)+': '+rec.missing.length+' missing record(s).');
        if(rec.additional.length)concerns.push(plain(side)+': '+rec.additional.length+' additional record(s).');
        if(rec.ambiguous.length||rec.null_singleton_matches?.length)concerns.push(plain(side)+': record identity needs review.');
        if(d[side]?.execution?.error)concerns.push(plain(side)+': '+d[side].execution.error);
      }
      if(concerns.length){const notice=node('div',null,'message attention');notice.append(node('strong','Before judging these results'));for(const line of concerns)notice.append(node('p',line));$('content').append(notice);}
      const fields=section('Field comparison','The reference is the expected answer. Missing or unreviewed labels do not establish correctness.');
      const controls=node('div',null,'field-toolbar'),select=node('select');select.id='field-filter';select.setAttribute('aria-label','Fields to review');
      const choices=[['changed','Changes first'],['','All fields'],...[...new Set(e.fields.map(f=>f.classification))].map(k=>[k,label(k)])];
      for(const [value,text]of choices){const opt=node('option',text);opt.value=value;select.append(opt);}
      select.value=choices.some(([k])=>k===state.classification)&&state.classification?state.classification:'changed';
      const lab=node('label','Show');lab.htmlFor=select.id;controls.append(lab,select);fields.append(controls);
      const rows=node('div',null,'field-rows');fields.append(rows);$('content').append(fields);
      const changed=f=>['corrected','regressed','changed_unscored','unchanged_error'].includes(f.classification);
      const order={regressed:0,corrected:1,changed_unscored:2,unchanged_error:3};
      function draw(){
        rows.replaceChildren();const chosen=e.fields.filter(f=>select.value==='changed'?changed(f):!select.value||f.classification===select.value).sort((a,b)=>(order[a.classification]??4)-(order[b.classification]??4));
        if(!chosen.length){rows.append(node('p','No fields in this view. Choose “All fields” to inspect the remaining findings.','empty'));return;}
        for(const f of chosen){
          const card=node('article',null,'field-card '+f.classification),head=node('div',null,'field-heading');
          const identity=d.reference?.records?.find(r=>r.record_id===f.record_id)?.identity?.docket;
          const title=node('div');if(f.record_id)title.append(node('span',identity||f.record_id,'eyebrow'));
          title.append(node('h3',f.path.map(plain).join(' / ')));head.append(title,badge(label(f.classification),f.classification));card.append(head);
          const grid=node('div',null,'compare-grid');
          for(const side of ['reference','baseline','candidate']){const cell=node('div',null,'compare-cell '+side);cell.append(node('h4',side==='reference'?'Reference · expected':plain(side)),displayValue(f[side]));grid.append(cell);}card.append(grid);
          if(f.unscored_reason)card.append(node('p','Not scored: '+plain(f.unscored_reason)+'.','small muted'));
          if(f.rationale)card.append(node('p',f.rationale,'rationale'));
          for(const ev of f.evidence||[])if(ev.quote){const quote=button('',()=>source(id,d.baseline?'baseline':'candidate',ev.quote),'evidence-link');quote.append(node('span','“'+ev.quote+'”'),node('strong','Check source ↗'));card.append(quote);}
          rows.append(card);
        }
      }
      select.onchange=draw;draw();enhanceSelects(controls);
      const diagnostics=node('details',null,'method-note');diagnostics.append(node('summary','Record matching and comparison details'));disclosure(diagnostics,'Source compatibility',e.compatibility);disclosure(diagnostics,'Record matching',e.records);$('content').append(diagnostics);
    }else{
      const records=section('Extracted findings','No candidate or reviewed reference has been supplied. These records have not been scored.');
      for(const record of d.baseline?.records||[]){const item=node('details',null,'record-detail');item.append(node('summary',record.docket||'Docket not identified'));const facts=node('dl',null,'facts');for(const [key,value]of Object.entries(record)){if(key==='docket')continue;facts.append(node('dt',plain(key)),node('dd',typeof value==='object'?pretty(value):String(value)));}item.append(facts);records.append(item);}
      if(!d.baseline?.records?.length)records.append(node('p',d.baseline?.execution?.error||'No finalized docket records in this group.','muted'));$('content').append(records);
    }
    const provenance=node('details',null,'method-note');provenance.append(node('summary','Source identities and raw artifacts'));
    for(const side of ['baseline','candidate'])if(d[side])disclosure(provenance,plain(side),d[side]);$('content').append(provenance);
  }catch(err){if(seq===state.detail&&epoch===state.epoch)error(err);}
}
async function source(id,side,quote=''){
  const seq=++state.source,epoch=state.epoch;state.returnFocus=document.activeElement;
  $('source').hidden=false;$('source-scrim').hidden=false;$('source-title').textContent='Evidence · '+plain(side);
  $('source-status').className='muted';$('source-status').textContent='Verifying the frozen source…';$('source-text').replaceChildren();$('close-source').focus();
  try{
    const d=await api('source',{id,side});if(seq!==state.source||epoch!==state.epoch)return;
    if(!d.verified)throw Error('Source integrity was not verified.');
    $('source-status').textContent='Verified source · '+id;$('source-status').title='SHA-256: '+d.sha256;
    const at=quote?d.text.indexOf(quote):-1;
    if(at>=0){$('source-text').append(document.createTextNode(d.text.slice(0,at)),node('mark',quote),document.createTextNode(d.text.slice(at+quote.length)));$('source-text').querySelector('mark').scrollIntoView({block:'center'});}
    else{$('source-text').textContent=d.text;$('source-text').scrollTop=0;if(quote)$('source-status').textContent+=' · Quote not found verbatim.';}
  }catch(err){if(seq===state.source&&epoch===state.epoch){$('source-status').textContent='Source unavailable: '+err.message;$('source-status').className='message danger';}}
}
$('close-source').onclick=closeSource;$('source-scrim').onclick=closeSource;
document.addEventListener('keydown',event=>{
  if($('source').hidden)return;
  if(event.key==='Escape'){event.preventDefault();closeSource();}
  if(event.key==='Tab'){
    const target=document.activeElement;
    if(event.shiftKey&&target===$('close-source')){event.preventDefault();$('source-text').focus();}
    else if(!event.shiftKey&&target===$('source-text')){event.preventDefault();$('close-source').focus();}
  }
});
$('dataset').onchange=()=>{state.dataset=$('dataset').value;loadDataset();};
for(const el of $('tabs').children)el.onclick=()=>navigate(el.dataset.view);
(async()=>{
  try{const catalog=await api('catalog');for(const d of catalog.datasets){const opt=node('option',d.label);opt.value=d.id;opt.disabled=!d.available;$('dataset').append(opt);}state.dataset=catalog.default||'demo';$('dataset').value=state.dataset;enhanceSelects();await loadDataset();}
  catch(err){error(err);}
})();
