'use strict';

// Pipelines: every resolved pipeline, its step tree, its tags, its runs, and diffs between two.
async function renderPipelines(seq) {
  crumbs(node('span', 'Pipelines', 'crumb-current'));
  const d = await api('pipelines'); if (seq !== state.seq) return;
  const table = el('table', 'cases', el('thead', null, el('tr', null, ...['Pipeline', 'Tags', 'Based on', 'Runs', 'Created'].map(t => node('th', t)))));
  const body = node('tbody');
  for (const p of d.pipelines) body.append(el('tr', null, el('td', null, link(p.id, '#/pipelines/' + p.id, 'case-link mono'), node('span', p.name || '', 'cell-sub')),
    el('td', null, ...p.tags.map(t => pill(t, 'status'))), el('td', null, p.base ? link(p.base, '#/pipelines/' + p.base, 'mono small') : node('span', 'a full definition', 'muted small')),
    node('td', fmt(p.runs), 'numeric'), node('td', p.created_at ? new Date(p.created_at).toLocaleDateString() : '', 'small')));
  table.append(body);
  const log = el('table', 'executions', el('thead', null, el('tr', null, ...['When', 'Tag', 'Pipeline', 'Why', 'Evidence'].map(t => node('th', t)))));
  const logBody = node('tbody');
  for (const e of [...d.log].reverse()) logBody.append(el('tr', null, node('td', new Date(e.at).toLocaleString(), 'small'), node('td', e.tag),
    el('td', null, link(e.pipeline, '#/pipelines/' + e.pipeline, 'mono')), node('td', e.note),
    el('td', null, e.evidence?.experiment ? link(e.evidence.experiment, `#/x/${encodeURIComponent('experiment:' + e.evidence.experiment + ':confirm')}`) : node('span', '—', 'muted'))));
  log.append(logBody);
  page(el('div', 'page-heading', node('h1', 'Pipelines'),
    node('p', 'Each pipeline is a tree of library steps with their settings and prompts; its ID is the hash of all of it. Tags name pipelines, and their log is the pipeline\'s history.', 'lede')),
    section('Resolved pipelines', null, el('div', 'table-wrap flat', table)), section('Tag history', 'Newest first.', el('div', 'table-wrap flat', log)));
}

async function renderPipeline(seq, r) {
  const other = r.params.compare || '';
  const d = await api('pipeline', {id: r.id, other}); if (seq !== state.seq) return;
  crumbs(link('Pipelines', '#/pipelines'), node('span', d.id, 'crumb-current'));
  const all = await api('pipelines'); if (seq !== state.seq) return;
  const head = el('div', 'dataset-head', el('div', 'dataset-title', node('h1', d.name || d.id), ...d.tags.map(t => pill(t, 'kind'))),
    d.description ? node('p', d.description, 'hypothesis-line') : null);
  const meta = el('dl', 'meta-row');
  const add = (label, value) => { if (value) meta.append(el('div', null, node('dt', label), el('dd', null, value))); };
  add('ID', node('span', d.id, 'mono'));
  add('Based on', d.definition?.base ? link(d.definition.base, '#/pipelines/' + d.definition.base, 'mono') : 'a full definition');
  add('Created', d.created_at ? new Date(d.created_at).toLocaleString() : null);
  add('Commit', d.commit ? node('span', d.commit.slice(0, 10), 'mono') : null);
  head.append(meta);
  const compare = node('select'); compare.setAttribute('aria-label', 'Compare with');
  for (const [value, text] of [['', 'Compare with…'], ...all.pipelines.filter(p => p.id !== d.id).map(p => [p.id, `${p.id} ${p.tags.join(', ')} ${p.name || ''}`])]) {
    const option = node('option', text); option.value = value; compare.append(option);
  }
  compare.value = other; compare.onchange = () => { location.hash = '#/pipelines/' + d.id + (compare.value ? '?compare=' + compare.value : ''); };
  const tags = Object.fromEntries(all.pipelines.flatMap(p => p.tags.map(t => [t, p.id])));
  const view = treeView({pipelines: d.pipelines, fact_labels: {}, tags}, Boolean(other));
  const changed = other ? (d.pipelines.diff || []).filter(x => x.status !== 'unchanged').length : null;
  const blocks = [head, section('Steps', other ? `Compared with ${other}: ${changed} positions differ. Select a step for its settings, prompts and diffs.`
    : 'Select a step for its description, settings and prompts.', el('div', 'case-toolbar', el('div', 'filter-control', compare)), view.graph, view.details)];
  if (d.runs.length) blocks.push(section('Runs', null, runsTable(d.runs)));
  if (d.definition?.changes?.length) blocks.push(el('details', 'technical', node('summary', 'Definition'), node('pre', pretty(d.definition))));
  page(...blocks);
  enhanceSelects(document);
}
