'use strict';

const $ = id => document.getElementById(id);
const node = (tag, text, className) => {
  const item = document.createElement(tag);
  if (text != null) item.textContent = text;
  if (className) item.className = className;
  return item;
};
const chip = (text, tone = '') => node('span', text, `chip ${tone}`);
const human = value => value ? String(value).replaceAll('_', ' ').replace(/^./, c => c.toUpperCase()) : 'Not established';
const number = value => value.toLocaleString();
const date = value => {
  if (!value) return 'Not established';
  if (/^\d{4}$/.test(value)) return value;
  if (/^\d{4}-\d{2}$/.test(value)) return new Intl.DateTimeFormat('en-US',{month:'long',year:'numeric',timeZone:'UTC'}).format(new Date(value+'-01T00:00:00Z'));
  const parsed = new Date(value+'T00:00:00Z');
  return Number.isNaN(parsed.getTime()) ? String(value) : new Intl.DateTimeFormat('en-US', {month:'short', day:'numeric', year:'numeric', timeZone:'UTC'}).format(parsed);
};
const active = new Set(['triaging', 'extracting', 'reviewing', 'measuring']);
const failed = new Set(['incomplete', 'review_failed', 'duration_failed', 'interrupted', 'scope_flagged', 'too_large']);
const statusNames = {not_planned:'Not planned', pending:'Queued', not_run:'Queued', triaging:'Checking scope', extracting:'Extracting', extracted:'Awaiting review', reviewing:'Reviewing', reviewed:'Awaiting duration', measuring:'Measuring duration', complete:'Complete', incomplete:'Failed / budget stopped', review_failed:'Review failed', duration_failed:'Duration failed', interrupted:'Interrupted', scope_flagged:'Scope needs review', too_large:'Separate route needed'};
let state = null, view = 'all', selected = null, docket = null, page = 1, ascending = true;
let loading = false, loadId = 0, detailSignature = '', dataSignature = '', evidenceId = 0, evidenceTrigger = null;
let recordsByGroup = new Map(), notesByGroup = new Map(), caseByGroup = new Map();

async function get(url) {
  const response = await fetch(url, {cache:'no-store'});
  const value = await response.json();
  if (!response.ok) throw Error(value.error || 'Unable to load data');
  return value;
}
function indexData() {
  recordsByGroup = new Map();
  for (const row of state.records) {
    if (!recordsByGroup.has(row.group)) recordsByGroup.set(row.group, []);
    recordsByGroup.get(row.group).push(row);
  }
  notesByGroup = new Map(state.review.map(row => [row.group, row.reasons]));
  caseByGroup = new Map(state.cases.map(c => [c.group, c]));
}
const rows = gid => recordsByGroup.get(gid) || [];
const notes = gid => notesByGroup.get(gid) || [];
function scopeClass(c) {
  const {municipal_appeal:m, housing:h} = c.scope;
  if (m === 'no' || h === 'no' || ['out_of_scope','no_members'].includes(c.route.route)) return 'excluded';
  if (m === 'yes' && h === 'yes') return 'confirmed';
  if (m === 'unclear' || h === 'unclear') return 'uncertain';
  return 'unassessed';
}
function scopeChip(c) {
  return {confirmed:()=>chip('Housing appeal', 'yes'), uncertain:()=>chip('Uncertain', 'unclear'), excluded:()=>chip('Outside scope', 'no'), unassessed:()=>chip('Not assessed')}[scopeClass(c)]();
}
function statusChip(c) {
  return chip(statusNames[c.status] || human(c.status), active.has(c.status) ? 'active' : failed.has(c.status) ? 'failed' : c.status === 'complete' ? 'yes' : '');
}
function municipalName(c) {
  return [...new Set(rows(c.group).map(r => r.record.project?.municipality).filter(Boolean))].join(', ') || '—';
}
function updateURL(push = false) {
  const url = new URL(location.href);
  url.search = '';
  if (view !== 'all') url.searchParams.set('run', view);
  if (selected) url.searchParams.set('group', selected);
  if (selected && docket) url.searchParams.set('docket', docket);
  history[push ? 'pushState' : 'replaceState'](null, '', url);
}
function openGroup(gid, push = true, targetDocket = null) {
  selected = gid; docket = targetDocket; detailSignature = '';
  closeEvidence(); updateURL(push); renderRoute();
  window.scrollTo({top:0});
}
function goHome(event) {
  event?.preventDefault(); selected = null; docket = null;
  closeEvidence(); updateURL(true); renderRoute(); window.scrollTo({top:0});
}
function groupLink(c, text, className) {
  const link = node('a', text, className);
  const url = new URL(location.href); url.search = '';
  if (view !== 'all') url.searchParams.set('run', view);
  url.searchParams.set('group', c.group);
  link.href = url.pathname + url.search;
  link.addEventListener('click', e => {if (!e.metaKey && !e.ctrlKey && !e.shiftKey) {e.preventDefault(); openGroup(c.group);}});
  return link;
}

function compositionChart(parent,categories,labels,total) {
  const chart=node('div',null,'composition-chart'), bar=node('div',null,'composition-bar');
  bar.setAttribute('role','img');
  bar.setAttribute('aria-label',`${total} appeal dockets: ${labels.map(([key,label])=>`${label}: ${categories[key].length}`).join('; ')}`);
  const legend=node('div',null,'composition-legend');
  for(const [key,label,tone='neutral'] of labels) {
    const members=categories[key], count=members.length, share=total?count/total*100:0;
    if(count && total) {
      const segment=node('span',share>=12?`${Math.round(share)}%`:null,`composition-segment ${tone}`);
      segment.style.width=`${share}%`;segment.title=`${label}: ${count} of ${total} · ${Math.round(share)}%`;bar.append(segment);
    }
    const keyItem=node('span',null,`composition-key ${tone}`);
    keyItem.append(node('span',null,'composition-swatch'),node('span',label),node('strong',number(count)));
    keyItem.title=total?`${count} of ${total} · ${Math.round(share)}%`:'No completed appeals';legend.append(keyItem);
  }
  chart.append(bar,legend);if(!total) chart.append(node('p','No completed appeals to summarize.','stats-note'));parent.append(chart);
}
function renderResearch() {
  const root=$('researchStats'), s=state.research;root.replaceChildren();
  if(!s) {root.append(node('p','Research summaries are unavailable.','muted'));return;}
  const total=s.appeals.length, d=s.duration, measured=[...d.exact,...d.at_least];
  const header=node('div',null,'stats-heading'), description=node('div');
  description.append(node('h2','Results so far'),node('p','Completed municipal housing appeals in this view · Counts are appeal dockets, not distinct projects.','muted'));header.append(description);
  root.append(header);
  const context=node('div',null,'stats-context');
  context.append(node('span',`${number(s.uncertain_scope.length)} scope-uncertain appeals kept separate`),node('span',`${number(s.review_flagged.length)} appeals in flagged groups included`));
  root.append(context);
  const metrics=node('div',null,'stats-metrics');
  const average=value=>value==null?'—':`${new Intl.NumberFormat('en-US',{maximumFractionDigits:1}).format(value)} days`;
  for(const [label,value,detail] of [
    ['Housing appeal dockets',number(total),`Across ${number(s.groups)} groups`],
    ['Supported duration',`${number(measured.length)} / ${number(total)}`,`${number(d.unknown.length)} with insufficient date information`],
    ['Average supported duration',average(d.mean_supported_days),`Exact + minimum intervals · n=${measured.length}`],
    ['Average exact duration',average(d.mean_exact_days),`Exact intervals only · n=${d.exact.length}`]
  ]) {
    const metric=node('div',null,'stat-metric');metric.append(node('span',label,'stat-label'),node('strong',value),node('span',detail,'stat-detail'));metrics.append(metric);
  }
  root.append(metrics);
  if(!total) root.append(node('p','No confirmed housing appeal records have completed processing in this view. Unknown averages remain blank.','stats-empty'));
  const grid=node('div',null,'stats-grid');
  const panel=(title,note)=>{
    const box=node('section',null,'stats-panel'), heading=node('div',null,'stats-panel-heading'), body=node('div',null,'stats-panel-body');
    heading.append(node('h3',title));if(note) heading.append(node('p',note,'stats-note'));box.append(heading,body);grid.append(box);return {heading,body};
  };
  const duration=panel('Duration distribution','Minimums can understate full durations and their average. Measures litigation, not proven construction delay.'), legend=node('div',null,'chart-legend');
  for(const [key,label] of [['exact','Exact'],['at_least','Minimum'],['unknown','Not established']]) legend.append(node('span',`${label} · ${d[key].length}`,`legend-item ${key}`));
  const chart=node('div',null,'duration-histogram'), labels=['≤3 mo','3–6 mo','6–12 mo','1–2 yr','2–4 yr','>4 yr'];
  const ranges=['0–90 days','91–180 days','181–365 days','366–730 days','731–1,460 days','Over 1,460 days'];
  const max=Math.max(1,...d.buckets.map(b=>b.exact.length+b.at_least.length));
  d.buckets.forEach((bucket,i)=>{
    const count=bucket.exact.length+bucket.at_least.length, column=node('div',null,'histogram-column');column.title=`${ranges[i]} · ${bucket.exact.length} exact, ${bucket.at_least.length} minimum`;
    column.setAttribute('role','img');column.setAttribute('aria-label',column.title);
    const track=node('span',null,'histogram-track'), bar=node('span',null,'histogram-bar');
    bar.style.height=`${count/max*100}%`;
    for(const key of ['at_least','exact']) {const part=node('span',null,`histogram-part ${key}`);part.style.height=`${count?bucket[key].length/count*100:0}%`;bar.append(part);}track.append(bar);column.append(track,node('span',labels[i],'histogram-label'));chart.append(column);
  });duration.body.append(chart,legend);
  const discretion=panel('Discretionary basis','Did the appeal rely on a discretionary test?');
  const discretionCategories={primary:s.centrality.yes,peripheral:s.centrality.no,primary_unknown:s.centrality.unclear,no:s.discretion.no,unclear:s.discretion.unclear};
  compositionChart(discretion.body,discretionCategories,[['primary','Yes · primary','green-dark'],['peripheral','Yes · peripheral','green-medium'],['primary_unknown','Yes · primary status not established','green-light'],['no','No','blue'],['unclear','Basis not established','amber']],total);
  const outcomes=panel('Observed permission','Latest permission in the supplied records; construction is not established.');
  compositionChart(outcomes.body,s.permission,[['allowed','Allowed','blue'],['allowed_with_conditions','With conditions','teal'],['denied','Denied','rose'],['unclear','Not established','amber']],total);
  const findings=node('div',null,'impact-summary');
  for(const [field,label] of [['housing_changes','Housing changes / conditions'],['restrictions','Stop / suspension orders']]) {
    const line=node('div',null,'impact-line');line.append(node('strong',label));
    for(const [key,name] of [['documented','documented'],['none_documented','none documented'],['unknown','not established']]) {const members=s.impacts[field][key];line.append(node('span',`${members.length} ${name}`,'impact-count'));}findings.append(line);
  }outcomes.body.append(findings,node('p','Categories may overlap. “None documented” does not establish that housing was unaffected or built.','stats-note'));
  const representation=panel('Appellant representation','One count per appeal. Any missing appellant status stays unknown.');
  compositionChart(representation.body,s.representation,[['represented','All represented','teal'],['pro_se','All self-represented','blue'],['mixed','Mixed','slate'],['unclear','Incomplete / unknown','amber']],total);
  root.append(grid);
}
function renderProgress() {
  const s = state.summary, total = state.cases.length;
  const counts = {complete:0, active:0, attention:0, waiting:0};
  for (const c of state.cases) {
    if (c.status === 'complete') counts.complete++;
    else if (active.has(c.status)) counts.active++;
    else if (failed.has(c.status)) counts.attention++;
    else counts.waiting++;
  }
  $('population').textContent = view === 'all' ? `${number(total)} archive groups · Most recent planned run shown for each group` : `${number(total)} groups planned in ${view}`;
  $('progressTitle').textContent = view === 'all' ? 'Archive processing coverage' : s.running ? 'Pipeline running' : counts.complete === total && total ? 'Run complete' : 'Pipeline idle';
  $('progressContext').textContent = view === 'all' ? `${number(s.planned || 0)} groups have been planned. Select a run to monitor its progress.` : 'Completed groups include scope exclusions. Research review flags are separate from processing failures.';
  $('progressCount').textContent = `${number(counts.complete)} / ${number(total)}`;
  $('progress').max = total || 1; $('progress').value = counts.complete;
  $('stageCounts').replaceChildren();
  for (const [key,label] of [['complete','complete'],['active','processing'],['waiting',view==='all'?'waiting / unplanned':'waiting'],['attention','processing issues']]) {
    const item = node('span'); item.append(node('b', number(counts[key])), node('span', label)); $('stageCounts').append(item);
  }
  const reviewCount=state.cases.filter(c=>c.review_required).length;
  if (reviewCount) {const item=node('span');item.append(node('b',number(reviewCount)),node('span','research review flags'));$('stageCounts').append(item);}
  $('cost').textContent = s.cost_usd == null ? '' : `$${s.cost_usd.toFixed(3)} API cost / reserved`;
  $('freshness').textContent = 'Auto-updates every 5 seconds';
}
function renderTable() {
  if (!state) return;
  const search = $('search').value.trim().toLowerCase();
  const sf = $('statusFilter').value, scope = $('scopeFilter').value;
  const matches = state.cases.filter(c => {
    const statusMatches = sf === 'all' || sf === 'complete' && c.status === 'complete' || sf === 'active' && active.has(c.status) || sf === 'attention' && failed.has(c.status) || sf==='review' && c.review_required || sf === 'waiting' && !active.has(c.status) && !failed.has(c.status) && c.status !== 'complete';
    return statusMatches && (scope === 'all' || scopeClass(c) === scope) && (!search || JSON.stringify([c.group,c.dockets,c.title,c.description,rows(c.group)]).toLowerCase().includes(search));
  });
  matches.sort((a,b) => (a.dockets.join(', ') || a.group).localeCompare(b.dockets.join(', ') || b.group, undefined, {numeric:true}) * (ascending?1:-1));
  const size = Number($('pageSize').value), pages = Math.max(1, Math.ceil(matches.length/size));
  page = Math.min(page,pages); const first = (page-1)*size;
  $('groupRows').replaceChildren();
  for (const c of matches.slice(first,first+size)) {
    const row = node('tr');
    const name = node('td'), link = groupLink(c,c.dockets.join(' · ') || c.group,'group-link');
    name.append(link); const title = node('span', c.title, 'project-title'); title.title = c.title; name.append(title);
    const documents = node('td', c.documents);
    const processing = node('td'), badges=node('div',null,'processing-badges'); badges.append(statusChip(c));
    if (c.review_required) {const flag = chip('⚑','unclear review-flag'); flag.title = 'Research findings have review notes'; flag.setAttribute('aria-label','Research review required'); badges.append(flag);}
    processing.append(badges);
    const classification = node('td'); classification.append(scopeChip(c));
    const municipality = node('td',municipalName(c),'optional-column');
    const action = node('td'); const open = groupLink(c,'→','row-open'); open.setAttribute('aria-label',`Open ${c.dockets.join(', ') || c.group}`); action.append(open);
    row.append(name,documents,processing,classification,municipality,action); $('groupRows').append(row);
  }
  if (!matches.length) {const row=node('tr'), cell=node('td','No groups match these filters.','empty'); cell.colSpan=6; row.append(cell); $('groupRows').append(row);}
  $('pageInfo').textContent = matches.length ? `${number(first+1)}–${number(Math.min(first+size,matches.length))} of ${number(matches.length)} groups` : '0 matching groups';
  $('pageNumber').textContent = `${page} / ${pages}`;
  $('prev').disabled = page===1; $('next').disabled = page===pages;
}

function explanation(parent, title, description, evidence, c) {
  if (!description && !evidence?.length) return;
  const button=node('button',null,'evidence-button');
  button.append(node('span','ⓘ','info-icon'),node('span',evidence?.length?'Evidence':'Details'));
  button.setAttribute('aria-label',`Evidence: ${title}`);button.title=title;
  button.addEventListener('click',()=>showExplanation(c,title,description,evidence || [],button));
  parent.append(button);return button;
}
function citations(parent,evidence,c,title='Supporting evidence') {
  return explanation(parent,title,null,evidence,c);
}
function facts(parent, pairs) {
  const list=node('dl',null,'facts');
  for (const [label,value] of pairs) {list.append(node('dt',label),node('dd',value));}
  parent.append(list);
}
function researchCard(title, index) {
  const card=node('section',null,'research-card'), heading=node('h2');
  heading.append(node('span',index,'question-number'),node('span',title)); card.append(heading); return card;
}
function durationCard(r,c) {
  const card=researchCard('How long was the project in appeal?', '01');
  const d=r.duration || {kind:'unknown'};
  card.append(node('strong',d.kind==='unknown'?'Not established':`${d.kind==='at_least'?'At least ':''}${number(d.days)} days`,'duration-value'));
  const type=node('div',null,'duration-type'); type.append(chip(d.kind==='exact'?'Exact interval in these records':d.kind==='at_least'?'Minimum duration':'Insufficient evidence',d.kind==='unknown'?'unclear':'active')); card.append(type);
  const bounds=node('div',null,'bounds');
  for (const [key,label] of [['start','Appeal began'],['end','Appeal ended']]) {
    const point=r.duration_bounds?.[key], column=node('div'), header=node('div',null,'finding-heading'); header.append(node('span',label,'label'));
    if (point) explanation(header,`${label}: date evidence`,point.description,point.evidence,c);
    column.append(header);
    column.append(chip(point ? point.relation==='exact'?'Exact · closed endpoint':'Uncertain · open endpoint':'Unknown endpoint',point?.relation==='exact'?'active':'unclear'));
    column.append(node('div',point ? point.relation==='exact'?'Exactly on':key==='start'?'On or before':'On or after' : 'No supported date','bound-relation'));
    column.append(node('div',date(point?.date),'bound-date'));
    if (point && point.relation!=='exact') column.append(node('p',key==='start'?'The actual start may be earlier.':'The actual end may be later.','endpoint-note'));
    bounds.append(column);
  }
  card.append(bounds);
  if (d.kind==='at_least') card.append(node('p','The appeal lasted at least this long. One or both endpoints are uncertain, so the full duration may be longer.','duration-caveat'));
  else card.append(node('p','This measures the appeal history shown in the supplied records, not proven construction delay.','duration-caveat'));
  explanation(card,'How this interval was established',r.duration_bounds?.note,[],c); return card;
}
function appellantsCard(r,c) {
  const card=researchCard('Who appealed, and what did they seek?', '02');
  if (!r.appellants?.length) card.append(node('div','Not established','answer unclear'));
  const seeks={overturn_approval:'Reverse permit approval',overturn_denial:'Reverse permit denial',change_conditions:'Change permit conditions',other:'Other relief',unclear:'Not established'};
  const roles={developer:'Developer / project owner',neighbor:'Neighbor',organization:'Organization',other:'Other party',unclear:'Not established'};
  const representation={represented:'With legal counsel',pro_se:'Self-represented',unclear:'Not established'};
  for (const person of r.appellants || []) {
    const box=node('div',null,'person'), header=node('div',null,'finding-heading'); header.append(node('div',person.name || 'Name not established','person-name'));
    explanation(header,'Appellant identity and role',null,person.evidence,c);box.append(header);
    facts(box,[['Role',roles[person.relationship] || human(person.relationship)],['Seeking',seeks[person.seeks] || human(person.seeks)],['Representation',representation[person.representation] || human(person.representation)]]);
    card.append(box);
  }
  return card;
}
function discretionCard(r,c) {
  const card=researchCard('Did the appeal rely on discretionary review?', '03'), d=r.discretion || {};
  const answers={yes:'Yes — discretionary basis',no:'No — non-discretionary basis',unclear:'Not established'};
  const finding=node('div',null,'finding-heading');finding.append(node('div',answers[d.involved] || 'Not established',`answer ${d.involved || 'unclear'}`));
  explanation(finding,'Discretionary basis of the appeal',d.description,d.evidence,c);card.append(finding);
  const central=node('div',null,'secondary-finding'); central.append(node('span','A primary part of the appeal?','muted'),chip({yes:'Yes',no:'No — peripheral',unclear:'Not established',not_applicable:'Not applicable'}[d.central] || 'Not established',d.central==='yes'?'yes':d.central==='unclear'?'unclear':''));
  explanation(central,'Whether discretion was a primary issue',null,d.central_evidence,c);card.append(central);
  if(d.description) {card.append(node('span','Reason for appeal','label reason-label'),node('p',d.description,'explanation'));}
  return card;
}
function outcomesCard(r,c) {
  const card=researchCard('Was housing allowed, changed, or stopped?', '04'), outcome=r.observed_status || {};
  const permission={allowed:'Allowed',allowed_with_conditions:'Allowed with conditions',denied:'Denied',unclear:'Not established'};
  const row=node('div',null,'outcome-row'); row.append(node('span','Latest observed permission'),chip(permission[outcome.permission] || 'Not established',outcome.permission==='unclear'?'unclear':'active')); card.append(row);
  if(outcome.description) card.append(node('p',outcome.description,'explanation'));
  explanation(card,'Observed housing outcome',null,outcome.evidence,c);
  const restrictions=node('div',null,'consequence'); restrictions.append(node('h3','Orders restricting the project'));
  if (!r.restrictions?.length) restrictions.append(node('p','No explicit stop or suspension order documented.','explanation'));
  else {restrictions.append(chip(`${r.restrictions.length} documented ${r.restrictions.length===1?'restriction':'restrictions'}`,'unclear')); const list=node('ul',null,'finding-list'); for (const item of r.restrictions) {const li=node('li',item.description); if (item.start || item.end) li.append(node('p',`${item.start?'From '+date(item.start):'Start unknown'} · ${item.end?'Through '+date(item.end):'End unknown'}`)); citations(li,item.evidence,c); list.append(li);} restrictions.append(list);} card.append(restrictions);
  const changes=node('div',null,'consequence'); changes.append(node('h3','Housing changes and permit conditions'));
  if (!r.housing_changes?.length) changes.append(node('p','No change to the proposal documented.','explanation'));
  else {changes.append(chip(`${r.housing_changes.length} documented ${r.housing_changes.length===1?'finding':'findings'}`)); const list=node('ul',null,'finding-list'); for (const item of r.housing_changes) {const li=node('li',item.description); citations(li,item.evidence,c); list.append(li);} changes.append(list);} card.append(changes);
  card.append(node('p','“Not documented” is a limit of these records, not proof that the project was unaffected or built.','duration-caveat'));
  return card;
}
function supportSection(parent,title) {
  const details=node('details'); details.append(node('summary',title)); const body=node('div',null,'support-body'); details.append(body); parent.append(details); return body;
}
function documentLinks(parent,c) {
  for (const doc of c.source_documents || []) {const line=node('div',null,'document-row'), title=node('a',`Document ${doc.doc} · ${doc.title || 'Untitled'}`); title.href=`/api/pdf?sha=${encodeURIComponent(doc.sha)}`; title.target='_blank'; title.rel='noopener'; line.append(title,node('span',`${doc.pages || '?'} pages`)); parent.append(line);}
}
function projectPanel(r,c) {
  const panel=node('section',null,'project-panel'), project=node('div'), decision=node('div');
  const projectHeading=node('div',null,'finding-heading');projectHeading.append(node('h2','Project under appeal'));
  explanation(projectHeading,'Project description and quantities',null,r.project?.evidence,c);project.append(projectHeading);
  project.append(node('p',r.project?.description || 'Project description not established.','project-description'));
  const quantities=node('div',null,'quantity-strip');
  for (const [value,label] of [[r.project?.proposed_units,'New / replacement homes'],[r.project?.proposed_lots,'Residential lots']]) {const cell=node('div');cell.append(node('strong',value==null?'Unknown':number(value)),node('span',label));quantities.append(cell);}project.append(quantities);
  const decisionHeading=node('div',null,'finding-heading');decisionHeading.append(node('h2','Decision being appealed'));
  explanation(decisionHeading,'Challenged municipal decision',null,r.municipal_decision?.evidence,c);decision.append(decisionHeading);
  const tags=node('div',null,'decision-tags');tags.append(chip(human(r.municipal_decision?.outcome),r.municipal_decision?.outcome==='unclear'?'unclear':'active'),node('span',r.municipal_decision?.date?`Decision date: ${date(r.municipal_decision.date)}`:'Decision date not established','muted'));decision.append(tags);
  decision.append(node('p',r.municipal_decision?.description || 'Challenged decision not established.','project-description'));
  panel.append(project,decision);return panel;
}
function exclusionPanel(c) {
  const scopeReview=c.scope_review || {}, reason=c.scope.reason || scopeReview.reason || 'The supplied documents do not meet the municipal housing appeal scope.';
  const reasoning=[c.scope.reason,scopeReview.reason].filter(Boolean).join(' ');
  let title='Outside the study scope';
  if(c.scope.municipal_appeal==='no') title=/\bAct\s*250\b/i.test(reasoning)?'Excluded: Act 250 appeal':'Excluded: not a municipal permit appeal';
  else if(c.scope.housing==='no') title='Excluded: not new or replacement housing';
  else if(c.route.route==='no_members') title='Excluded: no documents belong to this appeal';
  const panel=node('section',null,'exclusion-panel'), heading=node('div',null,'finding-heading');heading.append(node('h2',title));
  const evidence=[...(scopeReview.evidence || []),...(c.scope.evidence || [])];
  explanation(heading,'Reason for scope exclusion',c.scope.reason,evidence,c);panel.append(heading,node('p',reason,'explanation'));
  if(c.status!=='complete') panel.append(node('p','Scope exclusion is awaiting completion of the review stage.','endpoint-note'));
  return panel;
}
function renderSupport(root,r,c) {
  const support=node('section',null,'support');
  const history=supportSection(support,'Appeal timeline and related dockets');
  const timeline=node('ol',null,'timeline');
  const events=(r.timeline || []).map((event,index)=>({...event,index})).sort((a,b)=>(a.date || '0000').localeCompare(b.date || '0000') || a.index-b.index);
  for (const event of events) {const li=node('li'), time=node('time',date(event.date)); if (event.date) time.dateTime=event.date; const text=node('div'); text.append(chip(human(event.kind)),node('div',event.court_or_body,'muted'),node('p',event.description)); citations(text,event.evidence,c); li.append(time,text); timeline.append(li);} history.append(timeline);
  if (!events.length) history.append(node('p','No timeline milestones documented.','muted'));
  for (const related of r.related_dockets || []) {const item=node('p'); item.append(node('strong',related.docket),node('span',` · ${human(related.relationship)}`)); citations(item,related.evidence,c); history.append(item);}
  history.append(node('p',`Descriptive proceedings status: ${human(r.observed_status?.litigation)}. Use the evidenced duration bounds above to assess whether the interval is complete.`,'duration-caveat'));
  const documents=supportSection(support,`Source documents (${c.documents})`);
  documentLinks(documents,c);
  const reasons=notes(c.group); if (reasons.length) {const body=supportSection(support,`Research review notes (${reasons.length})`), list=node('ul',null,'review-notes'); for (const reason of reasons) list.append(node('li',reason)); body.append(list);}
  const raw=supportSection(support,'Full extracted record'); raw.append(node('pre',JSON.stringify(r,null,2),'raw'));
  root.append(support);
}
function renderDetail(force = false) {
  const c=caseByGroup.get(selected), root=$('detail');
  if (!c) {root.replaceChildren(node('p','This group is not in the selected view.','alert')); const back=node('a','← All groups','back-link'); back.href='/'; back.addEventListener('click',goHome); root.prepend(back); return;}
  const records=rows(c.group); if (!records.some(row=>row.record.docket===docket)) docket=records[0]?.record.docket || null;
  const signature=JSON.stringify([c,records,notes(c.group),docket]);
  if (!force && signature===detailSignature) return;
  detailSignature=signature; root.replaceChildren();
  const back=node('a','← Back to groups','back-link'); back.href=view==='all'?'/':`/?run=${encodeURIComponent(view)}`; back.addEventListener('click',goHome); root.append(back);
  const heading=node('div',null,'group-heading'), identity=node('div'); identity.append(node('h1',c.title));
  identity.append(node('p',c.dockets.join(' · ') || c.group,'muted'));
  const meta=node('div',null,'group-meta'); if (municipalName(c)!=='—') meta.append(node('span',municipalName(c),'muted')); meta.append(node('span',`${c.documents} source ${c.documents===1?'document':'documents'}`,'muted')); identity.append(meta); heading.append(identity,statusChip(c)); root.append(heading);
  const excluded=scopeClass(c)==='excluded';
  if (excluded) root.append(exclusionPanel(c));
  else {
  const strip=node('div',null,'scope-strip');
  for (const [label,value] of [['Municipal permit appeal',c.scope.municipal_appeal],['New / replacement housing',c.scope.housing]]) {const item=node('div',null,'scope-item'), header=node('div',null,'finding-heading'); header.append(node('span',label,'label')); if(label==='Municipal permit appeal') explanation(header,'Municipal housing scope',c.scope.reason,c.scope.evidence,c); item.append(header,chip(value==='yes'?'Confirmed':value==='no'?'No':value==='unclear'?'Not established':'Not assessed',value || '')); strip.append(item);}
  const review=node('div',null,'scope-item'); review.append(node('span','Research review','label'),chip(notes(c.group).length?'Needs attention':c.status==='complete'?'No automated flags':'Pending',notes(c.group).length?'unclear':'')); strip.append(review); root.append(strip);
  if (scopeClass(c)==='uncertain') root.append(node('p','Housing or municipal scope is uncertain. Keep this group separate from confirmed housing appeals.','alert'));
  }
  if (!records.length) {
    if(!excluded) root.append(node('p',c.status==='not_planned'?'This archive group has not been added to a pipeline run.':c.status==='complete'?'No extracted docket records are available.':`Current stage: ${statusNames[c.status] || human(c.status)}. Research answers appear after extraction, review, and duration measurement are complete.`,'research-card'));
    const reasons=notes(c.group); if (reasons.length) {const list=node('ul',null,'review-notes'); for (const reason of reasons) list.append(node('li',reason)); root.append(list);}
    const support=node('section',null,'support'); documentLinks(supportSection(support,`Source documents (${c.documents})`),c); root.append(support); return;
  }
  if (records.length>1) {
    const nav=node('div',null,'docket-nav'); nav.setAttribute('role','tablist'); nav.setAttribute('aria-label','Appeal dockets');
    for (const row of records) {const button=node('button',row.record.docket); button.setAttribute('role','tab'); button.setAttribute('aria-selected',row.record.docket===docket); button.addEventListener('click',()=>{docket=row.record.docket;updateURL();renderDetail(true);}); nav.append(button);} root.append(nav);
    root.append(node('p','Each docket has its own answers. Related appeal intervals may overlap; do not add them together.','docket-context'));
  }
  const r=records.find(row=>row.record.docket===docket).record, grid=node('div',null,'research-grid');
  root.append(projectPanel(r,c));
  grid.append(durationCard(r,c),appellantsCard(r,c),discretionCard(r,c),outcomesCard(r,c)); root.append(grid); renderSupport(root,r,c);
  root.append(node('p',`Findings describe the supplied court records. Source: ${c.run || 'archive'} · Group ${c.group}`,'detail-footer'));
}
function renderRoute() {
  if (!state) return;
  $('overview').hidden=!!selected; $('detail').hidden=!selected;
  if (selected) renderDetail(); else {renderResearch(); renderProgress(); renderTable();}
}
function openModal(title,subtitle,trigger) {
  evidenceId++;evidenceTrigger=trigger;
  $('source').hidden=false; $('scrim').hidden=false;
  document.querySelector('main').inert=true;document.querySelector('.app-header').inert=true;
  $('sourceTitle').textContent=title;$('sourceProject').textContent=subtitle;
  $('source').setAttribute('aria-label',title);
  $('closeSource').setAttribute('aria-label','Close details');
  document.querySelector('#source .eyebrow').textContent='Details';
  $('evidenceBody').replaceChildren();$('sourcePreview').hidden=true;
  $('source').scrollTop=0;$('closeSource').focus();
}
function showExplanation(c,title,description,evidence,trigger) {
  openModal(title,c.title,trigger);
  $('closeSource').setAttribute('aria-label','Close evidence');
  document.querySelector('#source .eyebrow').textContent='Source evidence';
  const body=$('evidenceBody');
  if(description) body.append(node('p',description,'evidence-description'));
  for(const item of evidence) {
    const citation=node('section',null,'evidence-citation'), heading=node('div',null,'evidence-citation-heading');
    heading.append(node('strong',`Document ${item.doc} · Page ${item.page}`));
    const links=node('div',null,'source-actions'), text=node('button','Page text');
    text.setAttribute('aria-label',`View text of document ${item.doc}, page ${item.page}`);
    text.addEventListener('click',()=>showSourcePage(c,item));links.append(text);
    const doc=c.source_documents?.find(d=>d.doc===item.doc);
    if(doc?.sha) {const pdf=node('a','PDF ↗');pdf.href=`/api/pdf?sha=${encodeURIComponent(doc.sha)}#page=${item.page}`;pdf.target='_blank';pdf.rel='noopener';pdf.setAttribute('aria-label',`Open PDF document ${item.doc}, page ${item.page}`);links.append(pdf);}
    heading.append(links);citation.append(heading,node('blockquote',item.quote));body.append(citation);
  }
  if(!evidence.length) body.append(node('p','No separate source quotation accompanies this explanation.','endpoint-note'));
}
async function showSourcePage(c,item) {
  const request=++evidenceId;
  $('sourcePreview').hidden=false;$('pageTitle').textContent=`Full text · Document ${item.doc}, page ${item.page}`;
  $('pageText').textContent='Loading source…';
  try {
    const source=await get(`/api/source?run=${encodeURIComponent(c.run)}&group=${encodeURIComponent(c.group)}&doc=${item.doc}&page=${item.page}`);
    if (request!==evidenceId) return;
    $('pageText').textContent=source.text;
    $('sourcePreview').scrollIntoView({block:'nearest',behavior:'smooth'});
  } catch (error) {if (request===evidenceId) $('pageText').textContent=error.message;}
}
function closeEvidence() {evidenceId++; $('source').hidden=true; $('scrim').hidden=true;document.querySelector('main').inert=false;document.querySelector('.app-header').inert=false;if (evidenceTrigger?.isConnected) evidenceTrigger.focus();}
async function load(force = false) {
  if (loading && !force) return;
  const request=++loadId; loading=true;
  try {
    const next=await get(view==='all'?'/api/groups':`/api/run?run=${encodeURIComponent(view)}`);
    if (request!==loadId) return;
    const signature=JSON.stringify([view,next.summary,next.cases,next.records,next.review]);
    if (force || signature!==dataSignature) {state=next;dataSignature=signature;indexData();renderRoute();}
    $('error').hidden=true;
  } catch (error) {if (request===loadId) {$('error').hidden=false; $('error').textContent=`Data refresh failed: ${error.message}. Showing the last loaded data.`; $('freshness').textContent='Refresh failed';}}
  finally {if (request===loadId) loading=false;}
}
async function loadRuns() {
  const runs=await get('/api/runs'), existing=new Set([...$('run').options].map(o=>o.value));
  for (const run of runs) {if (!existing.has(run.name)) {const option=node('option',run.name);option.value=run.name;$('run').append(option);}}
}
$('brand').addEventListener('click',goHome);
$('run').addEventListener('change',()=>{view=$('run').value;selected=null;docket=null;page=1;closeEvidence();updateURL(true);load(true);});
$('refresh').addEventListener('click',()=>{loadRuns().catch(()=>{});load(true);});
for (const id of ['search','statusFilter','scopeFilter','pageSize']) $(id).addEventListener('input',()=>{page=1;renderTable();});
$('sortName').addEventListener('click',()=>{ascending=!ascending;$('sortArrow').textContent=ascending?'↑':'↓';renderTable();});
$('prev').addEventListener('click',()=>{page--;renderTable();}); $('next').addEventListener('click',()=>{page++;renderTable();});
$('closeSource').addEventListener('click',closeEvidence);$('scrim').addEventListener('click',closeEvidence);
document.addEventListener('keydown',e=>{
  if (e.key==='Escape') closeEvidence();
  if (e.key==='Tab' && !$('source').hidden) {
    const focusable=[...$('source').querySelectorAll('button,a[href],summary')].filter(el=>el.getClientRects().length);
    const first=focusable[0], last=focusable.at(-1);
    if (e.shiftKey && document.activeElement===first) {e.preventDefault();last.focus();}
    else if (!e.shiftKey && document.activeElement===last) {e.preventDefault();first.focus();}
  }
});
window.addEventListener('popstate',()=>{const q=new URLSearchParams(location.search);view=q.get('run') || 'all';selected=q.get('group');docket=q.get('docket');$('run').value=view;$('run').dispatchEvent(new Event('selection-sync'));closeEvidence();detailSignature='';load(true);});
enhanceSelects();
(async()=>{
  try {await loadRuns();const q=new URLSearchParams(location.search);view=q.get('run') || 'all';if (![...$('run').options].some(o=>o.value===view)) view='all';$('run').value=view;$('run').dispatchEvent(new Event('selection-sync'));selected=q.get('group');docket=q.get('docket');await load(true);setInterval(()=>{if (!document.hidden) load();},5000);setInterval(()=>{if (!document.hidden) loadRuns().catch(()=>{});},15000);}
  catch(error){$('error').hidden=false;$('error').textContent=error.message;}
})();
