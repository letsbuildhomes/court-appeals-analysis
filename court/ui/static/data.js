'use strict';

// Datasets: every saved step output, and a viewer for each kind.
const KIND_TEXT = {documents: 'PDFs and their listing entries', pages: 'Selected text of every PDF page', cases: 'Docket groups with their documents and text',
  results: 'Structured results, one per case', traces: 'What happened to each item in a run', output: 'An intermediate step\'s output'};

function datasetLink(id, names) { return link((names?.length ? names.join(', ') + ' · ' : '') + id, '#/data/' + id, 'mono'); }

async function renderDatasets(seq) {
  crumbs(node('span', 'Data', 'crumb-current'));
  const d = await api('datasets'); if (seq !== state.seq) return;
  const byKind = {};
  for (const x of d.datasets) (byKind[x.kind] ??= []).push(x);
  const blocks = [el('div', 'page-heading', node('h1', 'Data'),
    node('p', 'Every saved dataset: each stage\'s output, results maps, intermediate outputs saved for experiments, and traces. A dataset\'s ID comes from its contents, so identical outputs share one.', 'lede'))];
  for (const kind of ['documents', 'pages', 'cases', 'results', 'output', 'traces', ...Object.keys(byKind).filter(k => !KIND_TEXT[k])]) {
    const rows = byKind[kind]; if (!rows) continue;
    const table = el('table', 'datasets', el('thead', null, el('tr', null, ...['Dataset', 'Items', 'Made by', 'Created'].map(t => node('th', t)))));
    const body = node('tbody');
    for (const x of rows) body.append(el('tr', null, el('td', null, datasetLink(x.id, x.names)),
      node('td', fmt(x.items), 'numeric'),
      el('td', 'small', x.run ? link(x.run, '#/runs/' + encodeURIComponent(x.run)) : node('span', 'imported', 'muted'), x.position ? node('span', ' at ' + x.position, 'muted') : null),
      node('td', x.created_at ? new Date(x.created_at).toLocaleDateString() : '', 'small')));
    table.append(body);
    const details = kind === 'traces' || kind === 'output';
    const panel = section(plain(kind) + ` (${rows.length})`, KIND_TEXT[kind] || null, el('div', 'table-wrap flat', table));
    blocks.push(details ? el('details', 'panel quiet-groups', node('summary', `${plain(kind)} (${rows.length}): ${KIND_TEXT[kind]}`), el('div', 'table-wrap flat', table)) : panel);
  }
  page(...blocks);
}

function reportPanel(d) {
  const r = d.report; if (!r) return null;
  const tiles = el('div', 'tiles');
  const tile = (label, value, note) => tiles.append(el('div', 'tile', node('span', label), node('strong', value), note ? node('small', note) : null));
  if (d.kind === 'cases') {
    tile('Cases', fmt(r.cases), `${fmt(r.multi_docket_cases)} with several dockets`);
    tile('Documents', fmt(r.documents), Object.entries(r.composition).map(([k, v]) => `${v} ${k}`).join(' · '));
    tile('Flags', fmt(Object.values(r.flags).reduce((a, b) => a + b, 0)), Object.entries(r.flags).map(([k, v]) => `${v} ${plain(k)}`).join(' · ') || 'none');
    tile('Weak membership', fmt(r.weak_documents), 'documents whose dockets came only from the listing, from nowhere, or disagree');
    return section('Grouping report', 'Basis: ' + Object.entries(r.basis).map(([k, v]) => `${v} ${plain(k)}`).join(' · ') + '. Filter the items to flagged cases to review them.', tiles);
  }
  if (d.kind === 'pages') {
    tile('Pages', fmt(r.pages), `${fmt(r.documents)} documents`);
    for (const [k, v] of Object.entries(r.status)) tile(plain(k), fmt(v), k === 'usable' ? Object.entries(r.source).filter(([s]) => s !== 'none').map(([s, n]) => `${n} ${s}`).join(' · ') : null);
    return section('Text audit', `${fmt(r.documents_with_unresolved_pages)} documents have unresolved pages.`, tiles);
  }
  if (d.kind === 'results') {
    const m = r.metrics;
    tile('Cases', fmt(m.cases), `${fmt(m.processed)} results · ${fmt(m.failed)} failed`);
    tile('Dispositions', Object.entries(r.dispositions).map(([k, v]) => `${v} ${plain(k)}`).join(' · '));
    tile('Records', fmt(m.records), `${fmt(m.duration_known)} with a known duration · ${fmt(m.duration_exact)} exact`);
    tile('Facts answered', (pct(m.facts_known, m.fact_slots) ?? '—') + '%', `${fmt(m.facts_known)} of ${fmt(m.fact_slots)}`);
    return section('Profile', Object.keys(r.failures).length ? 'Failures: ' + Object.entries(r.failures).map(([k, v]) => `${v} ${plain(k)}`).join(' · ') : null, tiles);
  }
  if (d.kind === 'documents') {
    tile('Documents', fmt(r.documents), Object.entries(r.sources).map(([k, v]) => `${v} from ${k}`).join(' · '));
    tile('Listing entries', fmt(r.listing_entries));
    tile('Size', (r.bytes / 1e6).toFixed(0) + ' MB');
    return section('Summary', null, tiles);
  }
  return null;
}

const ITEM_COLUMNS = {
  cases: [['Case', x => x.key], ['Dockets', x => x.dockets.join(', ')], ['Documents', x => fmt(x.documents)], ['Composition', x => x.composition],
    ['Flags', x => x.flags ? pill(x.flags + ' flagged', 'status attention') : ''], ['Weak', x => x.weak || '']],
  pages: [['Page', x => x.key.slice(0, 12) + '…/' + x.page], ['Status', x => x.status], ['Source', x => x.source || '—'], ['Reason', x => plain(x.reason)], ['Characters', x => fmt(x.chars)]],
  results: [['Case', x => x.key], ['Disposition', x => plain(x.disposition)], ['Records', x => fmt(x.records)], ['Dockets', x => x.dockets.join(', ')]],
  documents: [['Document', x => x.key.slice(0, 12) + '…'], ['Title', x => x.title], ['Posted', x => (x.posted || '').slice(0, 10)], ['Docket', x => x.docket], ['Listings', x => fmt(x.listings)]],
};

async function renderDataset(seq, r) {
  const p = r.params;
  const d = await api('dataset', {id: r.id, q: p.q || '', offset: p.offset || 0, limit: 50}); if (seq !== state.seq) return;
  crumbs(link('Data', '#/data'), node('span', d.names[0] || d.id, 'crumb-current'));
  const m = d.manifest, producer = m.producer || {};
  const head = el('div', 'dataset-head', el('div', 'dataset-title', node('h1', d.names[0] || d.id), pill(d.kind, 'kind')), node('p', KIND_TEXT[d.kind] || '', 'hypothesis-line'));
  const meta = el('dl', 'meta-row');
  const add = (label, value) => { if (value) meta.append(el('div', null, node('dt', label), el('dd', null, value))); };
  add('ID', node('span', d.id, 'mono'));
  add('Contract', m.contract);
  add('Made by', producer.run ? el('span', null, link(producer.run, '#/runs/' + encodeURIComponent(producer.run)), producer.position ? ' at ' + producer.position : '') : producer.imported_from || null);
  add('Pipeline', producer.pipeline ? link(producer.pipeline, '#/pipelines/' + producer.pipeline) : null);
  add('From', Object.entries(producer.inputs || {}).length ? el('span', null, ...Object.entries(producer.inputs).map(([n, id]) => link(`${n} ${id}`, '#/data/' + id, 'pill status'))) : null);
  add('Created', m.created_at ? new Date(m.created_at).toLocaleString() : null);
  head.append(meta);
  const go = changes => { location.hash = '#/data/' + d.id + '?' + new URLSearchParams(Object.entries({...p, offset: '', ...changes}).filter(([, v]) => v)); };
  const search = node('input'); search.type = 'search'; search.placeholder = 'Find an item…'; search.value = p.q || '';
  let timer; search.oninput = () => { clearTimeout(timer); timer = setTimeout(() => go({q: search.value}), 300); };
  const columns = ITEM_COLUMNS[d.kind] || [['Item', x => x.key], ['Preview', x => x.preview || '']];
  const table = el('table', 'cases', el('thead', null, el('tr', null, ...columns.map(([t]) => node('th', t)))));
  const body = node('tbody');
  for (const row of d.rows) {
    const cells = row.failure ? [link(row.key, `#/data/${d.id}/${row.key}`, 'case-link'), node('span', `${plain(row.failure.kind)} failure at ${row.failure.step}: ${row.failure.reason}`, 'error-text')]
      : columns.map(([, f], i) => i === 0 ? link(f(row), `#/data/${d.id}/${row.key}`, 'case-link') : f(row));
    body.append(el('tr', null, ...cells.map(c => c instanceof Node ? el('td', null, c) : node('td', c ?? ''))));
  }
  table.append(body);
  const pager = d.total > d.limit ? el('div', 'pagination', node('span', `${d.offset + 1}–${Math.min(d.offset + d.limit, d.total)} of ${fmt(d.total)}`),
    el('div', null, button('← Previous', () => go({offset: Math.max(0, d.offset - d.limit)})), button('Next →', () => go({offset: d.offset + d.limit})))) : null;
  page(head, reportPanel(d), section('Items', `${fmt(d.total)}${p.q ? ' matching' : ''}`, el('div', 'case-toolbar', search), el('div', 'table-wrap flat', table), pager));
  if (p.q) { search.focus(); search.setSelectionRange(search.value.length, search.value.length); }
}

// A PDF page beside its text, with page-by-page navigation.
function pageViewer(doc, pageNumber, pageCount, text, meta, go) {
  const image = node('img'); image.alt = `Page ${pageNumber} of the PDF`; image.src = `/api/page-image?doc=${doc}&page=${pageNumber}`;
  const message = node('p', 'Rendering page…', 'muted');
  image.onload = () => message.remove(); image.onerror = () => { message.textContent = 'Could not render this page.'; };
  const nav = el('div', 'viewer-nav', button('← Page', () => go(pageNumber - 1)), node('span', `Page ${pageNumber} of ${pageCount}`, 'small'), button('Page →', () => go(pageNumber + 1)));
  nav.children[0].disabled = pageNumber <= 1; nav.children[2].disabled = pageNumber >= pageCount;
  return el('div', 'page-viewer', el('div', 'viewer-head', nav, el('div', 'chips', ...meta)),
    el('div', 'viewer-panes', el('div', 'viewer-image', message, image), el('pre', 'viewer-text', text ?? 'No text for this page.')));
}

async function renderItem(seq, r) {
  const p = r.params;
  const d = await api('item', {id: r.id, key: r.key, pages: p.pages || ''}); if (seq !== state.seq) return;
  crumbs(link('Data', '#/data'), link(r.id, '#/data/' + r.id), node('span', r.key, 'crumb-current'));
  const head = el('div', 'dataset-head', el('div', 'dataset-title', node('h1', r.key), pill(d.kind, 'kind')));
  if (d.failure) return page(head, el('div', 'message danger', node('strong', `${plain(d.failure.kind)} failure at ${d.failure.step}. `), d.failure.reason));
  const v = d.value;
  if (d.kind === 'pages') {
    const go = n => { location.hash = `#/data/${r.id}/${v.doc}/${n}`; };
    const meta = [pill(v.status, v.status === 'usable' ? 'status' : 'status attention'), v.source ? pill(v.source, 'status') : null, pill(plain(v.reason), 'variable'),
      v.ocr_confidence != null ? pill(`OCR confidence ${v.ocr_confidence.toFixed(1)}%`, 'status') : null].filter(Boolean);
    return page(head, pageViewer(v.doc, v.page, d.page_count, v.text, meta, go));
  }
  if (d.kind === 'cases') return renderCase(r, d, head);
  if (d.kind === 'results') return renderResult(r, d, head);
  if (d.kind === 'documents') {
    const table = el('table', 'cases', el('thead', null, el('tr', null, ...['Title', 'Posted', 'Docket', 'Division', 'Listing page'].map(t => node('th', t)))));
    const body = node('tbody');
    for (const e of v.listings) body.append(el('tr', null, el('td', null, link(e.title, e.final_url || e.media_url)), node('td', (e.date_attribute || '').slice(0, 10)),
      node('td', e.docket_raw || '—'), node('td', e.division || '—'), node('td', `${e.page_index + 1}, entry ${e.ordinal + 1}`, 'small')));
    table.append(body);
    const pages = d.pages.pages, current = Number(p.page || pages[0] || 1);
    const blocks = [head, section('Listing entries', `${(v.bytes / 1e3).toFixed(0)} KB · ${v.file}`, el('div', 'table-wrap flat', table))];
    if (pages.length) {
      const text = await api('page-text', {id: d.pages.dataset, doc: v.sha256, page: current}).catch(() => null);
      blocks.push(section('Pages', `Text from ${d.pages.dataset}.`, pageViewer(v.sha256, current, pages.length, text?.text,
        text ? [pill(text.status, 'status'), text.source ? pill(text.source, 'status') : null].filter(Boolean) : [],
        n => { location.hash = `#/data/${r.id}/${r.key}?page=${n}`; })));
    }
    return page(...blocks);
  }
  page(head, el('pre', 'json', pretty(v)));
}

// Docket numbers as unbreakable chips; "none" stands out.
function dockets(list) {
  return list.length ? el('td', 'dockets', ...list.map(x => node('span', x, 'docket'))) : node('td', 'none', 'attention');
}

function renderCase(r, d, head) {
  const v = d.value, p = r.params;
  const table = el('table', 'cases', el('thead', null, el('tr', null, ...['Doc', 'Title', 'Posted', 'Caption dockets', 'Listing dockets', 'Basis', 'Flags', 'Pages'].map(t => node('th', t)))));
  const body = node('tbody');
  for (const doc of v.documents) {
    const weak = ['listing_only', 'none', 'disagree'].includes(doc.basis);
    body.append(el('tr', doc.doc === Number(p.doc || 1) ? 'selected-row' : null, el('td', null, link(String(doc.doc), `#/data/${r.id}/${r.key}?doc=${doc.doc}`)), node('td', doc.title || '—'),
      node('td', (doc.posted || '').slice(0, 10), 'small'), dockets(doc.caption_dockets), dockets(doc.listing_dockets),
      el('td', null, pill(plain(doc.basis), weak ? 'status attention' : 'status')),
      el('td', null, ...doc.flags.map(f => pill(f, 'status attention'))), node('td', fmt(doc.pages), 'numeric')));
  }
  table.append(body);
  const chosen = v.documents.find(x => x.doc === Number(p.doc || 1)) || v.documents[0];
  const pageNumber = Number(p.page || 1);
  const text = d.pages[String(chosen.doc)]?.[String(pageNumber)];
  const meta = [pill(`doc ${chosen.doc} · ${plain(chosen.basis)}`, 'variable'), ...chosen.flags.map(f => pill(f, 'status attention'))];
  return page(head, el('p', 'hypothesis-line', `${v.dockets.join(', ')} · ${plain(v.composition)} · ${fmt(d.text_chars)} characters of case text`),
    section('Documents', 'How grouping placed each document: the dockets read from its page-1 caption and from the court listing, and the flags it raised. Select a document to read it.', el('div', 'table-wrap flat', table)),
    section(`Document ${chosen.doc}`, 'The page beside the text this case gives the model for it.',
      pageViewer(chosen.sha256, pageNumber, chosen.pages, text, meta, n => { location.hash = `#/data/${r.id}/${r.key}?doc=${chosen.doc}&page=${n}`; })));
}

function renderResult(r, d, head) {
  const v = d.value, disposition = v.disposition || {};
  const sourceButton = (quotes, label = 'Source ↗') => d.cases ? button(label, () => openSource(() => api('source', {id: r.id, key: r.key}), quotes, 'Evidence', r.key), 'evidence-button baseline') : null;
  const summary = el('div', 'side-summary', el('div', 'side', node('span', 'Disposition', 'eyebrow'), node('strong', plain(disposition.value)),
    node('span', `municipal appeal: ${plain(disposition.municipal_appeal)} · housing: ${plain(disposition.housing)}`, 'muted small'),
    disposition.reason && disposition.reason !== 'unknown' ? node('p', disposition.reason, 'muted small') : null, sourceButton(disposition.evidence || [], 'Evidence ↗')),
    el('div', 'side', node('span', 'Records', 'eyebrow'), node('strong', fmt(v.records.length)),
      node('span', d.unverified.length ? `${d.unverified.length} quotations not found on their page` : 'every quotation found on its page', 'muted small'),
      d.cases ? link('Case text and documents →', `#/data/${d.cases}/${r.key}`, 'more-link') : null));
  const blocks = [head, summary];
  for (const one of d.facts) {
    const table = el('table', 'facts-table', el('thead', null, el('tr', null, node('th', 'Fact'), node('th', 'Answer'), node('th', 'Evidence'))));
    const body = node('tbody');
    for (const f of one.facts) body.append(el('tr', f.value == null ? 'fact missing' : 'fact', el('th', null, node('span', f.question, 'cell-sub'), node('span', f.label)),
      node('td', f.value ?? 'unknown', f.value == null ? 'unknown' : ''), el('td', 'evidence', f.evidence?.length ? sourceButton(f.evidence) : null)));
    table.append(body);
    blocks.push(el('section', 'panel record-panel', el('div', 'section-heading', node('h3', one.docket || 'Docket not identified'),
      node('p', `${one.facts.filter(f => f.value != null).length} of ${one.facts.length} facts answered`, 'muted small')), el('div', 'table-wrap flat', table)));
  }
  blocks.push(el('details', 'technical', node('summary', 'Raw result'), node('pre', pretty(v))));
  page(...blocks);
}
