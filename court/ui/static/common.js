'use strict';

// Shared helpers for every view: elements, formatting, the API and the source drawer.
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


async function api(path, params = {}) {
  params = Object.fromEntries(Object.entries(params).filter(([, v]) => v !== undefined && v !== null));
  const query = Object.keys(params).length ? '?' + new URLSearchParams(params) : '';
  const response = await fetch('api/' + path + query);
  if (response.status === 401) { location.reload(); throw Error('Sign in required'); }
  const value = await response.json();
  if (!response.ok || value.error) throw Error(value.error || 'Unable to load this view.');
  return value;
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

const state = {seq: 0, overviews: {}, source: 0, returnFocus: null, last: null};

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
// Show a case text in the drawer with quotations highlighted; ``load`` returns {text, sha256, verified}.
async function openSource(load, quotes, title, id) {
  const seq = ++state.source; state.returnFocus = document.activeElement;
  $('source').hidden = false; $('source-scrim').hidden = false; $('source-title').textContent = title;
  $('source-status').className = 'muted'; $('source-status').textContent = 'Verifying the case text…'; $('source-text').replaceChildren(); $('close-source').focus();
  try {
    const d = await load(); if (seq !== state.source) return;
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
    $('source-status').textContent = `Verified case text · ${id}` + (quotes?.length ? ` · ${marks.length} of ${quotes.length} quotations highlighted` : '') +
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

