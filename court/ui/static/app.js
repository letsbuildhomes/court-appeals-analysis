'use strict';

// The router, the home overview and live refresh.
// Routes: #/  ·  #/runs[/NAME]  ·  #/lab  ·  #/x/...  ·  #/data[/ID[/KEY]]  ·  #/pipelines[/ID]
function currentRoute() {
  const [path, query] = location.hash.replace(/^#\/?/, '').split('?');
  const parts = path.split('/').filter(Boolean).map(decodeURIComponent);
  const params = Object.fromEntries(new URLSearchParams(query || ''));
  const section = parts[0] || 'home';
  if (section === 'x') return {section: 'lab', page: 'dataset', dataset: parts[1], view: parts[2] || '', id: parts[3], params};
  if (section === 'runs') return {section, name: parts[1], params};
  if (section === 'data') return {section, id: parts[1], key: parts.slice(2).join('/'), params};
  if (section === 'pipelines') return {section, id: parts[1], params};
  return {section, params};
}

async function renderHome(seq) {
  crumbs(node('span', 'Overview', 'crumb-current'));
  const d = await api('home'); if (seq !== state.seq) return;
  state.live = d.running.length ? () => renderHome(state.seq) : null;
  const blocks = [el('div', 'page-heading', node('h1', 'Overview'),
    node('p', 'The pipeline turns court PDFs into structured research data; the lab tests changes to it. Runs, experiments, data and pipelines are all here, read-only.', 'lede'))];
  blocks.push(section(d.running.length ? 'Running now' : 'Recent runs', d.running.length ? 'Updating every two seconds.' : null,
    runsTable(d.running.length ? d.running : d.recent.slice(0, 5)), link('All runs →', '#/runs', 'more-link')));
  if (d.production) {
    const m = d.production.metrics || {};
    const tiles = el('div', 'tiles');
    const tile = (label, value, note) => tiles.append(el('div', 'tile', node('span', label), node('strong', value), note ? node('small', note) : null));
    tile('Pipeline', d.production.pipeline, d.production.name);
    if (m.cases != null) {
      tile('Cases', fmt(m.cases), `${fmt(m.processed)} with results · ${fmt(m.failed)} failed`);
      tile('Records', fmt(m.records), `${fmt(m.duration_known)} with a known duration`);
      tile('Facts answered', (pct(m.facts_known, m.fact_slots) ?? '—') + '%', `${fmt(m.facts_known)} of ${fmt(m.fact_slots)}`);
    }
    blocks.push(section('Production', d.production.description, tiles, el('div', 'status-chips',
      link('Quality profile →', '#/x/production', 'pill status'), link('Step tree →', '#/pipelines/' + d.production.pipeline, 'pill status'),
      d.production.output ? link('Results →', '#/data/' + d.production.output, 'pill status') : null)));
  }
  const experiments = el('div', 'experiment-list');
  for (const x of d.experiments) experiments.append(experimentCard(x));
  if (!d.experiments.length) experiments.append(el('div', 'empty', node('strong', 'No experiments yet. '), 'Create one with python -m court lab new (docs/LAB.md).'));
  blocks.push(section('Experiments', null, experiments));
  blocks.push(section('Named datasets', null, el('div', 'status-chips', ...d.datasets.map(x => link(x.names.join(', '), '#/data/' + x.id, 'pill status'))),
    link('All data →', '#/data', 'more-link')));
  page(...blocks);
}

const NAV = [['home', 'Overview', '#/'], ['runs', 'Runs', '#/runs'], ['lab', 'Lab', '#/lab'], ['data', 'Data', '#/data'], ['pipelines', 'Pipelines', '#/pipelines']];
function navigation(current) {
  $('nav').replaceChildren(...NAV.map(([key, label, to]) => {
    const a = link(label, to, key === current ? 'active' : ''); if (key === current) a.setAttribute('aria-current', 'page'); return a;
  }));
}

async function render(liveRefresh = false) {
  const seq = ++state.seq, r = currentRoute();
  if (!liveRefresh && $('source').hidden === false) closeSource();
  if (!liveRefresh) state.live = null;
  navigation(r.section);
  const scroll = window.scrollY;
  try {
    if (r.section === 'home') await renderHome(seq);
    else if (r.section === 'runs') await (r.name ? renderRun(seq, r) : renderRuns(seq));
    else if (r.section === 'lab') await (r.page === 'dataset' ? renderDataset_lab(seq, r) : renderIndex(seq));
    else if (r.section === 'data') await (r.key ? renderItem(seq, r) : r.id ? renderDataset(seq, r) : renderDatasets(seq));
    else if (r.section === 'pipelines') await (r.id ? renderPipeline(seq, r) : renderPipelines(seq));
    else page(el('div', 'empty', 'No such page. ', link('Go to the overview', '#/')));
    document.title = (NAV.find(([k]) => k === r.section)?.[1] || 'Court review') + ' · Court review';
    if (liveRefresh) window.scrollTo(0, scroll);
    else if (!(state.last && state.last.section === r.section && state.last.name === r.name && state.last.id === r.id)) window.scrollTo(0, 0);
  } catch (err) { if (seq === state.seq && !liveRefresh) failure(err); }
  state.last = r;
}
window.addEventListener('hashchange', () => render());
setInterval(() => {
  // Live views refresh unless the reader is typing or reading a source.
  if (state.live && !document.hidden && $('source').hidden && document.activeElement?.tagName !== 'INPUT') render(true);
}, 2000);
render();
