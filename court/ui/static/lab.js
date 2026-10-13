'use strict';

// Diverging order: worse on the left, better on the right.
const VERDICTS = [
  ['worsened', 'Worsened', 'Lost processing, records or specificity'],
  ['mixed', 'Mixed', 'Both better and worse signals'],
  ['changed', 'Needs judgment', 'Different answers; direction unknown'],
  ['unchanged', 'Unchanged', 'Same answers; wording may differ'],
  ['improved', 'Improved', 'Recovered, gained records or more specific'],
];
const VERDICT = Object.fromEntries(VERDICTS.map(([key, label, text]) => [key, {label, text}]));
const CHANGES = {less_specific: 'Less specific', different: 'Different answer', more_specific: 'More specific', newly_measured: 'Newly measured', unchanged: 'Unchanged'};
const KINDS = {experiment: 'Experiment', baseline: 'Production'};
// The lab: experiment rounds and production's quality profile.
// Routes: #/lab  ·  #/x/DATASET[/view[/case]][?filters]
const labApi = (path, params) => api('lab/' + path, params);
function href(dataset, view = '', id = '', params = {}) {
  const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== '' && v != null)).toString();
  return '#/x/' + encodeURIComponent(dataset) + (view ? '/' + view : '') + (id ? '/' + encodeURIComponent(id) : '') + (query ? '?' + query : '');
}
async function overview(dataset) {
  return state.overviews[dataset] ??= await labApi('overview', {dataset});
}
function verdictBadge(verdict, verified) {
  const badge = el('span', 'verdict ' + verdict, node('span', VERDICT[verdict]?.label || plain(verdict)));
  if (verdict !== 'unchanged') badge.append(node('small', verified ? 'verified' : 'unverified'));
  return badge;
}

// A stacked bar of case verdicts; segments link to the filtered case list.
function verdictBar(verdicts, total, target, size = '') {
  const bar = el('div', 'verdict-bar ' + size);
  bar.setAttribute('role', 'img');
  bar.setAttribute('aria-label', VERDICTS.filter(([k]) => verdicts[k]).map(([k, l]) => verdicts[k] + ' ' + l.toLowerCase()).join(', '));
  for (const [key, label] of VERDICTS) {
    const count = verdicts[key] || 0; if (!count) continue;
    const segment = target ? link('', target(key), 'segment ' + key) : node('span', null, 'segment ' + key);
    segment.style.flexGrow = count; segment.title = `${count} ${label.toLowerCase()}`;
    if (count / total >= 0.08) segment.textContent = count;
    bar.append(segment);
  }
  return bar;
}
function verdictLegend(verdicts, target) {
  const legend = el('div', 'verdict-legend');
  for (const [key, label, text] of VERDICTS) {
    const count = verdicts[key] || 0;
    const item = el(target && count ? 'a' : 'div', 'legend-item ' + key + (count ? '' : ' empty'),
      el('span', 'legend-head', node('span', null, 'swatch ' + key), node('strong', count), node('span', label)), node('small', text));
    if (target && count) item.href = target(key);
    legend.append(item);
  }
  return legend;
}
function variableChips(list) {
  return el('div', 'chips', (list || []).map(text => pill(text, 'variable')));
}

// ---------------------------------------------------------------- index
async function renderIndex(seq) {
  crumbs(node('span', 'Lab', 'crumb-current'));
  state.overviews = {};
  const catalog = await labApi('catalog'); if (seq !== state.seq) return;
  const experiments = catalog.datasets.filter(d => d.kind === 'experiment');
  const head = el('div', 'page-heading',
    node('h1', 'Experiments'),
    node('p', 'Each experiment tests one change to the pipeline: a probe round on the cases it targets, then a confirm round on the whole reference set. Each round compares the candidate\'s results with the baseline\'s, fact by fact. Without reference answers, a comparison shows direction, not correctness.', 'lede'));
  const list = el('div', 'experiment-list');
  if (!experiments.length) list.append(el('div', 'empty', node('strong', 'No experiments yet.'), node('p', 'Create one with python -m court lab new (docs/LAB.md).')));
  for (const d of experiments) list.append(experimentCard(d));
  const references = el('div', 'reference-grid');
  for (const d of catalog.datasets.filter(d => d.kind === 'baseline')) references.append(referenceCard(d));
  page(head, legendStrip(), list, el('div', 'section-heading spaced', node('h2', 'Production')), references, tagHistory(catalog));
}
function tagHistory(catalog) {
  const tags = Object.keys(catalog.tags || {});
  if (!tags.length) return null;
  const table = el('table', 'executions', el('thead', null, el('tr', null, ...['Tag', 'Pipeline', 'Since', 'Why', 'Evidence'].map(t => node('th', t)))));
  const body = node('tbody');
  for (const tag of tags) for (const entry of [...catalog.history[tag]].reverse())
    body.append(el('tr', entry.pipeline === catalog.tags[tag] ? 'current' : 'past', node('td', tag), node('td', entry.pipeline, 'mono'),
      node('td', new Date(entry.at).toLocaleDateString()), node('td', entry.note),
      el('td', null, entry.evidence?.experiment ? link(entry.evidence.experiment, href('experiment:' + entry.evidence.experiment + ':confirm')) : node('span', '—', 'muted'))));
  table.append(body);
  return el('div', null, el('div', 'section-heading spaced', node('h2', 'Pipeline tags'),
    node('p', 'Each tag names a pipeline; moving it keeps the earlier entries, so this log is the pipeline\'s history.', 'muted')), el('div', 'table-wrap', table));
}
function legendStrip() {
  const strip = el('div', 'legend-strip');
  for (const [key, label, text] of VERDICTS) strip.append(el('span', 'legend-inline', node('span', null, 'swatch ' + key), node('strong', label), node('span', text, 'muted')));
  return strip;
}
function experimentCard(d) {
  const card = link('', href(d.id), 'experiment-card' + (d.available ? '' : ' unavailable'));
  const left = el('div', 'card-main',
    el('div', 'card-title', node('h2', d.label),
      pill(d.status === 'finalized' ? (d.round === 'confirm' ? 'Confirm round' : 'Probe round') : d.status === 'planned' ? 'Not run' : 'Unavailable', d.status || 'error')),
    d.hypothesis ? node('p', d.hypothesis, 'hypothesis') : null,
    d.error ? node('p', d.error, 'message danger') : null,
    variableChips(d.variables),
    node('p', d.cases?.selected ? fmt(d.cases.selected) + ' cases' : '', 'muted small'));
  const right = el('div', 'card-impact');
  if (d.impact) {
    right.append(node('p', d.impact.headline, 'card-headline'), verdictBar(d.impact.verdicts, d.impact.cases, null, 'small'));
    right.append(el('p', 'muted small', d.reference ? 'Scored against reference answers' : 'Unverified: no reference answers',
      d.cost?.new_usd != null ? ' · ' + money(d.cost.new_usd) + ' new spending' : ''));
  } else if (d.status === 'planned') right.append(node('p', 'Not run yet', 'card-headline'));
  card.append(left, right);
  return card;
}
function referenceCard(d) {
  return link('', href(d.id), 'reference-card', el('div', 'card-title', node('h3', d.label), pill(KINDS[d.kind], 'kind')),
    node('p', d.description || d.hypothesis || '', 'muted small'),
    node('span', 'Explore quality profile →', 'card-action'));
}

// ---------------------------------------------------------------- dataset shell
const TABS = {
  comparison: [['', 'Impact'], ['cases', 'Cases'], ['changes', 'What changed'], ['cost', 'Cost']],
  baseline: [['', 'Quality profile'], ['cases', 'Cases']],
};
function header(o, r) {
  const c = o.card || {}, e = o.experiment || {}, compact = Boolean(r.view);
  const title = el('div', 'dataset-title', node('h1', c.label),
    pill(KINDS[o.kind] || plain(o.kind), 'kind'), o.status === 'planned' ? pill('Not run', 'planned') : null);
  const head = el('div', 'dataset-head', title);
  if (c.hypothesis && compact) head.append(node('p', c.hypothesis, 'hypothesis-line'));
  if (c.hypothesis && !compact) head.append(el('blockquote', 'hypothesis-block', node('span', o.kind === 'experiment' ? 'Hypothesis' : 'About', 'eyebrow'), node('p', c.hypothesis)));
  const meta = el('dl', 'meta-row');
  const add = (label, value) => { if (value) meta.append(el('div', null, node('dt', label), node('dd', value))); };
  if (o.runs?.length === 2) add('Compared', o.runs[0].name + ' → ' + o.runs[1].name);
  if (o.pipelines?.ids?.baseline) add('Pipelines', pipelineLine(o));
  if (e.round) add('Round', plain(e.round) + ' · runs start at ' + (e.start || 'the top') + (e.focus ? ' · cases chosen at ' + e.focus : ''));
  if (e.cases) add('Cases', e.cases.selected.length ? fmt(e.cases.selected.length)
    : e.selection === 'changed' ? 'Those whose input at ' + e.focus + ' changes' : e.selection === 'all' ? 'Every case' : '0');
  if (o.output) add('Results', o.output);
  if (e.created_at) add('Created', new Date(e.created_at).toLocaleString());
  if (meta.children.length && !compact) head.append(meta);
  if (c.variables?.length && !compact) head.append(el('div', 'variables-row', node('span', 'Changed', 'eyebrow'), variableChips(c.variables)));
  const tabs = TABS[o.kind === 'baseline' ? 'baseline' : 'comparison'];
  if (o.status === 'finalized') {
    const nav = el('nav', 'tabs'); nav.setAttribute('aria-label', 'Views');
    for (const [view, label] of tabs) {
      const tab = link(label, href(r.dataset, view), view === (r.view === 'case' ? 'cases' : r.view) ? 'active' : '');
      if (tab.className) tab.setAttribute('aria-current', 'page');
      if (view === 'cases' && o.impact) tab.append(node('span', fmt(Object.values(o.impact.groups).reduce((n, g) => n + g.cases, 0)), 'tab-count'));
      nav.append(tab);
    }
    head.append(nav);
  }
  return head;
}

async function renderDataset_lab(seq, r) {
  const o = await overview(r.dataset); if (seq !== state.seq) return;
  crumbs(link('Lab', '#/lab'), r.view ? link(o.card.label, href(r.dataset)) : node('span', o.card.label, 'crumb-current'),
    r.view === 'case' ? link('Cases', href(r.dataset, 'cases')) : null, r.view ? node('span', r.view === 'case' ? r.id : {changes: 'What changed', cases: 'Cases', cost: 'Cost'}[r.view] || plain(r.view), 'crumb-current') : null);
  document.title = o.card.label + ' · Experiments';
  if (o.status === 'planned') return page(header(o, r), plannedView(o));
  if (o.status === 'empty') return page(header(o, r), section('No results yet', null, node('p', 'Production has no run over every case yet. Run it with python -m court run NAME production.', 'muted')));
  const head = header(o, r);
  if (r.view === 'case') return caseView(seq, o, r, head);
  if (r.view === 'cases') return casesView(seq, o, r, head);
  if (o.kind === 'baseline') return page(head, profileView(o, r));
  if (r.view === 'changes') return page(head, changesView(o));
  if (r.view === 'cost') return page(head, costView(o));
  return impactView(seq, o, r, head);
}

// ---------------------------------------------------------------- impact
const MEASURES = [
  ['Cases fully processed', m => m.processed, m => 'of ' + fmt(m.cases), 1],
  ['Uncertain dispositions', m => m.uncertain, null, -1],
  ['Docket records', m => m.records, null, 1],
  ['Records with a known duration', m => m.duration_known, m => m.records ? pct(m.duration_known, m.records) + '% of records' : 'no records', 1],
  ['Exact durations', m => m.duration_exact, null, 1],
  ['Research facts answered', m => m.facts_known, m => m.fact_slots ? pct(m.facts_known, m.fact_slots) + '% of ' + fmt(m.fact_slots) : 'no records', 1],
  ['Evidence warnings', m => m.warnings, null, -1],
  ['Unverified quotations', m => m.unverified_quotes, null, -1],
];
function scorecard(g, accuracy, reference) {
  const table = el('table', 'scorecard');
  table.append(el('thead', null, el('tr', null, node('th', 'Measure'), node('th', 'Baseline'), node('th', 'Candidate'), node('th', 'Change'))));
  const body = node('tbody');
  for (const [label, value, detail, direction] of MEASURES) {
    const b = value(g.baseline), c = value(g.candidate), delta = c - b;
    const tone = !delta ? 'flat' : delta * direction > 0 ? 'up' : 'down';
    body.append(el('tr', null, node('th', label),
      el('td', null, node('strong', fmt(b)), detail ? node('small', detail(g.baseline)) : null),
      el('td', null, node('strong', fmt(c)), detail ? node('small', detail(g.candidate)) : null),
      el('td', null, node('span', delta ? (delta > 0 ? '+' : '−') + fmt(Math.abs(delta)) : 'No change', 'delta ' + tone))));
  }
  const stable = g.stability;
  body.append(el('tr', 'derived', node('th', 'Answers unchanged'), node('td', '—', 'muted'),
    el('td', null, node('strong', (pct(stable.unchanged, stable.compared) ?? '—') + '%'), node('small', fmt(stable.unchanged) + ' of ' + fmt(stable.compared) + ' compared facts')),
    node('td', 'Stability between runs', 'muted small')));
  if (reference && accuracy?.comparable_fields) {
    const delta = accuracy.candidate_correct - accuracy.baseline_correct;
    body.append(el('tr', 'derived', node('th', 'Correct on reference answers'),
      el('td', null, node('strong', fmt(accuracy.baseline_correct)), node('small', 'of ' + fmt(accuracy.comparable_fields))),
      el('td', null, node('strong', fmt(accuracy.candidate_correct)), node('small', 'of ' + fmt(accuracy.comparable_fields))),
      el('td', null, node('span', delta ? (delta > 0 ? '+' : '−') + Math.abs(delta) : 'No change', 'delta ' + (delta > 0 ? 'up' : delta < 0 ? 'down' : 'flat')))));
  } else {
    body.append(el('tr', 'derived', node('th', 'Correct on reference answers'), el('td', 'muted', 'No reference answers'), node('td', null), node('td', null)));
  }
  table.append(body);
  return el('div', 'table-wrap', table);
}
function factMap(rows, questions) {
  const wrap = el('div', 'fact-map'), quiet = [];
  for (const question of questions) {
    const group = rows.filter(r => r.question === question); if (!group.length) continue;
    const moved = group.filter(r => r.total - r.unchanged || r.corrected || r.regressed);
    quiet.push(...group.filter(r => !moved.includes(r)).map(r => r.label));
    if (!moved.length) continue;
    const block = el('div', 'fact-group', node('h3', question));
    for (const r of moved) {
      const bar = el('div', 'change-bar');
      for (const key of ['regressed', 'less_specific', 'different', 'more_specific', 'newly_measured', 'corrected', 'unchanged']) {
        if (!r[key]) continue;
        const segment = node('span', null, 'segment ' + key); segment.style.flexGrow = r[key];
        segment.title = r[key] + ' ' + (CHANGES[key] || 'verified ' + key).toLowerCase(); bar.append(segment);
      }
      const parts = ['less_specific', 'different', 'more_specific', 'newly_measured'].filter(k => r[k]).map(k => r[k] + ' ' + CHANGES[k].toLowerCase() + (k === 'different' && r[k] > 1 ? 's' : ''));
      if (r.corrected) parts.unshift(r.corrected + ' verified correct');
      if (r.regressed) parts.unshift(r.regressed + ' verified regression');
      block.append(el('div', 'fact-row', node('span', r.label, 'fact-label'), bar,
        node('span', parts.join(' · ') + ` (of ${r.total})`, 'fact-summary')));
    }
    wrap.append(block);
  }
  if (!wrap.children.length) wrap.append(node('p', 'No research fact changed in any compared record.', 'empty'));
  if (quiet.length) wrap.append(el('p', 'muted small quiet-facts', node('strong', 'Unchanged everywhere: '), quiet.join(', ')));
  const legend = el('div', 'change-legend');
  for (const key of ['less_specific', 'different', 'more_specific', 'unchanged']) legend.append(el('span', null, node('span', null, 'swatch ' + key), CHANGES[key]));
  if (rows.some(r => r.corrected || r.regressed)) for (const [key, text] of [['regressed', 'Verified regression'], ['corrected', 'Verified correct']]) legend.append(el('span', null, node('span', null, 'swatch ' + key), text));
  return el('div', null, legend, wrap);
}
async function impactView(seq, o, r, head) {
  const groups = o.impact.groups, g = groups.selected || groups.companion;
  const target = role => key => href(r.dataset, 'cases', '', {verdict: key, role});
  const outcome = section('Case outcomes', null,
    node('p', g.headline, 'headline'),
    verdictBar(g.verdicts, g.cases, target(groups.selected ? 'selected' : 'companion'), 'large'),
    verdictLegend(g.verdicts, target(groups.selected ? 'selected' : 'companion')));
  if (!o.impact.reference) outcome.append(el('p', 'context-note', node('strong', 'Unverified. '),
    '“Improved” means recovered processing, more records or more specific answers, and “worsened” the reverse. Without reference answers neither establishes correctness: a more specific answer can be wrong, and an unknown can be right.'));
  if (o.round?.regressions?.length) outcome.append(el('p', 'context-note', node('strong', `${o.round.regressions.length} regressed. `), 'Adopting this candidate lists them and asks for confirmation; the tag history records them.'));
  if (o.impact.groups.selected?.inputs?.same) outcome.append(el('p', 'context-note', node('strong', 'Inputs. '),
    `${fmt(o.impact.groups.selected.inputs.same)} of these cases had the same input as the baseline at ${o.experiment?.focus || 'the focus'}; any change in them came from chance or from how cases were batched, not from the change being tested.`));
  const measures = section('Quality measures', 'The round\'s cases, baseline versus candidate. Higher is better except uncertain dispositions, warnings and unverified quotations.', scorecard(g, o.impact.accuracy, o.impact.reference));
  const moved = section('Where answers moved', 'Each research fact across compared records. Quotations and wording are excluded; records present on one side only are counted as gained or lost records instead.',
    factMap(g.fact_map, o.impact.questions));
  const attention = section('Needs attention', 'Worsened, mixed and changed cases first.');
  page(head, outcome, measures, moved, attention);
  try {
    const d = await labApi('cases', {dataset: r.dataset, limit: 6});
    if (seq !== state.seq) return;
    const rows = d.cases.filter(c => c.impact && ['worsened', 'mixed', 'changed'].includes(c.impact.verdict));
    if (!rows.length) attention.append(node('p', 'No case worsened or needs judgment.', 'empty'));
    else attention.append(caseTable(rows, r.dataset, o));
    attention.append(link('All cases →', href(r.dataset, 'cases'), 'more-link'));
  } catch (err) { attention.append(node('p', err.message, 'message danger')); }
}

// ---------------------------------------------------------------- cases
function caseName(c) { return c.dockets?.[0] || c.case_id; }
function changeLine(h, showRecord) {
  const line = el('div', 'change-line ' + (h.verified || h.change),
    node('span', null, 'dot ' + (h.verified === 'regressed' ? 'less_specific' : h.verified === 'corrected' ? 'more_specific' : h.change)),
    node('span', h.label + (showRecord && h.record ? ' · ' + h.record : ''), 'change-label'),
    el('span', 'change-values', node('span', h.baseline ?? 'unknown', h.baseline == null ? 'unknown' : ''), ' → ', node('span', h.candidate ?? 'unknown', h.candidate == null ? 'unknown' : '')));
  if (h.verified === 'corrected' || h.verified === 'regressed') line.append(node('span', h.verified === 'corrected' ? '✓ correct' : '✗ regression', 'verified-mark ' + h.verified));
  return line;
}
function caseTable(rows, dataset, o) {
  const table = el('table', 'cases');
  const compared = Boolean(o.impact);
  table.append(el('thead', null, el('tr', null, node('th', 'Case'), node('th', compared ? 'What changed' : 'Gaps'),
    node('th', compared ? 'Assessment' : 'Processing'), node('th', 'Records'))));
  const body = node('tbody');
  for (const c of rows) {
    const im = c.impact;
    const name = el('td', null, link(caseName(c), href(dataset, 'case', c.case_id), 'case-link'),
      node('span', [c.case_id !== caseName(c) ? c.case_id : null, plain(c.disposition)].filter(Boolean).join(' · '), 'cell-sub'));
    const what = el('td', 'what');
    if (im) {
      const flows = {recovered: 'Recovered', broke: 'Broke', still_failing: 'Still failing'};
      if (flows[im.processing.flow]) what.append(el('div', 'flow ' + im.processing.flow, node('strong', flows[im.processing.flow]), ' ', plain(im.processing.baseline), ' → ', plain(im.processing.candidate)));
      const shown = im.highlights.slice(0, 2), several = new Set(shown.map(h => h.record)).size > 1;
      for (const h of shown) what.append(changeLine(h, several));
      const rest = im.chips.filter(text => !/^(Recovered|Broke|Still failing)/.test(text));
      if (rest.length) what.append(node('p', rest.join(' · '), 'chips-text'));
      if (!what.children.length) what.append(node('span', 'No change', 'muted'));
    } else what.append(node('span', c.gaps?.duration_unknown ? c.gaps.duration_unknown + ' unknown duration' + (c.gaps.duration_unknown > 1 ? 's' : '') : '—', 'muted'));
    const assessment = node('td');
    if (im) assessment.append(verdictBadge(im.verdict, im.verified));
    else assessment.append(node('span', plain(c.status.baseline), 'status-text'));
    const records = node('td', c.record_count.candidate == null ? fmt(c.record_count.baseline) : fmt(c.record_count.baseline) + ' → ' + fmt(c.record_count.candidate), 'numeric');
    body.append(el('tr', null, name, what, assessment, records));
  }
  table.append(body);
  return el('div', 'table-wrap', table);
}
async function casesView(seq, o, r, head) {
  const p = r.params, baselineOnly = o.kind === 'baseline';
  const go = changes => { location.hash = href(r.dataset, 'cases', '', {...p, offset: '', ...changes}); };
  const toolbar = el('div', 'case-toolbar');
  if (!baselineOnly) {
    const groups = o.impact.groups, counts = {};
    for (const [role, g] of Object.entries(groups)) if (!p.role || p.role === role) for (const [k, v] of Object.entries(g.verdicts)) counts[k] = (counts[k] || 0) + v;
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const segmented = el('div', 'segmented'); segmented.setAttribute('role', 'group'); segmented.setAttribute('aria-label', 'Assessment');
    for (const [key, label] of [['', 'All'], ...VERDICTS.map(([k, l]) => [k, l])]) {
      const count = key ? counts[key] || 0 : total;
      const choice = button('', () => go({verdict: key}), (p.verdict || '') === key ? 'active' : '');
      choice.append(key ? node('span', null, 'swatch ' + key) : '', node('span', label), node('span', count, 'count'));
      choice.setAttribute('aria-pressed', String((p.verdict || '') === key)); choice.disabled = key && !count;
      segmented.append(choice);
    }
    toolbar.append(segmented);
  } else {
    const statuses = Object.keys(o.profile.statuses).sort();
    toolbar.append(selectControl('Processing', 'status', [['', 'Any status'], ...statuses.map(s => [s, plain(s)])], p.status || '', v => go({status: v})));
    toolbar.append(selectControl('Disposition', 'scope', [['', 'Any disposition'], ['in_scope', 'In scope'], ['uncertain', 'Uncertain'], ['excluded', 'Excluded'], ['none', 'None (failed)']], p.scope || '', v => go({scope: v})));
    toolbar.append(selectControl('Gaps', 'gap', [['', 'Any'], ['duration_unknown', 'Unknown duration']], p.gap || '', v => go({gap: v})));
  }
  const search = node('input'); search.type = 'search'; search.placeholder = 'Find a case or docket…'; search.value = p.q || '';
  search.setAttribute('aria-label', 'Search cases');
  let timer; search.oninput = () => { clearTimeout(timer); timer = setTimeout(() => go({q: search.value}), 300); };
  toolbar.append(search);
  const output = el('div', null, node('p', 'Loading cases…', 'empty'));
  page(head, toolbar, output);
  enhanceSelects(toolbar);
  if (p.q) { search.focus(); search.setSelectionRange(search.value.length, search.value.length); }
  try {
    const offset = Number(p.offset || 0);
    const d = await labApi('cases', {dataset: r.dataset, verdict: p.verdict || '', role: p.role || '', q: p.q || '', status: p.status || '',
      scope: p.scope || '', gap: p.gap || '', offset, limit: 25});
    if (seq !== state.seq) return;
    output.replaceChildren(node('p', fmt(d.total) + (d.total === 1 ? ' case' : ' cases') + (p.verdict ? ' · ' + VERDICT[p.verdict].label.toLowerCase() : ''), 'list-summary'));
    if (!d.cases.length) { output.append(node('div', 'No cases match. Clear a filter or try another search.', 'empty')); return; }
    output.append(caseTable(d.cases, r.dataset, o));
    if (d.total > 25) {
      const prev = button('← Previous', () => go({offset: Math.max(0, offset - 25)})), next = button('Next →', () => go({offset: offset + 25}));
      prev.disabled = !offset; next.disabled = offset + 25 >= d.total;
      output.append(el('div', 'pagination', node('span', `${offset + 1}–${Math.min(offset + 25, d.total)} of ${fmt(d.total)}`), el('div', null, prev, next)));
    }
  } catch (err) { output.replaceChildren(node('p', err.message, 'message danger')); }
}
function selectControl(label, key, options, value, onchange) {
  const wrap = el('div', 'filter-control'), select = node('select'), id = 'filter-' + key;
  select.id = id; select.setAttribute('aria-label', label);
  for (const [v, text] of options) { const opt = node('option', text); opt.value = v; select.append(opt); }
  select.value = value; select.onchange = () => onchange(select.value);
  const lab = node('label', label); lab.htmlFor = id;
  wrap.append(lab, select); return wrap;
}

// ---------------------------------------------------------------- case detail
async function caseView(seq, o, r, head) {
  page(head, node('p', 'Loading case…', 'empty'));
  let d;
  try { d = await labApi('case', {dataset: r.dataset, id: r.id}); } catch (err) { if (seq === state.seq) page(head, node('p', err.message, 'message danger')); return; }
  if (seq !== state.seq) return;
  const im = d.impact, id = r.id, side = d.baseline ? 'baseline' : 'candidate';
  const docket = (d.baseline?.records?.[0] || d.candidate?.records?.[0])?.docket || d.baseline?.source?.dockets?.[0] || id;
  const title = el('div', 'case-title', el('div', null, node('span', id, 'case-id'), node('h2', docket)), im ? verdictBadge(im.verdict, im.verified) : null);
  const blocks = [title];
  if (im) {
    const why = el('div', 'why');
    for (const [key, label] of [['worse', 'Worse'], ['better', 'Better'], ['judgment', 'Needs judgment'], ['notes', 'Notes']])
      for (const text of im.signals[key] || []) why.append(el('div', 'signal ' + key, node('span', label, 'signal-kind'), node('span', text)));
    if (!why.children.length) why.append(node('p', im.records.wording_only.length ? 'Answers are unchanged; only wording or citations differ.' : 'No observable change.', 'muted'));
    blocks.push(why);
  }
  const sides = el('div', 'side-summary');
  for (const s of ['baseline', 'candidate']) {
    const c = d[s]; if (!c) continue;
    const unverified = c.status?.quotes?.unverified?.length;
    sides.append(el('div', 'side ' + s, node('span', s === 'baseline' ? 'Baseline' : 'Candidate', 'eyebrow'),
      node('strong', plain(c.status?.state)), node('span', `${c.records?.length || 0} records · ${plain(c.disposition?.value || 'no disposition')}` +
        (unverified ? ` · ${unverified} unverified quotation${unverified > 1 ? 's' : ''}` : ''), 'muted small'),
      c.disposition?.reason && c.disposition.reason !== 'unknown' ? node('p', c.disposition.reason, 'muted small') : null,
      c.status?.errors?.length ? node('p', c.status.errors.map(x => x.error).join('; '), 'error-text') : null,
      button('Read source ↗', () => source(id, s, []), 'text-button')));
  }
  blocks.push(sides);
  if (im) {
    const byRecord = new Map();
    for (const f of im.facts) { const key = f.record ?? '__case'; if (!byRecord.has(key)) byRecord.set(key, []); byRecord.get(key).push(f); }
    const quiet = [];
    for (const [record, facts] of byRecord) {
      const name = record === '__case' ? 'Disposition' : record;
      if (facts.some(f => f.change !== 'unchanged' || f.verified)) blocks.push(factTable(name, facts, id));
      else quiet.push([name, facts]);
    }
    if (quiet.length) {
      const details = el('details', 'panel quiet-groups', node('summary', `Unchanged: ${quiet.map(([name, facts]) => `${name} (${facts.length} facts)`).join(', ')}`));
      for (const [name, facts] of quiet) details.append(factTable(name, facts, id));
      blocks.push(details);
    }
    for (const one of im.one_sided) blocks.push(oneSided(one, id));
  } else if (d.facts) {
    for (const one of d.facts) blocks.push(oneSided(one, id, true));
    if (!d.facts.length) blocks.push(node('p', d.baseline?.status?.errors?.[0]?.error || 'No docket records for this case.', 'empty'));
  }
  for (const s of ['baseline', 'candidate']) if (d[s]?.trace?.length) blocks.push(traceView(d[s].trace, s));
  const raw = el('details', 'technical', node('summary', 'Raw results'));
  for (const s of ['baseline', 'candidate', 'reference']) if (d[s]) raw.append(node('h4', plain(s)), node('pre', pretty({...d[s], trace: undefined})));
  blocks.push(raw);
  page(head, blocks);
}
// How the case moved through the steps: calls (reused or new), warnings, rejections.
function traceView(trace, side) {
  const table = el('table', 'executions', el('thead', null, el('tr', null, ...['Step', 'Event', 'Detail'].map(t => node('th', t)))));
  const body = node('tbody');
  for (const e of trace) {
    const detail = e.event === 'call' ? [plain(e.kind), e.call ? e.call.slice(0, 12) : null, e.batch ? 'batch: ' + e.batch.join(', ') : null, e.rejected ? 'rejected: ' + e.rejected : null]
      : e.event === 'quotes' ? [`${e.checked} quotations checked`, e.unverified ? `${e.unverified.length} unverified` : null]
      : [e.text, e.reason, e.kind ? plain(e.kind) : null];
    body.append(el('tr', null, node('td', e.step, 'mono'), node('td', plain(e.event)), node('td', detail.filter(Boolean).join(' · '))));
  }
  table.append(body);
  return el('details', 'panel quiet-groups', node('summary', (side === 'baseline' ? 'Baseline' : 'Candidate') + ` processing trace (${trace.length} events)`), el('div', 'table-wrap flat', table));
}
function source(id, side, quotes) {
  const dataset = currentRoute().dataset;
  return openSource(() => labApi('source', {dataset, id, side}), quotes, (side === 'baseline' ? 'Baseline' : 'Candidate') + ' evidence', id);
}
function evidenceButtons(f, id) {
  const cell = el('td', 'evidence');
  for (const s of ['baseline', 'candidate']) {
    const quotes = f.evidence?.[s] || [];
    if (!quotes.length) continue;
    const b = button((s === 'baseline' ? 'Baseline' : 'Candidate') + ' ↗', () => source(id, s, quotes), 'evidence-button ' + s);
    b.title = quotes.map(q => '“' + q.quote + '”').join('\n');
    cell.append(b);
  }
  return cell;
}
function factTable(title, facts, id) {
  const changed = facts.filter(f => f.change !== 'unchanged' || f.verified);
  const table = el('table', 'facts-table');
  table.append(el('thead', null, el('tr', null, node('th', 'Fact'), node('th', 'Baseline'), node('th', 'Candidate'), node('th', 'Change'), node('th', 'Evidence'))));
  const body = node('tbody');
  const row = f => el('tr', 'fact ' + f.change + (f.verified ? ' ' + f.verified : ''),
    el('th', null, node('span', f.question, 'cell-sub'), node('span', f.label)),
    node('td', f.baseline ?? 'unknown', f.baseline == null ? 'unknown' : ''),
    node('td', f.candidate ?? 'unknown', f.candidate == null ? 'unknown' : ''),
    el('td', null, node('span', CHANGES[f.change], 'change-tag ' + f.change),
      f.verified === 'corrected' ? node('span', '✓ verified correct', 'verified-mark corrected') :
      f.verified === 'regressed' ? node('span', '✗ verified regression', 'verified-mark regressed') : null),
    evidenceButtons(f, id));
  for (const f of changed) body.append(row(f));
  const rest = facts.filter(f => !changed.includes(f));
  if (rest.length) {
    const toggle = el('tr', 'toggle-row', el('td', null, button(`Show ${rest.length} unchanged facts`, () => { toggle.replaceWith(...rest.map(row)); }, 'text-button')));
    toggle.firstChild.colSpan = 5; body.append(toggle);
  }
  if (!changed.length && !rest.length) body.append(el('tr', null, node('td', 'No facts recorded.', 'muted')));
  table.append(body);
  return el('section', 'panel record-panel', el('div', 'section-heading', node('h3', title),
    node('p', changed.length ? `${changed.length} of ${facts.length} facts changed` : 'All facts unchanged', 'muted small')), el('div', 'table-wrap flat', table));
}
function oneSided(one, id, profileMode = false) {
  const known = one.facts.filter(f => f.value != null);
  const table = el('table', 'facts-table');
  table.append(el('thead', null, el('tr', null, node('th', 'Fact'), node('th', 'Answer'), node('th', 'Evidence'))));
  const body = node('tbody');
  for (const f of one.facts) {
    const ev = el('td', 'evidence');
    if (f.evidence?.length) ev.append(button('Source ↗', () => source(id, one.side, f.evidence), 'evidence-button ' + one.side));
    body.append(el('tr', f.value == null ? 'fact missing' : 'fact', el('th', null, node('span', f.question, 'cell-sub'), node('span', f.label)),
      node('td', f.value ?? 'not answered', f.value == null ? 'unknown' : ''), ev));
  }
  table.append(body);
  const heading = profileMode ? one.docket || 'Docket not identified'
    : (one.side === 'candidate' ? 'Only in candidate: ' : 'Only in baseline: ') + (one.docket || 'unidentified docket');
  return el('section', 'panel record-panel ' + (profileMode ? '' : 'one-sided ' + one.side),
    el('div', 'section-heading', node('h3', heading), node('p', `${known.length} of ${one.facts.length} facts answered`, 'muted small')),
    el('div', 'table-wrap flat', table));
}

// ---------------------------------------------------------------- what changed
function lineDiff(a, b) {
  const x = a.split('\n'), y = b.split('\n'), n = x.length, m = y.length;
  const table = Array.from({length: n + 1}, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
    table[i][j] = x[i] === y[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
  const ops = []; let i = 0, j = 0;
  while (i < n || j < m) {
    if (i < n && j < m && x[i] === y[j]) { ops.push(['same', x[i]]); i++; j++; }
    else if (i < n && (j === m || table[i + 1][j] >= table[i][j + 1])) ops.push(['remove', x[i++]]);
    else ops.push(['add', y[j++]]);
  }
  return ops;
}
function inlineDiff(before, after) {
  let start = 0; while (start < before.length && before[start] === after[start]) start++;
  let end = 0; while (end < before.length - start && end < after.length - start && before[before.length - 1 - end] === after[after.length - 1 - end]) end++;
  return [before.slice(0, start), before.slice(start, before.length - end), after.slice(start, after.length - end), before.slice(before.length - end)];
}
function diffView(before, after) {
  const ops = lineDiff(before || '', after || ''), view = el('div', 'diff');
  let hidden = [];
  const flush = (last) => {
    if (!hidden.length) return;
    const keep = last ? hidden.slice(0, 2) : hidden.length > 5 ? [...hidden.slice(0, 2), null, ...hidden.slice(-2)] : hidden;
    if (last && hidden.length > 2) keep.push(null);
    for (const line of keep) view.append(line === null ? node('div', `⋯ ${hidden.length - 4 > 0 ? hidden.length - 4 : hidden.length - 2} unchanged lines`, 'diff-gap') : el('div', 'diff-line same', node('span', ' ', 'sign'), node('span', line)));
    hidden = [];
  };
  let first = true;
  for (let k = 0; k < ops.length; k++) {
    const [op, text] = ops[k];
    if (op === 'same') { hidden.push(text); continue; }
    if (first && hidden.length > 2) { view.append(node('div', `⋯ ${hidden.length - 2} unchanged lines`, 'diff-gap')); hidden = hidden.slice(-2); }
    first = false; flush(false);
    const pair = op === 'remove' && ops[k + 1]?.[0] === 'add' ? ops[k + 1][1] : null;
    if (pair !== null) {
      const [pre, removed, added, post] = inlineDiff(text, pair);
      view.append(el('div', 'diff-line remove', node('span', '−', 'sign'), el('span', null, pre, node('del', removed), post)));
      view.append(el('div', 'diff-line add', node('span', '+', 'sign'), el('span', null, pre, node('ins', added), post)));
      k++;
    } else view.append(el('div', 'diff-line ' + op, node('span', op === 'add' ? '+' : '−', 'sign'), node('span', text)));
  }
  if (!first) flush(true); else view.append(node('p', 'No difference.', 'muted'));
  return view;
}
const describeValue = v => v == null ? '—' : typeof v === 'number' ? fmt(v) : typeof v === 'object' ? (v.text || JSON.stringify(v)) : String(v).replaceAll('_', ' ');
const shortType = type => String(type || '').split('.').pop();
function stepIndex(descriptor) {
  const found = {};
  const walk = d => { found[d.path] = d; (d.children || []).forEach(walk); };
  if (descriptor) walk(descriptor);
  return found;
}
// The pipeline as a tree of steps: composite steps hold their children, and
// steps an experiment added, removed or changed are highlighted.
function treeView(o, comparing = true) {
  const pipes = o.pipelines || {}, c = pipes.candidate || pipes.baseline, b = pipes.candidate ? pipes.baseline : null;
  if (!c) return null;
  const rows = Object.fromEntries((comparing && pipes.diff || []).map(r => [r.path, r]));
  const base = stepIndex(b), labels = o.fact_labels || {};
  const tree = el('div', 'step-tree'), details = el('div', 'step-details'), cards = [];
  function changedBelow(d) { return (d.children || []).some(k => ['added', 'changed'].includes(rows[k.path]?.status) || changedBelow(k)); }
  function card(d, status) {
    const flag = status && status !== 'unchanged' ? node('span', status, 'change-flag ' + status) : null;
    const item = button('', () => show(d.path, status === 'removed' ? b : c), 'step-card ' + (status || '') + (d.conversation === 'continue' ? ' continues' : ''));
    item.dataset.path = d.path;
    item.append(...[el('span', 'step-top', node('strong', d.path ? d.name : d.name + ' (pipeline)'), node('span', shortType(d.type), 'step-type'), flag),
      d.model ? node('span', d.model.model + (d.conversation === 'continue' ? ' · continues the previous conversation' : ''), 'step-model') : null,
      d.selector ? node('span', d.selector.text, 'step-context') : null,
      node('span', d.description || 'No description.', 'step-description'),
      d.fields?.length ? el('span', 'step-facts', d.fields.slice(0, 4).map(f => node('span', labels[f] || plain(f), 'fact-chip')),
        d.fields.length > 4 ? node('span', '+' + (d.fields.length - 4), 'fact-chip more') : null) : null].filter(Boolean));
    cards.push(item);
    return item;
  }
  function branch(d) {
    const status = rows[d.path]?.status || (comparing && b && !base[d.path] ? 'added' : null);
    const wrap = el('div', 'tree-node' + (d.children?.length ? ' composite' : '') + (changedBelow(d) ? ' changed-below' : ''), card(d, status));
    if (d.children?.length) {
      const kids = el('div', 'tree-children');
      for (const k of d.children) kids.append(branch(k));
      // Removed steps stay visible where they were.
      for (const row of Object.values(rows)) if (row.status === 'removed' && row.path.split('/').slice(0, -1).join('/') === d.path && base[row.path])
        kids.append(el('div', 'tree-node removed', card(base[row.path], 'removed')));
      wrap.append(kids);
    }
    return wrap;
  }
  tree.append(branch(c));
  function show(path, side) {
    for (const item of cards) { item.classList.toggle('selected', item.dataset.path === path); item.setAttribute('aria-pressed', String(item.dataset.path === path)); }
    const d = stepIndex(side)[path], row = rows[path], previous = base[path];
    const facts = el('dl', 'step-facts-list');
    const add = (label, value) => { if (value) facts.append(el('div', null, node('dt', label), node('dd', value))); };
    add('Path', path || '(root)');
    add('Library entry', d.type);
    add('Saved as', d.save === true ? 'output' : d.save);
    add('Model', d.model ? d.model.model + (d.model.pricing ? ` · $${d.model.pricing.input}/M input, $${d.model.pricing.output ?? 0}/M output` : '') : 'No model call');
    if (d.model) add('Settings', Object.entries(d.model.settings || {}).map(([k, v]) => plain(k) + ' ' + describeValue(v)).join(' · '));
    add('Conversation', d.conversation === 'continue' ? 'Continues the previous step’s conversation in the same group' : d.conversation === 'fresh' ? 'Starts a fresh conversation' : null);
    add('Selector', d.selector?.text);
    add('Parameters', Object.entries(d.params || {}).map(([k, v]) => plain(k) + ' ' + describeValue(v)).join(' · '));
    add('Largest group', d.max_group != null ? fmt(d.max_group) : null);
    add('Reads', (d.reads || []).map(plain).join(', '));
    add('Writes', (d.writes || []).map(plain).join(', '));
    add('Affects', (d.fields || []).map(f => labels[f] || plain(f)).join(', '));
    add('Code', Object.entries(d.code || {}).map(([f, h]) => 'steps/' + f.replace(':', '/') + '.py · ' + h.slice(0, 8)).join(' · '));
    details.replaceChildren(el('div', 'step-detail-head', node('h3', d.name), node('span', shortType(d.type), 'muted small'),
      row && row.status !== 'unchanged' ? node('span', row.status, 'change-flag ' + row.status) : null),
      node('p', d.description || 'No description recorded.', 'step-summary'), facts);
    if (comparing && row?.status === 'changed') {
      if (row.fields?.length) {
        const table = el('table', 'settings-table', el('thead', null, el('tr', null, node('th', 'Changed'), node('th', 'Baseline'), node('th', 'Candidate'))));
        const body = node('tbody');
        for (const f of row.fields) body.append(el('tr', null, node('th', plain(f.field)), node('td', describeValue(f.base)), node('td', describeValue(f.value))));
        table.append(body); details.append(el('div', 'table-wrap flat', table));
      }

      if (row.reordered) details.append(node('p', 'The order of its steps changed.', 'context-note'));
    }
    for (const [name, prompt] of Object.entries(d.prompts || {})) {
      const change = row?.prompts?.find(x => x.name === name);
      if (comparing && change) details.append(node('h4', `Prompt: ${name} (${change.status})`), diffView(change.base, change.value));
      else details.append(el('details', 'prompt-text', node('summary', `Prompt: ${name}` + (prompt.file ? ' · ' + prompt.file : '')), node('pre', prompt.text)));
    }
    if (comparing && row?.status === 'unchanged' && !previous?.children?.length) details.append(node('p', 'Unchanged from the baseline.', 'muted'));
  }
  const first = cards.find(item => /\b(added|changed|removed)\b/.test(item.className) && item.dataset.path) || cards[0];
  if (first) first.click();
  return {graph: tree, details};
}
function pipelineLine(o) {
  const ids = o.pipelines?.ids || {}, tags = o.tags || {};
  const named = id => { const t = Object.entries(tags).filter(([, v]) => v === id).map(([k]) => k); return id ? id + (t.length ? ' (' + t.join(', ') + ')' : '') : '—'; };
  return ids.candidate ? named(ids.baseline) + ' → ' + named(ids.candidate) : named(ids.baseline);
}
function changesView(o) {
  const blocks = [];
  const view = treeView(o);
  const changed = (o.pipelines?.diff || []).filter(r => r.status !== 'unchanged');
  if (view) blocks.push(section('Pipeline', `${pipelineLine(o)}. Steps run top to bottom; each step decides how it processes its cases. Added, changed and removed steps are highlighted; select a step for its description, settings, prompts and diffs.`,
    view.graph, view.details));
  if (!changed.length) blocks.push(section('No step changed', null, node('p', 'Both sides used the same pipeline. Differences between them measure run-to-run variation.', 'muted')));
  const steps = Object.entries(o.round?.steps || {});
  if (steps.length) {
    const table = el('table', 'executions', el('thead', null, el('tr', null, ...['Step', 'Changed', 'Identical', 'Only baseline', 'Only candidate'].map(t => node('th', t)))));
    const body = node('tbody');
    for (const [path, d] of steps) body.append(el('tr', d.changed ? 'current' : 'past', node('td', path, 'mono'), node('td', fmt(d.changed)),
      node('td', fmt(d.identical)), node('td', fmt(d.removed)), node('td', fmt(d.added))));
    table.append(body);
    blocks.push(section('Where the change took effect', 'Each step\'s output in this round, candidate against the baseline\'s complete run: how many cases\' outputs changed at that step.', el('div', 'table-wrap flat', table)));
  }
  return blocks;
}

// ---------------------------------------------------------------- cost
function costView(o) {
  const cost = o.cost_summary || {}, g = o.impact.groups.selected, improved = g.verdicts.improved || 0;
  const tiles = el('div', 'tiles');
  const tile = (label, value, note) => tiles.append(el('div', 'tile', node('span', label), node('strong', value), note ? node('small', note) : null));
  tile('New spending', money(cost.new_usd), 'Charged by this round');
  tile('Reused', money(cost.reused_usd), 'What the identical saved calls it reused originally cost');
  tile('Calls', fmt((cost.events?.reused || 0) + (cost.events?.sent || 0)), `${fmt(cost.events?.sent || 0)} sent · ${fmt(cost.events?.reused || 0)} reused`);
  tile('Per improved case', improved && typeof cost.new_usd === 'number' ? money(cost.new_usd / improved) : '—', `${improved} improved cases`);
  return [section('Spending', null, tiles), el('details', 'technical', node('summary', 'Accounting'), node('pre', pretty(cost)))];
}

// ---------------------------------------------------------------- planned
function plannedView(o) {
  const e = o.experiment, blocks = [];
  const view = treeView(o);
  if (view) blocks.push(section('Pipeline', `${pipelineLine(o)}. Changed steps are highlighted.`, view.graph, view.details));
  const round = e.round || 'probe', plan = o.plan;
  if (plan) {
    const cost = plan.estimate || {}, removed = plan.removed.length;
    blocks.push(section('Plan', `From make lab-plan, ${new Date(plan.planned_at).toLocaleString()}. Estimated from a dry run: nothing was sent.`,
      el('div', 'tiles', ...[
        ['Cases', fmt(plan.keys), removed ? `plus ${fmt(removed)} merged away or removed` : `chosen at ${plan.focus}`],
        ['First calls to send', fmt(cost.events?.would_send || 0), plan.not_sent ? `${fmt(plan.not_sent)} cases need calls not in the cache` : 'everything is in the cache'],
        ['Estimated cost', money(cost.would_spend_usd ?? 0), 'an upper bound for the first calls only; later steps depend on their answers']]
        .map(([label, value, note]) => el('div', 'tile', node('span', label), node('strong', value), node('small', note))))));
  }
  blocks.push(section('Commands', 'Plan for a cost estimate (no model calls), then run the round with a cap on new spending. The round appears under Runs while it runs and here when it finishes.',
    node('pre', `make lab-plan EXPERIMENT=${e.name} ROUND=${round}\nmake lab-run-send EXPERIMENT=${e.name} ROUND=${round} MAX_SPEND=1.00`)));
  return blocks;
}

// ---------------------------------------------------------------- baseline profile
function profileView(o, r) {
  const p = o.profile, m = p.metrics;
  const tiles = el('div', 'tiles');
  for (const [label, value, note] of [
    ['Cases', fmt(m.cases), `${fmt(m.processed)} with results · ${fmt(m.failed)} failed`],
    ['Docket records', fmt(m.records), `${fmt(m.uncertain)} cases with an uncertain disposition`],
    ['Known durations', (pct(m.duration_known, m.records) ?? '—') + '%', `${fmt(m.duration_known)} of ${fmt(m.records)} records · ${fmt(m.duration_exact)} exact`],
    ['Facts answered', (pct(m.facts_known, m.fact_slots) ?? '—') + '%', `${fmt(m.facts_known)} of ${fmt(m.fact_slots)} fact slots`],
    ['Unverified quotations', fmt(m.unverified_quotes), 'Cited text not found on its page']])
    tiles.append(el('div', 'tile', node('span', label), node('strong', value), node('small', note)));
  const rows = [...p.facts].sort((a, b) => a.known / (a.total || 1) - b.known / (b.total || 1));
  const bars = el('div', 'completeness');
  for (const f of rows) {
    const share = pct(f.known, f.total) ?? 0, fill = node('span', null, 'fill'); fill.style.width = share + '%';
    const row = el('div', 'completeness-row' + (share < 80 ? ' weak' : ''), el('span', 'fact-label', node('span', f.question, 'cell-sub'), node('span', f.label)),
      el('div', 'track', fill), node('strong', share + '%'), node('span', `${fmt(f.total - f.known)} unknown · common: ${f.top.map(([v]) => v).join(', ') || '—'}`, 'muted small'));
    row.append(f.key === 'duration' ? link('Find cases →', href(r.dataset, 'cases', '', {gap: 'duration_unknown'}), 'more-link') : node('span'));
    bars.append(row);
  }
  const statuses = el('div', 'status-chips');
  for (const [status, count] of Object.entries(p.statuses).filter(([s]) => s !== 'null').sort((a, b) => b[1] - a[1]))
    statuses.append(link(`${fmt(count)} ${plain(status)}`, href(r.dataset, 'cases', '', {status}), 'pill status' + (status === 'complete' ? '' : ' attention')));
  const scopes = el('div', 'status-chips');
  for (const [scope, count] of Object.entries(p.dispositions)) scopes.append(link(`${fmt(count)} ${plain(scope)}`, href(r.dataset, 'cases', '', {scope}), 'pill status'));
  return [section('At a glance', null, tiles),
    section('Where answers are missing', 'Share of docket records with a known answer for each research fact, least complete first. Unknown can be correct; low completeness marks where an experiment could test whether answers are recoverable.', bars),
    section('Processing and disposition', 'Select one to list its cases.', el('div', 'two-col', el('div', null, node('h3', 'Processing'), statuses), el('div', null, node('h3', 'Disposition'), scopes))),
    o.pipelines?.baseline ? section('Pipeline', `${pipelineLine(o)}. Select a step for its description, configuration and prompts.`, ...Object.values(treeView(o, false))) : null];
}

