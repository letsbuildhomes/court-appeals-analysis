'use strict';

// Read-only views of experiments: how each one moved its cases' research facts.
const $ = id => document.getElementById(id);
function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text != null) el.textContent = String(text);
  if (cls) el.className = cls;
  return el;
}
function el(tag, cls, ...children) {
  const item = node(tag, null, cls);
  for (const child of children.flat()) if (child != null && child !== false) item.append(child instanceof Node ? child : String(child));
  return item;
}
function link(text, to, cls, ...children) { const a = el('a', cls, text || null, ...children); a.href = to; return a; }
function button(text, action, cls = '') { const b = node('button', text, cls); b.type = 'button'; b.onclick = action; return b; }
const plain = value => String(value ?? 'Unknown').replaceAll('_', ' ');
const pretty = value => JSON.stringify(value, null, 2);
const fmt = n => typeof n === 'number' ? n.toLocaleString() : '—';
const pct = (a, b) => b ? Math.round(100 * a / b) : null;
const money = v => typeof v === 'number' ? '$' + (Math.abs(v) < 0.1 && v !== 0 ? v.toFixed(4) : v.toFixed(2)) : '—';

// Diverging order: worse on the left, better on the right.
const VERDICTS = [
  ['worsened', 'Worsened', 'Lost processing, records or specificity'],
  ['mixed', 'Mixed', 'Both better and worse signals'],
  ['changed', 'Needs judgment', 'Different answers; direction unknown'],
  ['unchanged', 'Unchanged', 'Same answers; wording may differ'],
  ['improved', 'Improved', 'Recovered, gained records or more specific'],
];
const VERDICT = Object.fromEntries(VERDICTS.map(([key, label, text]) => [key, {label, text}]));
const CHANGES = {less_specific: 'Less specific', different: 'Different answer', more_specific: 'More specific', unchanged: 'Unchanged'};
const KINDS = {experiment: 'Controlled experiment', retrospective: 'Retrospective comparison', synthetic: 'Synthetic software test',
  comparison: 'Supplied comparison', baseline: 'Baseline'};
const state = {seq: 0, overviews: {}, source: 0, returnFocus: null, last: null};

// Routes: #/  ·  #/x/DATASET[/view[/case]][?filters]
function route() {
  const [path, query] = location.hash.replace(/^#\/?/, '').split('?');
  const parts = path.split('/').filter(Boolean).map(decodeURIComponent);
  const params = Object.fromEntries(new URLSearchParams(query || ''));
  return parts[0] === 'x' ? {page: 'dataset', dataset: parts[1], view: parts[2] || '', id: parts[3], params} : {page: 'index', params};
}
function href(dataset, view = '', id = '', params = {}) {
  const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== '' && v != null)).toString();
  return '#/x/' + encodeURIComponent(dataset) + (view ? '/' + view : '') + (id ? '/' + encodeURIComponent(id) : '') + (query ? '?' + query : '');
}
async function api(path, params = {}) {
  const query = Object.keys(params).length ? '?' + new URLSearchParams(params) : '';
  const response = await fetch('/api/' + path + query), value = await response.json();
  if (!response.ok || value.error) throw Error(value.error || 'Unable to load this view.');
  return value;
}
async function overview(dataset) {
  return state.overviews[dataset] ??= await api('overview', {dataset});
}
function page(...children) { $('content').replaceChildren(...children.flat().filter(Boolean)); }
function crumbs(...items) {
  const nav = $('crumbs'); nav.replaceChildren();
  items.filter(Boolean).forEach((item, i) => { if (i) nav.append(node('span', '/', 'crumb-sep')); nav.append(item); });
}
function failure(err) { page(el('div', 'message danger', node('strong', 'This view could not be loaded.'), node('p', err.message))); }
function section(title, sub, ...children) {
  const head = el('div', 'section-heading', node('h2', title), sub ? node('p', sub, 'muted') : null);
  return el('section', 'panel', head, ...children);
}
function pill(text, cls = '') { return node('span', text, 'pill ' + cls); }
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
  crumbs(node('span', 'Experiments', 'crumb-current'));
  state.overviews = {};
  const catalog = await api('catalog'); if (seq !== state.seq) return;
  const experiments = catalog.datasets.filter(d => ['experiment', 'retrospective', 'comparison'].includes(d.kind));
  const head = el('div', 'page-heading',
    node('h1', 'Experiments'),
    node('p', 'Each experiment reruns a frozen set of cases with one change and measures how their research facts moved against the baseline. Comparisons stay unverified until reviewed reference labels exist.', 'lede'));
  const list = el('div', 'experiment-list');
  if (!experiments.length) list.append(el('div', 'empty', node('strong', 'No experiments yet.'), node('p', 'Select a cohort and plan one with the experiment runner (docs/EXPERIMENT-RUNNER.md).')));
  for (const d of experiments) list.append(experimentCard(d));
  const references = el('div', 'reference-grid');
  for (const d of catalog.datasets.filter(d => ['baseline', 'synthetic'].includes(d.kind))) references.append(referenceCard(d));
  page(head, legendStrip(), list, el('div', 'section-heading spaced', node('h2', 'Baseline and fixtures')), references);
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
      pill(d.status === 'finalized' ? 'Finalized' : d.status === 'running' ? 'Running' : d.status === 'planned' ? 'Planned' : 'Unavailable', d.status || 'error'),
      pill(KINDS[d.kind] || plain(d.kind), 'kind')),
    d.hypothesis ? node('p', d.hypothesis, 'hypothesis') : null,
    d.error ? node('p', d.error, 'message danger') : null,
    variableChips(d.variables),
    node('p', [d.cases?.selected ? fmt(d.cases.selected) + ' cases' : null, d.cases?.companions ? '+' + d.cases.companions + ' batch companions' : null,
      d.cohort ? 'cohort: ' + d.cohort : null].filter(Boolean).join(' · '), 'muted small'));
  const right = el('div', 'card-impact');
  if (d.impact) {
    right.append(node('p', d.impact.headline, 'card-headline'), verdictBar(d.impact.verdicts, d.impact.cases, null, 'small'));
    right.append(el('p', 'muted small', d.reference ? 'Scored against reference labels' : 'Unverified: no reference labels',
      d.cost?.new_usd != null ? ' · ' + money(d.cost.new_usd) + ' new spending' : d.cost?.retrospective ? ' · existing runs, no new calls' : ''));
  } else if (d.status === 'planned' || d.status === 'running') {
    const estimate = d.cost?.estimate;
    right.append(node('p', d.status === 'planned' ? 'Not run yet' : `${fmt(d.progress?.complete)} of ${fmt(d.progress?.cases)} cases complete`, 'card-headline'));
    if (estimate) right.append(node('p', `Estimated new spending up to ${money(estimate.upper_bound_usd)} · ${money(estimate.inherited_usd)} of identical work reused`, 'muted small'));
  }
  card.append(left, right);
  return card;
}
function referenceCard(d) {
  return link('', href(d.id), 'reference-card', el('div', 'card-title', node('h3', d.label), pill(KINDS[d.kind], 'kind')),
    node('p', d.description || d.hypothesis || '', 'muted small'),
    node('span', d.kind === 'baseline' ? 'Explore quality profile →' : 'Open fixture →', 'card-action'));
}

// ---------------------------------------------------------------- dataset shell
const TABS = {
  comparison: [['', 'Impact'], ['cases', 'Cases'], ['changes', 'What changed'], ['cost', 'Cost']],
  baseline: [['', 'Quality profile'], ['cases', 'Cases']],
};
function header(o, r) {
  const c = o.card || {}, e = o.experiment || {}, compact = Boolean(r.view);
  const title = el('div', 'dataset-title', node('h1', c.label),
    pill(KINDS[o.kind] || plain(o.kind), 'kind'), o.status !== 'finalized' && o.kind !== 'baseline' ? pill(o.status === 'running' ? 'Running' : 'Planned', o.status) : null);
  const head = el('div', 'dataset-head', title);
  if (o.synthetic) head.append(el('p', 'synthetic-note', node('strong', 'Synthetic software test. '), 'Cases, labels and costs are invented to exercise every outcome; they are not research results.'));
  if (c.hypothesis && compact) head.append(node('p', c.hypothesis, 'hypothesis-line'));
  if (c.hypothesis && !compact) head.append(el('blockquote', 'hypothesis-block', node('span', ['experiment', 'retrospective'].includes(o.kind) ? 'Hypothesis' : 'About', 'eyebrow'), node('p', c.hypothesis)));
  const meta = el('dl', 'meta-row');
  const add = (label, value) => { if (value) meta.append(el('div', null, node('dt', label), node('dd', value))); };
  if (o.runs?.length === 2) add('Compared', o.runs[0].name + ' → ' + o.runs[1].name);
  if (e.cohort) add('Cohort', e.cohort.name + (e.cohort.query ? ' · ' + queryText(e.cohort.query) : ''));
  if (e.cases) add('Cases', fmt(e.cases.selected.length) + ' selected' + (e.cases.companions.length ? ` + ${e.cases.companions.length} batch companions` : ''));
  if (e.created_at) add('Created', new Date(e.created_at).toLocaleString());
  if (meta.children.length && !compact) head.append(meta);
  if (c.variables?.length && !compact) head.append(el('div', 'variables-row', node('span', 'Changed', 'eyebrow'), variableChips(c.variables)));
  const tabs = TABS[o.kind === 'baseline' ? 'baseline' : 'comparison'];
  if (o.status === 'finalized' || o.kind === 'baseline') {
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
function queryText(query) {
  if (query.shared_sources) return 'every case both runs share';
  return Object.entries(query).map(([k, v]) => plain(k) + ' = ' + (Array.isArray(v) ? v.join(' or ') : v)).join(', ');
}

async function renderDataset(seq, r) {
  const o = await overview(r.dataset); if (seq !== state.seq) return;
  crumbs(link('Experiments', '#/'), r.view ? link(o.card.label, href(r.dataset)) : node('span', o.card.label, 'crumb-current'),
    r.view === 'case' ? link('Cases', href(r.dataset, 'cases')) : null, r.view ? node('span', r.view === 'case' ? r.id : {changes: 'What changed', cases: 'Cases', cost: 'Cost'}[r.view] || plain(r.view), 'crumb-current') : null);
  document.title = o.card.label + ' · Experiments';
  if (o.status === 'planned' || o.status === 'running') return page(header(o, r), plannedView(o));
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
  ['Finalized docket records', m => m.records, null, 1],
  ['Records with a known duration', m => m.duration_known, m => m.records ? pct(m.duration_known, m.records) + '% of records' : 'no records', 1],
  ['Exact durations', m => m.duration_exact, null, 1],
  ['Research facts answered', m => m.facts_known, m => m.fact_slots ? pct(m.facts_known, m.fact_slots) + '% of ' + fmt(m.fact_slots) : 'no records', 1],
  ['Evidence warnings', m => m.warnings, null, -1],
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
    body.append(el('tr', 'derived', node('th', 'Correct on reference labels'),
      el('td', null, node('strong', fmt(accuracy.baseline_correct)), node('small', 'of ' + fmt(accuracy.comparable_fields))),
      el('td', null, node('strong', fmt(accuracy.candidate_correct)), node('small', 'of ' + fmt(accuracy.comparable_fields))),
      el('td', null, node('span', delta ? (delta > 0 ? '+' : '−') + Math.abs(delta) : 'No change', 'delta ' + (delta > 0 ? 'up' : delta < 0 ? 'down' : 'flat')))));
  } else {
    body.append(el('tr', 'derived', node('th', 'Correct on reference labels'), el('td', 'muted', 'No reference labels yet'), node('td', null), node('td', null)));
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
      for (const key of ['regressed', 'less_specific', 'different', 'more_specific', 'corrected', 'unchanged']) {
        if (!r[key]) continue;
        const segment = node('span', null, 'segment ' + key); segment.style.flexGrow = r[key];
        segment.title = r[key] + ' ' + (CHANGES[key] || 'verified ' + key).toLowerCase(); bar.append(segment);
      }
      const parts = ['less_specific', 'different', 'more_specific'].filter(k => r[k]).map(k => r[k] + ' ' + CHANGES[k].toLowerCase() + (k === 'different' && r[k] > 1 ? 's' : ''));
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
    '“Improved” means recovered processing, more records or more specific answers, and “worsened” the reverse. Without reviewed reference labels neither establishes correctness: a more specific answer can be wrong, and an unknown can be right.'));
  if (groups.companion && groups.selected) {
    const comp = groups.companion;
    outcome.append(el('div', 'companions', el('div', null, node('strong', 'Batch companions'), node('p', `${comp.cases} cases reviewed alongside the cohort for identical batch context. Their changes are side effects, not the experiment's target.`, 'muted small')),
      el('div', null, verdictBar(comp.verdicts, comp.cases, target('companion'), 'small'), node('p', comp.headline, 'small muted'))));
  }
  const measures = section('Quality measures', 'Selected cases, baseline versus candidate. Higher is better except warnings.', scorecard(g, o.impact.accuracy, o.impact.reference));
  const moved = section('Where answers moved', 'Each research fact across compared records. Quotations and wording are excluded; records present on one side only are counted as gained or lost records instead.',
    factMap(g.fact_map, o.impact.questions));
  const attention = section('Needs attention', 'Worsened, mixed and changed cases first.');
  page(head, outcome, measures, moved, attention);
  try {
    const d = await api('cases', {dataset: r.dataset, limit: 6});
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
      node('span', [c.case_id !== caseName(c) ? c.case_id : null, plain(c.scope_category) + ' scope'].filter(Boolean).join(' · '), 'cell-sub'),
      im?.role === 'companion' ? pill('batch companion', 'companion') : null);
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
    if (groups.companion) toolbar.append(selectControl('Cases', 'role', [['', 'All cases'], ['selected', 'Selected cohort'], ['companion', 'Batch companions']], p.role || '', v => go({role: v})));
  } else {
    const statuses = Object.keys(o.profile.statuses).sort();
    toolbar.append(selectControl('Processing', 'status', [['', 'Any status'], ...statuses.map(s => [s, plain(s)])], p.status || '', v => go({status: v})));
    toolbar.append(selectControl('Scope', 'scope', [['', 'Any scope'], ['confirmed', 'Confirmed'], ['uncertain', 'Uncertain'], ['excluded', 'Excluded']], p.scope || '', v => go({scope: v})));
    toolbar.append(selectControl('Gaps', 'gap', [['', 'Any'], ['duration_unknown', 'Unknown duration']], p.gap || '', v => go({gap: v})));
  }
  const search = node('input'); search.type = 'search'; search.placeholder = 'Find a case or docket…'; search.value = p.q || '';
  search.setAttribute('aria-label', 'Search cases');
  let timer; search.oninput = () => { clearTimeout(timer); timer = setTimeout(() => go({q: search.value}), 300); };
  toolbar.append(search);
  const output = el('div', null, node('p', 'Loading cases…', 'empty'));
  page(head, toolbar, baselineOnly ? cohortHint(o, p) : null, output);
  enhanceSelects(toolbar);
  if (p.q) { search.focus(); search.setSelectionRange(search.value.length, search.value.length); }
  try {
    const offset = Number(p.offset || 0);
    const d = await api('cases', {dataset: r.dataset, verdict: p.verdict || '', role: p.role || '', q: p.q || '', status: p.status || '',
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
function cohortHint(o, p) {
  const flags = [p.status ? '--status ' + p.status : null, p.scope ? '--scope ' + p.scope : null,
    p.gap === 'duration_unknown' ? '--duration-kind unknown' : null].filter(Boolean);
  if (!flags.length || !o.artifact_path) return null;
  const command = `make experiments-cohort COHORT_ARGS='data/experiments/cohorts/NAME.json --from ${o.artifact_path} --name NAME --description "WHY" ${flags.join(' ')}'`;
  return el('details', 'cohort-hint', node('summary', 'Turn these filters into an experiment cohort'), node('p', 'The cohort freezes this query and the cases it selects:', 'muted small'), node('pre', command));
}

// ---------------------------------------------------------------- case detail
async function caseView(seq, o, r, head) {
  page(head, node('p', 'Loading case…', 'empty'));
  let d;
  try { d = await api('case', {dataset: r.dataset, id: r.id}); } catch (err) { if (seq === state.seq) page(head, node('p', err.message, 'message danger')); return; }
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
    sides.append(el('div', 'side ' + s, node('span', s === 'baseline' ? 'Baseline' : 'Candidate', 'eyebrow'),
      node('strong', plain(c.execution?.status)), node('span', `${c.records?.length || 0} records · ${plain(c.scope_category)} scope`, 'muted small'),
      c.execution?.error ? node('p', c.execution.error, 'error-text') : null,
      button('Read source ↗', () => source(id, s, []), 'text-button')));
  }
  blocks.push(sides);
  if (im) {
    const byRecord = new Map();
    for (const f of im.facts) { const key = f.record ?? '__case'; if (!byRecord.has(key)) byRecord.set(key, []); byRecord.get(key).push(f); }
    const quiet = [];
    for (const [record, facts] of byRecord) {
      const name = record === '__case' ? 'Case scope' : record;
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
    if (!d.facts.length) blocks.push(node('p', d.baseline?.execution?.error || 'No finalized docket records in this group.', 'empty'));
  }
  const raw = el('details', 'technical', node('summary', 'Raw saved outputs'));
  for (const s of ['baseline', 'candidate', 'reference']) if (d[s]) raw.append(node('h4', plain(s)), node('pre', pretty(d[s])));
  blocks.push(raw);
  page(head, blocks);
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
function stepsById(recipe) {
  return Object.fromEntries((recipe?.phases || []).flatMap(phase => phase.steps.map(step => [step.id, step])));
}
function stepChanges(b, c) {
  if (!b || !c) return [[b ? 'Step' : 'Step', b ? 'present' : 'absent', c ? 'present' : 'removed']];
  const rows = [], add = (label, x, y) => { if (JSON.stringify(x) !== JSON.stringify(y)) rows.push([label, x, y]); };
  add('Component', b.component, c.component);
  add('Model', b.provider?.model, c.provider?.model);
  add('Provider', b.provider?.name, c.provider?.name);
  add('Conversation', b.context, c.context);
  for (const key of new Set([...Object.keys(b.settings || {}), ...Object.keys(c.settings || {})])) add(plain(key), b.settings?.[key], c.settings?.[key]);
  for (const role of new Set([...Object.keys(b.prompts || {}), ...Object.keys(c.prompts || {})]))
    if (b.prompts?.[role]?.sha256 !== c.prompts?.[role]?.sha256) rows.push(['Prompt: ' + role, b.prompts?.[role]?.name || '—', (c.prompts?.[role]?.name || '—') + ' (edited)']);
  return rows;
}
const describeValue = v => v == null ? '—' : typeof v === 'number' ? fmt(v) : String(v).replaceAll('_', ' ');
function recipeView(o, comparing = true) {
  const b = o.designs?.baseline?.recipe, c = o.designs?.candidate?.recipe;
  if (!c?.phases) return null;
  const base = stepsById(b), labels = o.fact_labels || {};
  const basePhases = Object.fromEntries((b?.phases || []).map(p => [p.id, p]));
  const graph = el('div', 'recipe-graph'), details = el('div', 'step-details');
  let selected = null;
  const cards = [];
  c.phases.forEach((phase, index) => {
    const batchChanged = comparing && JSON.stringify(basePhases[phase.id]?.batch || null) !== JSON.stringify(phase.batch || null);
    const panel = el('div', 'phase' + (batchChanged ? ' changed' : ''),
      el('div', 'phase-head', node('strong', plain(phase.id)), node('span', phase.kind === 'batched' ? 'Batched' : 'One case at a time', 'muted small'),
        phase.batch ? node('span', phase.batch.text + (batchChanged ? ' · changed' : ''), 'batch-tag') : null));
    const row = el('div', 'step-row');
    phase.steps.forEach((step, i) => {
      const changes = comparing ? stepChanges(base[step.id], step) : [];
      if (i) row.append(node('span', '→', 'step-arrow' + (step.context === 'continue' ? ' continues' : '')));
      const card = button('', () => show(step.id), 'step-card' + (changes.length ? ' changed' : '') + (step.context === 'continue' ? ' continues' : ''));
      card.dataset.step = step.id;
      card.append(el('span', 'step-top', node('strong', plain(step.id)), changes.length ? node('span', base[step.id] ? 'changed' : 'added', 'change-flag') : null),
        node('span', step.provider?.model || '—', 'step-model'),
        step.context === 'continue' ? node('span', '↳ continues the previous conversation', 'step-context') : node('span', 'Fresh conversation', 'step-context'),
        el('span', 'step-facts', (step.facts || []).slice(0, 3).map(f => node('span', labels[f] || plain(f), 'fact-chip')),
          (step.facts || []).length > 3 ? node('span', '+' + (step.facts.length - 3), 'fact-chip more') : null));
      cards.push(card); row.append(card);
    });
    panel.append(row); graph.append(panel);
    if (index < c.phases.length - 1) graph.append(node('div', '⟶', 'phase-arrow'));
  });
  const removed = Object.keys(base).filter(id => !stepsById(c)[id]);
  if (comparing && removed.length) graph.append(node('p', 'Removed steps: ' + removed.join(', '), 'muted small'));
  function show(id) {
    selected = id;
    for (const card of cards) { card.classList.toggle('selected', card.dataset.step === id); card.setAttribute('aria-pressed', String(card.dataset.step === id)); }
    const step = stepsById(c)[id], previous = base[id], changes = comparing ? stepChanges(previous, step) : [];
    details.replaceChildren(el('div', 'step-detail-head', node('h3', plain(id)), node('span', step.component + ' component', 'muted small')),
      node('p', step.summary || 'No description recorded.', 'step-summary'),
      el('dl', 'step-facts-list',
        el('div', null, node('dt', 'Reads'), node('dd', (step.reads || []).map(plain).join(', ') || '—')),
        el('div', null, node('dt', 'Writes'), node('dd', (step.writes || []).map(plain).join(', ') || '—')),
        el('div', null, node('dt', 'Affects'), node('dd', (step.facts || []).map(f => labels[f] || plain(f)).join(', ') || '—')),
        el('div', null, node('dt', 'Model'), node('dd', `${step.provider?.model || '—'}` + (step.provider?.pricing ? ` · $${step.provider.pricing.input}/M input, $${step.provider.pricing.output ?? 0}/M output` : ''))),
        el('div', null, node('dt', 'Settings'), node('dd', Object.entries(step.settings || {}).map(([k, v]) => plain(k) + ' ' + describeValue(v)).join(' · ') || '—'))));
    if (comparing) {
      if (!changes.length) details.append(node('p', 'Unchanged from the baseline.', 'muted'));
      else {
        const table = el('table', 'settings-table', el('thead', null, el('tr', null, node('th', 'Changed'), node('th', 'Baseline'), node('th', 'Candidate'))));
        const body = node('tbody');
        for (const [label, x, y] of changes) body.append(el('tr', null, node('th', label), node('td', describeValue(x)), node('td', describeValue(y))));
        table.append(body); details.append(el('div', 'table-wrap flat', table));
      }
      for (const [role, prompt] of Object.entries(step.prompts || {})) {
        if (previous?.prompts?.[role]?.sha256 === prompt.sha256) continue;
        details.append(node('h4', `Prompt for ${role}: ${prompt.name}`),
          diffView(o.designs.baseline?.prompts?.[previous?.prompts?.[role]?.name], o.designs.candidate?.prompts?.[prompt.name]));
      }
    }
  }
  const first = cards.find(card => card.classList.contains('changed')) || cards[0];
  if (first) show(first.dataset.step);
  return {graph, details};
}
function changesView(o) {
  const e = o.experiment, designs = o.designs, v = e?.variables || {}, blocks = [];
  if (o.kind === 'retrospective') blocks.push(el('p', 'context-note', node('strong', 'Retrospective comparison. '),
    'These runs were not produced as a controlled experiment. Differences can come from any change between them, including code, model behavior and the variables recorded below.'));
  const recipe = recipeView(o);
  if (recipe) blocks.push(section('Pipeline', 'Phases run left to right; steps within a phase run in order. Changed steps are highlighted; select a step to see what it does and what changed.',
    recipe.graph, recipe.details));
  const overrides = v.recipe || [];
  const notes = [...overrides.map(c => [c.path, describeValue(c.base), describeValue(c.value)]),
    ...(v.components || []).map(name => ['component file', '—', name]),
    ...(v.rerun?.length ? [['rerun', '—', v.rerun.join(', ')]] : [])];
  if (notes.length) {
    const table = el('table', 'settings-table', el('thead', null, el('tr', null, node('th', 'Override'), node('th', 'Baseline'), node('th', 'Candidate'))));
    const body = node('tbody'); for (const [path, x, y] of notes) body.append(el('tr', null, node('th', path), node('td', x), node('td', y)));
    table.append(body); blocks.push(o.kind === 'retrospective'
      ? section('Recorded differences', 'How the two runs\u2019 recipes differ.', el('div', 'table-wrap flat', table))
      : section('Recorded overrides', 'Exactly what the experiment definition changed in the recipe.', el('div', 'table-wrap flat', table)));
  }
  const prompts = Object.keys({...designs.baseline?.prompts, ...designs.candidate?.prompts}).sort();
  const changedPrompts = prompts.filter(k => designs.baseline?.prompts?.[k] !== designs.candidate?.prompts?.[k]);
  if (!changedPrompts.length && !notes.length && !(recipe && [...recipe.graph.querySelectorAll('.changed')].length))
    blocks.push(section('No variables changed', null, node('p', 'Both runs used the same recipe and prompts. Differences between them measure run-to-run variation.', 'muted')));
  if (!recipe) for (const key of changedPrompts) blocks.push(section(`Prompt: ${key}`, 'Removed text is struck through; added text is highlighted.',
    diffView(designs.baseline?.prompts?.[key], designs.candidate?.prompts?.[key])));
  else if (changedPrompts.length) blocks.push(node('p', 'Changed prompts: ' + changedPrompts.join(', ') + '. Select a step that uses one to see its diff.', 'muted small'));
  return blocks;
}

// ---------------------------------------------------------------- cost
function costView(o) {
  const e = o.experiment, cand = o.runs[1]?.cost || {}, base = o.runs[0]?.cost || {};
  const g = o.impact.groups.selected || o.impact.groups.companion;
  const improved = g.verdicts.improved || 0;
  const tiles = el('div', 'tiles');
  const tile = (label, value, note) => tiles.append(el('div', 'tile', node('span', label), node('strong', value), note ? node('small', note) : null));
  if (o.kind === 'retrospective') {
    tile('Baseline run, these cases', money(base.recorded_usd), 'Recorded charges, including retries');
    tile('Candidate run, these cases', money(cand.recorded_usd), 'Recorded charges, including retries');
    tile('New spending', money(0), 'A retrospective comparison makes no model calls');
  } else {
    tile('New spending', money(cand.new_usd), 'Charged by this experiment');
    tile('Estimate', e?.estimate ? '≤ ' + money(e.estimate.upper_bound_usd) : '—', 'Worst case planned for new calls');
    tile('Inherited', money(cand.inherited_usd), 'Identical saved calls reused from the base run');
    tile('Per improved case', improved && typeof cand.new_usd === 'number' ? money(cand.new_usd / improved) : '—', `${improved} improved selected cases`);
  }
  const blocks = [section('Spending', o.synthetic ? 'Simulated amounts.' : null, tiles)];
  if (e?.estimate && typeof cand.new_usd === 'number') {
    const max = Math.max(e.estimate.upper_bound_usd, cand.new_usd) || 1;
    const bars = el('div', 'cost-bars');
    for (const [label, value, cls] of [['Actual new spending', cand.new_usd, 'actual'], ['Planned upper bound', e.estimate.upper_bound_usd, 'bound']]) {
      const fill = node('span', null, 'fill ' + cls); fill.style.width = (100 * value / max) + '%';
      bars.append(el('div', 'cost-bar', node('span', label), el('div', 'track', fill), node('strong', money(value))));
    }
    blocks[0].append(bars);
  }
  const executions = o.executions || [];
  if (executions.length) {
    const table = el('table', 'executions', el('thead', null, el('tr', null, ...['Started', 'Cap', 'New spending', 'Outcome', 'Statuses after'].map(t => node('th', t)))));
    const body = node('tbody');
    for (const x of executions) body.append(el('tr', null, node('td', new Date(x.started_at).toLocaleString()), node('td', money(x.max_new_spend_usd)),
      node('td', money(x.new_spend_usd)), node('td', plain(x.outcome) + (x.error ? ': ' + x.error : '')),
      node('td', Object.entries(x.status_counts || {}).map(([k, v]) => v + ' ' + plain(k)).join(', '))));
    table.append(body); blocks.push(section('Executions', 'Each run invocation, with its cumulative cap on new spending.', el('div', 'table-wrap flat', table)));
  }
  blocks.push(el('details', 'technical', node('summary', 'Accounting metadata'), node('pre', pretty({baseline: base, candidate: cand}))));
  return blocks;
}

// ---------------------------------------------------------------- planned
function plannedView(o) {
  const e = o.experiment, progress = o.progress || {}, estimate = e.estimate || {};
  const blocks = [];
  const stage = {extracted: 'first pass done, awaiting review', not_run: 'not started'};
  const statuses = el('div', 'status-chips', Object.entries(progress.statuses || {}).map(([k, v]) => pill(v + ' ' + (stage[k] || plain(k)), 'status')));
  blocks.push(section(o.status === 'planned' ? 'Ready to run' : 'In progress', 'Results appear here after the experiment is finalized.', statuses));
  const tiles = el('div', 'tiles');
  for (const [label, value, note] of [['Upper bound', money(estimate.upper_bound_usd), 'Worst case for all new calls'],
    ['Inherited', money(estimate.inherited_usd), 'Identical saved calls the dry run reused'],
    ['New spending so far', money(o.cost_summary?.new_usd), 'Charged by this experiment'],
    ['New calls planned', fmt(Object.values(estimate.new_calls || {}).reduce((a, b) => a + b, 0)),
      Object.entries(estimate.new_calls || {}).map(([k, v]) => v + ' ' + k).join(', ') || 'None']])
    tiles.append(el('div', 'tile', node('span', label), node('strong', value), note ? node('small', note) : null));
  blocks.push(section('Estimate', 'From a dry run of the composed recipe: no model calls. Running enforces a cap on new spending before every call.', tiles));
  const recipe = recipeView(o);
  if (recipe) blocks.push(section('Pipeline', `Changed steps are highlighted. Review batching: ${plain(e.batching?.status)}.`, recipe.graph, recipe.details));
  blocks.push(section('Commands', 'Run with a cap on new spending, then finalize to compare.',
    node('pre', `make experiments-run EXPERIMENT=${e.name} EXPERIMENT_ARGS='--max-spend ${Math.max(0.5, Math.ceil((estimate.upper_bound_usd || 0.5) * 2) / 2).toFixed(2)}'\nmake experiments-finalize EXPERIMENT=${e.name}`)));
  return blocks;
}

// ---------------------------------------------------------------- baseline profile
function profileView(o, r) {
  const p = o.profile, m = p.metrics;
  const tiles = el('div', 'tiles');
  for (const [label, value, note] of [
    ['Source groups', fmt(m.cases), `${fmt(m.processed)} fully processed`],
    ['Finalized records', fmt(m.records), 'Docket records with every stage complete'],
    ['Known durations', (pct(m.duration_known, m.records) ?? '—') + '%', `${fmt(m.duration_known)} of ${fmt(m.records)} records · ${fmt(m.duration_exact)} exact`],
    ['Facts answered', (pct(m.facts_known, m.fact_slots) ?? '—') + '%', `${fmt(m.facts_known)} of ${fmt(m.fact_slots)} fact slots`]])
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
  for (const [status, count] of Object.entries(p.statuses).sort((a, b) => b[1] - a[1]))
    statuses.append(link(`${fmt(count)} ${plain(status)}`, href(r.dataset, 'cases', '', {status}), 'pill status' + (status === 'complete' ? '' : ' attention')));
  const scopes = el('div', 'status-chips');
  for (const [scope, count] of Object.entries(p.scopes)) scopes.append(link(`${fmt(count)} ${plain(scope)}`, href(r.dataset, 'cases', '', {scope}), 'pill status'));
  return [section('At a glance', null, tiles),
    section('Where answers are missing', 'Share of finalized records with a known answer for each research fact, least complete first. Unknown can be correct; low completeness marks where an experiment could test whether answers are recoverable.', bars),
    section('Processing and scope', 'Select a group to list its cases.', el('div', 'two-col', el('div', null, node('h3', 'Processing'), statuses), el('div', null, node('h3', 'Scope'), scopes)))];
}

// ---------------------------------------------------------------- source drawer
function findQuote(text, quote) {
  const words = String(quote).trim().split(/\s+/).filter(Boolean);
  if (!words.length) return null;
  const pattern = new RegExp(words.map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('\\s+'));
  const match = pattern.exec(text);
  return match ? [match.index, match.index + match[0].length] : null;
}
function findPage(text, doc, page) {
  const start = doc != null ? text.indexOf(`<document doc="${doc}"`) : 0;
  const at = text.indexOf(`<page number="${page}"`, Math.max(0, start));
  if (at < 0) return null;
  return [at, text.indexOf('>', at) + 1];
}
function closeSource() {
  state.source++; $('source').hidden = true; $('source-scrim').hidden = true;
  state.returnFocus?.focus?.(); state.returnFocus = null;
}
async function source(id, side, quotes) {
  const seq = ++state.source, dataset = route().dataset; state.returnFocus = document.activeElement;
  $('source').hidden = false; $('source-scrim').hidden = false; $('source-title').textContent = (side === 'baseline' ? 'Baseline' : 'Candidate') + ' evidence';
  $('source-status').className = 'muted'; $('source-status').textContent = 'Verifying the frozen source…'; $('source-text').replaceChildren(); $('close-source').focus();
  try {
    const d = await api('source', {dataset, id, side}); if (seq !== state.source) return;
    if (!d.verified) throw Error('Source integrity was not verified.');
    const text = d.text, marks = [], pages = [];
    for (const q of quotes || []) {
      const found = findQuote(text, q.quote);
      if (found) marks.push(found); else if (q.page != null) pages.push(findPage(text, q.doc, q.page));
    }
    marks.sort((a, b) => a[0] - b[0]);
    // Quotations are highlighted; unmatched ones fall back to their cited page.
    const spans = [...marks.map(([a, b]) => [a, b, 'mark']), ...pages.filter(Boolean).map(([a, b]) => [a, b, 'page'])].sort((x, y) => x[0] - y[0]);
    let cursor = 0; const out = $('source-text');
    for (const [start, end, kind] of spans) {
      if (start < cursor) continue;
      out.append(document.createTextNode(text.slice(cursor, start)), node(kind === 'mark' ? 'mark' : 'span', text.slice(start, end), kind === 'page' ? 'page-marker' : null));
      cursor = end;
    }
    out.append(document.createTextNode(text.slice(cursor)));
    const missing = (quotes || []).length - marks.length;
    $('source-status').textContent = `Verified source · ${id}` + (quotes?.length ? ` · ${marks.length} of ${quotes.length} quotations highlighted` : '') +
      (missing > 0 ? ` · ${missing} not verbatim; their cited pages are marked` : '');
    $('source-status').title = 'SHA-256: ' + d.sha256;
    (out.querySelector('mark') || out.querySelector('.page-marker'))?.scrollIntoView({block: 'center'});
    if (!spans.length) out.scrollTop = 0;
  } catch (err) { if (seq === state.source) { $('source-status').textContent = 'Source unavailable: ' + err.message; $('source-status').className = 'message danger'; } }
}
$('close-source').onclick = closeSource; $('source-scrim').onclick = closeSource;
document.addEventListener('keydown', event => {
  if ($('source').hidden) return;
  if (event.key === 'Escape') { event.preventDefault(); closeSource(); }
  if (event.key === 'Tab') {
    const target = document.activeElement;
    if (event.shiftKey && target === $('close-source')) { event.preventDefault(); $('source-text').focus(); }
    else if (!event.shiftKey && target === $('source-text')) { event.preventDefault(); $('close-source').focus(); }
  }
});

// ---------------------------------------------------------------- router
async function render() {
  const seq = ++state.seq, r = route(), sameView = state.last && state.last.dataset === r.dataset && state.last.view === r.view && r.view === 'cases';
  if ($('source').hidden === false) closeSource();
  try {
    if (r.page === 'index') { document.title = 'Experiments · Court review'; await renderIndex(seq); }
    else await renderDataset(seq, r);
    if (!sameView) window.scrollTo(0, 0);
  } catch (err) { if (seq === state.seq) failure(err); }
  state.last = r;
}
window.addEventListener('hashchange', render);
render();
