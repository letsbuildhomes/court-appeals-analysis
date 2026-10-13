'use strict';

// The reference set: hand-checked answers that pipelines are scored against.
// Routes: #/reference[?scope=&layer=&q=]  ·  #/reference/CASE
// The table is for scanning what each label says; a case page puts its answers beside the text they
// cite. Only what needs a person (open questions) is called out; low-confidence answers get a dot.
const SCOPE = {in_scope: 'In scope', uncertain: 'Uncertain', excluded: 'Excluded'};
const WORDS = {
  approved: 'Approved', approved_with_conditions: 'Approved with conditions', denied: 'Denied', other: 'Other',
  allowed: 'Allowed', allowed_with_conditions: 'Allowed with conditions',
  de_novo: 'De novo', on_the_record: 'On the record', resolved: 'Resolved', ongoing: 'Ongoing',
  represented: 'Lawyer', pro_se: 'Pro se', mixed: 'Mixed',
  overturn_approval: 'Overturn approval', overturn_denial: 'Overturn denial', change_conditions: 'Change conditions',
  developer: 'developer', neighbor: 'neighbor', organization: 'organization',
  filing: 'Filed', judgment: 'Judgment', further_appeal: 'Further appeal', reopening: 'Reopened', remand: 'Remand',
  resolution: 'Resolved', yes: 'Yes', no: 'No', not_applicable: 'Not applicable'};
const word = v => WORDS[v] ?? plain(v);
const isKnown = v => v != null && v !== 'unknown' && v !== '';
const SINGULAR = {neighbor: 'neighbor', developer: 'developer', organization: 'organization', other: 'other', unknown: 'role not stated'};
const PLURAL = {neighbor: 'neighbors', developer: 'developers', organization: 'organizations', other: 'others', unknown: 'roles not stated'};

function refQuery(params) {
  const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v)).toString();
  return '#/reference' + (query ? '?' + query : '');
}
function months(days) {
  if (days < 60) return `${days} d`;
  if (days < 730) return `${Math.round(days / 30.4)} mo`;
  return `${(days / 365.25).toFixed(1)} yr`;
}
function appellantSummary(r) {
  if (!r.appellants) return null;
  return Object.entries(r.appellant_kinds).sort((a, b) => b[1] - a[1])
    .map(([kind, n]) => `${n} ${n === 1 ? SINGULAR[kind] || kind : PLURAL[kind] || kind}`).join(', ');
}
// A cell's text, or a dash for what the documents don't establish.
function valueCell(value, cls = '', title = '') {
  const td = node('td', isKnown(value) ? value : '—', cls + (isKnown(value) ? '' : ' none'));
  if (title) td.title = title;
  return td;
}
function flagDot(flag) {
  if (!flag || !['low', 'disputed'].includes(flag.confidence)) return null;
  const dot = node('span', null, 'ref-dot ' + flag.confidence);
  dot.title = (flag.confidence === 'disputed' ? 'Disputed: ' : 'Low confidence: ') + flag.note;
  return dot;
}

// ---------------------------------------------------------------- the table
async function renderReference(seq, r) {
  crumbs(node('span', 'Reference', 'crumb-current'));
  const d = await api('reference'); if (seq !== state.seq) return;
  const p = r.params, go = changes => { location.hash = refQuery({...p, ...changes}); };
  const all = d.cases;
  const count = scope => all.filter(c => c.disposition === scope).length;
  const approved = all.filter(c => c.status === 'approved').length;

  const head = el('div', 'ref-head',
    el('div', null, node('h1', 'Reference set'),
      node('p', `${all.length} cases labeled by hand. Pipelines are scored against these answers, so each one should be right. Scan what was extracted and open any case to check it against its text.`, 'lede')),
    el('div', 'ref-progress', node('span', 'Approved', 'eyebrow'), node('strong', `${approved} of ${all.length}`),
      el('div', 'ref-meter', el('span', null)), node('small', approved ? '' : 'All 60 are labeled and verified; none approved yet.')));
  head.querySelector('.ref-meter span').style.width = `${100 * approved / all.length}%`;

  const blocks = [head];
  const asked = all.filter(c => c.questions.length);
  if (asked.length) {
    const list = el('ol', 'ref-questions');
    for (const c of asked) for (const q of c.questions)
      list.append(el('li', null, el('div', null, link(c.docket, '#/reference/' + encodeURIComponent(c.case), 'ref-docket'),
        node('span', 'Labeled ' + SCOPE[c.disposition].toLowerCase(), 'ref-badge ' + c.disposition)), node('p', q)));
    blocks.push(el('section', 'ref-ask', el('div', 'ref-ask-head', node('h2', `Questions for you`), node('span', String(list.children.length), 'ref-count')),
      node('p', 'The labelers could not settle these from the guide. Each is a definition call; your answer applies to every similar case.', 'muted small'), list));
  }

  const scopes = el('div', 'segmented'); scopes.setAttribute('role', 'group'); scopes.setAttribute('aria-label', 'Disposition');
  for (const [key, label, n] of [['', 'All', all.length], ...Object.entries(SCOPE).map(([k, l]) => [k, l, count(k)]).filter(([, , n]) => n)]) {
    const choice = button('', () => go({scope: key}), (p.scope || '') === key ? 'active' : '');
    choice.append(key ? node('span', null, 'ref-swatch ' + key) : '', node('span', label), node('span', n, 'count'));
    choice.setAttribute('aria-pressed', String((p.scope || '') === key)); scopes.append(choice);
  }
  const search = node('input'); search.type = 'search'; search.placeholder = 'Find a docket or town…'; search.value = p.q || '';
  search.setAttribute('aria-label', 'Find a docket or town');
  let timer; search.oninput = () => { clearTimeout(timer); timer = setTimeout(() => go({q: search.value}), 250); };
  const layer = selectControl('Layer', 'layer', [['', 'Both layers'], ['random', 'Random'], ['targeted', 'Targeted']], p.layer || '', v => go({layer: v}));
  const legend = el('div', 'ref-legend', el('span', null, node('span', '—', 'none'), ' not in the documents'),
    el('span', null, node('span', null, 'ref-dot low'), ' low confidence'));
  const toolbar = el('div', 'ref-toolbar', scopes, layer, search);

  const q = (p.q || '').toLowerCase();
  const shown = all.filter(c => (!p.scope || c.disposition === p.scope) && (!p.layer || c.layer === p.layer) &&
    (!q || `${c.docket} ${c.record?.municipality || ''} ${c.case}`.toLowerCase().includes(q)));
  blocks.push(toolbar, el('div', 'ref-table-head', node('span', `${shown.length} ${shown.length === 1 ? 'case' : 'cases'} · in scope first, then by how much is known`), legend),
    shown.length ? refTable(shown) : el('div', 'empty', 'No cases match. Clear a filter or try another search.'));
  page(...blocks);
  enhanceSelects(toolbar);
  if (p.q) { search.focus(); search.setSelectionRange(search.value.length, search.value.length); }
}

const COLUMNS = [
  // [group, heading, class, cell(row, record), fact keys whose flags mark it]
  ['Project', 'Units', 'num', r => r.units, ['units']],
  ['Project', 'Lots', 'num', r => r.lots, ['lots']],
  ['Municipal decision', 'Outcome', 'wrap w-outcome', r => isKnown(r.decision) ? word(r.decision) : null, ['decision_outcome']],
  ['Municipal decision', 'Date', 'date', r => r.decision_date, ['decision_date']],
  ['The appeal', 'Appellants', 'wrap w-appellants', appellantSummary, ['appellants']],
  ['The appeal', 'Counsel', '', r => isKnown(r.representation) ? word(r.representation) : null, ['representation']],
  ['The appeal', 'Seeks', 'wrap w-seeks', r => r.seeks.length ? r.seeks.map(word).join('; ') : null, []],
  ['The appeal', 'Review', '', r => isKnown(r.review) ? word(r.review) : null, ['review_standard']],
  ['The appeal', 'Discretion', '', r => r.discretion === 'yes' ? (r.discretion_central === 'yes' ? 'Yes, central' : 'Yes') : isKnown(r.discretion) ? word(r.discretion) : null,
    ['discretion_involved', 'discretion_central']],
  ['Duration', 'Filed', 'date', r => r.start ? (r.start.relation === 'on_or_before' ? '≤ ' : '') + r.start.date : null, ['start']],
  ['Duration', 'Ended', 'date', r => r.end ? (r.end.relation === 'on_or_after' ? '≥ ' : '') + r.end.date : null, ['end']],
  ['Duration', 'Length', 'num', r => typeof r.days === 'number' ? (r.duration_kind === 'at_least' ? '≥ ' : '') + months(r.days) : null, ['duration']],
  ['Status', 'Litigation', '', r => isKnown(r.litigation) ? word(r.litigation) : null, ['litigation']],
  ['Status', 'Permission', 'wrap w-permission', r => isKnown(r.permission) ? word(r.permission) : null, ['permission']],
  ['Also found', 'Restrictions', 'num', r => r.restrictions, ['restrictions']],
  ['Also found', 'Housing changes', 'num', r => r.housing_changes, ['housing_changes']],
  ['Also found', 'Related dockets', 'num', r => r.related, ['related']],
];

function refTable(rows) {
  const groups = [];
  for (const [group] of COLUMNS) if (groups.at(-1)?.[0] === group) groups.at(-1)[1]++; else groups.push([group, 1]);
  const top = el('tr', 'ref-groups', node('th', '', 'ref-sticky'), node('th', ''), ...groups.map(([g, n]) => { const th = node('th', g); th.colSpan = n; return th; }));
  const second = el('tr', null, node('th', 'Case', 'ref-sticky'), node('th', 'Known', 'num'), ...COLUMNS.map(([, h, cls]) => node('th', h, cls)));
  const table = el('table', 'ref-table', el('thead', null, top, second));
  const body = node('tbody');
  let current = null;
  for (const c of rows) {
    if (c.disposition !== current) {
      current = c.disposition;
      const n = rows.filter(x => x.disposition === current).length;
      const th = el('th', 'ref-sticky', node('span', null, 'ref-swatch ' + current), `${SCOPE[current]} · ${n}`);
      const rest = node('td', ''); rest.colSpan = COLUMNS.length + 1;
      body.append(el('tr', 'ref-section ' + current, th, rest));
    }
    body.append(refRow(c));
  }
  table.append(body);
  return el('div', 'ref-scroll', table);
}

function refRow(c) {
  const to = '#/reference/' + encodeURIComponent(c.case);
  const scopeFlag = c.flags.housing || c.flags.disposition || c.flags.municipal_appeal;
  const caseCell = el('th', 'ref-sticky', el('div', 'ref-case', link(c.docket || c.case, to, 'ref-docket'), flagDot(scopeFlag),
    c.questions.length ? node('span', '?', 'ref-asked') : null),
    node('span', c.record?.municipality && c.record.municipality !== 'unknown' ? c.record.municipality : '', 'ref-town'));
  if (c.questions.length) caseCell.querySelector('.ref-asked').title = 'Open question: ' + c.questions.join(' ');
  const tr = el('tr', 'ref-row ' + c.disposition, caseCell);
  tr.onclick = event => { if (!event.target.closest('a')) location.hash = to; };
  if (!c.record) {
    const reason = el('td', 'ref-reason', el('div', null, node('span', c.municipal_appeal === 'no' ? 'Not a municipal appeal. ' : c.housing === 'no' ? 'Not new housing. ' : '', 'ref-why'), c.reason));
    reason.colSpan = COLUMNS.length + 1;
    tr.append(reason);
    return tr;
  }
  const meter = el('td', 'num ref-known', el('div', null, el('div', 'ref-meter small', el('span', null)), node('span', `${c.known}/${c.slots}`)));
  meter.querySelector('.ref-meter span').style.width = `${100 * c.known / c.slots}%`;
  tr.append(meter);
  for (const [, heading, cls, cell, keys] of COLUMNS) {
    const value = cell(c.record);
    const td = valueCell(value, cls + (value === 0 && cls === 'num' && heading !== 'Units' && heading !== 'Lots' ? ' zero' : ''));
    const flag = keys.map(k => c.flags[k]).find(f => f && ['low', 'disputed'].includes(f.confidence));
    if (flag) td.append(flagDot(flag));
    tr.append(td);
  }
  return tr;
}

// ---------------------------------------------------------------- one case
async function renderReferenceCase(seq, r) {
  const d = await api('reference/case', {id: r.id}); if (seq !== state.seq) return;
  const c = d.row, label = d.label, result = label.result, record = result.records[0], m = d.manifest;
  crumbs(link('Reference', '#/reference'), node('span', c.docket, 'crumb-current'));
  const quotes = new Quotes();

  const go = id => id ? '#/reference/' + encodeURIComponent(id) : null;
  const stepper = el('div', 'ref-stepper', node('span', `${d.position} of ${d.total}`, 'muted small'),
    d.previous ? link('‹', go(d.previous), 'ref-step') : node('span', '‹', 'ref-step disabled'),
    d.next ? link('›', go(d.next), 'ref-step') : node('span', '›', 'ref-step disabled'));
  stepper.querySelectorAll('a.ref-step').forEach((a, i) => { a.title = (a.textContent === '‹' ? 'Previous' : 'Next') + ' case (← →)'; a.dataset.step = a.textContent === '‹' ? 'previous' : 'next'; });
  const town = record?.project.municipality;
  const head = el('header', 'ref-case-head',
    el('div', null, el('div', 'ref-title', node('h1', c.docket), node('span', SCOPE[c.disposition], 'ref-badge ' + c.disposition)),
      node('p', [isKnown(town) ? town : null, `${m.hints.documents} document${m.hints.documents === 1 ? '' : 's'}`,
        `${Math.round(m.hints.chars / 1000)}k characters`, `${m.layer} layer`, m.split === 'holdout' ? 'held back' : null].filter(Boolean).join(' · '), 'muted')),
    stepper);

  const left = el('div', 'ref-facts');
  for (const q of label.questions) left.append(el('div', 'ref-callout', node('strong', 'Question for you'), node('p', q)));
  left.append(scopeCard(result.disposition, c.flags, quotes));
  if (record) left.append(...recordCards(record, c.flags, quotes, label.disputed));
  left.append(madeCard(d, label, m));

  const text = el('div', 'ref-text', el('div', 'ref-text-head', node('strong', 'Case text'), node('span', 'Loading…', 'muted small')), el('div', 'ref-text-body'));
  page(head, el('div', 'ref-split', left, text));
  quotes.text = text;
  try {
    const source = await api('reference/source', {id: d.row.case}); if (seq !== state.seq) return;
    renderText(text, source, quotes, c.docket);
  } catch (err) { text.querySelector('.ref-text-head span').textContent = 'Text unavailable: ' + err.message; }
}

// Every quotation on the page, so the text can mark them and a click can find one.
class Quotes {
  constructor() { this.all = []; this.text = null; }
  add(evidence) {
    const id = this.all.length; this.all.push(evidence); return id;
  }
  list(evidence, limit = 2) {
    const items = (evidence || []).filter(e => e && e.quote);
    if (!items.length) return null;
    const box = el('div', 'ref-quotes');
    const make = e => {
      const id = this.add(e);
      const b = el('button', 'ref-quote', node('span', '“' + e.quote.trim() + '”', 'ref-quote-text'), node('span', `doc ${e.doc} · p. ${e.page}`, 'ref-cite'));
      b.type = 'button'; b.title = 'Show in the case text'; b.onclick = () => this.show(id, b);
      return b;
    };
    items.slice(0, limit).forEach(e => box.append(make(e)));
    if (items.length > limit) {
      const more = button(`${items.length - limit} more ${items.length - limit === 1 ? 'quotation' : 'quotations'}`, () => {
        more.replaceWith(...items.slice(limit).map(make));
      }, 'text-button ref-more');
      box.append(more);
    }
    return box;
  }
  show(id, from) {
    document.querySelectorAll('.ref-quote.active').forEach(x => x.classList.remove('active'));
    from?.classList.add('active');
    const body = this.text?.querySelector('.ref-text-body'); if (!body) return;
    body.querySelectorAll('mark.active').forEach(x => x.classList.remove('active'));
    const marks = [...body.querySelectorAll('mark')].filter(x => x.dataset.ids.split(' ').includes(String(id)));
    if (marks.length) {
      marks.forEach(x => x.classList.add('active'));
      body.scrollTo({top: marks[0].offsetTop - body.clientHeight / 3, behavior: 'smooth'});
      return;
    }
    const e = this.all[id];
    const pageEl = body.querySelector(`[data-doc="${e.doc}"][data-page="${e.page}"]`);
    if (pageEl) { pageEl.classList.add('flash'); setTimeout(() => pageEl.classList.remove('flash'), 1400); body.scrollTo({top: pageEl.offsetTop - 12, behavior: 'smooth'}); }
  }
}

// A judgment tag and its note: low confidence and disputes show the note; a medium call opens it on click.
function judgment(flag) {
  if (!flag) return [null, null];
  const note = node('p', flag.note, 'ref-note ' + flag.confidence);
  note.hidden = flag.confidence === 'medium';
  const tag = button(flag.confidence === 'disputed' ? 'Disputed' : flag.confidence === 'low' ? 'Low confidence' : 'Judgment call',
    () => { note.hidden = !note.hidden; }, 'ref-judgment ' + flag.confidence);
  tag.title = 'Why this took judgment';
  return [tag, note];
}

// One labeled answer: what it is, the answer, a judgment note when it took one, and its quotations.
function factRow(name, value, opts = {}) {
  const [tag, note] = judgment(opts.flag);
  const answer = el('div', 'ref-answer', isKnown(value) ? (value instanceof Node ? value : node('span', value)) : node('span', 'Not in the documents', 'none'), tag);
  return el('div', 'ref-fact', node('div', name, 'ref-label'),
    el('div', null, answer, note, opts.detail ? node('p', opts.detail, 'ref-detail') : null, opts.quotes));
}
function card(title, ...children) { return el('section', 'ref-card', node('h2', title), ...children.flat().filter(Boolean)); }
// A card whose heading carries a judgment, for an answer that is the whole card (such as who appealed).
function judgedCard(title, flag, ...children) {
  const [tag, note] = judgment(flag);
  return el('section', 'ref-card', el('div', 'ref-card-head', node('h2', title), tag), note, ...children.flat().filter(Boolean));
}
function flagFor(flags, disputed, ...keys) {
  for (const k of keys) if ((disputed || []).includes(k)) return {confidence: 'disputed', note: 'Left unresolved after verification; not scored.'};
  return keys.map(k => flags[k]).find(Boolean);
}

function scopeCard(disposition, flags, quotes) {
  const check = (name, v) => el('span', 'ref-check ' + v, node('span', v === 'yes' ? '✓' : v === 'no' ? '✕' : '?', 'ref-check-mark'), name);
  const [tag, note] = judgment(flagFor(flags, [], 'disposition', 'housing', 'municipal_appeal'));
  return el('section', 'ref-card ref-scope ' + disposition.value,
    node('h2', 'Scope'),
    el('div', 'ref-scope-line', check('Municipal appeal', disposition.municipal_appeal), check('New housing', disposition.housing), tag),
    note, node('p', disposition.reason, 'ref-reason-text'),
    quotes.list(disposition.evidence, 3));
}

function recordCards(r, flags, quotes, disputed) {
  const f = (...keys) => flagFor(flags, disputed, ...keys);
  const p = r.project, md = r.municipal_decision, st = r.observed_status, dis = r.discretion, du = r.duration;
  const num = v => isKnown(v) ? String(v) : null;
  const cards = [];
  cards.push(card('Project',
    node('p', isKnown(p.description) ? p.description : 'Not described.', 'ref-lead'),
    el('div', 'ref-grid', factRow('Municipality', p.municipality, {flag: f('municipality')}),
      factRow('Units', num(p.proposed_units), {flag: f('units')}), factRow('Lots', num(p.proposed_lots), {flag: f('lots')})),
    quotes.list(p.evidence)));
  cards.push(card('Municipal decision',
    el('div', 'ref-grid', factRow('Outcome', isKnown(md.outcome) ? word(md.outcome) : null, {flag: f('decision_outcome')}),
      factRow('Date', isKnown(md.date) ? md.date : null, {flag: f('decision_date')})),
    isKnown(md.description) ? node('p', md.description, 'ref-detail') : null, quotes.list(md.evidence)));

  const people = el('div', 'ref-people');
  for (const a of r.appellants) people.append(el('div', 'ref-person',
    el('div', 'ref-person-line', node('strong', isKnown(a.name) ? a.name : 'Unnamed'),
      node('span', [a.relationship, a.seeks, a.representation].map(v => isKnown(v) ? word(v) : null).filter(Boolean).join(' · '), 'muted')),
    quotes.list(a.evidence, 1)));
  const appealFlag = f('appellants', 'representation');
  cards.push(judgedCard(`Appellants${r.appellants.length ? ' · ' + r.appellants.length : ''}`, appealFlag,
    r.appellants.length ? people : node('p', 'Not named in the documents (or an enforcement action, which has none).', 'none')));

  const review = r.appeal_review || {standard: 'unknown', evidence: []};
  cards.push(card('The appeal',
    factRow('Review', isKnown(review.standard) ? word(review.standard) : null, {flag: f('review_standard'), quotes: quotes.list(review.evidence, 1)}),
    factRow('Discretion', isKnown(dis.involved) ? word(dis.involved) + (dis.involved === 'yes' && isKnown(dis.central) ? (dis.central === 'yes' ? ', and central' : dis.central === 'no' ? ', not central' : '') : '') : null,
      {flag: f('discretion_involved', 'discretion_central'), detail: isKnown(dis.description) ? dis.description : null,
       quotes: quotes.list([...(dis.evidence || []), ...(dis.central_evidence || [])])})));

  const list = (items, describe) => items.length ? el('ul', 'ref-list', ...items.map(x => el('li', null, node('span', describe(x)), quotes.list(x.evidence, 1)))) : node('span', 'None found', 'none');
  cards.push(card('Outcome',
    el('div', 'ref-grid', factRow('Litigation', isKnown(st.litigation) ? word(st.litigation) : null, {flag: f('litigation')}),
      factRow('Permission', isKnown(st.permission) ? word(st.permission) : null, {flag: f('permission')})),
    isKnown(st.description) ? node('p', st.description, 'ref-detail') : null, quotes.list(st.evidence),
    factRow('Restrictions', list(r.restrictions, x => x.description), {flag: f('restrictions')}),
    factRow('Housing changes', list(r.housing_changes, x => x.description), {flag: f('housing_changes')})));

  const bound = (b, before) => b === 'unknown' ? null : `${before && b.relation !== 'exact' ? (b.relation === 'on_or_before' ? 'On or before ' : 'On or after ') : ''}${b.date}`;
  const length = typeof du.days === 'number' ? `${du.kind === 'at_least' ? 'At least ' : ''}${fmt(du.days)} days (${months(du.days)})` : null;
  const timeline = el('ol', 'ref-timeline', ...r.timeline.map(e => el('li', 'ref-event ' + e.kind,
    node('span', isKnown(e.date) ? e.date : 'undated', 'ref-event-date'),
    el('div', null, el('div', null, node('strong', word(e.kind)), node('span', ' · ' + e.court_or_body, 'muted small')),
      node('p', e.description), quotes.list(e.evidence, 1)))));
  cards.push(card('Duration',
    el('div', 'ref-grid', factRow('Filed', bound(du.start, true), {flag: f('start')}), factRow('Ended', bound(du.end, true), {flag: f('end')}),
      factRow('Length', length, {flag: f('duration')})),
    isKnown(du.note) ? node('p', du.note, 'ref-detail') : null,
    node('h3', 'Timeline'), timeline));

  if (r.related_dockets.length) cards.push(card('Related dockets', el('ul', 'ref-list', ...r.related_dockets.map(x =>
    el('li', null, node('strong', x.docket + ' '), node('span', x.relationship), quotes.list(x.evidence, 1))))));
  return cards;
}

function madeCard(d, label, m) {
  const order = {low: 0, medium: 1, high: 2};
  const judgments = el('ul', 'ref-list', ...[...label.judgments].sort((a, b) => order[a.confidence] - order[b.confidence])
    .map(j => el('li', null, node('span', plain(j.confidence), 'ref-conf ' + j.confidence), node('strong', plain(j.field) + ' '), node('span', j.note))));
  const blocks = [node('p', `Chosen for the ${m.layer} layer (${plain(m.category)}): ${m.reason}.`, 'ref-detail'),
    node('h3', 'Judgment calls'), node('p', 'The labeler marks how sure it is of each answer that needed interpretation. Low confidence is worth a check; medium is usually a reasonable reading.', 'muted small'), judgments];
  if (d.disagreements.length) blocks.push(node('h3', 'Second reading'), node('p', 'A blind second reader disagreed on these; each was settled by rereading the source, and the label shows the result.', 'muted small'),
    el('ul', 'ref-list', ...d.disagreements.map(([field, mine, theirs, note]) => el('li', null, node('strong', plain(field) + ': '), `label ${mine}, second reader ${theirs}. `, node('span', note || '', 'muted')))));
  const history = [...(m.history || []), ...label.history].sort((a, b) => String(a.at).localeCompare(String(b.at)));
  blocks.push(node('h3', 'History'), el('ul', 'ref-list', ...history.map(h => el('li', null, node('strong', plain(h.step) + ' '), node('span', `${h.by}${h.note ? ' — ' + h.note : ''}`, 'muted')))));
  if (d.problems.length) blocks.push(el('div', 'message danger', node('strong', 'Check problems'), el('ul', null, ...d.problems.map(x => node('li', x)))));
  blocks.push(el('details', 'technical', node('summary', 'Label file'), node('pre', pretty(label))));
  return el('details', 'ref-card ref-made', node('summary', 'How this label was made'), ...blocks);
}

// The case text, one block per page, with every quotation on the page marked where it's found.
function renderText(container, source, quotes, currentDocket) {
  const body = container.querySelector('.ref-text-body');
  const text = source.text;
  const documents = Object.fromEntries((source.documents || []).map(x => [String(x.doc), x]));
  const byPage = {};
  quotes.all.forEach((e, id) => (byPage[`${e.doc}/${e.page}`] ||= []).push(id));
  const docPattern = /<document doc="(\d+)"[^>]*>|<page number="(\d+)"[^>]*>([\s\S]*?)<\/page>/g;
  let doc = null, pages = 0, found = 0;
  const fragment = document.createDocumentFragment();
  for (const match of text.matchAll(docPattern)) {
    if (match[1]) {
      doc = match[1];
      const info = documents[doc] || {};
      const shared = (info.dockets || []).length > 1 ? ` · also decides ${info.dockets.filter(x => x !== currentDocket).join(', ')}` : '';
      fragment.append(el('div', 'ref-doc', node('span', `Document ${doc}${info.posted ? ' · posted ' + info.posted : ''}`, 'eyebrow'),
        node('strong', info.title || 'Untitled'), shared ? node('span', shared.slice(3), 'muted small') : null));
      continue;
    }
    pages++;
    const number = match[2], content = match[3].replace(/^\n/, '');
    const block = el('div', 'ref-page'); block.dataset.doc = doc; block.dataset.page = number;
    block.append(node('div', `Page ${number}`, 'ref-page-no'));
    const pre = node('div', null, 'ref-page-text');
    const ranges = [];
    for (const id of byPage[`${doc}/${number}`] || []) {
      const at = findQuote(content, quotes.all[id].quote);
      if (at) ranges.push([...at, id]);
    }
    ranges.sort((a, b) => a[0] - b[0]);
    const merged = [];
    for (const [start, end, id] of ranges) {
      const last = merged.at(-1);
      if (last && start <= last[1]) { last[1] = Math.max(last[1], end); last[2].push(id); } else merged.push([start, end, [id]]);
    }
    let cursor = 0;
    for (const [start, end, ids] of merged) {
      pre.append(document.createTextNode(content.slice(cursor, start)));
      const mark = node('mark', content.slice(start, end)); mark.dataset.ids = ids.join(' ');
      pre.append(mark); cursor = end; found += ids.length;
    }
    pre.append(document.createTextNode(content.slice(cursor)));
    block.append(pre);
    fragment.append(block);
  }
  body.replaceChildren(fragment);
  container.querySelector('.ref-text-head span').textContent =
    `${(source.documents || []).length} document${(source.documents || []).length === 1 ? '' : 's'} · ${pages} pages · cited passages highlighted`;
  container.querySelector('.ref-text-head span').title = `${found} of ${quotes.all.length} quotations found · SHA-256 ${source.sha256}`;
}

// ← and → step through the cases in the table's order.
document.addEventListener('keydown', event => {
  const r = currentRoute();
  if (r.section !== 'reference' || !r.id || event.metaKey || event.ctrlKey || event.altKey) return;
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName) || !$('source').hidden) return;
  const step = {ArrowLeft: 'previous', ArrowRight: 'next'}[event.key];
  const target = step && document.querySelector(`.ref-step[data-step="${step}"]`);
  if (target) { event.preventDefault(); location.hash = target.getAttribute('href'); }
});
