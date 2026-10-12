'use strict';

// Runs, live: every run of any pipeline, whether over every case or an experiment's few.
const RUN_STATES = {running: 'Running', complete: 'Complete', stopped: 'Stopped', failed: 'Failed', interrupted: 'Interrupted'};
const ITEM_STATES = {working: 'Not finished yet', failing: 'Failing', done: 'Done', failed: 'Failed'};

function duration(seconds) {
  if (seconds == null || !isFinite(seconds)) return '—';
  seconds = Math.max(0, Math.round(seconds));
  const h = Math.floor(seconds / 3600), m = Math.floor(seconds / 60) % 60, s = seconds % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
}
function since(start, end) {
  if (!start) return null;
  return ((end ? new Date(end) : new Date()) - new Date(start)) / 1000;
}
function stateBadge(stateName) {
  return el('span', 'run-state ' + stateName, stateName === 'running' ? node('span', null, 'pulse') : null, RUN_STATES[stateName] || plain(stateName));
}
function runKind(r) {
  const l = r.labels || {};
  if (l.experiment) return `${l.experiment} · ${l.round} ${l.side}`;
  return r.keys ? `${fmt(r.keys)} chosen cases` : 'every input';
}
function spendBar(spent, limit, mode) {
  if (mode !== 'send') return node('span', mode === 'dry' ? 'dry run: no calls sent' : 'cache only: no calls sent', 'muted small');
  const fill = node('span', null, 'fill' + (limit && spent / limit > .9 ? ' hot' : ''));
  fill.style.width = (limit ? Math.min(100, 100 * spent / limit) : 0) + '%';
  return el('div', 'spend', el('div', 'track', fill), node('span', `${money(spent)} of ${money(limit)}`, 'small'));
}
function runRow(r) {
  const progress = r.state === 'running' ? (r.current.length ? 'at ' + r.current[r.current.length - 1] : 'starting') :
    r.total != null ? `${fmt(r.done ?? r.total)} items` + (Object.keys(r.failed).length ? ' · ' + Object.entries(r.failed).map(([k, n]) => `${n} ${plain(k)}`).join(', ') : '') : '—';
  return el('tr', 'click', el('td', null, link(r.name, '#/runs/' + encodeURIComponent(r.name), 'case-link'), node('span', runKind(r), 'cell-sub')),
    el('td', null, stateBadge(r.state)), node('td', r.at, 'mono'), node('td', progress),
    el('td', null, spendBar(r.spent_usd, r.limit_usd, r.mode)),
    node('td', `${fmt(r.calls.reused)} reused · ${fmt(r.calls.sent)} sent`, 'numeric small'),
    node('td', r.started_at ? new Date(r.started_at).toLocaleString() : '—', 'small'),
    node('td', duration(since(r.started_at, r.state === 'running' ? null : r.finished_at)), 'numeric'));
}
function runsTable(rows) {
  const table = el('table', 'runs', el('thead', null, el('tr', null, ...['Run', 'State', 'Starts at', 'Progress', 'Spending', 'Calls', 'Started', 'Time'].map(t => node('th', t)))));
  const body = node('tbody');
  for (const r of rows) body.append(runRow(r));
  if (!rows.length) body.append(el('tr', null, el('td', 'empty', 'No runs yet.')));
  table.append(body);
  return el('div', 'table-wrap', table);
}

async function renderRuns(seq) {
  crumbs(node('span', 'Runs', 'crumb-current'));
  const d = await api('runs'); if (seq !== state.seq) return;
  const running = d.runs.filter(r => r.state === 'running');
  state.live = running.length ? () => renderRuns(state.seq) : null;
  page(el('div', 'page-heading', node('h1', 'Runs'),
    node('p', 'Every run of a pipeline: over all its inputs, or over an experiment\'s cases. Running runs update every two seconds, with spending against their cap.', 'lede')),
    running.length ? section('Running now', null, runsTable(running)) : null,
    section(running.length ? 'All runs' : 'Runs', 'Newest first. Running a name again continues it: finished work comes from the cache.', runsTable(d.runs.filter(r => r.state !== 'running'))));
}

function sparkline(points, limit) {
  const width = 560, height = 90, svgNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`); svg.setAttribute('class', 'sparkline'); svg.setAttribute('role', 'img');
  svg.setAttribute('preserveAspectRatio', 'none');
  if (points.length < 2) { svg.setAttribute('aria-label', 'Not enough charges yet'); return svg; }
  const times = points.map(([t]) => new Date(t).getTime()), values = points.map(([, v]) => v);
  const t0 = times[0], t1 = times[times.length - 1] || t0 + 1, top = Math.max(limit || 0, values[values.length - 1]) || 1;
  const x = t => 4 + (width - 8) * (t - t0) / Math.max(1, t1 - t0), y = v => height - 4 - (height - 8) * v / top;
  const path = document.createElementNS(svgNS, 'path');
  path.setAttribute('d', points.map(([t, v], i) => (i ? 'L' : 'M') + x(new Date(t).getTime()).toFixed(1) + ' ' + y(v).toFixed(1)).join(' '));
  path.setAttribute('class', 'line');
  svg.append(path);
  if (limit) {
    const cap = document.createElementNS(svgNS, 'line');
    cap.setAttribute('x1', 0); cap.setAttribute('x2', width); cap.setAttribute('y1', y(limit)); cap.setAttribute('y2', y(limit)); cap.setAttribute('class', 'cap');
    svg.append(cap);
  }
  svg.setAttribute('aria-label', `Spending rose to ${money(values[values.length - 1])} over ${duration((t1 - t0) / 1000)}`);
  return svg;
}

function stepsTable(d) {
  const table = el('table', 'steps', el('thead', null, el('tr', null, ...['Step', 'State', 'Items', 'Progress', 'Calls', 'Spent', 'Tokens in / out', 'Time'].map(t => node('th', t)))));
  const body = node('tbody'), steps = [...d.steps];
  // Steps the pipeline has but the run has not reached yet, in tree order.
  const order = [];
  const walk = (n, depth) => { order.push([n.path, depth, n]); (n.children || []).forEach(c => walk(c, depth + 1)); };
  if (d.tree) walk(d.tree, 0);
  const atRoot = d.record.at || '';
  const known = Object.fromEntries(steps.map(s => [s.path, s]));
  for (const [path, depth, n] of order) {
    if (atRoot && !(path === atRoot || path.startsWith(atRoot + '/'))) continue;
    const s = known[path];
    const status = !s ? 'waiting' : s.finished_at ? 'done' : 'running';
    const items = s ? (s.outputs != null ? `${fmt(s.inputs)} → ${fmt(s.outputs)}` : fmt(s.inputs)) : '—';
    const failed = s && Object.keys(s.failed || {}).length ? Object.entries(s.failed).map(([k, v]) => `${v} ${plain(k)}`).join(', ') : '';
    const share = s && s.inputs ? (s.finished_at ? 1 : Math.min(1, (s.touched || 0) / s.inputs)) : 0;
    const fill = node('span', null, 'fill' + (status === 'running' ? ' live' : '')); fill.style.width = (100 * share) + '%';
    const sp = s?.spending, calls = sp ? sp.calls : null;
    body.append(el('tr', status, el('td', null, el('span', 'step-name', node('span', null, 'indent-' + Math.min(depth, 4)), node('span', n.name || '(pipeline)')), node('span', n.type, 'cell-sub')),
      el('td', null, node('span', status === 'running' ? 'Running' : status === 'done' ? 'Done' : 'Waiting', 'step-state ' + status)),
      el('td', 'numeric', items, failed ? node('span', failed, 'cell-sub attention') : null),
      el('td', null, s && !n.children?.length ? el('div', 'track small', fill) : null,
        status === 'running' && s.eta_seconds ? node('span', '~' + duration(s.eta_seconds) + ' left', 'cell-sub') : status === 'running' && s.touched ? node('span', `${fmt(s.touched)} touched`, 'cell-sub') : null),
      node('td', calls ? [calls.sent ? calls.sent + ' sent' : null, calls.reused ? calls.reused + ' reused' : null, calls.failed ? calls.failed + ' failed' : null, calls.would_send ? calls.would_send + ' would send' : null].filter(Boolean).join(' · ') : '', 'small'),
      node('td', sp && sp.spent_usd ? money(sp.spent_usd) : '', 'numeric'),
      node('td', sp && (sp.tokens.input || sp.tokens.output) ? `${fmt(sp.tokens.input)} / ${fmt(sp.tokens.output)}` : '', 'numeric small'),
      node('td', s ? duration(since(s.started_at, s.finished_at)) : '', 'numeric small')));
  }
  table.append(body);
  return el('div', 'table-wrap flat', table);
}

async function renderRun(seq, r) {
  const d = await api('run', {name: r.name}); if (seq !== state.seq) return;
  const rec = d.record, sp = d.spending, live = d.state === 'running';
  crumbs(link('Runs', '#/runs'), node('span', rec.name, 'crumb-current'));
  state.live = live ? () => renderRun(state.seq, r) : null;
  const l = rec.labels || {};
  const head = el('div', 'dataset-head', el('div', 'dataset-title', node('h1', rec.name), stateBadge(d.state)),
    rec.note ? node('p', rec.note, 'hypothesis-line') : null);
  const meta = el('dl', 'meta-row');
  const add = (label, value) => { if (value) meta.append(el('div', null, node('dt', label), el('dd', null, value))); };
  add('Pipeline', link(rec.pipeline + (d.tags.length ? ` (${d.tags.join(', ')})` : ''), '#/pipelines/' + rec.pipeline));
  add('Starts at', rec.at || 'the root');
  add('Inputs', Object.entries(rec.inputs || {}).length ? el('span', null, ...Object.entries(rec.inputs).map(([n, id]) => link(`${n} ${id}`, '#/data/' + id, 'pill status'))) : 'none');
  add('Cases', rec.keys ? `${fmt(rec.keys.length)} chosen` : 'every input');
  add('Experiment', l.experiment ? link(`${l.experiment} · ${l.round} round · ${l.side}`, `#/x/${encodeURIComponent('experiment:' + l.experiment + ':' + l.round)}`) : null);
  add('Mode', rec.options.mode === 'send' ? `sends new calls, capped at ${money(rec.options.limit_usd)}` : rec.options.mode === 'dry' ? 'dry run: estimates new calls' : 'cache only: sends nothing');
  add('Executions', rec.executions > 1 ? `${rec.executions} (continued)` : null);
  head.append(meta);
  const failed = rec.items?.failed || {};
  const why = [
    failed.budget ? `The spending cap stopped ${fmt(failed.budget)} items: each call reserves its worst-case cost before it is sent.` : null,
    failed.not_sent ? `${fmt(failed.not_sent)} items need calls this run did not send (${rec.options.mode === 'dry' ? 'a dry run' : 'cache only'}).` : null,
    failed.delivery ? `${fmt(failed.delivery)} items had a failed or uncertain delivery; --resend-failed resends each once.` : null].filter(Boolean);
  if (d.state === 'stopped' && why.length) head.append(el('p', 'context-note', node('strong', 'Stopped. '), why.join(' '),
    ' Running the same name again continues it: finished calls are reused for free.'));
  if (d.state === 'failed' || d.state === 'interrupted') head.append(el('p', 'message danger', node('strong', d.state === 'failed' ? 'The run failed. ' : 'The run stopped without finishing. '), rec.error || 'Its process ended while running. Running the same name again continues it.'));

  const elapsed = since(rec.execution_started_at || rec.started_at, live ? null : rec.finished_at);
  const tiles = el('div', 'tiles');
  const tile = (label, value, note) => tiles.append(el('div', 'tile', node('span', label), node('strong', value), note ? node('small', note) : null));
  const states = d.items || {};
  tile('Elapsed', duration(elapsed), live ? `updated ${new Date(d.updated_at || Date.now()).toLocaleTimeString()}` : rec.finished_at ? 'finished ' + new Date(rec.finished_at).toLocaleString() : null);
  tile('Items', fmt(d.total), live ? `${fmt(states.working || 0)} in progress · ${fmt(states.failing || 0)} failing` : rec.items ? `${fmt(Object.values(rec.items.failed || {}).reduce((a, b) => a + b, 0))} failed` : null);
  tile('New spending', money(sp.spent_usd), rec.options.mode === 'send' ? `cap ${money(rec.options.limit_usd)}` + (rec.executions > 1 ? ` · ${money(sp.since_usd)} this execution` : '') + (sp.outstanding_usd ? ` · ${money(sp.outstanding_usd)} reserved in flight` : '') : rec.options.mode === 'dry' ? `would spend up to ${money(sp.would_spend_usd)}` : 'nothing sent');
  tile('Reused', money(sp.reused_value_usd), `${fmt(sp.calls.reused)} saved calls, free`);
  tile('Calls sent', fmt(sp.calls.sent), sp.calls.failed ? `${fmt(sp.calls.failed)} failed deliveries` : 'none failed');
  tile('Tokens', fmt(sp.tokens.input + sp.tokens.output), `${fmt(sp.tokens.input)} in (${fmt(sp.tokens.cached)} cached) · ${fmt(sp.tokens.output)} out`);
  const blocks = [head, section(live ? 'Now' : 'Summary', null, tiles)];
  if (rec.options.mode === 'send') blocks.push(section('Spending over time', 'Each delivered call\'s charge, cumulative; the line across is the cap.', spendBar(sp.spent_usd, rec.options.limit_usd, 'send'), sparkline(sp.timeline, rec.options.limit_usd)));
  blocks.push(section('Steps', 'The pipeline\'s steps from where this run starts. Items and progress are this execution\'s; calls, spending and tokens cover every execution of the run. A step touches an item when it calls a model or notes something for it; a batching step touches several at once.', stepsTable(d)));
  if (Object.keys(d.datasets).length) {
    blocks.push(section('Saved outputs', live ? 'From the last finished execution; this one saves its own when it finishes.' : null, el('div', 'status-chips', ...Object.entries(d.datasets).map(([path, x]) => link(`${path || '(root)'} · ${x.kind} · ${x.id}`, '#/data/' + x.id, 'pill status')))));
  }
  const items = section('Items', 'Where each case is, or how it ended. Select one for its trace.');
  blocks.push(items);
  page(...blocks);
  renderItems(seq, r, items);
}

async function renderItems(seq, r, target) {
  const p = r.params;
  const d = await api('run/items', {name: r.name, q: p.q || '', state: p.state || '', offset: p.offset || 0, limit: 50}); if (seq !== state.seq) return;
  const go = changes => { location.hash = '#/runs/' + encodeURIComponent(r.name) + '?' + new URLSearchParams(Object.entries({...p, offset: '', ...changes}).filter(([, v]) => v)); };
  const toolbar = el('div', 'case-toolbar');
  const segmented = el('div', 'segmented');
  for (const [key, label] of [['', 'All'], ...Object.entries(ITEM_STATES)]) {
    const count = key ? d.states[key] || 0 : Object.values(d.states).reduce((a, b) => a + b, 0);
    if (key && !count) continue;
    const choice = button('', () => go({state: key}), (p.state || '') === key ? 'active' : '');
    choice.append(node('span', label), node('span', fmt(count), 'count'));
    segmented.append(choice);
  }
  const search = node('input'); search.type = 'search'; search.placeholder = 'Find an item…'; search.value = p.q || '';
  let timer; search.oninput = () => { clearTimeout(timer); timer = setTimeout(() => go({q: search.value}), 300); };
  toolbar.append(segmented, search);
  const table = el('table', 'cases', el('thead', null, el('tr', null, ...['Item', 'State', 'Last step', 'Detail'].map(t => node('th', t)))));
  const body = node('tbody');
  for (const row of d.rows) {
    const open = button(row.key, () => showTrace(r.name, row.key, d.output), 'text-button mono');
    body.append(el('tr', null, el('td', null, open), el('td', null, node('span', ITEM_STATES[row.state] || plain(row.state), 'step-state ' + row.state)),
      node('td', row.step || '—', 'mono small'), node('td', [row.kind ? plain(row.kind) : null, row.reason, row.event && row.state !== 'done' ? plain(row.event) : null].filter(Boolean).join(' · '), 'small')));
  }
  if (!d.rows.length) body.append(el('tr', null, el('td', 'empty', d.total ? 'No items match.' : 'No items yet: the run has not noted any.')));
  table.append(body);
  const trace = el('div', 'trace-panel'); trace.id = 'trace-panel';
  const pager = d.total > d.limit ? el('div', 'pagination', node('span', `${d.offset + 1}–${Math.min(d.offset + d.limit, d.total)} of ${fmt(d.total)}`),
    el('div', null, button('← Previous', () => go({offset: Math.max(0, d.offset - d.limit)})), button('Next →', () => go({offset: d.offset + d.limit})))) : null;
  target.append(toolbar, el('div', 'table-wrap flat', table), pager, trace);
}

async function showTrace(name, key, output) {
  const panel = $('trace-panel');
  const d = await api('run/trace', {name, key});
  const table = el('table', 'executions', el('thead', null, el('tr', null, ...['Step', 'Event', 'Detail'].map(t => node('th', t)))));
  const body = node('tbody');
  for (const e of d.events.filter(Boolean)) body.append(el('tr', null, node('td', e.step, 'mono'), node('td', plain(e.event)),
    node('td', [e.kind ? plain(e.kind) : null, e.call ? e.call.slice(0, 12) : null, e.batch ? 'batch: ' + e.batch.join(', ') : null, e.text, e.reason, e.rejected ? 'rejected: ' + e.rejected : null,
      e.checked != null ? `${e.checked} quotations checked` : null].filter(Boolean).join(' · '))));
  table.append(body);
  panel.replaceChildren(section(`Trace · ${key}`, d.final ? 'Every event the run noted for this item.' : 'Live: the latest event only; the full trace is saved when the run finishes.',
    el('div', 'table-wrap flat', table), output ? link('Open its output →', `#/data/${output}/${key}`, 'more-link') : null));
  panel.scrollIntoView({block: 'nearest'});
}
