/* The AI Agent Profiler report: one renderer for the web view and for `profiler-client open`.
 *
 * ProfilerReport.render(root, data, { name, meta, declareReadOnly }) draws a report JSON into `root` (an element with
 * class "sheet"); `meta` is a list of short lines for under the title. It reads nothing but `data`,
 * makes no request, and writes only inside `root`. The host page adds its own buttons to `.tools`.
 *
 * A dataset overview (`data.overview` with `data.reports`, one report per workflow) draws a list of the
 * workflows first; a row opens that workflow's report from `data.reports` and a back button returns.
 * The host's buttons in `.tools`, and anything it appended to `root`, move along. render returns { show(workflow), overview() } to switch by code.
 *
 * Themes: static/theme.css holds one set of variables per theme, picked by data-theme on <html>; static/theme.js sets it
 * and makes the toggle. This file stores nothing.
 */
(function () {
'use strict';
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const money = v => v == null ? '—' : v === 0 ? '$0' : `$${v.toFixed(v < 1 ? 3 : 2)}`;
// a step's or an agent's cost: a trace gives tool calls no cost, so none recorded is a dash, not $0
const spend = v => v ? money(v) : '—';
const count = n => Number(n || 0).toLocaleString();
const plural = (n, word) => `${count(n)} ${word}${n === 1 ? '' : 's'}`;
// every percent on the page: part of whole, rounded, with anything above 0 and under 1% as "<1%"
const share = (part, whole) => { if (!whole) return ''; const p = 100 * part / whole; return p > 0 && p < 1 ? '<1%' : `${Math.round(p)}%`; };

const KIND = {
  CACHE_RESULT: 'Same call, same input, same result',
  EXTRACT_DETERMINISTIC_SUBSTEP: 'Same steps in the same order',
  CONDITIONAL_LLM_BYPASS: 'An LLM choice that never changes',
  PARTIAL_LLM_BYPASS: 'An LLM choice that mostly stays the same',
  REVIEW_HIDDEN_STATE: 'Same request, different results',
  CROSS_AGENT_DUPLICATE: 'Same call by more than one agent in a run',
};
// "Do these first": a finding's plain title, and what the next step is called
const TITLE = {
  CACHE_RESULT: 'Reuse a repeated result',
  CONDITIONAL_LLM_BYPASS: 'Replace an LLM decision with a rule',
  PARTIAL_LLM_BYPASS: 'Needs review: a rule for most calls',
  EXTRACT_DETERMINISTIC_SUBSTEP: 'Script a fixed sequence',
  DETERMINISTIC_REGION: 'Skip the LLM for one slice of inputs',
  REVIEW_HIDDEN_STATE: 'Unstable result: find the cause',
  CROSS_AGENT_DUPLICATE: 'Fetch once, share between agents',
};
const STEP = { replay: 'Replay', review: 'Review', declare: 'Declare', investigate: 'Investigate' };
const EVIDENCE = {
  CACHE_RESULT: n => `${plural(n, 'call')} had the same input and gave the same result.`,
  CONDITIONAL_LLM_BYPASS: n => `The LLM made the same choice in all ${plural(n, 'call')}.`,
  // a choice the model makes most of the time: a rule handles those calls, the model the rest
  PARTIAL_LLM_BYPASS: (n, o) => `A rule handles ${o.consistency == null ? 'most' : share(o.consistency, 1)} of ${plural(n, 'call')}, the rest falls back to the model.` +
    (o.covered_cost ? ` The calls the rule handles cost ${money(o.covered_cost)}.` : ''),
  EXTRACT_DETERMINISTIC_SUBSTEP: n => `These steps ran in this order ${plural(n, 'time')}.`,
  DETERMINISTIC_REGION: n => `${plural(n, 'call')} fall in this slice of inputs.`,
  REVIEW_HIDDEN_STATE: n => `Across ${plural(n, 'call')}, the same request gave different results.`,
  CROSS_AGENT_DUPLICATE: n => `In ${plural(n, 'run')}, more than one agent made this call with the same input and got the same result.`,
};
// a decision that follows from its input: one lookup, from each input it has seen 10+ times to its choice
const lookupInputs = o => o.type === 'CONDITIONAL_LLM_BYPASS' ? (o.by_input ? o.by_input.filter(b => b.confirmed).length : o.lookup_entries || 0) : 0;
const title = o => lookupInputs(o) ? 'Replace an LLM decision with a lookup' : TITLE[o.type] || o.type;
const evidenceOf = o => lookupInputs(o)
  ? `${plural(lookupInputs(o), 'different input')}, each always given the same choice, across ${plural(o.observations, 'call')}.`
  : (EVIDENCE[o.type] || (k => plural(k, 'call') + ' seen.'))(o.observations, o);
// what the map marks on a step a finding is about: each has its own words, never colour alone
const MARK = {
  unstable: { badge: 'unstable result', legend: 'the same request gave different results' },
  replace: { badge: 'rule or script', legend: 'LLM call a rule or script could replace' },
  review: { badge: 'needs review', legend: 'LLM call a rule could handle most of the time' },
  repeats: { badge: 'repeats', legend: 'tool call that repeats with the same input and result' },
  shared: { badge: 'shared', legend: 'tool call more than one agent makes in a run, with the same input and result' },
};
// the agents a finding says repeat a call between them within a run
const sharedBy = o => o.type === 'CROSS_AGENT_DUPLICATE' ? o.agents || [] : o.between_agents?.agents || [];
const LLM_AFTER = 'llm_call ← ';
// the map steps a finding is about; scripting a sequence removes the LLM calls that chose its next steps;
// a next step about an agent's unnamed LLM turns (``turns``) is about all of them
const findingNodes = o => o.turns ? o.turns.map(t => t.title) : o.type === 'EXTRACT_DETERMINISTIC_SUBSTEP'
  ? [...(o.nodes || []), ...(o.nodes || []).slice(0, -1).map(n => LLM_AFTER + n)] : (o.nodes || []);
// a finding's row in the steps table: the step it's listed under
const rowOf = o => (o.nodes || [o.title])[0];
// a map box's step: its id, or for a step more than one agent uses, the `step` its per-agent box names
const stepOf = n => n.step ?? n.id;
function marksOf(data, byId) {
  const marks = new Map();
  const boxes = new Map();  // step -> its boxes, one per agent that uses it
  for (const n of byId.values()) boxes.set(stepOf(n), [...(boxes.get(stepOf(n)) || []), n]);
  const add = (step, kind, o, only) => {
    for (const n of boxes.get(step) || []) {
      if (only && n.kind !== only || o.identity?.agent && n.step && n.agent !== o.identity.agent) continue;
      const m = marks.get(n.id) || { kinds: new Set(), rows: new Set([step]) };
      m.kinds.add(kind);
      m.rows.add(rowOf(o));
      marks.set(n.id, m);
    }
  };
  for (const o of [...(data.summary?.opportunities || []), ...(data.summary?.next_steps || [])]) {
    const nodes = o.nodes || [];
    if (o.type === 'REVIEW_HIDDEN_STATE') nodes.forEach(id => add(id, 'unstable', o));
    else if (o.type === 'CACHE_RESULT') nodes.forEach(id => add(id, 'repeats', o, 'step'));
    else if (o.type === 'CROSS_AGENT_DUPLICATE') { /* marked below, on every agent's copy */ }
    else if (o.type === 'EXTRACT_DETERMINISTIC_SUBSTEP') nodes.slice(0, -1).forEach(id => add(LLM_AFTER + id, 'replace', o));
    else if (o.type === 'PARTIAL_LLM_BYPASS') nodes.forEach(id => add(id, 'review', o, 'llm_call'));
    else nodes.forEach(id => add(id, 'replace', o, 'llm_call'));  // a rule, or a rule for one slice
    const agents = sharedBy(o);
    for (const step of agents.length ? nodes : []) {
      const copies = (boxes.get(step) || []).filter(n => n.kind === 'step' && (!n.step || agents.includes(n.agent)));
      for (const n of copies) {
        const m = marks.get(n.id) || { kinds: new Set(), rows: new Set([step]) };
        m.kinds.add('shared');
        m.peers = new Set([...(m.peers || []), ...copies.map(c => c.id)]);
        marks.set(n.id, m);
      }
    }
  }
  return marks;
}

// why the report can't tell whether a step's calls repeat
const UNSURE = {
  different: { cell: 'None', note: 'every call had a different input' },
  unrecorded: { cell: 'Can’t compare', note: 'what it saw isn’t recorded' },
  few: { cell: 'Can’t tell yet', note: '' },
};
const unsure = s => {
  const ev = s.evidence || {};
  if (s.classification !== 'INSUFFICIENT_EVIDENCE') return null;
  if (ev.repeated_inputs && !ev.determinism_confidence) return 'unrecorded';  // no repeat counted: none was recorded in full
  if (ev.occurrences > 1 && ev.unique_inputs === ev.occurrences) return 'different';
  return 'few';
};
const unsureTry = s => unsure(s) === 'unrecorded'
  ? 'The trace doesn’t record all of what this step saw, so calls that look the same can’t be compared.' +
    (String(s.node).startsWith('llm_call') ? ' Prepare the dataset with <code>profiler-client prepare --llm-fingerprint</code> to include a fingerprint of what each LLM call read (no text is sent).' : '')
  : `Only ${count(s.evidence?.repeated_observations || 0)} of its ${plural(s.occurrences, 'call')} had an input seen on another call. ` +
    'Send more runs of this workflow, so the same inputs come up again and the report can tell whether this step repeats.';

// every pseudonym (h:…) in a name or a sentence shows as its first 7 characters; the whole text is in the tooltip
const HASH = /h:([0-9a-f]{7})[0-9a-f]{8,}/g;  // global: used only with .replace, which ignores lastIndex
const HAS_HASH = /h:[0-9a-f]{15,}/;  // non-global: safe for .test (no lastIndex state to leak between calls)
// a hosted multi-agent report names its agents and their decisions "Agent 3", "After Agent 1": set per report
let DISPLAY = new Map();
const shortHashes = text => esc(text).replace(HASH, (m, s) => DISPLAY.has(m) ? esc(DISPLAY.get(m)) : `<span class="h">${s}</span>`);
const stepName = n => {
  const text = String(n);
  const hashed = HAS_HASH.test(text);
  return `<span class="step"${hashed ? ` title="${esc(text)}"` : ''}>${shortHashes(text)}</span>`;
};

// whose result an LLM call read, a run's prompt and reply -- never from the order steps ran in
const MAP_NONE = {
  NO_EVENT_IDS: 'No map: this dataset was prepared with a profiler-client older than 0.7.0, so its steps have no ids to link. Prepare it again with 0.7.0 or later to see the map.',
  NO_PARENT_LINKS: "No map: no step in this dataset names the step that started it, so there are no links to draw. The map isn't guessed from the order steps ran in.",
};
const MAP_LINKS = 14;
// what each kind of map link means; "ran next" is only step order, so it is never called a choice
const LINK_KIND = {
  invokes: { word: 'chose', legend: 'chose: the trace records which call started this step' },
  reads_result: { word: 'read the result of', legend: 'read the result of: the next LLM call read this step’s result' },
  follows: { word: 'ran next', legend: 'ran next: taken from the order steps ran in; the trace records no link', dashed: true },
  linked: { word: 'linked', legend: 'linked: a parent link your log records' },
};
const ORDER_NOTE = 'Built from step order within each run. Steps that ran in parallel may appear in sequence.';
const MAP_T = 18, MAP_S = 21;  // characters that fit a map box's title line and its smaller lines
const MAP_MIN_SCALE = 0.8;  // a map never shrinks below this to fit its box: a wider one scrolls sideways inside it
const MAP_END = { '@prompt': 'Prompt', '@reply': 'Reply' };
const shortId = n => String(n).replace(HASH, 'h:$1');  // the pseudonym's first 7 characters, never its display name
const plainName = n => String(n).replace(HASH, (m, s) => DISPLAY.get(m) || `h:${s}`);
const runPct = x => share(x, 1);
// a handoff's label: started together with another agent ("both"), one way of several ("either"), or back to an earlier agent
const handoffTag = e => e.loop ? `return · ${runPct(e.loop.share_of_runs)} of runs, ${e.loop.passes} passes`
  : e.fork ? `${e.fork.mode} · ${runPct(e.fork.share_of_runs)} of runs` : '';
function renderMap(q, data, all = false) {
  const avail = data.availability?.graph, graph = data.graph;
  if (!avail?.available || !graph) {
    const why = avail ? MAP_NONE[avail.code] || 'No map for this dataset.' : 'No map: this report was made before maps were added.';
    q('map').innerHTML = `<p class="none">${esc(why)}</p>`;
    q('mapWhy').textContent = '';
    return new Map();
  }
  const byId = new Map(graph.nodes.map(n => [n.id, n]));
  // a workflow that runs through more than one agent is drawn in bands, one per agent, in the order runs reach them
  const agents = [], seen = new Set(['@prompt']), queue = ['@prompt'];
  const meet = id => { const a = byId.get(id)?.agent; if (a && !agents.includes(a)) agents.push(a); };
  while (queue.length) {
    const id = queue.shift();
    meet(id);
    for (const e of graph.edges) if (e.from === id && !seen.has(e.to)) { seen.add(e.to); queue.push(e.to); }
  }
  graph.nodes.forEach(n => meet(n.id));  // agents no run reaches from the prompt come last
  const banded = agents.length > 1;
  // the prompt opens the first band; the reply closes the band of the step that replies most
  const bandOf = id => !banded || id === '@prompt' ? 0
    : id === '@reply' ? bandOf(graph.edges.find(e => e.to === '@reply')?.from)
    : Math.max(0, agents.indexOf(byId.get(id)?.agent));
  const handoff = e => bandOf(e.from) !== bandOf(e.to);
  // agents one call starts together ("both": they run side by side) share a row of bands
  const groups = [];
  for (let b = 0; b < (banded ? agents.length : 1); b++) {
    const fork = graph.edges.find(e => e.fork?.mode === 'both' && bandOf(e.to) === b)?.from;
    const with_ = fork !== undefined && groups.find(g => g.fork === fork);
    if (with_) with_.bands.push(b); else groups.push({ fork, bands: [b] });
  }
  // the most-taken links (the server lists them most-taken first) and the steps they join;
  // in bands, every handoff and the most-taken links inside each band; `all` draws every link
  const inBand = new Map();
  const links = graph.edges.filter(e => {
    if (all || handoff(e)) return true;
    const b = bandOf(e.from), n = inBand.get(b) || 0;
    inBand.set(b, n + 1);
    return n < MAP_LINKS;
  });
  const joined = new Set(links.flatMap(e => [e.from, e.to])), shown = graph.nodes.filter(n => joined.has(n.id));
  const marks = marksOf(data, new Map(shown.map(n => [n.id, n])));

  // names: a pseudonymous step is "Tool A", "Tool B"… (most-called first); an LLM call is "after" what it read
  const letters = new Map();
  for (const n of shown) if (n.kind === 'step' && HAS_HASH.test(stepOf(n)) && !letters.has(stepOf(n))) letters.set(stepOf(n), `Tool ${String.fromCharCode(65 + letters.size % 26)}${letters.size >= 26 ? Math.floor(letters.size / 26) : ''}`);
  const label = id => { const step = byId.has(id) ? stepOf(byId.get(id)) : id; return MAP_END[id] ? MAP_END[id].toLowerCase() : letters.get(step) || plainName(step); };
  const lines = n => {
    if (n.kind === 'llm_call') {
      const after = String(stepOf(n)).replace(/^llm_call ← /, '');
      const what = after === 'user_prompt' ? 'prompt' : label(after), stats = `${count(n.occurrences)} · ${money(n.cost)}`;
      return `after ${what}`.length <= MAP_S ? ['LLM call', `after ${what}`, stats] : ['LLM call after', what, stats];
    }
    if (letters.has(stepOf(n))) return [letters.get(stepOf(n)), plainName(stepOf(n)), plural(n.occurrences, 'call')];
    const [t, ...rest] = wrapName(plainName(stepOf(n)), MAP_T);  // a long name takes the second line, in the same type
    return [t, rest.join(''), plural(n.occurrences, 'call'), rest.length > 0];
  };
  // a line still too long for its box is squeezed to fit, never cut
  const squeeze = (t, max) => t.length > max ? ` textLength="${W - 28}" lengthAdjust="spacingAndGlyphs"` : '';

  // places: walk out from the prompt along the busiest links; a step sits right of what started it,
  // and the LLM call that reads a first-row step's result sits under it. In bands, each band is laid
  // out the same way from where runs enter it, and the bands stack top to bottom
  const place = (ids, inner) => {
    const at = new Map(), taken = new Set(), key = (c, r) => `${c},${r}`;
    const put = (id, c, r) => { while (taken.has(key(c, r))) r++; at.set(id, { c, r }); taken.add(key(c, r)); };
    const mine = shown.filter(n => ids.has(n.id));
    const starts = mine.filter(n => n.kind === 'start' || !inner.some(e => e.to === n.id && e.from !== n.id));
    const queue = [];
    for (const n of starts.filter(n => n.id !== '@reply')) { put(n.id, 0, at.size ? Math.max(...[...at.values()].map(p => p.r)) + 1 : 0); queue.push(n.id); }
    while (queue.length) {
      const id = queue.shift(), p = at.get(id);
      for (const e of inner.filter(e => e.from === id && e.to !== '@reply' && !at.has(e.to))) {
        if (e.kind === 'reads_result' && p.r === 0) put(e.to, p.c, 1); else put(e.to, p.c + 1, p.r);
        queue.push(e.to);
      }
    }
    for (const n of mine) if (!at.has(n.id) && n.id !== '@reply') put(n.id, 0, Math.max(0, ...[...at.values()].map(p => p.r)) + 1);
    if (ids.has('@reply')) put('@reply', Math.max(0, ...[...at.values()].map(p => p.c)), 0);
    return at;
  };
  const at = new Map(), bands = [];
  let row = 0, level = 0;  // level: a row of bands, one band or a side-by-side group
  for (const group of groups) {
    const mine = [];
    let col = 0;
    for (const b of group.bands) {
      const ids = new Set([...joined].filter(id => bandOf(id) === b));
      if (!ids.size) continue;
      const local = place(ids, links.filter(e => !handoff(e) && ids.has(e.from)));
      for (const [id, p] of local) at.set(id, { c: p.c + col, r: p.r + row, b: level });
      const cols = Math.max(...[...local.values()].map(p => p.c)) + 1;
      mine.push({ agent: agents[b], level, r0: row, rows: Math.max(...[...local.values()].map(p => p.r)) + 1, c0: col, c1: col + cols - 1 });
      col += cols;
    }
    if (!mine.length) continue;
    const rows = Math.max(...mine.map(m => m.rows));
    for (const m of mine) bands.push({ ...m, r1: row + rows - 1, alone: mine.length === 1 });
    row += rows;
    level++;
  }

  const W = 172, H = 68, PW = 96, PH = 44, CX = 236, RY = 200, PAD = 16, TOP = 40, HEAD = banded ? 36 : 0;
  const rowY = (r, b) => TOP + r * RY + (b + 1) * HEAD;  // a band's name sits above its first row
  const box = id => {
    const { c, r, b } = at.get(id), end = MAP_END[id], w = end ? PW : W, h = end ? PH : H;
    const x = PAD + c * CX + (W - w) / 2, y = rowY(r, b) + (H - h) / 2;
    return { x, y, w, h, cx: x + w / 2, cy: y + h / 2 };
  };
  const cols = Math.max(...[...at.values()].map(p => p.c)) + 1, rows = Math.max(...[...at.values()].map(p => p.r)) + 1;
  const width = PAD * 2 + (cols - 1) * CX + W, height = rowY(rows - 1, level - 1) + H + 56;
  const bandRects = banded ? bands.map((band, b) => {
    const y = rowY(band.r0, band.level) - 48, h = rowY(band.r1, band.level) + H + 18 - y;
    const x = band.alone ? PAD / 2 : PAD / 2 + band.c0 * CX, w = band.alone ? width - PAD : (band.c1 - band.c0) * CX + W + PAD;
    return `<g class="band"><rect x="${x}" y="${y}" width="${w}" height="${h}" rx="8"/>` +
      // top right: runs enter a band in its first column, so the handoff arrows come in on the left
      `<text x="${x + w - PAD / 2}" y="${y + 22}" text-anchor="end"><tspan class="k">Agent ${b + 1} of ${bands.length}</tspan> ${esc(plainName(band.agent))}` +
      `${HAS_HASH.test(band.agent) ? `<title>${esc(band.agent)}</title>` : ''}</text></g>`;
  }) : [];

  const total = data.summary?.totals?.cost || shown.reduce((s, n) => s + n.cost, 0);
  const hot = shown.filter(n => n.cost > 0).sort((a, b) => b.cost - a.cost)[0];
  const max = Math.max(1, ...links.map(e => e.transition_count));
  const ARROW = 'markerUnits="userSpaceOnUse" markerWidth="12" markerHeight="12" viewBox="0 0 10 10" refX="9" refY="5" orient="auto-start-reverse"';

  // where a step goes more than one way, each way's share of everything that left it (all links, not just those drawn)
  const out = new Map();
  for (const e of graph.edges) out.set(e.from, [...(out.get(e.from) || []), e]);
  const share = e => { const all = out.get(e.from), p = 100 * e.transition_count / all.reduce((s, x) => s + x.transition_count, 0); return p < 1 ? '<1%' : `${Math.round(p)}%`; };
  const splits = e => out.get(e.from).length > 1;

  const paths = links.filter(e => e.from !== e.to && at.has(e.from) && at.has(e.to)).map(e => {
    const a = box(e.from), b = box(e.to), w = 1.5 + 8 * e.transition_count / max;
    const { c: ca, r: ra } = at.get(e.from), { c: cb, r: rb } = at.get(e.to);
    const isHot = hot && e.to === hot.id && e.kind === 'reads_result';
    if (handoff(e)) {  // to the next agent's band: out of the bottom, into the top, labelled with the runs that took it
      const down = b.y > a.y, x1 = a.cx, y1 = down ? a.y + a.h : a.y, x2 = b.cx, y2 = down ? b.y - 2 : b.y + b.h + 2, mid = (y1 + y2) / 2;
      const from = agents[bandOf(e.from)], to = agents[bandOf(e.to)], tag = handoffTag(e);
      return `<path class="edge handoff k-${esc(e.kind)}${e.loop ? ' return' : ''}" d="M${x1},${y1} C${x1},${mid} ${x2},${mid} ${x2},${y2}" stroke-width="${w.toFixed(1)}" marker-end="url(#mapArrow)">` +
        `<title>${esc(plainName(from))} ${e.loop ? 'hands back' : 'hands over'} to ${esc(plainName(to))}: ${esc(label(e.from))} → ${esc(label(e.to))} in ${esc(plural(e.distinct_run_count, 'run'))}${tag ? `; ${esc(tag)}` : ''}${LINK_KIND[e.kind] ? ` (${esc(LINK_KIND[e.kind].word)})` : ''}</title></path>` +
        `<text class="edge-n" x="${(x1 + x2) / 2 + 8}" y="${mid + 4}">${esc(tag || plural(e.distinct_run_count, 'run'))}</text>`;
    }
    let d, lx, ly, anchor = 'middle';
    if (ca === cb) {  // same column: down on the left, up on the right
      const down = b.y > a.y, x = a.cx + (down ? -30 : 30);
      const y1 = down ? a.y + a.h : a.y, y2 = down ? b.y - 2 : b.y + b.h + 2;
      d = `M${x},${y1} L${x},${y2}`; lx = x + (down ? -10 : 10); ly = (y1 + y2) / 2 + 4; anchor = down ? 'end' : 'start';
    } else if (ra === rb && cb > ca) {  // same row, rightwards
      const x1 = a.x + a.w, x2 = b.x - 2;
      d = `M${x1},${a.cy} L${x2},${b.cy}`; lx = (x1 + x2) / 2; ly = a.cy - 12;
    } else if (cb > ca) {  // rightwards to another row: leave by the top or bottom, clear of the row's own links
      const up = rb < ra, x1 = a.x + a.w - 10, y1 = up ? a.y : a.y + a.h, x2 = b.x - 2, y2 = b.cy;
      d = `M${x1},${y1} C${x1},${y2} ${x1},${y2} ${x2},${y2}`;
      lx = (x1 + x2) / 2; ly = y2 - 8;  // over its level run, between the two columns
    } else {  // back to an earlier column: arc over the top
      const x1 = a.cx, x2 = b.cx, y1 = a.y, y2 = b.y - 2, high = Math.min(y1, y2) - 34;
      d = `M${x1},${y1} C${x1},${high} ${x2},${high} ${x2},${y2}`; lx = (x1 + x2) / 2; ly = high + 8;
    }
    const kind = LINK_KIND[e.kind];
    return `<path class="edge k-${esc(e.kind)}${isHot ? ' hot' : ''}" d="${d}" stroke-width="${w.toFixed(1)}" marker-end="url(#${isHot ? 'mapArrowHot' : 'mapArrow'})">` +
      `<title>${esc(label(e.from))} → ${esc(label(e.to))}${kind ? ` (${esc(kind.word)})` : ''}: ${esc(plural(e.transition_count, 'time'))} in ${esc(plural(e.distinct_run_count, 'run'))}${splits(e) ? `, ${share(e)} of what left ${esc(label(e.from))}` : ''}</title></path>` +
      `<text class="edge-n" x="${lx}" y="${ly}" text-anchor="${anchor}">${esc(count(e.transition_count))}${splits(e) ? ` · ${share(e)}` : ''}</text>`;
  });
  const loops = new Map(links.filter(e => e.from === e.to).map(e => [e.from, e.transition_count]));
  const nodes = shown.filter(n => at.has(n.id)).map(n => {
    const b = box(n.id);
    if (MAP_END[n.id]) return `<g class="node end"><rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="22"/><text x="${b.cx}" y="${b.cy + 5}" text-anchor="middle">${MAP_END[n.id]}</text></g>`;
    const [t, s1, s2, wrapped] = lines(n), loop = loops.get(n.id);
    const kinds = Object.keys(MARK).filter(k => marks.get(n.id)?.kinds.has(k));
    const cls = `node${n.kind === 'llm_call' ? ' llm' : ''}${hot && n.id === hot.id ? ' hot' : ''}${kinds.map(k => ` ${k}`).join('')}`;
    let bx = b.x + 8;
    const badges = kinds.map(k => {
      const w = 16 + 6.2 * MARK[k].badge.length, x = bx;
      bx += w + 6;
      return `<g class="badge ${k}"><rect x="${x}" y="${b.y - 10}" width="${w}" height="20" rx="10"/><text x="${x + w / 2}" y="${b.y + 4}" text-anchor="middle">${esc(MARK[k].badge)}</text></g>`;
    });
    return `<g class="${cls}" data-node="${esc(n.id)}" data-step="${esc(stepOf(n))}"${kinds.length ? ` data-marks="${kinds.map(k => MARK[k].badge).join(', ')}"` : ''}><title>${esc(n.step ? `${n.step} (${n.agent})` : n.id)}</title><rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="6"/>` +
      `<text class="t" x="${b.x + 14}" y="${b.y + 26}"${squeeze(t, MAP_T)}>${esc(t)}</text>` +
      (s1 ? `<text class="${wrapped ? 't' : 's'}" x="${b.x + 14}" y="${b.y + 44}"${squeeze(s1, wrapped ? MAP_T : MAP_S)}>${esc(s1)}</text>` : '') +
      `<text class="s" x="${b.x + 14}" y="${b.y + (s1 ? 59 : 50)}">${esc(s2)}${loop ? ` · ↻ ${esc(count(loop))}` : ''}</text>${badges.join('')}</g>`;
  });
  const left = graph.edges.length - links.length, unlinked = graph.nodes.filter(n => !joined.has(n.id)).length;
  const more = left ? `<p class="more">${esc(`This map shows the ${count(links.length)} most-taken of ${plural(graph.edges.length, 'link')}. ${plural(left, 'link')}${unlinked ? ` and ${plural(unlinked, 'step')}` : ''} ${left + unlinked === 1 ? 'is' : 'are'} hidden to keep it readable; every step is in the table below.`)} ` +
      `<button type="button" class="link" data-map="all">Show all ${count(graph.edges.length)} links</button></p>`
    : all && graph.edges.length > MAP_LINKS ? `<p class="more">Showing all ${count(graph.edges.length)} links. <button type="button" class="link" data-map="less">Show only the most-taken</button></p>` : '';
  const present = Object.keys(MARK).filter(k => [...marks.values()].some(m => m.kinds.has(k)));
  const drawnKinds = Object.keys(LINK_KIND).filter(k => links.some(e => e.kind === k && e.from !== e.to && at.has(e.from) && at.has(e.to)));
  q('map').innerHTML = (present.length ? `<p class="legend marks">${present.map(k => `<span><i class="pill ${k}">${esc(MARK[k].badge)}</i>${esc(MARK[k].legend)}</span>`).join('')}` +
    '<span>Select a marked step to see its rows in the table</span></p>' : '') + `<div class="mapscroll"><svg viewBox="0 0 ${width} ${height}" style="min-width:${Math.round(width * MAP_MIN_SCALE)}px" role="group" aria-label="${esc(`Workflow map: ${plural(shown.length, 'step')}${banded ? ` in ${plural(bands.length, 'agent')}` : ''} and ${plural(paths.length, 'link')} across ${plural(graph.run_count, 'run')}`)}">` +
    `<defs><marker id="mapArrow" ${ARROW}><path class="arrow" d="M0,0 L10,5 L0,10 z"/></marker>` +
    `<marker id="mapArrowHot" ${ARROW}><path class="arrow hot" d="M0,0 L10,5 L0,10 z"/></marker></defs>` +
    bandRects.join('') + paths.join('') + nodes.join('') + '</svg></div>' + more +
    '<p class="legend kinds"><span><i class="sw llm"></i>LLM call (has a cost)</span><span><i class="sw tool"></i>Tool call</span>' +
    (hot && total ? `<span><i class="sw hot"></i>Largest share of spend (${Math.round(100 * hot.cost / total)}%)</span>` : '') + '</p>' +
    (drawnKinds.length ? `<p class="legend links">${drawnKinds.map(k => `<span><svg class="ln${LINK_KIND[k].dashed ? '' : ' solid'}" width="28" height="8" aria-hidden="true"><path d="M0,4 H28"/></svg>${esc(LINK_KIND[k].legend)}</span>`).join('')}</p>` : '') +
    (drawnKinds.includes('follows') ? `<p class="order-note">${esc(ORDER_NOTE)}</p>` : '');

  const name = id => {
    const n = byId.get(id), base = MAP_END[id] || (n?.kind === 'llm_call' ? lines(n).slice(0, 2).join(' ') : label(id));  // whole names, not the box's lines
    return n?.step ? `${base} (${plainName(n.agent)})` : base;  // one box per agent: say whose
  };
  const way = (n, e) => {
    const read = String(stepOf(n)).replace(/^llm_call ← /, '');
    const what = e.to === '@reply' ? 'replies' : e.to === n.id ? 'repeats itself'
      : byId.get(e.to)?.kind === 'llm_call' ? `goes on to the ${name(e.to)}` : `calls ${name(e.to)}${stepOf(byId.get(e.to) || { id: e.to }) === read ? ' again' : ''}`;
    return `${what} ${plural(e.transition_count, 'time')} (${share(e)})`;
  };
  const branches = shown.filter(n => (out.get(n.id) || []).length > 1)
    .map(n => `${name(n.id)} goes ${out.get(n.id).length} ways: it ${out.get(n.id).map(e => way(n, e)).join(', ')}. `).join('');
  const handoffs = banded ? `Each band is one agent, in the order runs reach them; a link from one band to the next is a handoff, labelled with how many runs took it. ` +
    (graph.edges.some(e => e.fork) ? 'Where one step hands over to more than one agent, "both" means it starts them together and they run side by side (their bands sit next to each other); "either" means each run takes one of the ways; the % is the share of runs reaching that step that take the link. ' : '') +
    (graph.edges.some(e => e.loop) ? 'A return link goes back to an agent that already ran in that run: it gives the share of runs that take it and the average number of passes in those runs. ' : '') : '';
  q('mapWhy').textContent = handoffs + branches +
    `Arrow width is how many times a hop happened across ${plural(graph.run_count, 'run')}; where a step goes more than one way, the % is that way's share. Links come from the trace: an LLM call choosing a tool (“chose”), the tool whose result the next LLM call read (“read the result of”), the prompt that started a run, and the LLM call that replied. Where a trace doesn't record which LLM call chose a tool (OpenTelemetry traces often don't), the link is taken from the order the calls ran in within one run and drawn dashed as “ran next”; it is not a choice.`;
  return marks;
}

const gapsOf = data => (data.data_quality?.gaps || []).filter(gap => gap.key !== 'user_input');  // the client drops it on purpose
// cost filled in from tokens at the user's prices, not recorded: every dollar figure is an estimate
const costEstimated = data => (data.data_quality?.gaps || []).some(gap => gap.key === 'cost' && gap.status === 'estimated');
const ESTIMATED = 'Cost estimated from tokens at your prices; the trace recorded none (see What the report couldn’t see).';

// say so under the headline, where the dollar figures are read
function markEstimate(q, estimated) {
  if (!estimated) return;
  const sub = q('sub').textContent;
  q('sub').innerHTML = `${sub ? `${esc(sub)} ` : ''}<span class="estimated">${esc(ESTIMATED)}</span>`;
}


function renderSteps(q, data, showAllSteps) {
  const t = data.summary?.totals || {}, opps = data.summary?.opportunities || [], replaceable = data.summary?.replaceable;
  const steps = Object.entries(data.mine?.by_agent || {}).flatMap(([agent, a]) => (a.actions || []).map(x => ({ ...x, agent, finds: [] })));
  for (const o of opps) {
    const node = (o.nodes || [o.title])[0], agent = o.identity?.agent;
    const row = steps.find(s => s.node === node && (!agent || s.agent === agent)) || steps.find(s => s.node === node);
    if (row) row.finds.push(o);
  }
  for (const s of steps) s.repeat = s.finds.filter(o => o.type === 'CACHE_RESULT').reduce((n, o) => n + (o.saving || 0), 0);
  steps.sort((a, b) => (b.total_cost || 0) - (a.total_cost || 0) || b.finds.length - a.finds.length);
  const listed = steps.filter(s => s.total_cost > 0 || s.finds.length), shown = showAllSteps ? steps : listed;
  const max = Math.max(0, ...steps.map(s => s.total_cost || 0)), cost = t.cost || 0;
  const repeated = steps.reduce((n, s) => n + s.repeat, 0), first = listed[0];

  // the Repeats column says what was found and what it cost; the Try row says what to do about it
  const repeats = s => {
    const cache = s.finds.filter(o => o.type === 'CACHE_RESULT'), lines = [];
    if (cache.length) {
      const calls = cache.reduce((n, o) => n + o.observations, 0);
      lines.push(`${count(calls - cache.length)} of ${plural(calls, 'call')}${s.repeat ? `, ${money(s.repeat)}` : ''}` +
        (cache.length > 1 ? `<span class="note">across ${count(cache.length)} different inputs</span>` : ''));
    }
    for (const type of Object.keys(KIND).filter(k => k !== 'CACHE_RESULT')) {
      const found = s.finds.filter(o => o.type === type);
      if (!found.length) continue;
      const saving = found.reduce((n, o) => n + (o.saving || 0), 0);
      lines.push(`${esc(KIND[type])}${found.length > 1 ? ` (${count(found.length)})` : ''}${saving ? `, ${money(saving)}` : ''}`);
    }
    if (lines.length) return lines.map(l => `<div class="find">${l}</div>`).join('');
    const why = UNSURE[unsure(s)];
    return why ? `<span class="muted">${why.cell}</span>${why.note ? `<span class="note">${why.note}</span>` : ''}` : '<span class="muted">None</span>';
  };
  const tries = s => {
    const order = Object.keys(KIND), finds = [...s.finds].sort((x, y) => order.indexOf(x.type) - order.indexOf(y.type));
    const acts = [...new Set(finds.map(o => o.type === 'EXTRACT_DETERMINISTIC_SUBSTEP'
      ? `${shortHashes(o.action)}: ${(o.nodes || []).map(stepName).join(' → ')}` : shortHashes(o.action)))];
    if (!acts.length && needsTry(s)) acts.push(unsureTry(s));
    return acts;
  };
  const needsTry = s => ['unrecorded', 'few'].includes(unsure(s)) && cost && s.total_cost / cost >= .05;

  // the takeaways: the same numbers as the table and the sections below, in plain sentences
  const runs = t.runs || 0, gaps = gapsOf(data).length, asked = listed.filter(s => !s.finds.length && needsTry(s));
  const toTry = listed.flatMap(tries).length - asked.length;
  const needRuns = asked.filter(s => unsure(s) === 'few').length, unrecorded = asked.length - needRuns;
  q('says').innerHTML = [
    runs && cost ? `One run costs about <b>${money(cost / runs)}</b>.` : '',
    first && cost && listed.length > 1 ? `The biggest cost is ${stepName(first.node)}: <b>${money(first.total_cost)}</b>, ${share(first.total_cost, cost)} of the spend.` : '',
    // the headline's figure, each call counted once; adding up the rows' repeats would count some calls twice
    replaceable?.cost && cost
      ? `<b>${money(replaceable.cost)}</b> (${share(replaceable.cost, cost)}) went to calls a rule, a script or a cache could replace, from ${plural(replaceable.findings, 'finding')}, each call counted once.`
      : replaceable && cost ? 'The report found no calls that a rule, a script or a cache could replace.'
      : repeated
      ? `<b>${money(repeated)}</b> (${share(repeated, cost)}) went on repeated calls: the same input and the same result as an earlier call. Reusing those results instead of calling again would avoid that spend.`
      : 'The report found no repeated calls.',
    data.mode === 'diagnose' ? ''
      : toTry ? `There ${toTry === 1 ? 'is 1 change' : `are ${count(toTry)} changes`} to try, on the rows marked <strong>Try:</strong> in the table below.`
      : 'There are no changes to try yet.',
    needRuns ? `For ${plural(needRuns, 'step')}, the report can’t tell yet whether calls repeat: it needs more runs of this workflow.` : '',
    unrecorded ? `For ${plural(unrecorded, 'step')}, the trace doesn’t record all of what the step saw, so the report can’t compare its calls.` : '',
    gaps ? `Your data is missing ${plural(gaps, 'kind')} of information, so the report may have missed some findings: see “What the report couldn’t see”.` : '',
  ].filter(Boolean).map(l => `<li>${l}</li>`).join('');

  const row = s => {
    const acts = tries(s);
    return `<tr class="${acts.length ? 'has-try' : ''}" data-node="${esc(s.node)}">
      <td data-label="Step">${stepName(s.node)}<span class="note">${plural(s.occurrences, 'call')}</span></td>
      <td class="barcell"><div class="spendbar"><i style="width:${max ? 100 * (s.total_cost || 0) / max : 0}%"></i><i class="rep" style="width:${max ? 100 * s.repeat / max : 0}%"></i></div></td>
      <td class="num" data-label="Cost">${s.total_cost ? `${money(s.total_cost)}<span class="note">${share(s.total_cost, cost)}</span>` : '—<span class="note">none recorded</span>'}</td>
      <td data-label="Repeats">${repeats(s)}</td></tr>` +
      (acts.length ? `<tr class="try" data-node="${esc(s.node)}"><td colspan="4">${acts.map(a => `<strong>Try:</strong> ${a}`).join('<br>')}</td></tr>` : '');
  };
  // more than one agent: its steps under its name, with its spend; its steps with no cost and no finding fold into one line
  const agents = [...new Set(steps.map(s => s.agent))];
  const ag = new Map((data.graph?.agent_graph?.agents || []).map(a => [a.id, a]));
  const spent = a => ag.get(a)?.cost ?? null;  // the agent map's figure: the rows overlap, so they don't add up to it
  agents.sort((x, y) => (spent(y) || 0) - (spent(x) || 0) || x.localeCompare(y));
  const grouped = agents.length > 1 ? agents.map(a => {
    const mine = steps.filter(s => s.agent === a), rows = mine.filter(s => shown.includes(s)), folded = mine.length - rows.length;
    return `<tr class="agent"><th colspan="4" scope="rowgroup">${stepName(a)}${spent(a) !== null
      ? ` <span class="k">${spent(a) ? `${money(spent(a))}, ${share(spent(a), cost)} of spend` : 'no cost recorded'}</span>` : ''}</th></tr>` + rows.map(row).join('') +
      (folded ? `<tr class="fold"><td colspan="4">${plural(folded, 'more step')} recorded no cost and no repeats.</td></tr>` : '');
  }).join('') : null;
  q('steps').querySelector('tbody').innerHTML = (grouped ?? shown.map(row).join('')) + (steps.length > listed.length ? `<tr class="more"><td colspan="4">${showAllSteps
      ? `<button class="link" type="button" data-steps="less">Hide the steps with no cost and no repeats</button>`
      : `${plural(steps.length - listed.length, 'more step')} recorded no cost and no repeats. <button class="link" type="button" data-steps="all">Show them</button>`}</td></tr>` : '');
}

// Openly7's brand bar and footer: shown when the host asks (profiler-client open and app, `brand: true`);
// a host with its own Openly7 header, like the signed-in site, leaves them hidden
const FOOT = `
  <p class="brandfoot" data-r="brandFoot" hidden><b>Openly7</b> · AI Agent Profiler beta · Your traces stay on your machine; Openly7 sees pseudonyms only.</p>`;
const HEAD = `
  <div class="brandbar" data-r="brand" hidden><span class="o7">Openly<b>7</b></span><span class="prod">AI Agent Profiler</span><span class="beta">beta</span></div>
  <button class="back" type="button" data-r="back" hidden>← All workflows</button>
  <div class="sheet-head">
    <div class="title">
      <div class="grade mini" data-r="grade" hidden><b data-r="gradeLetter"></b></div>
      <div><h2 data-r="name" tabindex="-1"></h2><div class="when" data-r="meta"></div>
        <span class="restored" data-r="restored" hidden>Names restored on this machine only</span></div>
    </div>
    <div class="tools"></div>
  </div>`;

const SHEET = HEAD + `
  <div class="lead" data-r="lead" hidden>
    <p class="head" data-r="head"></p>
    <p class="sub" data-r="sub"></p>
  </div>
  <ul class="risks" data-r="risks" hidden aria-label="Findings by risk"></ul>
  <p class="notes" data-r="notes"></p>
  <p class="totals" data-r="totals"></p>
  <p class="scope" data-r="scope" hidden></p>
  <section class="says">
    <h3>What your report says</h3>
    <ul data-r="says"></ul>
  </section>
  <section class="map">
    <h3>How your workflow runs</h3>
    <div class="mapbox" data-r="map"></div>
    <p class="why" data-r="mapWhy"></p>
    <div class="stepsof" data-r="stepsOf" hidden>
      <h4 data-r="stepsOfName"></h4>
      <div class="mapbox" data-r="stepMap"></div>
      <p class="why" data-r="stepWhy"></p>
    </div>
    <div class="events" data-r="events" hidden></div>
    <details class="capacity" data-r="capacity" hidden>
      <summary>Capacity view for this workflow</summary>
      <div data-r="capBody"></div>
    </details>
  </section>
  <section class="handoffs" data-r="handoffSection" hidden>
    <h3>Handoffs</h3>
    <table class="tbl" data-r="handoffs">
      <thead><tr><th>From</th><th>To</th><th>Kind</th><th class="num">Runs</th><th class="num">Share</th></tr></thead>
      <tbody></tbody>
    </table>
    <p class="why">Share: of the runs that reach where the handoff starts. A route under 10% of runs is not drawn on the map.</p>
  </section>
  <section class="amprint" data-r="amPrint" hidden></section>
  <section class="first" data-r="firstSection" hidden>
    <h3>Do these first</h3>
    <div data-r="first"></div>
  </section>
  <section class="between" data-r="betweenSection" hidden>
    <h3>Between agents</h3>
    <p class="why">Calls more than one agent makes in the same run, with the same input and the same result. One fetch could serve them all.</p>
    <div data-r="between"></div>
  </section>
  <section class="allfind" data-r="allSection" hidden>
    <h3>All findings</h3>
    <table class="tbl" data-r="all">
      <thead><tr><th>What to change</th><th>Evidence</th><th>Risk</th><th class="num">In this dataset</th></tr></thead>
      <tbody></tbody>
    </table>
    <p class="why">Most saving first. Greyed rows aren’t in the headline: each says why.</p>
  </section>
  <section>
    <h3>Where the money went</h3>
    <p class="keys" aria-hidden="true"><span class="key"><b></b>Spend</span><span class="key"><b class="rep"></b>Spent on repeats</span></p>
    <table class="tbl" data-r="steps">
      <thead><tr><th>Step</th><th class="barh"><span class="sr">Share of spend</span></th><th class="num">Cost</th><th>Repeats</th></tr></thead>
      <tbody></tbody>
    </table>
    <p class="why">Each % is that step's share of the workflow's total spend. <code>llm_call ← X</code> is the model deciding what to do after X. A tool's cost includes the LLM calls that chose it, so those dollars also appear in the <code>llm_call</code> rows: rows overlap, and their % don't add up to 100. A dash: the trace records no cost for that step, as for most tool calls.</p>
    <p class="why">Repeats are calls with the same input and the same result as an earlier call.</p>
  </section>
  <section data-r="gapsSection">
    <h3>What the report couldn't see</h3>
    <dl class="gaps" data-r="gaps"></dl>
  </section>` + FOOT;

// With `showEvent` (the report's path on this machine), a map step lists a few of its calls, each with the
// profiler-client command that prints its original record. The page runs nothing itself.
const shellQuote = s => `'${String(s).replace(/'/g, `'\\''`)}'`;

function wireEvents(q, data, reportPath) {
  const examples = new Map();
  for (const e of data.graph?.edges || []) {
    const ids = examples.get(e.to) || new Set();
    for (const x of e.evidence_sample || []) if (ids.size < 5) ids.add(x.event_id);
    examples.set(e.to, ids);
  }
  const panel = q('events');
  const show = g => {
    const id = g.dataset.node, ids = [...examples.get(id)];
    panel.innerHTML = `<p><b>${stepName(g.dataset.step)}</b>: ${plural(ids.length, 'example call')}. On this machine, each command prints the call's original record from your trace file.</p>` +
      `<ul>${ids.map(x => `<li><code>profiler-client show-event ${esc(shellQuote(reportPath))} ${esc(x)}</code> <button type="button" class="link" data-copy>Copy</button></li>`).join('')}</ul>`;
    panel.hidden = false;
  };
  for (const g of q('map').querySelectorAll('g[data-node]')) {
    if (!examples.get(g.dataset.node)?.size) continue;
    g.classList.add('pick');
    g.setAttribute('tabindex', '0');
    g.setAttribute('role', 'button');
    g.setAttribute('aria-label', `Example calls of ${g.dataset.step}`);
    g.addEventListener('click', () => show(g));
    g.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); show(g); } });
  }
  if (q('map').querySelector('g.pick')) q('map').querySelector('.legend.kinds')?.insertAdjacentHTML('beforeend', '<span>Click a step to look up example calls</span>');
}

function wireCopy(q) {
  q('events').addEventListener('click', e => {
    const b = e.target.closest('button[data-copy]');
    if (!b) return;
    const code = b.previousElementSibling;
    // the clipboard can be refused on a local file: then select the command for the user to copy
    navigator.clipboard?.writeText(code.textContent).then(() => { b.textContent = 'Copied'; },
      () => getSelection().selectAllChildren(code)) ?? getSelection().selectAllChildren(code);
  });
}


// the headline: how much of this workflow's spend went to calls the findings could remove, each call counted once
function renderLead(q, data) {
  const r = data.summary?.replaceable, cost = data.summary?.totals?.cost || 0;
  q('lead').hidden = !r;
  if (!r) return;
  const what = 'a rule, a script or a cache could replace';
  const found = `from ${plural(r.findings, 'finding')}, each call counted once`;
  if (!cost) {
    q('head').textContent = `No cost is recorded for this workflow, so the report can’t say how much of it went to calls ${what}.`;
    q('sub').textContent = '';
  } else if (!r.cost) {
    q('head').textContent = `The report found no calls in this workflow that ${what}.`;
    q('sub').textContent = `It cost ${money(cost)} in this dataset.`;
  } else if (r.cost >= 50) {
    q('head').innerHTML = `<b>${money(r.cost)}</b> of the ${money(cost)} this workflow cost went to calls ${what}.`;
    q('sub').textContent = `That is ${share(r.cost, cost)} of this workflow’s spend in this dataset, ${found}.`;
  } else {  // a small sum reads as nothing: lead with the share
    q('head').innerHTML = `<b>${share(r.cost, cost)}</b> of this workflow’s spend went to calls ${what}.`;
    q('sub').textContent = `${money(r.cost)} of ${money(cost)} in this dataset, ${found}.`;
  }
}

// three tiles under the headline, one per risk, each with its label in text (never color alone). The
// savings are the headline's findings only, so they add up to it; a high-risk finding is counted, not priced.
const RISK_TILES = [
  ['low', 'Low risk', 'Replay it against past calls before you change anything.'],
  ['medium', null, 'Check the reasons against the agent’s code first.'],
  ['high', null, 'Find the cause before changing this step.'],
];
// findings that overlap one counted in the headline are listed, not counted again, so a tile can list more
// findings than the headline counts; say how many it counts (older results have no counted figure)
const inHeadline = t => t.counted == null || t.counted === t.count ? '' : `, ${t.counted ? count(t.counted) : 'none'} in the headline`;
// an empty tile says what its emptiness means, not just "0 findings". The server marks a finding high risk only
// when a step's result changes for the same input, so no high-risk finding means no step did
const EMPTY_TILE = {
  low: () => 'None yet: every finding needs a check first. More runs, or steps declared read-only, can make one low risk.',
  medium: () => 'None.',
  high: runs => `Nothing to investigate: no step gave different results for the same input${runs ? ` in ${plural(runs, 'run')}` : ''}.`,
};
const INVESTIGATE_SHOWN = 5;
// the steps behind the high-risk findings, one line each, so an engineer knows where to look
function investigateList(found, many) {
  const steps = new Map();
  for (const { wf, f } of found) {
    const step = (f.nodes || [])[0] ?? f.title, key = `${wf}\u0000${step}`;
    if (!steps.has(key)) steps.set(key, { wf, step, n: 0, own: false });
    const s = steps.get(key);
    s.n++;
    s.own ||= f.type === 'REVIEW_HIDDEN_STATE';
  }
  // the step whose own result changes first; a repeat that is only unsafe because results change elsewhere after
  const all = [...steps.values()].sort((a, b) => b.own - a.own || b.n - a.n);
  const line = s => `<li>${stepName(s.step)}${many ? ` <span class="k">· ${stepName(s.wf)}</span>` : ''}: ` +
    `${s.own ? 'gives different results for the same input' : 'repeats a result, but results change for the same input elsewhere'}` +
    `${s.n > 1 ? ` <span class="k">(${count(s.n)} findings)</span>` : ''}</li>`;
  const more = all.length - INVESTIGATE_SHOWN;
  return `<ul class="inv">${all.slice(0, INVESTIGATE_SHOWN).map(line).join('')}</ul>` +
    (more > 0 ? `<p class="k">And ${plural(more, 'more step')}, in each workflow’s All findings.</p>` : '');
}
// found: [{ wf, f }] for every finding these totals count, to name the high-risk ones; runs: for the empty tiles
function renderRisks(q, totals, labels = {}, { found = [], runs = 0 } = {}) {
  q('risks').hidden = !totals;
  if (!totals) return;
  const high = found.filter(x => x.f.risk === 'high'), many = new Set(found.map(x => x.wf)).size > 1;
  q('risks').innerHTML = RISK_TILES.map(([risk, name, act]) => {
    const t = totals[risk] || { count: 0, saving: 0 };
    const head = `<li class="tile" data-risk="${risk}"><h3>${esc(name || labels[risk] || risk)}</h3>` +
      (name && labels[risk] ? `<p class="k">${esc(labels[risk])}</p>` : '');
    if (!t.count) return `${head}<p class="empty">${esc(EMPTY_TILE[risk](runs))}</p></li>`;
    const figure = risk === 'high' ? '' : `<p class="fig">${t.saving ? `${money(t.saving)} <span class="k">in this dataset</span>` : 'No saving counted'}</p>`;
    const list = risk === 'high' && high.length ? investigateList(high, many) : '';
    return `${head}${figure}<p>${plural(t.count, 'finding')}${inHeadline(t)}</p>${list}<p class="k">${esc(act)}</p></li>`;
  }).join('');
}

// the risk label, then the reasons the evidence line doesn't already give (those quote the call count)
const riskLine = (n, labels) => {
  const why = (n.risk_reasons || []).filter(r => !new RegExp(`\\b${n.observations}\\b`).test(r));
  return `${labels[n.risk] || n.risk}${why.length ? `: ${why.join('; ')}` : ''}.`;
};

// "All findings": every finding, the ones the headline counts first; a held-out one says why it isn't counted.
// A held finding a counted one covers (covered_by: same calls, saves at least as much) folds under that one,
// behind a line that opens it; `open` holds the counted rows whose folded findings are shown
const ALL_SHOWN = 10;
const HELD = {
  overlap: 'Not in the headline: a larger finding already counts these calls.',
  no_saving: 'No saving counted.',
  needs_review: 'Not in the headline: needs review. The model still answers the calls the rule doesn’t cover.',
};
function renderAll(q, data, showAll, declare, open = new Set()) {
  const rows = data.summary?.findings, labels = data.summary?.risk_labels || {};
  q('allSection').hidden = !Array.isArray(rows) || !rows.length;
  if (q('allSection').hidden) return;
  const parentOf = r => (r.counted || !rows[r.covered_by]?.counted ? null : r.covered_by);
  const under = new Map();
  rows.forEach(r => { const p = parentOf(r); if (p !== null) under.set(p, [...(under.get(p) || []), r]); });
  const top = [...rows.filter(r => r.counted), ...rows.filter(r => !r.counted && parentOf(r) === null)];
  const shown = showAll ? top : top.slice(0, ALL_SHOWN);
  // held out until its step is declared read-only: the question, and the host's button or how to answer it
  const why = r => r.counted ? '' : parentOf(r) !== null ? esc(HELD.overlap) : r.held_out !== 'declaration' ? esc(HELD[r.held_out] || '')
    : `<b>Does this step only read?</b> ${declare ? `<button type="button" class="btn-map" data-declare="${rows.indexOf(r)}">Declare it read-only</button>`
      : 'Not in the headline until you say so: declare it read-only (Tell us about the agent), then analyze again.'}`;
  const tr = (r, sub) =>
    `<tr${r.counted ? '' : ` class="held${sub ? ' sub' : ''}"`}><td><b>${esc(title(r))}</b><span class="note">${(r.nodes || [r.title]).map(stepName).join(' → ')}</span>` +
    `${why(r) ? `<span class="note readq">${why(r)}</span>` : ''}</td><td data-label="Evidence">${esc(evidenceOf(r))}</td>` +
    `<td data-label="Risk">${esc(labels[r.risk] || r.risk)}</td><td class="num" data-label="In this dataset">${r.saving ? money(r.saving) : '—'}</td></tr>`;
  const fold = r => {
    const i = rows.indexOf(r), kids = under.get(i);
    if (!kids) return '';
    const isOpen = open.has(i);
    return `<tr class="fold"><td colspan="4"><button type="button" class="link" data-covered="${i}" aria-expanded="${isOpen}">` +
      `${isOpen ? `Hide the ${count(kids.length)} smaller ${kids.length === 1 ? 'finding' : 'findings'}` : `+${count(kids.length)} smaller ${kids.length === 1 ? 'finding' : 'findings'} on the same calls`}</button></td></tr>` +
      (isOpen ? kids.map(k => tr(k, true)).join('') : '');
  };
  q('all').querySelector('tbody').innerHTML = shown.map(r => tr(r) + fold(r)).join('') +
    (top.length > ALL_SHOWN
      ? `<tr class="more"><td colspan="4"><button type="button" class="btn-map" data-all="${showAll ? 'fewer' : 'all'}">${showAll ? `Show the first ${ALL_SHOWN}` : `Show all ${count(top.length)} findings`}</button></td></tr>` : '');
}

// "Do these first": the server's next steps, at most three
function renderFirst(q, data, onMap) {
  const steps = data.summary?.next_steps, labels = data.summary?.risk_labels || {};
  q('firstSection').hidden = !Array.isArray(steps);
  if (!Array.isArray(steps)) return;
  if (!steps.length) {
    const gaps = gapsOf(data);
    q('first').innerHTML = `<p class="nothing">The report found nothing to change yet${gaps.length
      ? `. Your data is missing ${esc(gaps.map(g => g.label).join(', '))}, so it may have missed some: see “What the report couldn’t see”.` : '.'}</p>`;
    return;
  }
  // one card per step: findings on the same calls (a cache and a rule) are options for one saving, not two savings
  const cards = [];
  steps.forEach((n, i) => {
    const key = `${(n.nodes || [n.title]).join(' ')} ${n.agent || n.identity?.agent || ''}`, card = cards.find(c => c.key === key);
    if (card) card.opts.push(n); else cards.push({ key, i, opts: [n] });
  });
  q('first').innerHTML = `<ol>${cards.map(({ i, opts: [n, ...more] }) => {
    if (n.turns) return turnsCard(n, i, labels, onMap);
    const saving = Math.max(0, ...[n, ...more].map(o => o.saving || 0));
    const opt = o => `<p>${esc(evidenceOf(o))}</p>${o.do ? `<p class="do">${esc(o.do)}</p>` : ''}<p class="risk">${esc(riskLine(o, labels))}</p>`;
    return `<li class="card">
    <p class="top"><span class="stepl">${esc(STEP[n.step] || n.step)}</span><span class="cost">${saving ? `${money(saving)} <span class="k">in this dataset</span>` : 'No saving counted'}</span></p>
    <h4>${esc([n, ...more].map((o, j) => { const t = title(o); return j ? t.charAt(0).toLowerCase() + t.slice(1) : t; }).join(', or '))}</h4>
    <p class="where">${(n.nodes || [n.title]).map(stepName).join(' → ')}</p>
    ${more.length ? [n, ...more].map(o => `<h5>${esc(title(o))}</h5>${opt(o)}`).join('') + '<p class="k">Either one saves the same calls: the amount is counted once.</p>' : opt(n)}
    ${findingNodes(n).some(id => onMap.has(id)) ? `<button type="button" class="btn-map" data-first="${i}">Show on the map</button>` : ''}
  </li>`;
  }).join('')}</ol>`;
}

// one agent's unnamed LLM turns (named by the step they came after), each always making the same choice:
// one card, as one place in that agent's prompt to change; different calls, so their amounts add up
function turnsCard(n, i, labels, onMap) {
  return `<li class="card">
    <p class="top"><span class="stepl">${esc(STEP[n.step] || n.step)}</span><span class="cost">${n.saving ? `${money(n.saving)} <span class="k">in this dataset</span>` : 'No saving counted'}</span></p>
    <h4>${esc(title(n))}: ${esc(plural(n.turns.length, 'LLM turn'))} of ${stepName(n.agent)}</h4>
    <ul>${n.turns.map(t => `<li><span class="where">${stepName(t.title)}</span> ${esc(evidenceOf({ type: n.type, observations: t.observations }))}${t.saving ? ` ${money(t.saving)}.` : ''}</li>`).join('')}</ul>
    ${n.do ? `<p class="do">${esc(n.do)}</p>` : ''}<p class="risk">${esc(riskLine(n, labels))}</p>
    ${findingNodes(n).some(id => onMap.has(id)) ? `<button type="button" class="btn-map" data-first="${i}">Show on the map</button>` : ''}
  </li>`;
}

// "Between agents": each finding a call more than one agent repeats within a run, naming the agents
function renderBetween(q, data) {
  const found = (data.summary?.opportunities || []).filter(o => sharedBy(o).length > 1);
  const seen = new Set(), rows = found.filter(o => {  // one card per step: a cache finding per agent shares its note
    const key = `${o.type} ${rowOf(o)}`;
    return !seen.has(key) && seen.add(key);
  });
  q('betweenSection').hidden = !rows.length;
  q('between').innerHTML = rows.length ? `<ol>${rows.map(o => {
    const agents = sharedBy(o), runs = o.type === 'CROSS_AGENT_DUPLICATE' ? o.observations : o.between_agents.runs;
    const time = o.repeated_latency_ms ? `, ${(o.repeated_latency_ms / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 })} s` : '';
    return `<li class="card">
    <p class="top"><span class="stepl">${esc(title(o))}</span><span class="cost">${o.saving ? `${money(o.saving)} <span class="k">in this dataset</span>` : 'No dollar cost recorded'}</span></p>
    <h4>${stepName(rowOf(o))}</h4>
    <p class="where">${agents.map(stepName).join(' · ')}</p>
    <p>${esc(`The same call by ${plural(agents.length, 'agent')} in ${plural(runs, 'run')}`)}${o.type === 'CROSS_AGENT_DUPLICATE'
      ? esc(`: ${plural(o.repeated_calls, 'repeated call')}${time}.`) : esc('. Its result also repeats across runs, so reusing it covers these calls too.')}</p>
    <p class="do">${shortHashes(o.action || '')}.</p>
  </li>`;
  }).join('')}</ol>` : '';
}

// select steps on the map as it is drawn now
const pickOnMap = (q, ids) => q('map').querySelectorAll('g[data-node]').forEach(g => g.classList.toggle('sel', ids.has(g.dataset.node) || ids.has(g.dataset.step)));

// a marked map step shows its rows in the table
function wireMarks(q, data, marks) {
  const groups = [...q('map').querySelectorAll('g[data-node]')];
  const rows = ids => [...q('steps').querySelectorAll('tr[data-node]')].filter(tr => ids.has(tr.dataset.node));
  const showRows = id => {
    const ids = marks.get(id).rows;
    let found = rows(ids);
    if (new Set(found.map(tr => tr.dataset.node)).size < ids.size && q('steps').querySelector('button[data-steps="all"]')) {
      renderSteps(q, data, true);
      found = rows(ids);
    }
    q('steps').querySelectorAll('tr.hl').forEach(tr => tr.classList.remove('hl'));
    found.forEach(tr => tr.classList.add('hl'));
    pickOnMap(q, new Set([id, ...(marks.get(id).peers || [])]));
    found[0]?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  };
  for (const g of groups.filter(g => marks.has(g.dataset.node))) {
    const id = g.dataset.node, events = g.classList.contains('pick');
    g.classList.add('pick');
    g.setAttribute('tabindex', '0');
    g.setAttribute('role', 'button');
    g.setAttribute('aria-label', `${g.dataset.step}, ${g.dataset.marks}: show its rows in the table${events ? ' and example calls' : ''}`);
    g.addEventListener('click', () => showRows(id));
    g.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); showRows(id); } });
  }
}

// ---- the agent map: a workflow that runs through more than one agent opens with one box per agent and
// one per LLM decision between them, at the positions graph.agent_graph.layout worked out. Selecting a box
// fills the details panel; the busiest boxes are numbered hot spots, with a button each under the map.
const AM_FILL = [0.05, 0.15, 0.3];  // four fill steps by share of spend: under 5%, 5-15%, 15-30%, 30% and over
const AM_HOT = 5, AM_HOT_MIN = 0.05;
const fillOf = s => AM_FILL.filter(t => (s || 0) >= t).length;
// a name on lines of at most `max` characters, broken after a hyphen or an underscore; never cut
const wrapName = (name, max) => {
  const lines = [];
  let cur = '';
  for (const part of String(name).split(/(?<=[-_ ])/)) {
    if (cur && (cur + part).length > max) { lines.push(cur); cur = part; } else cur += part;
  }
  return [...lines, cur];
};
const amPct = s => share(s || 0, 1);

function agentMapModel(data) {
  const ag = data.graph.agent_graph, nodes = data.graph.nodes || [], total = data.summary?.totals?.cost;
  const shareOf = (cost, stored) => total ? (cost || 0) / total : stored;  // the same sum as the table's
  const byId = new Map(nodes.map(n => [n.id, n]));
  const marks = marksOf(data, byId);
  const hosted = ag.agents.some(a => HAS_HASH.test(a.id));
  const agentNo = new Map(ag.agents.map((a, i) => [a.id, i + 1]));
  const agentName = id => hosted ? `Agent ${agentNo.get(id)}` : id;
  const stepTitle = id => String(id).startsWith(LLM_AFTER) ? `LLM call after ${shortId(String(id).slice(LLM_AFTER.length))}` : shortId(id);
  const items = new Map();
  for (const a of ag.agents) {
    const kinds = new Set(a.step_ids.flatMap(s => [...(marks.get(s)?.kinds || [])]));
    items.set(a.id, { id: a.id, type: 'agent', name: agentName(a.id), sub: hosted ? shortId(a.id) : '', share: shareOf(a.cost, a.share), cost: a.cost, kinds, a });
  }
  for (const d of ag.decisions) {
    // a pseudonymous decision is named after the agents whose results it reads
    const reads = ag.links.filter(x => x.kind === 'reads' && x.decision === d.id).map(x => agentNo.get(x.from_agent)).sort((x, y) => x - y);
    const name = hosted && HAS_HASH.test(d.id) && reads.length ? `After Agent${reads.length > 1 ? 's' : ''} ${reads.join(' + ')}` : stepTitle(d.id);
    items.set(d.id, { id: d.id, type: 'decision', name, sub: name === stepTitle(d.id) ? '' : shortId(d.id), share: shareOf(d.cost, d.share), cost: d.cost, kinds: new Set(marks.get(d.id)?.kinds || []), d });
  }
  // findings, each with the server's next step for it (risk and what to do) where there is one
  const steps = data.summary?.next_steps || [];
  const findings = (data.summary?.opportunities || []).map(o => ({ ...o, next: steps.find(n => n.type === o.type && rowOf(n) === rowOf(o)) }));
  for (const n of steps) if (!findings.some(o => o.next === n)) findings.push({ ...n, next: n });
  const agentOfStep = step => [...new Set(nodes.filter(n => stepOf(n) === step && n.agent).map(n => n.agent))];
  for (const it of items.values()) {
    const once = new Map();
    it.findings = findings.filter(o => {
      const on = findingNodes(o);
      if (it.type === 'decision') {  // its step, made by its agent ("handoff @ card-activation" is card-activation's handoff)
        const node = byId.get(it.id);
        return on.includes(it.id) || (!!node?.agent && o.type !== 'EXTRACT_DETERMINISTIC_SUBSTEP' && on.includes(stepOf(node)) && (!o.identity?.agent || o.identity.agent === node.agent));
      }
      const mine = new Set(it.a.step_ids.map(s => stepOf(byId.get(s) || { id: s })));
      // a finding that names no agent is its only if all its steps are its own: every agent here has a "handoff"
      return o.identity?.agent ? on.some(s => mine.has(s)) && (o.identity.agent === it.id || sharedBy(o).includes(it.id))
        : sharedBy(o).length ? sharedBy(o).includes(it.id) : !!o.nodes?.length && o.nodes.every(s => mine.has(s));
    }).filter(o => {
      const key = `${o.type} ${(o.nodes || [o.title]).join(' ')}`, was = once.get(key);
      if (was) { was.observations = (was.observations || 0) + (o.observations || 0); return false; }
      once.set(key, o = { ...o });
      return true;
    }).map(o => once.get(`${o.type} ${(o.nodes || [o.title]).join(' ')}`));
    it.investigate = it.findings.some(o => o.next?.step === 'investigate') || it.kinds.has('unstable');
  }
  // hot spots: the five largest shares of spend at 5% or more, and anything to investigate; the router's
  // own box holds its decisions' spend too, so it is never one
  const ranked = [...items.values()].filter(it => it.id !== ag.router).sort((x, y) => (y.share || 0) - (x.share || 0) || x.id.localeCompare(y.id));
  const top = new Set(ranked.filter(it => (it.share || 0) >= AM_HOT_MIN).slice(0, AM_HOT));
  const hot = ranked.filter(it => top.has(it) || it.investigate);
  hot.forEach((it, i) => { it.hot = i + 1; });
  // where a finding's step is on the agent map: its decision box, else its agent's box
  const where = step => items.has(step) ? step : agentOfStep(step).find(a => items.has(a));
  // the names the rest of the report uses for the same pseudonyms
  const display = new Map(hosted ? [...items.values()].filter(it => it.sub && /^h:[0-9a-f]+$/.test(it.id)).map(it => [it.id, it.name]) : []);
  return { ag, byId, marks, items, hot, hosted, agentName, decisionName: id => items.get(id)?.name || stepTitle(id), where, display };
}

function renderAgentMap(q, data, m) {
  const { ag, items, hot } = m, L = ag.layout, runs = data.graph.run_count || 0;
  const rank = new Map(ag.agents.map(a => [a.id, a.rank]));
  // a route past agents that run in between says which it skips: "79% skip compliance-review"
  const skipped = x => {
    const from = x.kind === 'handoff' ? Math.max(-1, ...ag.links.filter(l => l.kind === 'reads' && l.decision === x.decision).map(l => rank.get(l.from_agent) ?? -1)) : rank.get(x.from_agent);
    const past = ag.agents.filter(a => a.id !== ag.router && a.rank > from && a.rank < rank.get(x.to_agent));
    return past.length === 1 ? `skip ${m.agentName(past[0].id)}` : past.length > 1 ? `skip ${past.length} agents` : '';
  };
  const routes = L.routes.map(r => {
    const links = r.links.map(i => ag.links[i]);
    const main = links.find(x => x.kind !== 'reads') || links[0], back = main.mode === 'return';
    const pts = r.points, d = pts.map(([x, y], i) => `${i ? 'L' : 'M'}${x},${y}`).join(' ');
    const skip = main.kind === 'reads' || back ? '' : skipped(main);
    let label = '';
    if (back) {  // right of its straight stretch, on two lines
      const [[ax, ay], [, by]] = pts.slice(1).map((p, i) => [pts[i], p]).filter(([a, b]) => a[0] === b[0]).sort(([a, b], [c, e]) => Math.abs(e[1] - c[1]) - Math.abs(b[1] - a[1]))[0] || [pts[0], pts[1]];
      const words = main.loop ? [`back: ${runPct(main.loop.share_of_runs)} of runs`, `${main.loop.passes} passes`] : [`back: ${amPct(main.share)}`];
      const y0 = (ay + by) / 2 + 4 - 7 * (words.length - 1);
      label = `<text class="edge-n" x="${ax + 6}" y="${y0}">${words.map((w, i) => `<tspan x="${ax + 6}" dy="${i ? 14 : 0}">${esc(w)}</tspan>`).join('')}</text>`;
    } else if (main.kind !== 'reads') {  // at the route's start: above its first sideways stretch, else beside the line
      const text = `${amPct(main.share)}${skip ? ` ${skip}` : ''}`;
      const k = pts.findIndex((p, i) => i && pts[i - 1][1] === p[1] && Math.abs(p[0] - pts[i - 1][0]) >= 40);
      if (k > 0) {
        const [[x1, y1], [x2]] = [pts[k - 1], pts[k]], right = x2 > x1;
        label = `<text class="edge-n" x="${x1 + (right ? 4 : -4)}" y="${y1 - 5}" text-anchor="${right ? 'start' : 'end'}">${esc(text)}</text>`;
      } else {
        const [[x1, y1], [, y2]] = pts;
        label = `<text class="edge-n" x="${x1 + 5}" y="${y1 + (y2 > y1 ? 14 : -6)}">${esc(text)}</text>`;
      }
    }
    const to = r.to.startsWith('together:') ? links.map(x => m.agentName(x.to_agent)).join(' and ') : m.agentName(main.to_agent);
    const title = main.kind === 'reads' ? `${m.decisionName(main.decision)} reads the result of ${links.map(x => m.agentName(x.from_agent)).join(' and ')}`
      : `${main.kind === 'handoff' ? m.decisionName(main.decision) : m.agentName(main.from_agent)} ${back ? 'hands back' : 'hands over'} to ${to}${main.mode === 'both' && links.length > 1 ? ', together,' : ''} in ${plural(main.runs, 'run')}${skip ? `, past ${skip.slice(5)}` : ''}`;
    return [`<path class="edge${back ? ' return' : ''}" d="${d}" marker-end="url(#amArrow)"><title>${esc(title)}</title></path>`, label];
  });
  const frames = L.frames.map(f => `<g class="together"><rect x="${f.x}" y="${f.y}" width="${f.w}" height="${f.h}" rx="8"/>` +
    `<text x="${f.x + 8}" y="${f.y + f.h - 6}">run together</text></g>`);
  const markWords = it => Object.keys(MARK).filter(k => it.kinds.has(k)).map(k => MARK[k].badge);
  const boxes = L.boxes.map(b => {
    const it = items.get(b.id);
    if (b.kind === 'router') {
      return `<g class="am router" data-an="${esc(b.id)}" tabindex="0" role="button" aria-label="${esc(`${it.name}, routes the run: ${amPct(it.share)} of spend`)}">` +
        `<rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="6"/><text class="t" x="${b.x + 12}" y="${b.y + b.h / 2 + 5}">${esc(it.name)}` +
        `${it.sub ? ` <tspan class="s">${esc(it.sub)}</tspan>` : ''} <tspan class="s">routes the run · its decisions below</tspan></text></g>`;
    }
    const f = fillOf(it.share), cls = `am ${b.kind} f${f}${it.kinds.has('unstable') ? ' unstable' : ''}${it.hot ? ' hotspot' : ''}`;
    const words = markWords(it);
    const label = `${it.hot ? `Hot spot ${it.hot}: ` : ''}${it.name}${it.sub ? ` (${it.sub})` : ''}, ${amPct(it.share)} of spend${words.length ? `, ${words.join(', ')}` : ''}`;
    const open = `<g class="${cls}" data-an="${esc(b.id)}" tabindex="0" role="button" aria-label="${esc(label)}"><title>${esc(label)}</title>` +
      `<rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="${b.kind === 'chip' ? 4 : 6}"/>`;
    if (b.kind === 'chip') {
      const chip = `${it.hot ? `${it.hot} · ` : ''}${it.name.replace(` @ ${m.agentName(b.agent)}`, '')} · ${amPct(it.share)}`;
      return open + `<text class="c" x="${b.x + 6}" y="${b.y + b.h - 6}"${chip.length * 7.2 > b.w - 12 ? ` textLength="${b.w - 12}" lengthAdjust="spacingAndGlyphs"` : ''}>${esc(chip)}</text></g>`;
    }
    // the name on up to two lines; an agent's pseudonym under "Agent N"; a hosted decision's only in its tooltip and panel
    const left = b.x + 10, inner = b.w - 20, max = Math.floor(inner / 7);
    const named = it.type === 'agent' && it.sub ? [it.name, it.sub] : wrapName(it.name, max);
    const lines = named.length > 2 ? [named[0], named.slice(1).join('')] : named;
    const lineY = i => b.y + 18 + 15 * i, figY = lineY(lines.length - 1) + 17;
    const text = lines.map((t, i) => `<text class="${it.sub && it.type === 'agent' && i ? 's' : 't'}" x="${left}" y="${lineY(i)}"${t.length > max ? ` textLength="${inner}" lengthAdjust="spacingAndGlyphs"` : ''}>${esc(t)}</text>`).join('');
    const figures = b.kind === 'decision' ? `${amPct(it.share)} · ${money(it.cost)}` : `${amPct(it.share)} · ${plural(it.a.steps, 'step')}`;
    // finding words in ink under the figures, on as many lines as fit above any chips; only "unstable result" in red
    const below = Math.min(b.y + b.h, ...L.boxes.filter(c => c.kind === 'chip' && c.agent === b.id).map(c => c.y));
    const room = Math.max(1, Math.floor((below - 3 - figY - 15) / 14) + 1), rows = [];
    words.forEach((w, i) => {
      const t = w + (i < words.length - 1 ? ',' : ''), row = rows.at(-1), len = row ? row.reduce((n, x) => n + x.length + 1, 0) : 0;
      if (row && (len + t.length <= max + 1 || rows.length === room)) row.push(t); else rows.push([t]);
    });
    const marks = rows.map((row, j) => `<text class="w" x="${left}" y="${figY + 15 + 14 * j}"${row.join(' ').length > max + 1 ? ` textLength="${inner}" lengthAdjust="spacingAndGlyphs"` : ''}>` +
      row.map((t, i) => `<tspan${t.replace(/,$/, '') === MARK.unstable?.badge ? ' class="bad"' : ''}>${esc(t)}${i < row.length - 1 ? ' ' : ''}</tspan>`).join('') + '</text>').join('');
    return open + text + `<text class="f" x="${left}" y="${figY}">${esc(figures)}</text>` + marks +
      (it.hot ? `<circle class="no" cx="${b.x}" cy="${b.y}" r="10"/><text class="no" x="${b.x}" y="${b.y + 4}" text-anchor="middle">${it.hot}</text>` : '') + '</g>';
  });
  const ARROW = 'markerUnits="userSpaceOnUse" markerWidth="8" markerHeight="8" viewBox="0 0 10 10" refX="9" refY="5" orient="auto-start-reverse"';
  const hidden = ag.links.filter(x => x.kind !== 'reads' && (x.share || 0) < 0.1).length;
  const legend = '<p class="legend"><span><i class="sw f0"></i><i class="sw f1"></i><i class="sw f2"></i><i class="sw f3"></i>Share of spend: under 5%, 5–15%, 15–30%, 30% and over (also printed in each box)</span>' +
    '<span><i class="sw no">1</i>Hot spot, by share of spend</span><span><i class="sw dash"></i>Unstable result</span>' +
    '<span><svg class="ln" width="28" height="8" aria-hidden="true"><path d="M0,4 H28"/></svg>Hands back to an earlier agent; a % on a link is the share of runs that take it</span>' +
    (hidden ? `<span>${esc(`${plural(hidden, 'route')} taken by under 10% of runs ${hidden === 1 ? 'is' : 'are'} not drawn; ${hidden === 1 ? 'it is' : 'they are'} in the details`)}</span>` : '') + '</p>';
  const buttons = hot.length ? `<div class="hotspots"><h4>Hot spots</h4><ol>${hot.map(it => {
    const what = it.findings.length ? it.findings.map(o => title(o)).filter((t, i, a) => a.indexOf(t) === i).join('; ') : 'no finding';
    return `<li><button type="button" data-an="${esc(it.id)}"><b>${it.hot}</b> <span>${esc(it.name)}${it.sub ? ` <span class="h">${esc(it.sub)}</span>` : ''}</span><span class="k">${esc(`${amPct(it.share)} of spend · ${what}`)}</span></button></li>`;
  }).join('')}</ol></div>` : '';
  q('map').innerHTML = `<div class="agentmap"><div class="ammap mapscroll"><svg viewBox="0 0 ${L.width} ${L.height}" style="max-width:${L.width}px;min-width:${Math.round(L.width * MAP_MIN_SCALE)}px" ` +
    `role="group" aria-label="${esc(`Agent map: ${plural(ag.agents.length, 'agent')} and ${plural(ag.decisions.length, 'decision')} between them, across ${plural(runs, 'run')}`)}">` +
    `<defs><marker id="amArrow" ${ARROW}><path class="arrow" d="M0,0 L10,5 L0,10 z"/></marker></defs>` +
    frames.join('') + routes.map(r => r[0]).join('') + boxes.join('') + routes.map(r => r[1]).join('') + `</svg></div>${legend}${buttons}<div class="ampanel" data-r="amPanel" aria-live="polite"></div></div>`;
  q('mapWhy').textContent = 'Each box is one agent, or one LLM call that decides which agent runs next. ' +
    'Links come from the trace: which result a decision read, and which agent it started. A % on a link is the share of runs reaching where it starts that take it.';
}

// every handoff on the agent map, the routes too rare to draw among them: from, to, kind, runs, share
const passes = n => `${count(n)} pass${n === 1 ? '' : 'es'}`;
const HANDOFF_KIND = { both: 'together with another', either: 'one of several', return: 'back to an earlier agent' };
function renderHandoffs(q, m) {
  // in run order: a decision ranks with the latest agent whose result it reads
  const rank = new Map(m.ag.agents.map(a => [a.id, a.rank ?? -1]));
  const at = x => x.kind === 'handoff' ? Math.max(-1, ...m.ag.links.filter(l => l.kind === 'reads' && l.decision === x.decision).map(l => rank.get(l.from_agent) ?? -1)) : rank.get(x.from_agent) ?? -1;
  const rows = m.ag.links.filter(x => x.kind !== 'reads').sort((x, y) => at(x) - at(y) || (y.share || 0) - (x.share || 0));
  q('handoffSection').hidden = !rows.length;
  q('handoffs').querySelector('tbody').innerHTML = rows.map(x => {
    const from = x.kind === 'handoff' ? m.decisionName(x.decision) : m.agentName(x.from_agent);
    const kind = (x.kind === 'direct' && x.mode !== 'return' ? 'direct, no LLM decision' : HANDOFF_KIND[x.mode] || x.mode) +
      (x.loop ? `, ${passes(x.loop.passes)} each` : '') + ((x.share || 0) < 0.1 ? ' · not drawn' : '');
    return `<tr><td data-label="From">${esc(from)}</td><td data-label="To">${esc(m.agentName(x.to_agent))}</td><td data-label="Kind">${esc(kind)}</td>` +
      `<td class="num" data-label="Runs">${count(x.runs)}</td><td class="num" data-label="Share">${amPct(x.share)}</td></tr>`;
  }).join('');
}

// a map item's findings, each with its evidence sentence and the risk reason
function findsHtml(m, it, labels) {
  return it.findings.length ? `<ul class="finds">${it.findings.map(o => {
    const ev = evidenceOf(o);
    const peers = sharedBy(o).filter(a => a !== it.id);
    return `<li><b>${esc(title(o))}</b> <span class="where">${(o.nodes || [o.title]).map(stepName).join(' → ')}</span>` +
      `<span>${esc(ev)}${peers.length ? esc(` Also made by ${peers.map(m.agentName).join(', ')}.`) : ''}</span>` +
      (o.next ? `<span class="k">${esc(riskLine(o.next, labels))}</span>` : '') + '</li>';
  }).join('')}</ul>` : '<p class="k">No finding on this one.</p>';
}

// one agent's steps: its step map, or, where the trace links none of them to another, a table of them
function agentStepsInto(q, data, m, it, slot, why, all = false) {
  const mine = new Set(it.a.step_ids);
  const graph = { ...data.graph, nodes: data.graph.nodes.filter(n => mine.has(n.id)), edges: data.graph.edges.filter(e => mine.has(e.from) && mine.has(e.to)) };
  const sub = k => q({ map: slot, mapWhy: why }[k] || k);
  if (!graph.edges.length) {
    const rows = graph.nodes.slice().sort((x, y) => (y.occurrences || 0) - (x.occurrences || 0)).map(n => {
      const words = Object.keys(MARK).filter(k => m.marks.get(n.id)?.kinds?.has(k)).map(k => MARK[k].badge).join(', ');
      return `<tr><td>${stepName(stepOf(n))}</td><td class="num">${count(n.occurrences)}</td><td class="num">${spend(n.cost)}</td><td>${esc(words)}</td></tr>`;
    }).join('');
    q(slot).innerHTML = `<table class="tbl agsteps"><thead><tr><th>Step</th><th class="num">Calls</th><th class="num">Cost</th><th>Marks</th></tr></thead><tbody>${rows}</tbody></table>`;
    q(why).textContent = 'The trace links none of these steps to another, so they are listed, not mapped.';
    return { graph, sub, marks: null };
  }
  const marks = renderMap(sub, { ...data, graph }, all);
  const svg = q(slot).querySelector('svg');  // a few steps: drawn at their natural size, never stretched
  if (svg?.viewBox) svg.style.maxWidth = `${svg.viewBox.baseVal.width}px`;
  return { graph, sub, marks };
}

// on paper, in place of the panel and the hot spot buttons: a hot spots table, then one section per agent
// with spend or findings, each with its step map and findings
function renderAgentPrint(q, data, m) {
  const labels = data.summary?.risk_labels || {}, what = it => it.findings.length
    ? it.findings.map(o => title(o)).filter((t, i, a) => a.indexOf(t) === i).join('; ') : 'no finding';
  const name = it => `${esc(it.name)}${it.sub ? ` <span class="h">${esc(it.sub)}</span>` : ''}`;
  const table = m.hot.length ? `<h3>Hot spots</h3><table class="tbl hot"><thead><tr><th class="num">#</th><th>Where</th><th class="num">Share</th><th class="num">Cost</th><th>Finding</th></tr></thead><tbody>` +
    m.hot.map(it => `<tr><td class="num">${it.hot}</td><td>${name(it)}</td><td class="num">${amPct(it.share)}</td><td class="num">${spend(it.cost)}</td><td>${esc(what(it))}</td></tr>`).join('') +
    '</tbody></table>' : '';
  const agents = m.ag.agents.map(a => m.items.get(a.id)).filter(it => (it.cost || 0) > 0 || it.findings.length);
  q('amPrint').innerHTML = table + agents.map((it, i) => `<div class="agent"><h3>${name(it)}</h3>` +
    `<p class="k">${esc(`${it.cost ? `${amPct(it.share)} of spend, ${money(it.cost)}` : 'No cost recorded'}, ${plural(it.a.steps, 'step')}.`)}</p>` +
    `<div class="mapbox" data-r="pm${i}"></div><p class="why" data-r="pw${i}"></p><h4>Findings on its steps</h4>${findsHtml(m, it, labels)}</div>`).join('');
  agents.forEach((it, i) => agentStepsInto(q, data, m, it, `pm${i}`, `pw${i}`));
  q('amPrint').hidden = !(table || agents.length);
}

function agentPanel(q, data, m, id, showEvent) {
  const it = m.items.get(id), ag = m.ag, runs = data.graph.run_count || 0, labels = data.summary?.risk_labels || {};
  if (!it) return;
  const routes = links => {
    // agents started together are one line: "to credit-assessment and fraud-screening, together: 1,242 runs (68%)"
    const both = links.filter(x => x.mode === 'both'), lines = links.filter(x => x.mode !== 'both').map(x => [[x], x]);
    if (both.length) lines.unshift([both, both[0]]);
    return lines.length ? `<ul class="routes">${lines.map(([xs, x]) => {
      const to = xs.map(y => m.agentName(y.to_agent)).join(' and '), how = x.mode === 'return' ? 'back to' : 'to';
      return `<li>${esc(`${how} ${to}${xs.length > 1 ? ', together' : ''}: ${plural(x.runs, 'run')} (${amPct(x.share)})${x.loop ? `, ${passes(x.loop.passes)} each` : ''}${(x.share || 0) < 0.1 ? ' · not drawn: under 10% of runs' : ''}`)}</li>`;
    }).join('')}</ul>` : '<p class="k">It hands over to no other agent.</p>';
  };
  const finds = findsHtml(m, it, labels);
  let html;
  if (it.type === 'decision') {
    html = `<p class="kind">LLM decision</p><h4>${esc(it.name)}</h4>` +
      `<p>${esc(`Made by ${m.agentName(it.d.agent)}: ${plural(it.d.calls, 'call')}, ${money(it.cost)}, ${amPct(it.share)} of spend.`)}</p>` +
      `<h5>Findings</h5>${finds}<h5>Where it sends the run</h5>${routes(ag.links.filter(x => x.kind === 'handoff' && x.decision === id))}` +
      `<p class="acts"><button type="button" class="btn" data-act="evidence">See the evidence</button>${showEvent ? '<button type="button" class="btn" data-act="calls">Show the calls</button>' : ''}</p>`;
  } else {
    const a = it.a, when = a.runs >= runs ? 'in every run' : `in ${plural(a.runs, 'run')} of ${count(runs)} (${amPct(a.runs / (runs || 1))})`;
    const along = a.alongside.length ? `, alongside ${a.alongside.map(m.agentName).join(' and ')}` : '';
    const stepsOf = a.step_ids.map(s => m.byId.get(s)).filter(Boolean).sort((x, y) => (y.occurrences || 0) - (x.occurrences || 0));
    const direct = ag.links.filter(x => x.kind === 'direct' && x.from_agent === id);
    html = `<p class="kind">${id === ag.router ? 'Agent that routes the run' : 'Agent'}</p><h4>${esc(it.name)}${it.sub ? ` <span class="h">${esc(it.sub)}</span>` : ''}</h4>` +
      `<p>${esc(`Runs ${when}${along}. ${plural(a.steps, 'step')}, ${it.cost ? `${money(it.cost)}, ${amPct(it.share)} of spend` : 'no cost recorded'}.`)}</p>` +
      `<h5>Findings on its steps</h5>${finds}` +
      (direct.length ? `<h5>Hands over without an LLM decision</h5>${routes(direct)}` : '') +
      `<h5>Its steps</h5><ul class="steplist">${stepsOf.map(n => `<li>${stepName(stepOf(n))} <span class="k">${esc(plural(n.occurrences, 'call'))}</span></li>`).join('')}</ul>` +
      `<p class="acts"><button type="button" class="btn" data-act="steps">Open this agent's steps</button></p>`;
  }
  q('amPanel').innerHTML = html;
  q('amPanel').dataset.an = id;
}

// the per-agent step map, under the agent map: today's map, limited to one agent's steps
function renderAgentSteps(q, data, m, id, showEvent, all = false) {
  // only the links between its own steps: how runs reach it and leave it is on the agent map above
  q('stepsOf').hidden = false;
  q('stepsOfName').textContent = `${m.items.get(id).name}${m.items.get(id).sub ? ` (${m.items.get(id).sub})` : ''}: its steps`;
  const { graph, sub, marks } = agentStepsInto(q, data, m, m.items.get(id), 'stepMap', 'stepWhy', all);
  if (!marks) return;
  if (showEvent) wireEvents(sub, { ...data, graph }, showEvent);
  wireMarks(sub, data, marks);
  q('stepMap').onclick = e => {  // its own "show all links" control
    const b = e.target.closest('button[data-map]');
    if (b) renderAgentSteps(q, data, m, id, showEvent, b.dataset.map === 'all');
  };
}

function wireAgentMap(q, data, m, showEvent) {
  const select = id => {
    q('map').querySelectorAll('[data-an]').forEach(g => {
      const on = g.dataset.an === id;
      g.classList.toggle('sel', on);
      if (g.tagName.toLowerCase() === 'g' || g.tagName === 'BUTTON') g.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    agentPanel(q, data, m, id, showEvent);
  };
  q('map').addEventListener('click', e => {
    const t = e.target.closest('[data-an]');
    if (t && !t.closest('.ampanel')) select(t.dataset.an);
    const act = e.target.closest('button[data-act]');
    if (!act) return;
    const id = q('amPanel').dataset.an, it = m.items.get(id);
    if (act.dataset.act === 'steps') { renderAgentSteps(q, data, m, id, showEvent); q('stepsOf').scrollIntoView({ block: 'start', behavior: 'smooth' }); }
    if (act.dataset.act === 'evidence') {  // its rows in the table below
      const rows = [...q('steps').querySelectorAll('tr[data-node]')].filter(tr => tr.dataset.node === stepOf(m.byId.get(id) || { id }));
      q('steps').querySelectorAll('tr.hl').forEach(tr => tr.classList.remove('hl'));
      rows.forEach(tr => tr.classList.add('hl'));
      rows[0]?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
    if (act.dataset.act === 'calls') {
      const owner = it.d?.agent;
      if (owner) { renderAgentSteps(q, data, m, owner, showEvent); q('stepMap').querySelector(`g[data-node="${CSS.escape(id)}"]`)?.dispatchEvent(new MouseEvent('click')); q('events').scrollIntoView({ block: 'center', behavior: 'smooth' }); }
    }
  });
  q('map').addEventListener('keydown', e => {
    const g = e.target.closest('g[data-an]');
    if (g && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); select(g.dataset.an); }
  });
  const first = m.hot[0]?.id || m.ag.decisions[0]?.id || m.ag.agents[0].id;
  select(first);
  return select;
}

// `restored`: the host put the reader's own names back on this machine (profiler-client `open`); the server never does
function renderHead(q, g, name, meta, restored = false) {
  q('restored').hidden = !restored;
  q('name').textContent = plainName(name);  // a pseudonym shows as its first 7 characters
  q('meta').textContent = [...meta, g && `dataset grade ${g.grade} (${String(g.word || '').toLowerCase()})`].filter(Boolean).join(' · ');
  q('grade').hidden = !g;
  if (g) { q('grade').dataset.g = g.grade; q('gradeLetter').textContent = g.grade; q('grade').setAttribute('aria-label', `Dataset grade ${g.grade}`); }
}

// With `declareReadOnly` ((steps, workflow) => void), a finding held out until its step is declared read-only
// gets a button that hands the host its steps and the report's workflow; without it the row says how to declare them.
// the capacity sizing the pipeline solved for this workflow (B-POP): how many LLM calls a run
// needs at peak, what each input rests on, and how the answer moves with the one it assumes
const capCalls = n => n == null ? '—' : `${Number(n).toFixed(Number.isInteger(+n) ? 0 : 1)} ${+n === 1 ? 'call' : 'calls'}`;
const times = v => `${+(+v).toFixed(2)}×`;
// each input the sizing rests on, in plain words: from the traces, or set because traces can't show it
function capInputs(f, st) {
  const used = f?.used || {}, pv = f?.defaults?.provenance || {}, src = k => pv[k]?.label === 'DERIVED' ? 'From your traces' : 'Assumed';
  const rows = [];
  if (pv.unit_cost) rows.push(['Cost of a call', src('unit_cost'), 'What this workflow’s LLM calls cost, divided by how many there were.']);
  if (pv.unit_value) rows.push(['Value of a used call', src('unit_value'), pv.unit_value.label === 'DERIVED' ? 'Given with the analysis.'
    : `Traces can’t show it, so it is set to ${used.unit_cost ? times(used.unit_value / used.unit_cost) : 'a multiple of'} the cost of a call.`]);
  if (pv.capacity_headroom) rows.push(['Headroom', src('capacity_headroom'), `At most ${times(used.capacity_headroom)} the peak; nothing in the traces measures it.`]);
  if (pv.max_demand) rows.push(['Peak calls per run', src('max_demand'), `The ${esc(used.demand_model || '')} of calls per run across ${plural(st.count, 'run')}, not the single largest run.`]);
  return rows.map(r => `<tr><td>${r[0]}</td><td data-label="Source">${r[1]}</td><td data-label="How">${r[2]}</td></tr>`).join('');
}
// the sizing at each assumed value, runs of the same size merged: "1.5× to 5× its cost"
function capSensitivity(sens) {
  const runs = [];
  for (const s of sens || []) {
    const x = Math.max(0, s.x?.x || 0), last = runs[runs.length - 1];
    if (last && last.x === x) last.to = s.ratio; else runs.push({ from: s.ratio, to: s.ratio, x });
  }
  return runs.map(r => `<tr><td>${times(r.from)}${r.to !== r.from ? ` to ${times(r.to)}` : ''} its cost</td><td class="num" data-label="Size for">${r.x ? capCalls(r.x) : 'None, not worth providing'}</td></tr>`).join('');
}
function renderCapacity(q, data) {
  const p = data.profile, f = data.formula;
  q('capacity').hidden = !p?.x;
  if (!p?.x) { q('capBody').innerHTML = ''; return; }
  const st = f?.defaults?.usage_stats || {}, model = f?.used?.demand_model || f?.defaults?.demand_model || '';
  const inputs = capInputs(f, st), sens = capSensitivity(p.sensitivity);
  const agents = (p.by_agent?.agents || []).map(a =>
    `<tr><td>${stepName(a.agent)}</td><td class="num" data-label="Peak calls per run">${capCalls(a.demand)}</td><td class="num" data-label="Size for">${capCalls(a.x)}</td></tr>`).join('');
  q('capBody').innerHTML = `
    <p>Size this workflow for <b>${capCalls(p.x.x)}</b> per run; runs used ${capCalls(p.y?.y)} at that size.
      The peak is the ${esc(model)} of ${plural(st.count, 'run')}${st.count ? ` (median ${capCalls(st.p50)}, most ${capCalls(st.max)})` : ''}.</p>
    <p class="why">This sizes capacity only. It doesn’t change any finding or the saving above.</p>
    ${agents ? `<table class="tbl"><thead><tr><th>Agent</th><th class="num">Peak calls per run</th><th class="num">Size for</th></tr></thead><tbody>${agents}</tbody></table>` : ''}
    ${inputs ? `<table class="tbl"><thead><tr><th>Input</th><th>Source</th><th>How</th></tr></thead><tbody>${inputs}</tbody></table>` : ''}
    ${sens ? `<p class="why">The value of a used call can’t be seen in traces, so here is how the size moves with it.</p>
      <table class="tbl"><thead><tr><th>If a used call is worth</th><th class="num">Size for</th></tr></thead><tbody>${sens}</tbody></table>` : ''}`;
}

const showBrand = (q, on) => { q('brand').hidden = !on; q('brandFoot').hidden = !on; };

function renderReport(root, data, { name = '', meta = [], showEvent = null, declareReadOnly = null, restored = false, brand = false } = {}) {
  root.innerHTML = SHEET;
  const q = k => root.querySelector(`[data-r="${k}"]`);
  const sm = data.summary || {}, t = sm.totals || {}, g = data.grade;
  renderHead(q, g, name, meta, restored);
  showBrand(q, brand);
  // the map, its wiring and the cards that point at it, drawn again when the reader shows all links or fewer
  DISPLAY = new Map();
  const am = data.availability?.graph?.available && data.graph?.agent_graph?.layout ? agentMapModel(data) : null;
  if (am) DISPLAY = am.display;
  let selectAgent = null;
  const drawMap = all => {
    if (am) {
      renderAgentMap(q, data, am);
      renderAgentPrint(q, data, am);
      renderHandoffs(q, am);
      selectAgent = wireAgentMap(q, data, am, showEvent);
      const onMap = new Set(data.graph.nodes.flatMap(n => [n.id, stepOf(n)]).filter(s => am.where(s)));
      renderFirst(q, data, onMap);
      return;
    }
    const marks = renderMap(q, data, all);
    if (showEvent) wireEvents(q, data, showEvent);
    wireMarks(q, data, marks);
    renderFirst(q, data, new Set([...q('map').querySelectorAll('g[data-node]')].map(g => g.dataset.node)));
  };
  renderLead(q, data);
  markEstimate(q, costEstimated(data));
  renderRisks(q, data.summary?.risk_totals, data.summary?.risk_labels,
    { found: (data.summary?.findings || []).map(f => ({ wf: '', f })), runs: data.summary?.totals?.runs });
  renderBetween(q, data);
  q('map').addEventListener('click', e => {
    const b = e.target.closest('button[data-map]');
    if (!b) return;
    drawMap(b.dataset.map === 'all');
    q('map').querySelector('button[data-map]')?.focus({ preventScroll: true });
  });
  if (showEvent) wireCopy(q);
  q('first').addEventListener('click', e => {
    const b = e.target.closest('button[data-first]');
    if (!b) return;
    const on = findingNodes(data.summary.next_steps[b.dataset.first]);
    if (am) selectAgent(on.map(am.where).find(Boolean));
    else pickOnMap(q, new Set(on));
    q('map').scrollIntoView({ block: 'center', behavior: 'smooth' });
  });
  q('totals').innerHTML = `${money(t.cost)} <span class="k">across</span> ${plural(t.runs, 'run')}<span class="k">,</span> ` +
    `${plural(t.llm_calls, 'LLM call')} <span class="k">and</span> ${plural(t.tool_calls, 'tool call')}<span class="k">,</span> ${count(t.tokens)} tokens.`;

  const workflows = data.observe?.workflows || [], wf = workflows.find(w => w.name === data.observe?.selected_workflow);
  const allRuns = workflows.reduce((n, w) => n + (w.num_runs || 0), 0);
  q('scope').innerHTML = wf && workflows.length > 1
    ? `This report covers one workflow, ${stepName(wf.name)}: ${plural(wf.num_runs, 'run')} of the ${count(allRuns)} in your dataset.` : '';
  q('scope').hidden = !(wf && workflows.length > 1);
  renderSteps(q, data, false);
  drawMap(false);
  q('steps').addEventListener('click', e => {
    const b = e.target.closest('button[data-steps]');
    if (b) renderSteps(q, data, b.dataset.steps === 'all');
  });
  let allShown = false;
  const allOpen = new Set();
  renderAll(q, data, allShown, declareReadOnly, allOpen);
  q('all').addEventListener('click', e => {
    const b = e.target.closest('button[data-all], button[data-declare], button[data-covered]');
    if (!b) return;
    if (b.dataset.declare) return declareReadOnly(data.summary.findings[b.dataset.declare].nodes, data.observe?.selected_workflow ?? null);
    if (b.dataset.covered) {
      const i = Number(b.dataset.covered);
      if (!allOpen.delete(i)) allOpen.add(i);
    } else allShown = b.dataset.all === 'all';
    renderAll(q, data, allShown, declareReadOnly, allOpen);
    if (b.dataset.covered) q('all').querySelector(`button[data-covered="${b.dataset.covered}"]`)?.focus();
  });

  renderCapacity(q, data);
  const gaps = gapsOf(data);
  q('gaps').innerHTML = gaps.map(gap => `<dt>${esc(gap.label.charAt(0).toUpperCase() + gap.label.slice(1))}</dt><dd>${esc(gap.effect || '')}</dd>`).join('');
  q('gapsSection').hidden = gaps.length === 0;
  q('notes').textContent = sm.notes || '';
}

// ---- Agent flow (issue #59): one workflow's agents, its router's decisions and handoffs as the graph builder laid
// them out, with Openly7's remarks pinned to them. Remarks come from the findings: the priced ones are exactly the
// counted findings, so they add up to the workflow's headline; the rest say what they are (also found, not priced,
// for information). Animations: traffic along each handoff by its share of runs, a typical run (the most common
// path, with average costs: the report holds no single run), and "what if", which takes each box's counted saving
// off. They stop for readers who ask for reduced motion, and never use animation frames (they stall in background tabs).

const FLOW_TABS = `
  <div class="sv-tabs" role="tablist" aria-label="Report views" data-r="tabs">
    <button type="button" role="tab" id="tab-savings" data-view="savings" aria-selected="true" aria-controls="view-savings">Savings report</button>
    <button type="button" role="tab" id="tab-flow" data-view="flow" aria-selected="false" aria-controls="view-flow" tabindex="-1">Agent flow</button>
    <button type="button" role="tab" id="tab-live" data-view="live" aria-selected="false" aria-controls="view-live" tabindex="-1">Live demo</button>
  </div>`;
const FLOW_VIEW = `
  <div class="fl-none" data-r="flowNone" hidden></div>
  <div data-r="flowBody">
    <div class="fl-bar">
      <p class="fl-sum" data-r="flowSum"></p>
      <label class="fl-pick" data-r="flowPickWrap" hidden>Workflow <select data-r="flowPick"></select></label>
    </div>
    <div class="fl-ctl">
      <button type="button" class="fl-btn" data-r="flowPlay">▶ Play a typical run</button>
      <label><input type="checkbox" data-r="flowWhatIf"> What if we apply the counted remarks</label>
      <label><input type="checkbox" data-r="flowTraffic"> Traffic</label>
      <label><input type="checkbox" data-r="flowSafe"> Share-safe: neutral labels instead of names</label>
    </div>
    <p class="fl-run" data-r="flowRun" hidden></p>
    <div class="fl-grid">
      <div class="fl-card">
        <p class="fl-hint" aria-hidden="true">Scroll sideways to see the whole flow →</p>
        <div class="fl-scroll" tabindex="0" aria-label="Agent flow diagram, scrolls sideways"><svg class="fl-svg" data-r="flowSvg" role="img" aria-labelledby="fl-title" viewBox="0 0 920 440">
          <title id="fl-title">Agent flow: the router’s decisions, the agents they hand work to, and Openly7’s numbered remarks</title>
          <g data-r="flowBase"></g><g data-r="flowTrafficLayer"></g><g data-r="flowToken"></g><g data-r="flowPins"></g>
        </svg></div>
        <p class="sv-legend fl-legend"><span><i class="fl-k hand"></i>hands work to (width = share of runs)</span><span><i class="fl-k reads"></i>a decision reads its result</span><span><i class="fl-k loop"></i>back to an earlier agent</span><span><i class="fl-k tint"></i>agent tint = its share of spend</span></p>
      </div>
      <div class="fl-card fl-side"><div class="fl-sidein">
        <h3>Openly7 remarks</h3>
        <ol class="fl-remarks" data-r="flowRemarks"></ol>
        <p class="why">The diagram holds structure, counts and costs only: no prompts, inputs or outputs. With share-safe on, it shows neutral labels (Agent A, Decision 1, Step 1) instead of names, so you can send it outside your company.</p>
      </div></div>
    </div>
  </div>`;

const reduceMotion = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;

// the workflows with an agent flow to draw: more than one agent, and a layout
const flowWorkflows = data => Object.entries(data.reports || {})
  .filter(([, r]) => (r?.graph?.agent_graph?.agents || []).length > 1 && r.graph.agent_graph.layout)
  .sort((a, b) => (b[1].summary?.totals?.cost || 0) - (a[1].summary?.totals?.cost || 0)).map(([n]) => n);

function flowModel(rep) {
  const g = rep.graph, ag = g.agent_graph, layout = ag.layout, runs = g.run_count || 0;
  const box = new Map((layout.boxes || []).map(b => [b.id, b]));
  const agents = new Map(ag.agents.map(a => [a.id, { ...a, save: 0 }]));
  const decisions = new Map((ag.decisions || []).map(d => [d.id, d]));
  // which agent a counted finding's saving belongs to: the analysis names it on the matching opportunity;
  // else the one agent whose steps hold the finding's step
  const oppAgent = new Map((rep.summary?.opportunities || []).map(o => [[o.type, o.title, (o.saving || 0).toFixed(6)].join('\u0000'), o.identity?.agent]));
  const stepAgents = step => ag.agents.filter(a => (a.step_ids || []).some(s => s === step || s.startsWith(step + ' @ '))).map(a => a.id);
  const counted = (rep.summary?.findings || []).filter(f => f.counted && f.saving);
  let unattributed = 0;
  for (const f of counted) {
    // the finding's own agent when the server names it; else its opportunity's; else the one agent with the step
    const named = f.agent || oppAgent.get([f.type, f.title, (f.saving || 0).toFixed(6)].join('\u0000'));
    const owners = named && agents.has(named) ? [named] : stepAgents((f.nodes || [f.title])[0]);
    if (owners.length === 1) agents.get(owners[0]).save += f.saving; else unattributed += f.saving;
  }
  const total = rep.summary?.totals?.cost || 0, saved = rep.summary?.replaceable?.cost || 0;
  return { ag, layout, box, runs, agents, decisions, router: ag.router && agents.get(ag.router), total, saved, unattributed,
           links: ag.links || [], frames: layout.frames || [], findings: rep.summary?.findings || [],
           opps: rep.summary?.opportunities || [], ports: flowPorts(ag.links || [], box) };
}

// where each handoff leaves its decision and reaches its agent: a box's handoffs are spread along its edge in the
// order of their other ends, so no two arrows share an end and each share label sits beside its own arrow
function flowPorts(links, box) {
  const hand = links.filter(l => l.kind === 'handoff' && box.has(l.decision) && box.has(l.to_agent));
  const mid = id => box.get(id).x + box.get(id).w / 2, ports = new Map(hand.map(l => [l, {}]));
  const spread = (key, other, end) => {
    const groups = new Map();
    for (const l of hand) groups.set(l[key], [...(groups.get(l[key]) || []), l]);
    for (const [id, ls] of groups) {
      const b = box.get(id);
      ls.sort((p, q) => mid(p[other]) - mid(q[other]));
      ls.forEach((l, i) => { ports.get(l)[end] = ls.length === 1 ? mid(id) : b.x + b.w * (i + 1) / (ls.length + 1); });
    }
  };
  spread('decision', 'to_agent', 'x1'); spread('to_agent', 'decision', 'x2');
  return ports;
}

// the remarks: priced ones (the counted findings, by where they sit) first, then the rest
function flowRemarks(m) {
  const out = [], top = id => { const b = m.box.get(id); return b ? [b.x + b.w - 8, b.y + 8] : [16, 16]; };
  const r = m.router;
  if (r) {
    const share = m.total ? r.cost / m.total : 0;
    if (r.save > 1e-9) out.push({ at: [m.box.get(r.id)?.x + m.box.get(r.id)?.w - 14 || 890, 28], on: [r.id], save: r.save,
      title: `The router takes ${share >= 0.01 ? Math.round(share * 100) + '%' : '<1%'} of the spend`,
      text: n => `Calls of ${n(r.id)} made the same choice for the same input every time. A rule could make them.` });
  }
  const others = [...m.agents.values()].filter(a => a.id !== m.ag.router && a.save > 1e-9).sort((a, b) => b.save - a.save);
  if (others.length) {
    const sum = others.reduce((s, a) => s + a.save, 0);
    out.push({ at: top(others[0].id), on: others.map(a => a.id), save: sum,
      title: others.length > 1 ? 'The agents’ own routing calls repeat too' : `${others[0].id}’s calls repeat`,
      text: n => `${andList(others.map(a => `${n(a.id)} ${money(a.save)}`))}: their LLM calls pick the same next step for the same input.` });
  }
  if (m.unattributed > 1e-9) out.push({ at: [16, 16], on: [], save: m.unattributed, title: 'Counted on steps several agents share',
    text: () => 'Repeated choices on steps more than one agent runs; this report doesn’t say which agent’s calls they are.' });
  // not priced: the same data fetched by several agents, findings that add nothing, rare but costly agents, loops back
  for (const o of m.opps.filter(o => o.type === 'CROSS_AGENT_DUPLICATE' && (o.agents || []).length > 1).slice(0, 2))
    out.push({ at: top(o.agents[0]), on: o.agents.filter(a => m.box.has(a)), save: null, note: 'Not priced yet',
      title: 'Agents fetch the same data', text: n => `${andList(o.agents.map(n))} ${o.agents.length === 2 ? 'both' : 'all'} call ${n(o.title)} in the same run. Fetch it once and pass it in the handoff.` });
  const also = m.findings.filter(f => !f.counted && f.saving && (f.covered_by != null || f.held_out === 'overlap')).sort((a, b) => b.saving - a.saving)[0];
  if (also) {
    const at = (also.nodes || []).map(s => [...m.decisions.keys()].find(d => d === s)).find(Boolean);
    out.push({ at: at ? top(at) : [16, 16], on: at ? [at] : [], save: null, note: 'Adds nothing to the total',
      title: TITLE[also.type] || 'Also found', text: n => `${n(also.title)} saves ${money(also.saving)} on its own, but its calls are already counted above.` });
  }
  const perRun = a => a.runs ? a.cost / a.runs : 0;
  const typical = [...m.agents.values()].map(perRun).filter(Boolean).sort((a, b) => a - b);
  const median = typical[Math.floor(typical.length / 2)] || 0;
  for (const a of m.agents.values()) {
    if (a.id !== m.ag.router && m.runs && a.runs <= m.runs / 4 && median && perRun(a) >= 3 * median)
      out.push({ at: top(a.id), on: [a.id], save: null, note: 'For information', title: 'Rare but costly',
        text: n => `${n(a.id)} ran in ${a.runs} of ${m.runs} runs, at about ${money(perRun(a))} a run: ${Math.round(perRun(a) / median)}× the typical agent’s.` });
  }
  for (const l of m.links.filter(l => l.kind === 'direct' && l.mode === 'return'))
    out.push({ at: top(l.from_agent), on: [l.from_agent, l.to_agent], save: null, note: 'For information', title: 'A loop back',
      text: n => `${n(l.from_agent)} handed work back to ${n(l.to_agent)} in ${plural(l.loop?.runs ?? l.runs, 'run')}. Nothing to save; shown so the path isn’t a surprise.` });
  return out;
}

// a curve from the bottom of one box to the top of another, and a loop for a hand back
const flowCurve = (a, b, p = {}) => { const x1 = p.x1 ?? a.x + a.w / 2, y1 = a.y + a.h, x2 = p.x2 ?? b.x + b.w / 2, y2 = b.y;
  return `M${x1} ${y1} C ${x1} ${y1 + 45}, ${x2} ${y2 - 45}, ${x2} ${y2 - 2}`; };
const flowLoop = (a, b) => `M${a.x + 8} ${a.y + 10} C ${a.x - 26} ${a.y - 20}, ${b.x - 26} ${b.y + b.h + 20}, ${b.x + 8} ${b.y + b.h - 4}`;

// the most common path: from the router's first decision, each decision's busiest handoff (and the agents it
// starts together), skipping decisions that read only agents the path didn't reach. Costs are averages.
function flowTypicalRun(m) {
  const decs = [...m.decisions.values()].sort((a, b) => (m.box.get(a.id)?.x ?? 0) - (m.box.get(b.id)?.x ?? 0));
  const seen = new Set(m.router ? [m.router.id] : []), steps = [];  // the run starts at the router
  const routerOwn = m.router ? Math.max(0, m.router.cost - decs.reduce((s, d) => s + (d.cost || 0), 0)) / (m.runs || 1) : 0;
  if (m.router) steps.push([[m.router.id], routerOwn / 2]);
  for (const d of decs) {
    const reads = m.links.filter(l => l.kind === 'reads' && l.decision === d.id).map(l => l.from_agent);
    if (reads.length && !reads.some(a => seen.has(a))) continue;
    const hands = m.links.filter(l => l.kind === 'handoff' && l.decision === d.id && m.agents.has(l.to_agent));
    if (!hands.length) continue;
    const best = Math.max(...hands.map(l => l.share || 0));
    const to = hands.filter(l => (l.share || 0) === best || (l.mode === 'both' && (l.share || 0) >= best - 1e-9)).map(l => l.to_agent);
    steps.push([[d.id], (d.cost || 0) / (d.calls || 1)]);
    steps.push([to, to.reduce((s, a) => s + (m.agents.get(a).cost / (m.agents.get(a).runs || 1)), 0)]);
    to.forEach(a => seen.add(a));
  }
  if (m.router) steps.push([[m.router.id], routerOwn / 2]);
  return steps;
}

// a box's name, never cut: on one line, or on two split at a separator nearest the middle; a line still too long is
// drawn narrower to fit (textLength). The full name is also the box's tooltip.
// characters a line holds (12px bold, about 6.6px a character), less the room of a remark pin in the box's corner
const flowFit = (b, pinned) => Math.max(6, Math.floor((b.w - (pinned ? 36 : 20)) / 6.6));
function flowSplit(t, n) {
  if (t.length <= n) return [t];
  let best = -1;
  for (let i = 1; i < t.length - 1; i++) {
    if (!/[_\-\s.\/]/.test(t[i - 1]) || /\s/.test(t[i])) continue;  // break after a separator
    if (best < 0 || Math.abs(i - t.length / 2) < Math.abs(best - t.length / 2)) best = i;
  }
  const at = best > 0 ? best : Math.min(n, Math.ceil(t.length / 2));
  return [t.slice(0, at).trimEnd(), t.slice(at).trimStart()];
}
const flowLines = (t, b, pinned) => flowSplit(t, flowFit(b, pinned)).length;
function flowName(t, b, pad, pinned) {
  const n = flowFit(b, pinned), lines = flowSplit(t, n), y = lines.length > 1 ? [17, 31] : [22];
  return `<title>${esc(t)}</title>` + lines.map((l, i) => `<text class="t1" x="${b.x + pad}" y="${b.y + y[i]}"` +
    (l.length > n ? ` textLength="${b.w - pad - (pinned ? 26 : 10)}" lengthAdjust="spacingAndGlyphs"` : '') + `>${esc(l)}</text>`).join('');
}

function flowBase(m, s) {
  const lab = id => esc(s.label(id));
  // a box with a remark's pin in its top right corner: its name keeps clear of the pin
  const pinned = b => s.remarks.some(r => r.at[0] >= b.x + b.w - 30 && r.at[0] <= b.x + b.w + 6 && r.at[1] >= b.y - 6 && r.at[1] <= b.y + 20);
  const on = new Set(s.active >= 0 ? s.remarks[s.active].on : []), cls = (base, id) => base + (on.has(id) ? ' hl' : '') + (s.live.has(id) ? ' is-live' : '');
  const parts = ['<defs><marker id="fl-arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" class="fl-arrow"></path></marker></defs>'];
  for (const f of m.frames) parts.push(`<rect class="fl-frame" x="${f.x}" y="${f.y}" width="${f.w}" height="${f.h}" rx="10"></rect><text class="fl-flabel" x="${f.x + 6}" y="${f.y + f.h - 6}">start together</text>`);
  for (const l of m.links) {
    if (l.kind === 'reads' && m.box.has(l.from_agent) && m.box.has(l.decision)) {
      const a = m.box.get(l.from_agent), d = m.box.get(l.decision);
      parts.push(`<path class="fl-reads" d="M${a.x + a.w / 2 + 18} ${a.y} C ${a.x + a.w / 2 + 18} ${a.y - 50}, ${d.x + d.w / 2 + 18} ${d.y + d.h + 50}, ${d.x + d.w / 2 + 18} ${d.y + d.h}"></path>`);
    } else if (l.kind === 'handoff' && m.box.has(l.decision) && m.box.has(l.to_agent)) {
      const d = m.box.get(l.decision), a = m.box.get(l.to_agent), sh = l.share || 0, p = m.ports.get(l) || {};
      parts.push(`<path class="fl-hand" stroke-width="${(1 + sh * 3.5).toFixed(2)}" marker-end="url(#fl-arr)" d="${flowCurve(d, a, p)}"></path>`);
      // the share sits just under the decision, on the side its arrow does not bend towards
      const x1 = p.x1 ?? d.x + d.w / 2, x2 = p.x2 ?? a.x + a.w / 2, right = x2 < x1;
      if (sh < 1) parts.push(`<text class="fl-share" x="${x1 + (right ? 5 : -5)}" y="${d.y + d.h + 13}" text-anchor="${right ? 'start' : 'end'}">${Math.round(sh * 100)}%</text>`);
    } else if (l.kind === 'direct' && m.box.has(l.from_agent) && m.box.has(l.to_agent)) {
      parts.push(`<path class="fl-back" marker-end="url(#fl-arr)" d="${flowLoop(m.box.get(l.from_agent), m.box.get(l.to_agent))}"></path>`);
    }
  }
  const rb = m.router && m.box.get(m.router.id);
  if (rb) {
    const c = m.router.cost - m.router.save * s.whatIf;
    parts.push(`<g class="${cls('fl-router', m.router.id)}"><rect x="${rb.x}" y="${rb.y}" width="${rb.w}" height="${rb.h}" rx="6"></rect>` +
      `<text x="${rb.x + 10}" y="${rb.y + 16}">${lab(m.router.id)} · router · ${money(c)}${s.whatIf && m.router.save ? ` (rules save ${money(m.router.save * s.whatIf)})` : ` · ${share(m.router.cost, m.total)} of spend`}</text></g>`);
  }
  for (const d of m.decisions.values()) {
    const b = m.box.get(d.id); if (!b) continue;
    parts.push(`<g class="${cls('fl-dec', d.id)}"><rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="8"></rect>` +
      flowName(s.label(d.id), b, 8, pinned(b)) + `<text class="t2" x="${b.x + 8}" y="${b.y + (flowLines(s.label(d.id), b, pinned(b)) > 1 ? 48 : 42)}">${plural(d.calls, 'call')}</text>` +
      `<text class="t2" x="${b.x + 8}" y="${b.y + (flowLines(s.label(d.id), b, pinned(b)) > 1 ? 63 : 58)}">${money(d.cost)}</text></g>`);
  }
  for (const a of m.agents.values()) {
    const b = m.box.get(a.id); if (!b || a.id === m.ag.router) continue;
    const cut = a.save * s.whatIf, h = a.cost ? b.h * (cut / a.cost) : 0;
    parts.push(`<g class="${cls('fl-agent', a.id)}"><rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="10" class="fl-bg"></rect>` +
      `<rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="10" class="fl-tint" fill-opacity="${(0.04 + (a.share || 0) * 1.4).toFixed(3)}"></rect>` +
      (h > 0.5 ? `<rect x="${b.x + 1}" y="${(b.y + b.h - h - 1).toFixed(1)}" width="${b.w - 2}" height="${h.toFixed(1)}" rx="6" class="fl-saved"></rect>` : '') +
      `<rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="10" class="fl-edge"></rect>` +
      flowName(s.label(a.id), b, 10, pinned(b)) + `<text class="t2" x="${b.x + 10}" y="${b.y + (flowLines(s.label(a.id), b, pinned(b)) > 1 ? 48 : 42)}">${a.runs} of ${m.runs} runs</text>` +
      `<text class="t2${cut ? ' t-save' : ''}" x="${b.x + 10}" y="${b.y + (flowLines(s.label(a.id), b, pinned(b)) > 1 ? 63 : 58)}">${cut ? `${money(a.cost - cut)} · −${money(cut)}` : `${money(a.cost)} · ${share(a.cost, m.total)}`}</text></g>`);
  }
  return parts.join('');
}

function flowTraffic(m) {
  const dots = [];
  const along = (d, n, dur, cls) => { for (let i = 0; i < n; i++)
    dots.push(`<circle r="3.2" class="${cls}"><animateMotion dur="${dur}s" repeatCount="indefinite" begin="${(-i * dur / n).toFixed(2)}s" path="${d}"></animateMotion></circle>`); };
  for (const l of m.links) {
    if (l.kind === 'handoff' && m.box.has(l.decision) && m.box.has(l.to_agent))
      along(flowCurve(m.box.get(l.decision), m.box.get(l.to_agent), m.ports.get(l)), Math.max(1, Math.round((l.share || 0) * 4)), 3.2, 'fl-dot');
    if (l.kind === 'direct' && l.mode === 'return' && m.box.has(l.from_agent) && m.box.has(l.to_agent))
      along(flowLoop(m.box.get(l.from_agent), m.box.get(l.to_agent)), 1, 4, 'fl-dot loop');
  }
  return dots.join('');
}

function renderFlow(q, data) {
  const names = flowWorkflows(data);
  q('flowNone').hidden = names.length > 0;
  q('flowBody').hidden = !names.length;
  if (!names.length) {
    q('flowNone').innerHTML = '<p>No agent flow to draw: each workflow in this dataset has one agent, so nothing hands work to anything else. Open a workflow from the savings report to see its steps.</p>';
    return null;
  }
  const s = { active: -1, whatIf: 0, live: new Set(), traffic: !reduceMotion(), remarks: [], label: x => x, timer: null, run: null };
  let m = null;
  // share-safe: neutral labels instead of names (the router, Agent A.., Decision 1.., Step 1..), so a shared diagram
  // holds no name and no pseudonym anyone could match; labels follow the layout, left to right
  let safeLabels = new Map();
  const safeFor = id => {
    if (!safeLabels.has(id)) safeLabels.set(id, `Step ${[...safeLabels.values()].filter(v => v.startsWith('Step')).length + 1}`);
    return safeLabels.get(id);
  };
  const shortName = id => q('flowSafe').checked ? safeFor(id) : plainName(id);
  const draw = () => {
    s.label = shortName;
    q('flowBase').innerHTML = flowBase(m, s);
    const n = id => `<code>${esc(s.label(id))}</code>`;
    q('flowRemarks').innerHTML = s.remarks.map((r, i) => `<li data-i="${i}" class="${i === s.active ? 'on' : ''}">` +
      `<button type="button" class="fl-n${r.save == null ? ' plain' : ''}" data-i="${i}" aria-pressed="${i === s.active}">${i + 1}</button>` +
      `<span><b>${esc(r.title)}</b> ${r.text(n)}<br>${r.save != null ? `<span class="fl-sv">${money(r.save)}</span> <span class="pill ${TIER.medium[0]}">counted</span>` : `<span class="note">${esc(r.note || '')}</span>`}</span></li>`).join('');
    q('flowPins').innerHTML = s.remarks.map((r, i) => `<g class="fl-pin${r.save == null ? ' plain' : ''}${i === s.active ? ' on' : ''}"><circle cx="${r.at[0]}" cy="${r.at[1]}" r="11"></circle>` +
      `<text x="${r.at[0]}" y="${r.at[1] + 4}" text-anchor="middle">${i + 1}</text></g>`).join('');
    const now = m.total - m.saved * s.whatIf;
    q('flowSum').innerHTML = s.whatIf ? `<b>${money(now)}</b> after the counted remarks (was ${money(m.total)}, −${share(m.saved * s.whatIf, m.total)})`
      : `<b>${money(m.total)}</b> spent · ${money(m.saved)} (${share(m.saved, m.total)}) could be saved` +
        (m.saved ? ` <span class="pill ${(m.findings.some(f => f.counted && f.risk === 'low') ? TIER.low : TIER.medium)[0]}">${m.findings.every(f => !f.counted || f.risk !== 'low') ? 'All review first' : 'Partly ready'}</span>` : '');
  };
  const load = wf => {
    stopRun();
    m = flowModel(data.reports[wf]);
    const byX = ids => ids.sort((a, b) => (m.box.get(a)?.x ?? 0) - (m.box.get(b)?.x ?? 0) || (m.box.get(a)?.y ?? 0) - (m.box.get(b)?.y ?? 0));
    safeLabels = new Map([
      ...(m.router ? [[m.router.id, 'Router']] : []),
      ...byX([...m.agents.keys()].filter(id => id !== m.ag.router)).map((id, i) => [id, `Agent ${String.fromCharCode(65 + i % 26)}${i >= 26 ? Math.floor(i / 26) : ''}`]),
      ...byX([...m.decisions.keys()]).map((id, i) => [id, `Decision ${i + 1}`]),
    ]);
    s.remarks = flowRemarks(m); s.active = -1; s.whatIf = 0; q('flowWhatIf').checked = false;
    const L = m.layout; q('flowSvg').setAttribute('viewBox', `0 0 ${L.width || 920} ${L.height || 440}`);
    q('flowTrafficLayer').innerHTML = s.traffic ? flowTraffic(m) : '';
    draw();
  };
  // what if: each box's counted saving comes off over a second, on timers
  const tween = to => {
    clearTimeout(s.timer);
    const from = s.whatIf, t0 = Date.now(), ms = reduceMotion() ? 0 : 900;
    const step = () => { const k = ms ? Math.min(1, (Date.now() - t0) / ms) : 1; s.whatIf = from + (to - from) * (1 - Math.pow(1 - k, 3)); draw();
      if (k < 1) s.timer = setTimeout(step, 16); };
    step();
  };
  const stopRun = () => { clearTimeout(s.run); s.run = null; s.live = new Set(); q('flowToken').innerHTML = ''; q('flowRun').hidden = true;
    q('flowPlay').textContent = '▶ Play a typical run'; if (m) draw(); };
  const playRun = () => {
    stopRun();
    const steps = flowTypicalRun(m); let i = 0, cost = 0;
    q('flowRun').hidden = false; q('flowPlay').textContent = '■ Stop';
    const next = () => {
      if (i >= steps.length) { q('flowRun').textContent = `A typical run: ${steps.length} steps, about ${money(cost)}. The most common path, with average costs; not a recorded run.`;
        s.live = new Set(); draw(); s.run = setTimeout(stopRun, 2600); return; }
      const [ids, c] = steps[i]; cost += c;
      const cs = ids.map(id => m.box.get(id)).filter(Boolean).map(b => [b.x + b.w / 2, b.y + b.h / 2]);
      const [x, y] = [cs.reduce((a, p) => a + p[0], 0) / cs.length, cs.reduce((a, p) => a + p[1], 0) / cs.length];
      q('flowToken').innerHTML = `<circle class="fl-token" r="7" cx="${x}" cy="${y}"></circle>`;
      s.live = new Set(ids); draw();
      q('flowRun').textContent = `A typical run, step ${i + 1} of ${steps.length}: ${ids.map(id => s.label(id)).join(' + ')} · about ${money(cost)} so far`;
      i++; s.run = setTimeout(next, reduceMotion() ? 1400 : 1100);
    };
    next();
  };
  const select = i => { s.active = i === s.active ? -1 : i; draw(); };
  q('flowRemarks').addEventListener('click', e => { const b = e.target.closest('[data-i]'); if (b) select(+b.dataset.i); });
  q('flowPlay').addEventListener('click', () => s.run ? stopRun() : playRun());
  q('flowWhatIf').addEventListener('change', e => tween(e.target.checked ? 1 : 0));
  q('flowTraffic').checked = s.traffic;
  q('flowTraffic').addEventListener('change', e => { s.traffic = e.target.checked; q('flowTrafficLayer').innerHTML = s.traffic ? flowTraffic(m) : ''; });
  q('flowSafe').addEventListener('change', draw);
  q('flowPickWrap').hidden = names.length < 2;
  q('flowPick').innerHTML = names.map(n => `<option value="${esc(n)}">${esc(plainName(n))}</option>`).join('');
  q('flowPick').addEventListener('change', e => load(e.target.value));
  load(names[0]);
  return { model: () => m, remarks: () => s.remarks, typicalRun: () => flowTypicalRun(m), stop: stopRun };
}

// the overview's two views, as tabs: click, arrow keys, Home and End; one tab in the tab order
function wireTabs(q, onShow) {
  const order = ['savings', 'flow', 'live'];
  const views = { savings: q('viewSavings'), flow: q('viewFlow'), live: q('viewLive') };
  const tabs = Object.fromEntries(order.map(v => [v, q('tabs').querySelector(`[data-view="${v}"]`)]));
  const show = v => { for (const k of order) { const on = k === v; tabs[k].setAttribute('aria-selected', String(on)); tabs[k].tabIndex = on ? 0 : -1; views[k].hidden = !on; } onShow(v); };
  q('tabs').addEventListener('click', e => { const b = e.target.closest('[data-view]'); if (b) show(b.dataset.view); });
  q('tabs').addEventListener('keydown', e => {
    const at = order.findIndex(k => tabs[k] === e.target); if (at < 0) return;
    const to = { ArrowRight: at + 1, ArrowLeft: at - 1, Home: 0, End: order.length - 1 }[e.key]; if (to == null) return;
    e.preventDefault(); const v = order[(to + order.length) % order.length]; tabs[v].focus(); show(v);
  });
  show('savings');
  return show;
}

// ---- Live demo (issue #59): a trading-terminal view that simulates calls with this report's own numbers. Nothing
// streams: each workflow's calls are its counted findings (their calls and saving per call) plus one group for the
// rest of its calls and spend, drawn in proportion to the calls, so the simulated spend mix and saveable share are
// the report's. A seeded generator makes it repeatable. It pauses while its tab or the page is hidden, updates its
// tiles in place so keyboard focus stays, and runs on timers.

const LIVE_VIEW = `
  <p class="lv-note">A demo: it <b>simulates</b> calls with this report’s own numbers. Nothing streams anywhere. A real live view needs a client that sends events as they happen.</p>
  <div class="lv-term" data-r="liveTerm">
    <div class="lv-top">
      <span class="lv-name">Openly<b>7</b> · Agent Spend Exchange</span>
      <span class="lv-live"><i aria-hidden="true"></i>LIVE DEMO</span>
      <span class="lv-dim" data-r="liveClock">09:00:00</span>
      <span class="lv-ctl"><button type="button" data-r="livePlay">❚❚ Pause</button><button type="button" data-r="liveSpeed">Speed 20×</button><button type="button" data-r="liveReset">Restart</button></span>
    </div>
    <div class="lv-kpis">
      <div class="lv-kpi"><div class="lv-krow"><span class="lv-l">Spend rate</span><span class="lv-badge" data-r="liveRateChg">–</span></div>
        <div class="lv-v"><span data-r="liveRate">0.000</span><span class="lv-u"> $/min</span></div>
        <svg class="lv-spark" data-r="liveSparkRate" viewBox="0 0 100 28" preserveAspectRatio="none" aria-hidden="true"></svg><div class="lv-c lv-dim">spend per minute, last 30 minutes</div></div>
      <div class="lv-kpi"><div class="lv-krow"><span class="lv-l">Spent</span><span class="lv-badge dim" data-r="liveCalls">0 calls</span></div>
        <div class="lv-v" data-r="liveSpend">$0.00</div>
        <svg class="lv-spark" data-r="liveSparkSpend" viewBox="0 0 100 28" preserveAspectRatio="none" aria-hidden="true"></svg><div class="lv-c lv-dim" data-r="liveAvg">$0.0000 per call</div></div>
      <div class="lv-kpi"><div class="lv-krow"><span class="lv-l">Saveable</span><span class="lv-badge save" data-r="liveShare">0% of spend</span></div>
        <div class="lv-v lv-save" data-r="liveSave">$0.00</div>
        <div class="lv-meter" data-r="liveMeter" role="img" aria-label="Saveable share of spend"><span data-r="liveMeterFill"></span><i data-r="liveMeterMark"></i></div>
        <div class="lv-c lv-dim" data-r="liveMeterNote"></div></div>
      <div class="lv-kpi"><div class="lv-krow"><span class="lv-l">Repeats caught</span><span class="lv-badge amber" data-r="liveRepRate">0 / min</span></div>
        <div class="lv-v lv-amber" data-r="liveReps">0</div>
        <svg class="lv-spark amber" data-r="liveSparkReps" viewBox="0 0 100 28" preserveAspectRatio="none" aria-hidden="true"></svg><div class="lv-c lv-dim">calls a rule, lookup or cache could take</div></div>
    </div>
    <div class="lv-hhead"><span>Workflows · tint = share of spend that’s saveable · ▲▼ = its last call’s cost against the one before</span><span>select a tile to chart it</span></div>
    <div class="lv-heat" data-r="liveHeat"></div>
    <div class="lv-grid">
      <div class="lv-panel"><h3><span data-r="liveChartTitle">All workflows · spend per minute</span><span>1 candle = 1 simulated minute</span></h3>
        <canvas class="lv-canvas" data-r="liveChart" aria-label="Candles of spend per simulated minute, red when a minute cost more than the one before and green when it cost less, with the saveable part of each minute as a blue line"></canvas></div>
      <div class="lv-panel"><h3><span>Call tape</span><span>saveable in blue</span></h3><ul class="lv-tape" data-r="liveTape"></ul></div>
    </div>
    <div class="lv-foot"><span><span class="lv-up">▲ Red candle</span>: this minute cost more than the one before</span><span><span class="lv-down">▼ Green candle</span>: it cost less</span><span><span class="lv-save">■ Blue line</span>: the part of each minute’s spend that could be saved</span></div>
  </div>`;

// a seeded generator (xorshift32), so a run of the demo, and its tests, can be repeated exactly
const seeded = seed => { let x = (seed | 0) || 1; return () => { x ^= x << 13; x ^= x >>> 17; x ^= x << 5; return (x >>> 0) / 4294967296; }; };
// what a counted finding's calls would become, as the savings report names it
const liveTag = f => f.type === 'CONDITIONAL_LLM_BYPASS' || f.type === 'PARTIAL_LLM_BYPASS' ? (/lookup/i.test(f.action || '') ? 'lookup could decide' : 'rule could decide')
  : f.type === 'CACHE_RESULT' ? 'repeat: reuse' : f.type === 'CROSS_AGENT_DUPLICATE' ? 'fetch once' : 'saveable';
// a workflow's ticker: its words' initials, then the rest of its last word (pull-request-review: PRREV)
const tickerOf = (name, used) => {
  const words = plainName(name).replace(/^h:/, '').toUpperCase().split(/[^A-Z0-9]+/).filter(Boolean);
  let t = (words.length > 1 ? words.map(w => w[0]).join('') + words[words.length - 1].slice(1) : words.join('')).slice(0, 5) || 'WF';
  for (let i = 2; used.has(t); i++) t = t.slice(0, 4) + i;
  used.add(t); return t;
};

// each workflow: its spend, its calls, and the groups calls are drawn from
function liveModel(data) {
  const used = new Set(), out = [];
  for (const row of data.overview?.workflows || []) {
    const rep = data.reports?.[row.name]; if (!rep) continue;
    const t = rep.summary?.totals || {}, spend = row.spend || t.cost || 0;
    const calls = (t.llm_calls || 0) + (t.tool_calls || 0) || row.runs || 0;
    if (!spend || !calls) continue;
    const saveable = (rep.summary?.findings || []).filter(f => f.counted && f.saving > 0 && f.observations > 0)
      .map(f => ({ name: f.title, calls: f.observations, cost: f.saving / f.observations, saveable: true, tag: liveTag(f) }));
    const savedCalls = saveable.reduce((s, x) => s + x.calls, 0), saved = saveable.reduce((s, x) => s + x.calls * x.cost, 0);
    const rest = Math.max(1, calls - savedCalls);
    const names = new Set(saveable.map(x => x.name));
    const shown = Object.values(rep.mine?.by_agent || {}).flatMap(b => b.actions || []).filter(a => !names.has(a.node) && a.occurrences > 0);
    out.push({ name: row.name, sym: tickerOf(row.name, used), spend, saved, calls,
               groups: [...saveable, { name: null, calls: rest, cost: Math.max(0, spend - saved) / rest, saveable: false, tag: '' }],
               shown: shown.length ? shown : [{ node: 'call', occurrences: 1 }] });
  }
  return out;
}

// one simulated call: a workflow by its calls, a group by its calls, a cost around the group's average
function liveCall(model, rand) {
  const pick = (items, w) => { let r = rand() * items.reduce((s, x) => s + w(x), 0); for (const x of items) { r -= w(x); if (r <= 0) return x; } return items[items.length - 1]; };
  const wf = pick(model, w => w.calls), g = pick(wf.groups, x => x.calls);
  const step = g.name || pick(wf.shown, a => a.occurrences).node;
  const cost = g.cost * (0.8 + rand() * 0.4);
  return { wf, step, cost, saving: g.saveable ? cost : 0, tag: g.tag };
}

// n calls with a seed: each workflow's spend and saveable spend, for checking the mix against the report
function liveSimulate(data, n, seed = 1) {
  const model = liveModel(data), rand = seeded(seed), by = Object.fromEntries(model.map(w => [w.name, { spend: 0, save: 0, calls: 0 }]));
  const tags = new Set();
  for (let i = 0; i < n; i++) { const c = liveCall(model, rand); by[c.wf.name].spend += c.cost; by[c.wf.name].save += c.saving; by[c.wf.name].calls++; if (c.tag && c.saving) tags.add(`${c.step} · ${c.tag}`); }
  return { workflows: by, tags: [...tags] };
}

const liveSpark = vals => {
  if (vals.length < 2) return '';
  const lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
  const pts = vals.map((v, i) => [i / (vals.length - 1) * 100, 26 - (v - lo) / span * 22]);
  const line = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
  const [ex, ey] = pts[pts.length - 1];
  return `<path class="f" d="${line} L100 28 L0 28 Z"></path><path class="l" d="${line}"></path><circle cx="${ex}" cy="${ey}" r="1.8"></circle>`;
};
const clockOf = s => [s / 3600 % 24, s / 60 % 60, s % 60].map(x => String(Math.floor(x)).padStart(2, '0')).join(':');

function renderLive(q, data, { visible = () => true } = {}) {
  const model = liveModel(data);
  if (!model.length) { q('liveTerm').innerHTML = '<p class="lv-empty">No live demo: this report records no cost or no calls to simulate.</p>'; return null; }
  const target = data.overview?.totals?.spend ? (data.overview.totals.replaceable || 0) / data.overview.totals.spend : 0;
  const scale = Math.max(0.1, Math.ceil(target * 2 * 10) / 10);  // the meter runs 0 to about twice the report's share
  q('liveMeterMark').style.left = `${Math.min(100, target / scale * 100)}%`;
  q('liveMeterNote').textContent = `Scale 0–${Math.round(scale * 100)}% · mark: the report’s ${share(target, 1) || '0%'}`;
  let s, rand, speed = 20, playing = true, timer = null, focus = 'all';
  const reset = () => {
    rand = seeded(7);
    s = { sim: 9 * 3600, spend: 0, save: 0, calls: 0, reps: 0, candles: { all: [] }, by: {} };
    for (const w of model) { s.by[w.sym] = { spend: 0, save: 0, calls: 0, last: 0, prev: 0, hist: [] }; s.candles[w.sym] = []; }
    q('liveTape').innerHTML = '';
  };
  const candle = (key, minute) => {
    const c = s.candles[key];
    if (!c.length || c[c.length - 1].m !== minute) { const prev = c.length ? c[c.length - 1].close : 0;
      c.push({ m: minute, open: prev, high: prev, low: prev, close: prev, cost: 0, save: 0, reps: 0 }); if (c.length > 36) c.shift(); }
    return c[c.length - 1];
  };
  const tape = [];
  const emit = () => {
    const c = liveCall(model, rand), b = s.by[c.wf.sym];
    b.prev = b.last || c.cost; b.last = c.cost; b.spend += c.cost; b.save += c.saving; b.calls++;
    s.spend += c.cost; s.save += c.saving; s.calls++; if (c.saving) s.reps++;
    for (const key of ['all', c.wf.sym]) {
      const k = candle(key, Math.floor(s.sim / 60));
      k.cost += c.cost; k.save += c.saving; if (c.saving) k.reps++;
      k.close = k.cost; k.high = Math.max(k.high, k.close); k.low = Math.min(k.low || k.close, k.close);
    }
    tape.unshift(`<li class="${c.saving ? 'is-save' : ''}"><span class="lv-dim">${clockOf(s.sim)}</span><span class="lv-sym">${c.wf.sym}</span>` +
      `<span class="lv-st">${stepName(c.step)}${c.saving ? ` <span class="lv-save">· ${c.tag}</span>` : ''}</span><span class="${c.saving ? 'lv-save' : ''}">$${c.cost.toFixed(4)}</span></li>`);
    tape.length = Math.min(tape.length, 40);
  };
  // tiles are made once and updated in place, so keyboard focus on one survives every update
  q('liveHeat').dataset.n = String(Math.min(3, model.length));  // one or two workflows: their tiles fill the row
  q('liveHeat').innerHTML = model.map(w => `<button type="button" class="lv-tile" data-sym="${w.sym}"><span class="lv-tint" aria-hidden="true"></span>` +
    `<span class="lv-th"><span class="lv-sym">${w.sym}</span><span class="lv-chg"></span></span><span class="lv-nm">${stepName(w.name)}</span>` +
    `<span class="lv-big"><span class="lv-pct">0%</span> <span class="lv-dim">saveable</span></span>` +
    `<svg class="lv-spark" viewBox="0 0 100 28" preserveAspectRatio="none" aria-hidden="true"></svg><span class="lv-meta"><span class="lv-sp"></span><span class="lv-save lv-sv"></span></span></button>`).join('');
  const tiles = new Map(model.map(w => [w.sym, q('liveHeat').querySelector(`[data-sym="${w.sym}"]`)]));
  const chg = b => b.prev ? (b.last - b.prev) / b.prev * 100 : 0;
  const arrow = v => v > 0 ? `▲${Math.abs(v).toFixed(1)}%` : v < 0 ? `▼${Math.abs(v).toFixed(1)}%` : '·';
  const paint = () => {
    q('liveClock').textContent = clockOf(s.sim);
    const c = s.candles.all, prev = c[c.length - 2], before = c[c.length - 3];
    q('liveRate').textContent = (prev ? prev.cost : 0).toFixed(3);
    if (prev && before?.cost) { const v = (prev.cost - before.cost) / before.cost * 100;
      q('liveRateChg').textContent = (v >= 0 ? '▲ +' : '▼ ') + v.toFixed(1) + '%'; q('liveRateChg').className = 'lv-badge ' + (v >= 0 ? 'up' : 'down'); }
    const done = c.slice(0, -1).slice(-30); let run = 0;
    q('liveSparkRate').innerHTML = liveSpark(done.map(x => x.cost));
    q('liveSparkSpend').innerHTML = liveSpark(done.map(x => (run += x.cost)));
    q('liveSparkReps').innerHTML = liveSpark(done.map(x => x.reps));
    q('liveRepRate').textContent = `${prev ? prev.reps : 0} / min`;
    q('liveSpend').textContent = '$' + s.spend.toFixed(2);
    q('liveCalls').textContent = plural(s.calls, 'call');
    q('liveAvg').textContent = '$' + (s.calls ? s.spend / s.calls : 0).toFixed(4) + ' per call';
    const sh = s.spend ? s.save / s.spend : 0;
    q('liveSave').textContent = '$' + s.save.toFixed(2);
    q('liveShare').textContent = `${Math.round(sh * 100)}% of spend`;
    q('liveMeterFill').style.width = `${Math.min(100, sh / scale * 100)}%`;
    q('liveMeter').setAttribute('aria-label', `Saveable share of spend: ${Math.round(sh * 100)}%`);
    q('liveReps').textContent = count(s.reps);
    for (const w of model) {
      const b = s.by[w.sym], t = tiles.get(w.sym), p = b.spend ? b.save / b.spend : 0, v = chg(b);
      if (!t) continue;
      t.querySelector('.lv-tint').style.opacity = Math.min(0.4, 0.05 + p * 0.45).toFixed(3);
      t.classList.toggle('sel', focus === w.sym);
      t.setAttribute('aria-label', `${plainName(w.name)}: ${Math.round(p * 100)}% saveable`);
      const e = t.querySelector('.lv-chg'); e.className = 'lv-chg ' + (v > 0 ? 'lv-up' : v < 0 ? 'lv-down' : 'lv-dim'); e.textContent = arrow(v);
      t.querySelector('.lv-pct').textContent = `${Math.round(p * 100)}%`;
      t.querySelector('svg').innerHTML = liveSpark((s.candles[w.sym] || []).slice(0, -1).slice(-20).map(x => x.cost));
      t.querySelector('.lv-sp').textContent = `$${b.spend.toFixed(2)} spent`;
      t.querySelector('.lv-sv').textContent = `$${b.save.toFixed(2)}`;
    }
    q('liveTape').innerHTML = tape.join('');
    drawChart();
  };
  const drawChart = () => {
    const cv = q('liveChart'), ctx = cv.getContext?.('2d'); if (!ctx) return;
    const box = cv.getBoundingClientRect?.(); const w = box?.width || 0, h = 230; if (!w) return;
    const d = globalThis.devicePixelRatio || 1; cv.width = w * d; cv.height = h * d; ctx.setTransform(d, 0, 0, d, 0, 0);
    const css = getComputedStyle(cv), v = n => css.getPropertyValue(n).trim();
    const data = s.candles[focus] || [], padR = 58, top = 12, base = h - 22;
    const max = Math.max(0.001, ...data.map(c => c.high)) * 1.1, y = val => base - (base - top) * val / max;
    ctx.clearRect(0, 0, w, h); ctx.font = '11px ' + (v('--mono') || 'monospace'); ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) { const val = max * i / 4, yy = y(val); ctx.strokeStyle = v('--lv-line'); ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(w - padR, yy); ctx.stroke();
      ctx.fillStyle = v('--lv-dim'); ctx.fillText('$' + val.toFixed(3), w - padR + 6, yy + 4); }
    const slot = (w - padR) / 36, bw = Math.max(3, slot * 0.6);
    data.forEach((c, i) => { const x = i * slot + slot / 2; ctx.strokeStyle = ctx.fillStyle = c.close >= c.open ? v('--lv-up') : v('--lv-down');
      ctx.beginPath(); ctx.moveTo(x, y(c.high)); ctx.lineTo(x, y(c.low)); ctx.stroke();
      const yo = y(c.open), yc = y(c.close); ctx.fillRect(x - bw / 2, Math.min(yo, yc), bw, Math.max(1, Math.abs(yc - yo))); });
    ctx.strokeStyle = v('--lv-save'); ctx.lineWidth = 2; ctx.beginPath();
    data.forEach((c, i) => { const x = i * slot + slot / 2; i ? ctx.lineTo(x, y(c.save)) : ctx.moveTo(x, y(c.save)); }); ctx.stroke();
  };
  // a tick: simulated seconds pass at the chosen speed; nothing runs while the tab or the page is hidden
  let last = Date.now();
  const tick = () => {
    const now = Date.now(), dt = Math.min(0.25, (now - last) / 1000); last = now;
    const hidden = typeof document !== 'undefined' && document.hidden;
    if (playing && visible() && !hidden) {
      const before = s.sim; s.sim += dt * speed;
      const n = Math.floor(s.sim / 1.4) - Math.floor(before / 1.4);
      for (let i = 0; i < n; i++) emit();
      if (n) paint();
    }
    timer = setTimeout(tick, 100);
  };
  const warm = () => { for (let i = 0; i < 900; i++) { s.sim += 1.4; emit(); } paint(); };
  q('livePlay').addEventListener('click', () => { playing = !playing; q('livePlay').textContent = playing ? '❚❚ Pause' : '▶ Play'; });
  q('liveSpeed').addEventListener('click', () => { speed = speed === 20 ? 120 : speed === 120 ? 5 : 20; q('liveSpeed').textContent = `Speed ${speed}×`; });
  q('liveReset').addEventListener('click', () => { reset(); warm(); });
  q('liveHeat').addEventListener('click', e => { const t = e.target.closest('[data-sym]'); if (!t) return;
    focus = focus === t.dataset.sym ? 'all' : t.dataset.sym;
    q('liveChartTitle').textContent = focus === 'all' ? 'All workflows · spend per minute' : `${focus} · spend per minute`; paint(); });
  reset(); warm();
  // the candles are drawn on a canvas in the theme's colours: draw them again when the theme changes
  if (typeof document !== 'undefined' && document.addEventListener) document.addEventListener(THEME_EVENT, drawChart);
  return { start() { clearTimeout(timer); last = Date.now(); tick(); }, stop() { clearTimeout(timer); timer = null; }, state: () => s, model };
}

// The savings overview: what the dataset could save, what to do first, and where. Every figure is a finding the
// server's analysis counts in its headline (each call counted once); "Also found" findings are shown but never added.
const OVERVIEW = HEAD + FLOW_TABS + `
  <div id="view-savings" role="tabpanel" aria-labelledby="tab-savings" data-r="viewSavings">
  <div class="sv-hero">
    <div class="sv-card">
      <p class="sv-big" data-r="svBig"></p>
      <p class="sv-lead" data-r="svLead"></p>
      <p class="why estimated" data-r="svEstimate" hidden></p>
      <div class="sv-stack" data-r="svStack" role="img"></div>
      <p class="sv-legend" data-r="svLegend"></p>
      <p class="why" data-r="svInvestigate" hidden></p>
      <p class="why sv-wait" data-r="svWaiting" hidden></p>
    </div>
    <div class="sv-card sv-proj" data-r="svProj">
      <label for="sv-runs">Your runs per month</label>
      <input id="sv-runs" data-r="svRuns" type="number" min="0" step="1000" inputmode="numeric" placeholder="for example 100000">
      <p class="sv-out" data-r="svOut"></p>
      <p data-r="svOutReady"></p>
      <p class="why" data-r="svNote"></p>
    </div>
  </div>
  <section data-r="svActionsSec">
    <h3>Do these first</h3>
    <ol class="sv-actions" data-r="svActions"></ol>
    <p class="why" data-r="svActionsNote"></p>
    <div data-r="svAlso"></div>
  </section>
  <section>
    <h3>Where the savings are</h3>
    <p class="why">The workflows with the most spend a rule, a script or a cache could replace come first. Open one to see its map and every finding.</p>
    <p class="keys" aria-hidden="true"><span class="key"><b></b>Spend</span><span class="key"><b class="rep"></b>Could be replaced</span></p>
    <table class="tbl wfs" data-r="workflows">
      <thead><tr><th>Workflow</th><th class="barh"><span class="sr">Spend that could be replaced</span></th><th class="num">Spend</th><th class="num">Could be replaced</th><th>Findings</th></tr></thead>
      <tbody></tbody>
    </table>
    <p class="why">Each call belongs to one workflow, so the rows add up to the total above.</p>
  </section>
  <details class="sv-more" open>
    <summary>Details for engineers</summary>
    <ul class="risks" data-r="risks" hidden aria-label="Findings by risk"></ul>
    <p class="totals" data-r="totals"></p>
  </details>
  </div>
  <div id="view-flow" role="tabpanel" aria-labelledby="tab-flow" data-r="viewFlow" hidden>` + FLOW_VIEW + `
  </div>
  <div id="view-live" role="tabpanel" aria-labelledby="tab-live" data-r="viewLive" hidden>` + LIVE_VIEW + `
  </div>` + FOOT;

// a risk in words, never color alone
const TIER = { low: ['ready', 'Ready · low risk'], medium: ['review', 'Review first'], high: ['invest', 'Investigate'] };
// how to act on each kind of finding
const HOW = {
  CONDITIONAL_LLM_BYPASS: ['Write the rule or lookup the finding shows: which input gives which choice.',
    'Replay it against past calls, and keep the model as a fallback for inputs it hasn’t seen.'],
  PARTIAL_LLM_BYPASS: ['Write the rule for the inputs whose choice never changes.', 'Send every other input to the model, and replay against past calls first.'],
  CACHE_RESULT: ['Reuse the first result for the same input.', 'Check that the step only reads before switching the cache on.'],
  EXTRACT_DETERMINISTIC_SUBSTEP: ['Say which of these steps change something, so the report can tell whether a script is safe.',
    'Script the steps in order and replay it against past runs.'],
  CROSS_AGENT_DUPLICATE: ['Fetch the result once in the run.', 'Pass it to the other agent in the handoff.'],
};
const LLM_AT = /^llm_call ← (.+)$/;
const andList = xs => xs.length < 2 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`;
const TOP_ACTIONS = 3, ALSO_SHOWN = 2;

// The overview's figures from the result: totals and risk tiers from `overview`, actions from each workflow's
// counted findings. One step's LLM calls, split by what came before them (llm_call ← X), are one action.
function savingsModel(data) {
  const o = data.overview || {}, t = o.totals || {}, rt = o.risk_totals;
  const runs = (o.workflows || []).reduce((n, r) => n + (r.runs || 0), 0);
  const groups = new Map(), also = [], waiting = new Map();
  for (const [wf, rep] of Object.entries(data.reports || {})) {
    const fs = rep?.summary?.findings || [];
    const countedSteps = new Set(fs.filter(f => f.counted).flatMap(f => f.nodes || []));
    for (const f of fs) {
      if (!f.saving) continue;
      // held out only until its step is declared read-only, on steps no counted finding touches: a counted rule
      // on the same step may already save these calls (covered_by misses one that saves less), so only there is
      // the saving what one answer adds. A high-risk one needs its cause found first, so it isn't offered here
      if (!f.counted && f.held_out === 'declaration' && f.covered_by == null && f.risk !== 'high' &&
          !(f.nodes || []).some(n => countedSteps.has(n))) {
        const step = (f.nodes || [])[0] ?? f.title, key = `${wf} ${step}`;
        if (!waiting.has(key)) waiting.set(key, { wf, step, saving: 0 });
        waiting.get(key).saving += f.saving;
        continue;
      }
      // not counted because other findings already count its calls: shown, never added
      if (!f.counted) { if (f.covered_by != null || f.held_out === 'overlap') also.push({ wf, f }); continue; }
      const at = LLM_AT.exec(f.title || '');
      const key = [wf, f.type, f.risk, at ? '@llm' : f.title].join('\u0000');
      if (!groups.has(key)) groups.set(key, { wf, type: f.type, risk: f.risk, action: f.action, title: at ? null : f.title,
                                              places: [], saving: 0, observations: 0, reasons: [] });
      const g = groups.get(key);
      g.saving += f.saving;
      g.observations += f.observations || 0;
      if (at) g.places.push(at[1]);
      for (const r of f.risk_reasons || []) if (!g.reasons.includes(r)) g.reasons.push(r);
    }
  }
  // ready (low risk) first, then review first, each by saving: "do these first" starts with what's safe to do
  const rank = r => ({ low: 0, medium: 1, high: 2 }[r] ?? 1);
  const actions = [...groups.values()].sort((a, b) => rank(a.risk) - rank(b.risk) || b.saving - a.saving || a.wf.localeCompare(b.wf));
  also.sort((a, b) => b.f.saving - a.f.saving);
  return { spend: t.spend || 0, saved: t.replaceable || 0, runs, actions, also,
           waiting: [...waiting.values()].sort((a, b) => b.saving - a.saving),
           tiers: rt ? { ready: rt.low?.saving || 0, review: rt.medium?.saving || 0, investigate: rt.high?.count || 0 } : null };
}

// one rounding for every figure: whole dollars once the total reaches $100, else cents; a total shown with its
// parts is the sum of the rounded parts, so the page always adds up
const projector = (total, n) => {
  const whole = total * n >= 100;
  const rnd = v => whole ? Math.round(v) : Math.round(v * 100) / 100;
  return { rnd, usd: v => '$' + (whole ? v.toLocaleString('en-US') : v.toFixed(2)) };
};
const perRunMoney = v => v >= 0.01 ? money(v) : `$${v.toFixed(4)}`;

function renderSavingsHead(q, m) {
  const what = 'a rule, a script or a cache could replace';
  q('svStack').hidden = q('svLegend').hidden = !m.spend || !m.saved;
  if (!m.spend) {
    q('svBig').textContent = '';
    q('svLead').textContent = `No cost is recorded in this dataset, so the report can’t say how much of it went to calls ${what}. Give token prices to estimate it.`;
    return;
  }
  q('svBig').innerHTML = `${money(m.saved)} <small>of ${money(m.spend)} could be saved</small>`;
  if (!m.saved) { q('svLead').textContent = `The report found no calls in this dataset that ${what}.`; return; }
  q('svLead').innerHTML = `<b>${share(m.saved, m.spend)}</b> of what these agents spent went to calls ${what}.`;
  const pct = v => (100 * v / m.spend).toFixed(2) + '%';
  const needed = `<span><i class="needed"></i>${money(Math.max(0, m.spend - m.saved))} needed work</span>`;
  if (m.tiers) {
    const { ready, review } = m.tiers;
    q('svStack').innerHTML = `<i class="ready" style="width:${pct(ready)}"></i><i class="review" style="width:${pct(review)}"></i>`;
    q('svStack').setAttribute('aria-label', `Spend: ${money(ready)} ready to save, ${money(review)} to review first, ${money(m.spend - m.saved)} needed`);
    q('svLegend').innerHTML = `<span><i class="ready"></i><b>${money(ready)}</b> ready to save · low risk</span>` +
      `<span><i class="review"></i><b>${money(review)}</b> review first</span>${needed}`;
    q('svInvestigate').hidden = !m.tiers.investigate;
    q('svInvestigate').innerHTML = m.tiers.investigate ? `<span class="pill invest">Investigate</span> Plus ${plural(m.tiers.investigate, 'finding')} to investigate: results that change for the same input. They aren’t priced until the cause is known.` : '';
  } else {  // a result from before risk tiers: one bar for all of it
    q('svInvestigate').hidden = true;
    q('svStack').innerHTML = `<i class="saved" style="width:${pct(m.saved)}"></i>`;
    q('svStack').setAttribute('aria-label', `Spend: ${money(m.saved)} could be saved, ${money(m.spend - m.saved)} needed`);
    q('svLegend').innerHTML = `<span><i class="saved"></i><b>${money(m.saved)}</b> could be saved</span>${needed}`;
  }
}

// the top actions, each with its saving per run or, with a volume, per month; returns the rounded monthly figures
function renderActions(q, m, n) {
  const top = m.actions.slice(0, TOP_ACTIONS);
  const p = projector(m.runs ? m.saved / m.runs : 0, n);
  const monthly = top.map(a => n && m.runs ? p.rnd(a.saving / m.runs * n) : null);
  q('svActionsSec').hidden = !top.length;
  q('svActions').innerHTML = top.map((a, i) => {
    const [cls, word] = TIER[a.risk] || TIER.medium;
    const places = a.places.map(x => x === 'user_prompt' ? 'the prompt' : `<code>${stepName(x)}</code>`);
    const where = places.length ? `the LLM call after ${andList(places)}${places.length > 1 ? ` (${places.length} places)` : ''}`
      : `step <code>${stepName(a.title)}</code>`;
    const why = a.reasons.filter(r => !/not recorded/.test(r)).slice(0, 2).join('; ') || `${plural(a.observations, 'call')} behind it`;
    const steps = HOW[a.type] || [a.action];
    return `<li class="sv-act"><span class="rank">${i + 1}</span>
      <div><h4>${esc(TITLE[a.type] || plainName(a.action || ''))}</h4><p class="where">${stepName(a.wf)} · ${where}</p>
        <p class="why"><span class="pill ${cls}">${word}</span> ${esc(why.charAt(0).toUpperCase() + why.slice(1))}.</p></div>
      <p class="money"><b>${money(a.saving)}</b><span>${monthly[i] != null ? `≈ ${p.usd(monthly[i])} / month` : `${perRunMoney(a.saving / (m.runs || 1))} per run`}</span></p>
      <details><summary>How to do it</summary><ol>${steps.map(s => `<li>${esc(s)}</li>`).join('')}</ol></details></li>`;
  }).join('');
  const sum = top.reduce((s, a) => s + a.saving, 0);
  const allReady = m.tiers && top.length && top.every(a => a.risk === 'low') && Math.abs(sum - m.tiers.ready) < 1e-6;
  q('svActionsNote').textContent = top.length ? `Savings never overlap: each call is counted once, under one action. ` +
    `${top.length === 1 ? 'This action is' : `These ${top.length} actions are`} ${money(sum)} of the ${money(m.saved)}${allReady ? ', all of the ready saving' : ''}.` : '';
  q('svAlso').innerHTML = m.also.slice(0, ALSO_SHOWN).map(({ wf, f }) =>
    `<p class="sv-also"><b>Also found, adds nothing to the total:</b> ${esc(TITLE[f.type] || plainName(f.action || ''))} in ${stepName(wf)} (<code>${stepName(f.title)}</code>). ` +
    `${money(f.saving)} on its own, but its calls are already counted above.</p>`).join('');
  return { monthly, allReady };
}

// the projection: the observed saving per run times the reader's volume; nothing is replayed
function renderProjection(q, m, n) {
  const shown = renderActions(q, m, n);
  q('svProj').hidden = !m.runs || !m.saved;
  const perRun = m.runs ? m.saved / m.runs : 0;
  q('svNote').textContent = `A projection: the saving observed per run (${perRunMoney(perRun)}) times your volume. Not a forecast, and nothing has been replayed yet.`;
  if (!n) {
    q('svOut').textContent = `${perRunMoney(perRun)} per run`;
    q('svOutReady').textContent = 'Type your runs per month to see a monthly figure.';
    return;
  }
  const p = projector(perRun, n);
  const ready = shown.allReady ? shown.monthly.reduce((s, v) => s + v, 0) : p.rnd(m.tiers ? m.tiers.ready / m.runs * n : 0);
  const rest = p.rnd((m.saved - (m.tiers ? m.tiers.ready : 0)) / m.runs * n);
  q('svOut').textContent = `${p.usd(p.rnd(ready + rest))} / month`;
  q('svOutReady').textContent = m.tiers ? `could be saved · ${p.usd(p.rnd(ready))} of it ready now` : 'could be saved';
}

// savings one answer would add: steps whose repeats count once someone says they only read. "Up to": the
// analysis picks again once they're declared, so it may keep less than the sum
function renderWaiting(q, m) {
  const total = m.waiting.reduce((n, w) => n + w.saving, 0), many = new Set(m.waiting.map(w => w.wf)).size > 1;
  q('svWaiting').hidden = !m.spend || !total;
  if (q('svWaiting').hidden) { q('svWaiting').innerHTML = ''; return; }
  const steps = m.waiting.map(w => `${stepName(w.step)}${many ? ` (${stepName(w.wf)})` : ''}`);
  const one = m.waiting.length === 1;
  q('svWaiting').innerHTML = `<span class="pill review">One answer away</span> <b>Up to ${money(total)} more</b> if ` +
    `${one ? 'this step only reads' : `these ${count(m.waiting.length)} steps only read`}: ${andList(steps)}. ` +
    `${one ? 'It isn’t' : 'They aren’t'} counted above until you declare ${one ? 'it' : 'them'} read-only (Tell us about the agent), then analyze again.`;
}

function renderSavings(q, data) {
  const m = savingsModel(data);
  renderSavingsHead(q, m);
  renderWaiting(q, m);
  const runs = q('svRuns');
  const update = () => renderProjection(q, m, Math.max(0, +runs.value || 0));
  runs.addEventListener('input', update);
  update();
  return m;
}

function renderOverviewRows(q, o, reports) {
  const rows = o.workflows || [], labels = o.risk_labels || {};
  const agents = r => Object.keys(reports?.[r.name]?.mine?.by_agent || {}).length;
  const max = Math.max(0, ...rows.map(r => r.spend || 0));
  const w = v => max ? 100 * (v || 0) / max : 0;
  q('workflows').querySelector('tbody').innerHTML = rows.map(r => `<tr>
      <td><button class="link open-wf" type="button" data-wf="${esc(r.name)}">${stepName(r.name)}</button><span class="note">${plural(r.runs, 'run')}${agents(r) > 1 ? ` · ${plural(agents(r), 'agent')}` : ''}</span></td>
      <td class="barcell"><div class="spendbar"><i style="width:${w(r.spend)}%"></i><i class="rep" style="width:${w(r.replaceable)}%"></i></div></td>
      <td class="num" data-label="Spend">${money(r.spend)}</td>
      <td class="num" data-label="Could be replaced">${money(r.replaceable)}<span class="note">${r.spend ? share(r.replaceable || 0, r.spend) : ''}</span></td>
      <td data-label="Findings">${count(r.findings)}${r.highest_risk ? `<span class="note">${esc(labels[r.highest_risk] || r.highest_risk)}</span>` : ''}</td></tr>`).join('');
}

function renderOverview(root, data, { name = '', meta = [], showEvent = null, declareReadOnly = null, workflow = null, restored = false, brand = false } = {}) {
  const o = data.overview;
  // redraw `root`, keeping what the host added: its buttons in .tools and anything after the sheet
  let own = new Set();
  const redraw = draw => {
    const tools = [...(root.querySelector('.tools')?.childNodes || [])];
    const after = [...root.childNodes].filter(n => n.nodeType === 1 && !own.has(n));
    draw();
    own = new Set(root.childNodes);
    root.querySelector('.tools').append(...tools);
    root.append(...after);
  };
  const overview = (from = null) => redraw(() => {
    root.innerHTML = OVERVIEW;
    const q = k => root.querySelector(`[data-r="${k}"]`);
    renderHead(q, data.grade, name, meta, restored);
    showBrand(q, brand);
    renderSavings(q, data);
    const reports = Object.values(data.reports || {});
    if (reports.some(costEstimated)) {  // under the savings figure, where it's read
      // the spend leaves out calls with no price for their model; say how many
      const unpriced = reports.reduce((n, r) => n + (gapsOf(r).find(g => g.key === 'cost')?.unpriced || 0), 0);
      q('svEstimate').hidden = false;
      q('svEstimate').textContent = ESTIMATED + (unpriced ? ` ${plural(unpriced, 'call')} ${unpriced === 1 ? 'has' : 'have'} no price, so the spend leaves ${unpriced === 1 ? 'it' : 'them'} out.` : '');
    }
    const runs = (o.workflows || []).reduce((n, r) => n + (r.runs || 0), 0);
    const found = Object.entries(data.reports || {}).flatMap(([wf, r]) => (r?.summary?.findings || []).map(f => ({ wf, f })));
    renderRisks(q, o.risk_totals, o.risk_labels, { found, runs });
    q('totals').innerHTML = `${money(o.totals?.spend)} <span class="k">across</span> ${plural(runs, 'run')} <span class="k">in</span> ${plural((o.workflows || []).length, 'workflow')}.`;
    renderOverviewRows(q, o, data.reports);
    const flow = renderFlow(q, data);
    const live = renderLive(q, data, { visible: () => !q('viewLive').hidden });
    wireTabs(q, v => { if (v !== 'flow') flow?.stop(); if (v === 'live') live?.start(); else live?.stop(); });
    q('workflows').addEventListener('click', e => {
      const b = e.target.closest('button[data-wf]');
      if (b) show(b.dataset.wf);
    });
    if (from != null) [...root.querySelectorAll('button[data-wf]')].find(b => b.dataset.wf === from)?.focus();
  });
  const show = wf => {
    if (!data.reports[wf]) return;
    redraw(() => {
      renderReport(root, data.reports[wf], { name: wf, meta: [plainName(name), ...meta].filter(Boolean), showEvent, declareReadOnly, restored, brand });
      const back = root.querySelector('[data-r="back"]');
      back.hidden = false;
      back.addEventListener('click', () => overview(wf));
    });
    root.querySelector('[data-r="name"]').focus({ preventScroll: true });
    root.scrollIntoView({ block: 'start' });
  };
  if (workflow && data.reports[workflow]) show(workflow); else overview();
  return { show, overview: () => overview() };
}

// A report with your names put back from a name map (`profiler-client names`), in this browser: the same
// swap `profiler-client open` makes on your machine, so a report looks the same on both. Names come only from
// the map's datasets that this report was made from, so another dataset's names never land in it. Nothing is
// sent anywhere: this only reads the map it is given. Returns { data, restored, unmatched, unlisted } or, for a
// map of other datasets or not a name map, { error }.
const NAME_MAP = 'openly7-name-map';
const PSEUDONYM = /h:[0-9a-f]{64}/g;
function restoreNames(data, map) {
  if (!map || map.format !== NAME_MAP || map.version !== 1 || typeof map.names !== 'object' || !Array.isArray(map.datasets))
    return { error: 'This file isn’t a name map. Make one with: profiler-client names <report.json>' };
  const hashes = Array.isArray(data?.payload_sha256s) && data.payload_sha256s.length ? data.payload_sha256s
    : typeof data?.payload_sha256 === 'string' ? [data.payload_sha256] : [];
  const listed = hashes.filter(h => map.datasets.includes(h));
  if (!listed.length) return { error: 'This name map is for another dataset, so no names were restored.' };
  const names = new Map(Object.entries(map.names).filter(([k, v]) => /^h:[0-9a-f]{64}$/.test(k) && typeof v === 'string'));
  const found = new Set(), missing = new Set();
  const swap = s => s.replace(PSEUDONYM, t => { if (names.has(t)) { found.add(t); return names.get(t); } missing.add(t); return t; });
  const walk = v => typeof v === 'string' ? swap(v)
    : Array.isArray(v) ? v.map(walk)
      : v && typeof v === 'object' ? Object.fromEntries(Object.entries(v).map(([k, x]) => [swap(k), walk(x)])) : v;
  return { data: walk(data), restored: found.size, unmatched: missing.size, unlisted: hashes.filter(h => !listed.includes(h)) };
}

function render(root, data, opts = {}) {
  if (data && data.overview && data.reports) return renderOverview(root, data, opts);
  renderReport(root, data, opts);
  return { show() {}, overview() {} };
}

// the event static/theme.js sends when the theme changes (the live demo redraws its canvas in the new colours)
const THEME_EVENT = 'openly7-theme';

window.ProfilerReport = { render, restoreNames, liveSimulate };
})();
