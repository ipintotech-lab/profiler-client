/* The AI Agent Profiler report: one renderer for the web view and for `profiler-client open`.
 *
 * ProfilerReport.render(root, data, { name, meta }) draws a report JSON into `root` (an element with
 * class "sheet"); `meta` is a list of short lines for under the title. It reads nothing but `data`,
 * makes no request, and writes only inside `root`. The host page adds its own buttons to `.tools`.
 *
 * A dataset overview (`data.overview` with `data.reports`, one report per workflow) draws a list of the
 * workflows first; a row opens that workflow's report from `data.reports` and a back button returns.
 * The host's buttons in `.tools`, and anything it appended to `root`, move along. render returns { show(workflow), overview() } to switch by code.
 */
(function () {
'use strict';
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const money = v => v == null ? '—' : v === 0 ? '$0' : `$${v.toFixed(v < 1 ? 3 : 2)}`;
const count = n => Number(n || 0).toLocaleString();
const plural = (n, word) => `${count(n)} ${word}${n === 1 ? '' : 's'}`;
const share = (part, whole) => whole ? `${Math.round(100 * part / whole)}%` : '';

const KIND = {
  CACHE_RESULT: 'Same call, same input, same result',
  EXTRACT_DETERMINISTIC_SUBSTEP: 'Same steps in the same order',
  CONDITIONAL_LLM_BYPASS: 'An LLM choice that never changes',
  REVIEW_HIDDEN_STATE: 'Same request, different results',
  CROSS_AGENT_DUPLICATE: 'Same call by more than one agent in a run',
};
// "Do these first": a finding's plain title, and what the next step is called
const TITLE = {
  CACHE_RESULT: 'Reuse a repeated result',
  CONDITIONAL_LLM_BYPASS: 'Replace an LLM decision with a rule',
  EXTRACT_DETERMINISTIC_SUBSTEP: 'Script a fixed sequence',
  DETERMINISTIC_REGION: 'Skip the LLM for one slice of inputs',
  REVIEW_HIDDEN_STATE: 'Unstable result: find the cause',
  CROSS_AGENT_DUPLICATE: 'Fetch once, share between agents',
};
const STEP = { replay: 'Replay', review: 'Review', declare: 'Declare', investigate: 'Investigate' };
const EVIDENCE = {
  CACHE_RESULT: n => `${plural(n, 'call')} had the same input and gave the same result.`,
  CONDITIONAL_LLM_BYPASS: n => `The LLM made the same choice in all ${plural(n, 'call')}.`,
  EXTRACT_DETERMINISTIC_SUBSTEP: n => `These steps ran in this order ${plural(n, 'time')}.`,
  DETERMINISTIC_REGION: n => `${plural(n, 'call')} fall in this slice of inputs.`,
  REVIEW_HIDDEN_STATE: n => `Across ${plural(n, 'call')}, the same request gave different results.`,
  CROSS_AGENT_DUPLICATE: n => `In ${plural(n, 'run')}, more than one agent made this call with the same input and got the same result.`,
};
// what the map marks on a step a finding is about: each has its own words, never colour alone
const MARK = {
  unstable: { badge: 'unstable result', legend: 'the same request gave different results' },
  replace: { badge: 'rule or script', legend: 'LLM call a rule or script could replace' },
  repeats: { badge: 'repeats', legend: 'tool call that repeats with the same input and result' },
  shared: { badge: 'shared', legend: 'tool call more than one agent makes in a run, with the same input and result' },
};
// the agents a finding says repeat a call between them within a run
const sharedBy = o => o.type === 'CROSS_AGENT_DUPLICATE' ? o.agents || [] : o.between_agents?.agents || [];
const LLM_AFTER = 'llm_call ← ';
// the map steps a finding is about; scripting a sequence removes the LLM calls that chose its next steps
const findingNodes = o => o.type === 'EXTRACT_DETERMINISTIC_SUBSTEP'
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
    (String(s.node).startsWith('llm_call') ? ' Prepare the dataset with <code>profiler-client prepare --llm-inputs</code> to record a pseudonym of what each LLM call read.' : '')
  : `Only ${count(s.evidence?.repeated_observations || 0)} of its ${plural(s.occurrences, 'call')} had an input seen on another call. ` +
    'Send more runs of this workflow, so the same inputs come up again and the report can tell whether this step repeats.';

// every pseudonym (h:…) in a name or a sentence shows as its first 7 characters; the whole text is in the tooltip
const HASH = /h:([0-9a-f]{7})[0-9a-f]{8,}/g;  // global: used only with .replace, which ignores lastIndex
const HAS_HASH = /h:[0-9a-f]{15,}/;  // non-global: safe for .test (no lastIndex state to leak between calls)
const shortHashes = text => esc(text).replace(HASH, '<span class="h">$1</span>');
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
const MAP_MIN_SCALE = 0.8;  // a map never shrinks below this to fit its box: a wider one scrolls sideways inside it
const MAP_END = { '@prompt': 'Prompt', '@reply': 'Reply' };
const plainName = n => String(n).replace(HASH, 'h:$1');
const runPct = x => `${Math.round(100 * x)}%`;
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
  const fit = (t, k) => t.length > k ? `${t.slice(0, k - 1)}…` : t;
  const label = id => { const step = byId.has(id) ? stepOf(byId.get(id)) : id; return MAP_END[id] ? MAP_END[id].toLowerCase() : letters.get(step) || plainName(step); };
  const lines = n => {
    if (n.kind === 'llm_call') {
      const after = String(stepOf(n)).replace(/^llm_call ← /, '');
      return ['LLM call', `after ${fit(after === 'user_prompt' ? 'prompt' : label(after), 15)}`, `${count(n.occurrences)} · ${money(n.cost)}`];
    }
    return letters.has(stepOf(n)) ? [letters.get(stepOf(n)), fit(plainName(stepOf(n)), 20), plural(n.occurrences, 'call')]
      : [fit(plainName(stepOf(n)), 18), '', plural(n.occurrences, 'call')];
  };

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
      return `<path class="edge handoff${e.loop ? ' return' : ''}" d="M${x1},${y1} C${x1},${mid} ${x2},${mid} ${x2},${y2}" stroke-width="${w.toFixed(1)}" marker-end="url(#mapArrow)">` +
        `<title>${esc(plainName(from))} ${e.loop ? 'hands back' : 'hands over'} to ${esc(plainName(to))}: ${esc(label(e.from))} → ${esc(label(e.to))} in ${esc(plural(e.distinct_run_count, 'run'))}${tag ? `; ${esc(tag)}` : ''}</title></path>` +
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
    return `<path class="edge${isHot ? ' hot' : ''}" d="${d}" stroke-width="${w.toFixed(1)}" marker-end="url(#${isHot ? 'mapArrowHot' : 'mapArrow'})">` +
      `<title>${esc(label(e.from))} → ${esc(label(e.to))}: ${esc(plural(e.transition_count, 'time'))} in ${esc(plural(e.distinct_run_count, 'run'))}${splits(e) ? `, ${share(e)} of what left ${esc(label(e.from))}` : ''}</title></path>` +
      `<text class="edge-n" x="${lx}" y="${ly}" text-anchor="${anchor}">${esc(count(e.transition_count))}${splits(e) ? ` · ${share(e)}` : ''}</text>`;
  });
  const loops = new Map(links.filter(e => e.from === e.to).map(e => [e.from, e.transition_count]));
  const nodes = shown.filter(n => at.has(n.id)).map(n => {
    const b = box(n.id);
    if (MAP_END[n.id]) return `<g class="node end"><rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="22"/><text x="${b.cx}" y="${b.cy + 5}" text-anchor="middle">${MAP_END[n.id]}</text></g>`;
    const [t, s1, s2] = lines(n), loop = loops.get(n.id);
    const kinds = Object.keys(MARK).filter(k => marks.get(n.id)?.kinds.has(k));
    const cls = `node${n.kind === 'llm_call' ? ' llm' : ''}${hot && n.id === hot.id ? ' hot' : ''}${kinds.map(k => ` ${k}`).join('')}`;
    let bx = b.x + 8;
    const badges = kinds.map(k => {
      const w = 16 + 6.2 * MARK[k].badge.length, x = bx;
      bx += w + 6;
      return `<g class="badge ${k}"><rect x="${x}" y="${b.y - 10}" width="${w}" height="20" rx="10"/><text x="${x + w / 2}" y="${b.y + 4}" text-anchor="middle">${esc(MARK[k].badge)}</text></g>`;
    });
    return `<g class="${cls}" data-node="${esc(n.id)}" data-step="${esc(stepOf(n))}"${kinds.length ? ` data-marks="${kinds.map(k => MARK[k].badge).join(', ')}"` : ''}><title>${esc(n.step ? `${n.step} (${n.agent})` : n.id)}</title><rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="6"/>` +
      `<text class="t" x="${b.x + 14}" y="${b.y + 26}">${esc(t)}</text>` +
      (s1 ? `<text class="s" x="${b.x + 14}" y="${b.y + 44}">${esc(s1)}</text>` : '') +
      `<text class="s" x="${b.x + 14}" y="${b.y + (s1 ? 59 : 50)}">${esc(s2)}${loop ? ` · ↻ ${esc(count(loop))}` : ''}</text>${badges.join('')}</g>`;
  });
  const left = graph.edges.length - links.length, unlinked = graph.nodes.filter(n => !joined.has(n.id)).length;
  const more = left ? `<p class="more">${esc(`This map shows the ${count(links.length)} most-taken of ${plural(graph.edges.length, 'link')}. ${plural(left, 'link')}${unlinked ? ` and ${plural(unlinked, 'step')}` : ''} ${left + unlinked === 1 ? 'is' : 'are'} hidden to keep it readable; every step is in the table below.`)} ` +
      `<button type="button" class="link" data-map="all">Show all ${count(graph.edges.length)} links</button></p>`
    : all && graph.edges.length > MAP_LINKS ? `<p class="more">Showing all ${count(graph.edges.length)} links. <button type="button" class="link" data-map="less">Show only the most-taken</button></p>` : '';
  const present = Object.keys(MARK).filter(k => [...marks.values()].some(m => m.kinds.has(k)));
  q('map').innerHTML = (present.length ? `<p class="legend marks">${present.map(k => `<span><i class="pill ${k}">${esc(MARK[k].badge)}</i>${esc(MARK[k].legend)}</span>`).join('')}` +
    '<span>Select a marked step to see its rows in the table</span></p>' : '') + `<div class="mapscroll"><svg viewBox="0 0 ${width} ${height}" style="min-width:${Math.round(width * MAP_MIN_SCALE)}px" role="img" aria-label="${esc(`Workflow map: ${plural(shown.length, 'step')}${banded ? ` in ${plural(bands.length, 'agent')}` : ''} and ${plural(paths.length, 'link')} across ${plural(graph.run_count, 'run')}`)}">` +
    `<defs><marker id="mapArrow" ${ARROW}><path class="arrow" d="M0,0 L10,5 L0,10 z"/></marker>` +
    `<marker id="mapArrowHot" ${ARROW}><path class="arrow hot" d="M0,0 L10,5 L0,10 z"/></marker></defs>` +
    bandRects.join('') + paths.join('') + nodes.join('') + '</svg></div>' + more +
    '<p class="legend kinds"><span><i class="sw llm"></i>LLM call (has a cost)</span><span><i class="sw tool"></i>Tool call</span>' +
    (hot && total ? `<span><i class="sw hot"></i>Largest share of spend (${Math.round(100 * hot.cost / total)}%)</span>` : '') + '</p>';

  const name = id => {
    const n = byId.get(id), base = MAP_END[id] || (n?.kind === 'llm_call' ? lines(n).slice(0, 2).join(' ') : label(id));
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
    `Arrow width is how many times a hop happened across ${plural(graph.run_count, 'run')}; where a step goes more than one way, the % is that way's share. Links come from the trace: an LLM call choosing a tool, the tool whose result the next LLM call read, the prompt that started a run, and the LLM call that replied. Where a trace doesn't record which LLM call chose a tool or read its result (OpenTelemetry traces often don't), the link is taken from the order the calls ran in within one run.`;
  return marks;
}

const gapsOf = data => (data.data_quality?.gaps || []).filter(gap => gap.key !== 'user_input');  // the client drops it on purpose


function renderSteps(q, data, showAllSteps) {
  const t = data.summary?.totals || {}, opps = data.summary?.opportunities || [];
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
      lines.push(`${count(calls - cache.length)} of ${plural(calls, 'call')}, ${money(s.repeat)}` +
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
    repeated
      ? `<b>${money(repeated)}</b> (${share(repeated, cost)}) went on repeated calls: the same input and the same result as an earlier call. Reusing those results instead of calling again would avoid that spend.`
      : 'The report found no repeated calls.',
    data.mode === 'diagnose' ? ''
      : toTry ? `There ${toTry === 1 ? 'is 1 change' : `are ${count(toTry)} changes`} to try, on the rows marked <strong>Try:</strong> in the table below.`
      : 'There are no changes to try yet.',
    needRuns ? `For ${plural(needRuns, 'step')}, the report can’t tell yet whether calls repeat: it needs more runs of this workflow.` : '',
    unrecorded ? `For ${plural(unrecorded, 'step')}, the trace doesn’t record all of what the step saw, so the report can’t compare its calls.` : '',
    gaps ? `Your data is missing ${plural(gaps, 'kind')} of information, so the report may have missed some findings: see “What the report couldn’t see”.` : '',
  ].filter(Boolean).map(l => `<li>${l}</li>`).join('');

  q('steps').querySelector('tbody').innerHTML = shown.map(s => {
    const acts = tries(s);
    return `<tr class="${acts.length ? 'has-try' : ''}" data-node="${esc(s.node)}">
      <td data-label="Step">${stepName(s.node)}<span class="note">${plural(s.occurrences, 'call')}</span></td>
      <td class="barcell"><div class="spendbar"><i style="width:${max ? 100 * (s.total_cost || 0) / max : 0}%"></i><i class="rep" style="width:${max ? 100 * s.repeat / max : 0}%"></i></div></td>
      <td class="num" data-label="Cost">${money(s.total_cost)}<span class="note">${share(s.total_cost || 0, cost)}</span></td>
      <td data-label="Repeats">${repeats(s)}</td></tr>` +
      (acts.length ? `<tr class="try" data-node="${esc(s.node)}"><td colspan="4">${acts.map(a => `<strong>Try:</strong> ${a}`).join('<br>')}</td></tr>` : '');
  }).join('') + (steps.length > listed.length ? `<tr class="more"><td colspan="4">${showAllSteps
      ? `<button class="link" type="button" data-steps="less">Hide the steps with no cost and no repeats</button>`
      : `${plural(steps.length - listed.length, 'more step')} recorded no cost and no repeats. <button class="link" type="button" data-steps="all">Show them</button>`}</td></tr>` : '');
}

const HEAD = `
  <button class="back" type="button" data-r="back" hidden>← All workflows</button>
  <div class="sheet-head">
    <div class="title">
      <div class="grade mini" data-r="grade" hidden><b data-r="gradeLetter"></b></div>
      <div><h2 data-r="name" tabindex="-1"></h2><div class="when" data-r="meta"></div></div>
    </div>
    <div class="tools"></div>
  </div>`;

const SHEET = HEAD + `
  <div class="lead" data-r="lead" hidden>
    <p class="head" data-r="head"></p>
    <p class="sub" data-r="sub"></p>
  </div>
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
  </section>
  <section class="first" data-r="firstSection" hidden>
    <h3>Do these first</h3>
    <div data-r="first"></div>
  </section>
  <section class="between" data-r="betweenSection" hidden>
    <h3>Between agents</h3>
    <p class="why">Calls more than one agent makes in the same run, with the same input and the same result. One fetch could serve them all.</p>
    <div data-r="between"></div>
  </section>
  <section>
    <h3>Where the money went</h3>
    <p class="keys" aria-hidden="true"><span class="key"><b></b>Spend</span><span class="key"><b class="rep"></b>Spent on repeats</span></p>
    <table class="tbl" data-r="steps">
      <thead><tr><th>Step</th><th class="barh"><span class="sr">Share of spend</span></th><th class="num">Cost</th><th>Repeats</th></tr></thead>
      <tbody></tbody>
    </table>
    <p class="why">Each % is that step's share of the workflow's total spend. <code>llm_call ← X</code> is the model deciding what to do after X. A tool's cost includes the LLM calls that chose it, so those dollars also appear in the <code>llm_call</code> rows: rows overlap, and their % don't add up to 100.</p>
    <p class="why">Repeats are calls with the same input and the same result as an earlier call.</p>
  </section>
  <section data-r="gapsSection">
    <h3>What the report couldn't see</h3>
    <dl class="gaps" data-r="gaps"></dl>
  </section>`;

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

const pct = (part, whole) => { const p = 100 * part / whole; return p > 0 && p < 1 ? '<1%' : `${Math.round(p)}%`; };

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
    q('sub').textContent = `That is ${pct(r.cost, cost)} of this workflow’s spend in this dataset, ${found}.`;
  } else {  // a small sum reads as nothing: lead with the share
    q('head').innerHTML = `<b>${pct(r.cost, cost)}</b> of this workflow’s spend went to calls ${what}.`;
    q('sub').textContent = `${money(r.cost)} of ${money(cost)} in this dataset, ${found}.`;
  }
}

// the risk label, then the reasons the evidence line doesn't already give (those quote the call count)
const riskLine = (n, labels) => {
  const why = (n.risk_reasons || []).filter(r => !new RegExp(`\\b${n.observations}\\b`).test(r));
  return `${labels[n.risk] || n.risk}${why.length ? `: ${why.join('; ')}` : ''}.`;
};

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
  q('first').innerHTML = `<ol>${steps.map((n, i) => `<li class="card">
    <p class="top"><span class="stepl">${esc(STEP[n.step] || n.step)}</span><span class="cost">${n.saving ? `${money(n.saving)} <span class="k">in this dataset</span>` : 'No saving counted'}</span></p>
    <h4>${esc(TITLE[n.type] || n.type)}</h4>
    <p class="where">${(n.nodes || [n.title]).map(stepName).join(' → ')}</p>
    <p>${esc((EVIDENCE[n.type] || (k => plural(k, 'call') + ' seen.'))(n.observations))}</p>
    ${n.do ? `<p class="do">${esc(n.do)}</p>` : ''}
    <p class="risk">${esc(riskLine(n, labels))}</p>
    ${findingNodes(n).some(id => onMap.has(id)) ? `<button type="button" class="btn-map" data-first="${i}">Show on the map</button>` : ''}
  </li>`).join('')}</ol>`;
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
    <p class="top"><span class="stepl">${esc(TITLE[o.type] || o.type)}</span><span class="cost">${o.saving ? `${money(o.saving)} <span class="k">in this dataset</span>` : 'No dollar cost recorded'}</span></p>
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
const amPct = s => { const p = 100 * (s || 0); return p > 0 && p < 1 ? '<1%' : `${Math.round(p)}%`; };

function agentMapModel(data) {
  const ag = data.graph.agent_graph, nodes = data.graph.nodes || [];
  const byId = new Map(nodes.map(n => [n.id, n]));
  const marks = marksOf(data, byId);
  const hosted = ag.agents.some(a => HAS_HASH.test(a.id));
  const agentNo = new Map(ag.agents.map((a, i) => [a.id, i + 1]));
  const agentName = id => hosted ? `Agent ${agentNo.get(id)}` : id;
  const stepTitle = id => String(id).startsWith(LLM_AFTER) ? `LLM call after ${plainName(String(id).slice(LLM_AFTER.length))}` : plainName(id);
  const items = new Map();
  for (const a of ag.agents) {
    const kinds = new Set(a.step_ids.flatMap(s => [...(marks.get(s)?.kinds || [])]));
    items.set(a.id, { id: a.id, type: 'agent', name: agentName(a.id), sub: hosted ? plainName(a.id) : '', share: a.share, cost: a.cost, kinds, a });
  }
  for (const d of ag.decisions) {
    // a pseudonymous decision is named after the agents whose results it reads
    const reads = ag.links.filter(x => x.kind === 'reads' && x.decision === d.id).map(x => agentNo.get(x.from_agent)).sort((x, y) => x - y);
    const name = hosted && HAS_HASH.test(d.id) && reads.length ? `After Agent${reads.length > 1 ? 's' : ''} ${reads.join(' + ')}` : stepTitle(d.id);
    items.set(d.id, { id: d.id, type: 'decision', name, sub: name === stepTitle(d.id) ? '' : plainName(d.id), share: d.share, cost: d.cost, kinds: new Set(marks.get(d.id)?.kinds || []), d });
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
      if (it.type === 'decision') return on.includes(it.id);
      const mine = new Set(it.a.step_ids.map(s => stepOf(byId.get(s) || { id: s })));
      return on.some(s => mine.has(s)) && (!o.identity?.agent || o.identity.agent === it.id || sharedBy(o).includes(it.id));
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
  return { ag, byId, marks, items, hot, hosted, agentName, decisionName: id => items.get(id)?.name || stepTitle(id), where };
}

function renderAgentMap(q, data, m) {
  const { ag, items, hot } = m, L = ag.layout, runs = data.graph.run_count || 0;
  const rank = new Map(ag.agents.map(a => [a.id, a.rank]));
  const linkText = x => {
    if (x.kind === 'reads') return '';
    if (x.mode === 'return' && x.loop) return `back: ${runPct(x.loop.share_of_runs)} of runs, ${x.loop.passes} passes`;
    // a route past agents that run in between: say which it skips
    const from = x.kind === 'handoff' ? Math.max(-1, ...ag.links.filter(l => l.kind === 'reads' && l.decision === x.decision).map(l => rank.get(l.from_agent) ?? -1)) : rank.get(x.from_agent);
    const skipped = ag.agents.filter(a => a.id !== ag.router && a.rank > from && a.rank < rank.get(x.to_agent));
    const skip = skipped.length === 1 ? ` skip ${m.agentName(skipped[0].id)}` : skipped.length > 1 ? ` skip ${skipped.length} agents` : '';
    return [amPct(x.share), skip];
  };
  const routes = L.routes.map(r => {
    const links = r.links.map(i => ag.links[i]);
    const main = links.find(x => x.kind !== 'reads') || links[0];
    const cls = `edge ${main.kind}${main.mode === 'return' ? ' return' : ''}`;
    const w = main.kind === 'reads' ? 1.5 : 1.5 + 3 * Math.min(1, main.share || 0);
    const pts = r.points, d = pts.map(([x, y], i) => `${i ? 'L' : 'M'}${x},${y}`).join(' ');
    const text = linkText(main), [pct, skip] = Array.isArray(text) ? text : [text, ''];
    let label = '';
    if (main.mode === 'return') {  // left of its longest vertical stretch, on three lines
      const [[ax, ay], [, by]] = pts.slice(1).map((p, i) => [pts[i], p]).filter(([a, b]) => a[0] === b[0]).sort(([a, b], [c, d]) => Math.abs(d[1] - c[1]) - Math.abs(b[1] - a[1]))[0] || [pts[0], pts[1]];
      const words = main.loop ? ['back', `${runPct(main.loop.share_of_runs)} of runs`, `${main.loop.passes} passes`] : ['back', pct];
      label = `<text class="edge-n" x="${ax - 8}" y="${(ay + by) / 2 - 8 * (words.length - 1)}" text-anchor="end">${words.map((w, i) => `<tspan x="${ax - 8}" dy="${i ? 16 : 0}">${esc(w)}</tspan>`).join('')}</text>`;
    } else if (pct) {  // beside the last stretch into the box it goes to
      const [x1, y1] = pts[pts.length - 2], [x2, y2] = pts[pts.length - 1];
      label = x1 === x2
        ? `<text class="edge-n" x="${x2 + 4}" y="${y2 + (y2 > y1 ? -10 : 18)}">${esc(pct)}</text>`
        : `<text class="edge-n" x="${x2 + (x2 > x1 ? -10 : 10)}" y="${y2 - 6}" text-anchor="${x2 > x1 ? 'end' : 'start'}">${esc(pct)}</text>`;
    }
    const title = `${main.kind === 'reads' ? `${m.decisionName(main.decision)} reads ${m.agentName(main.from_agent)}'s result`
      : `${main.kind === 'handoff' ? m.decisionName(main.decision) : m.agentName(main.from_agent)} ${main.mode === 'return' ? 'hands back' : 'hands over'} to ${m.agentName(main.to_agent)}`} in ${plural(main.runs, 'run')}${skip ? `, past ${skip.replace(' skip ', '')}` : ''}`;
    return `<path class="${cls}" d="${d}" stroke-width="${w.toFixed(1)}" marker-end="url(#amArrow)"><title>${esc(title)}</title></path>${label}`;
  });
  const frames = L.frames.map(f => `<g class="together"><rect x="${f.x}" y="${f.y}" width="${f.w}" height="${f.h}" rx="8"/>` +
    `<text x="${f.x + 8}" y="${f.y + f.h - 7}">run together</text></g>`);
  const markWords = it => Object.keys(MARK).filter(k => it.kinds.has(k)).map(k => MARK[k].badge).join(', ');
  const boxes = L.boxes.map(b => {
    const it = items.get(b.id);
    if (b.kind === 'router') {
      return `<g class="am router" data-an="${esc(b.id)}" tabindex="0" role="button" aria-label="${esc(`${it.name}, routes the run: ${amPct(it.share)} of spend`)}">` +
        `<rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="6"/><text class="t" x="${b.x + 12}" y="${b.y + 21}">${esc(it.name)}` +
        `${it.sub ? ` <tspan class="s">${esc(it.sub)}</tspan>` : ''} <tspan class="s">routes the run · its decisions below</tspan></text></g>`;
    }
    const f = fillOf(it.share), cls = `am ${b.kind} f${f}${it.kinds.has('unstable') ? ' unstable' : ''}${it.hot ? ' hotspot' : ''}`;
    const label = `${it.hot ? `Hot spot ${it.hot}: ` : ''}${it.name}${it.sub ? ` (${it.sub})` : ''}, ${amPct(it.share)} of spend${markWords(it) ? `, ${markWords(it)}` : ''}`;
    const open = `<g class="${cls}" data-an="${esc(b.id)}" tabindex="0" role="button" aria-label="${esc(label)}"><title>${esc(b.id)}</title>` +
      `<rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="${b.kind === 'chip' ? 4 : 6}"/>`;
    if (b.kind === 'chip') {
      return open + `<text class="c" x="${b.x + 6}" y="${b.y + b.h - 6}" textLength="${b.w - 12}" lengthAdjust="spacingAndGlyphs">${esc(`${it.hot ? `${it.hot} · ` : ''}${it.name} · ${amPct(it.share)}`)}</text></g>`;
    }
    const left = b.x + (it.hot ? 34 : 12), inner = b.x + b.w - 8 - left, max = Math.floor(inner / 7);
    const lines = it.sub ? [it.name, it.sub] : wrapName(it.name, max).slice(0, 2);
    if (!it.sub && wrapName(it.name, max).length > 2) lines[1] = wrapName(it.name, max).slice(1).join('');
    const text = lines.map((t, i) => `<text class="${it.sub && i ? 's' : 't'}" x="${left}" y="${b.y + 18 + 15 * i}"${t.length > max ? ` textLength="${inner}" lengthAdjust="spacingAndGlyphs"` : ''}>${esc(t)}</text>`).join('');
    const figures = b.kind === 'decision' ? `${amPct(it.share)} · ${money(it.cost)}` : `${amPct(it.share)} · ${plural(it.a.steps, 'step')}`;
    const words = markWords(it);
    return open + (it.hot ? `<circle class="no" cx="${b.x + 18}" cy="${b.y + 18}" r="11"/><text class="no" x="${b.x + 18}" y="${b.y + 22}" text-anchor="middle">${it.hot}</text>` : '') +
      text + `<text class="f" x="${left}" y="${b.y + 51}">${esc(figures)}</text>` +
      (words ? `<text class="w" x="${left}" y="${b.y + b.h - 7}"${words.length > max + 2 ? ` textLength="${inner}" lengthAdjust="spacingAndGlyphs"` : ''}>${esc(words)}</text>` : '') + '</g>';
  });
  const ARROW = 'markerUnits="userSpaceOnUse" markerWidth="10" markerHeight="10" viewBox="0 0 10 10" refX="9" refY="5" orient="auto-start-reverse"';
  const hidden = ag.links.filter(x => x.kind !== 'reads' && (x.share || 0) < 0.1).length;
  const legend = '<p class="legend"><span><i class="sw f0"></i><i class="sw f1"></i><i class="sw f2"></i><i class="sw f3"></i>Share of spend: under 5%, 5–15%, 15–30%, 30% and over (also printed in each box)</span>' +
    '<span><i class="sw no">1</i>Hot spot, by share of spend</span><span><i class="sw dash"></i>Unstable result</span><span><svg class="ln" width="28" height="8" aria-hidden="true"><path d="M0,4 H28"/></svg>Hands back to an earlier agent</span>' +
    (hidden ? `<span>${esc(`${plural(hidden, 'route')} taken by under 10% of runs ${hidden === 1 ? 'is' : 'are'} not drawn; ${hidden === 1 ? 'it is' : 'they are'} in the details`)}</span>` : '') + '</p>';
  const buttons = hot.length ? `<div class="hotspots"><h4>Hot spots</h4><ol>${hot.map(it => {
    const what = it.findings.length ? it.findings.map(o => TITLE[o.type] || o.type).filter((t, i, a) => a.indexOf(t) === i).join('; ') : 'no finding';
    return `<li><button type="button" data-an="${esc(it.id)}"><b>${it.hot}</b> <span>${esc(it.name)}${it.sub ? ` <span class="h">${esc(it.sub)}</span>` : ''}</span><span class="k">${esc(`${amPct(it.share)} of spend · ${what}`)}</span></button></li>`;
  }).join('')}</ol></div>` : '';
  q('map').innerHTML = `<div class="agentmap"><div class="ammain"><div class="mapscroll"><svg viewBox="0 0 ${L.width} ${L.height}" style="max-width:${L.width}px;min-width:${Math.round(L.width * MAP_MIN_SCALE)}px" ` +
    `role="group" aria-label="${esc(`Agent map: ${plural(ag.agents.length, 'agent')} and ${plural(ag.decisions.length, 'decision')} between them, across ${plural(runs, 'run')}`)}">` +
    `<defs><marker id="amArrow" ${ARROW}><path class="arrow" d="M0,0 L10,5 L0,10 z"/></marker></defs>` +
    frames.join('') + routes.join('') + boxes.join('') + `</svg></div>${legend}</div></div>` +
    `<div class="amunder">${buttons}<aside class="ampanel" data-r="amPanel" aria-live="polite"></aside></div>`;
  q('mapWhy').textContent = 'Each box is one agent, or one LLM call that decides which agent runs next. ' +
    'Links come from the trace: which result a decision read, and which agent it started. A % on a link is the share of runs reaching where it starts that take it.';
}

function agentPanel(q, data, m, id, showEvent) {
  const it = m.items.get(id), ag = m.ag, runs = data.graph.run_count || 0, labels = data.summary?.risk_labels || {};
  if (!it) return;
  const routes = links => links.length ? `<ul class="routes">${links.map(x => {
    const to = m.agentName(x.to_agent), how = x.mode === 'return' ? 'back to' : x.mode === 'both' ? 'together with another agent, to' : 'to';
    return `<li>${esc(`${how} ${to}: ${plural(x.runs, 'run')} (${amPct(x.share)})${x.loop ? `, ${x.loop.passes} passes each` : ''}${(x.share || 0) < 0.1 ? ' · not drawn: under 10% of runs' : ''}`)}</li>`;
  }).join('')}</ul>` : '<p class="k">It hands over to no other agent.</p>';
  const finds = it.findings.length ? `<ul class="finds">${it.findings.map(o => {
    const ev = (EVIDENCE[o.type] || (k => plural(k, 'call') + ' seen.'))(o.observations);
    const peers = sharedBy(o).filter(a => a !== id);
    return `<li><b>${esc(TITLE[o.type] || o.type)}</b> <span class="where">${(o.nodes || [o.title]).map(stepName).join(' → ')}</span>` +
      `<span>${esc(ev)}${peers.length ? esc(` Also made by ${peers.map(m.agentName).join(', ')}.`) : ''}</span>` +
      (o.next ? `<span class="k">${esc(riskLine(o.next, labels))}</span>` : '') + '</li>';
  }).join('')}</ul>` : '<p class="k">No finding on this one.</p>';
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
      `<p>${esc(`Runs ${when}${along}. ${plural(a.steps, 'step')}, ${money(it.cost)}, ${amPct(it.share)} of spend.`)}</p>` +
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
  // links from other agents start at the map's "Prompt", links to them end at its "Reply"
  const mine = new Set(m.items.get(id).a.step_ids), ends = new Map();
  for (const e of data.graph.edges) {
    const from = mine.has(e.from) ? e.from : '@prompt', to = mine.has(e.to) ? e.to : '@reply';
    if (from === '@prompt' && to === '@reply') continue;
    const key = `${from} ${to}`, was = ends.get(key);
    ends.set(key, was ? { ...was, transition_count: was.transition_count + e.transition_count, distinct_run_count: was.distinct_run_count + e.distinct_run_count }
      : from === e.from && to === e.to ? e : { from, to, kind: e.kind, transition_count: e.transition_count, distinct_run_count: e.distinct_run_count });
  }
  const graph = { ...data.graph, nodes: data.graph.nodes.filter(n => mine.has(n.id)), edges: [...ends.values()].sort((x, y) => y.transition_count - x.transition_count) };
  const sub = k => q({ map: 'stepMap', mapWhy: 'stepWhy' }[k] || k);
  q('stepsOf').hidden = false;
  q('stepsOfName').textContent = `${m.items.get(id).name}${m.items.get(id).sub ? ` (${m.items.get(id).sub})` : ''}: its steps`;
  const marks = renderMap(sub, { ...data, graph }, all);
  const svg = q('stepMap').querySelector('svg');  // a few steps: drawn at their natural size, never stretched
  if (svg) svg.style.maxWidth = `${svg.viewBox.baseVal.width}px`;
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

function renderHead(q, g, name, meta) {
  q('name').textContent = plainName(name);  // a pseudonym shows as its first 7 characters
  q('meta').textContent = [...meta, g && `dataset grade ${g.grade} (${String(g.word || '').toLowerCase()})`].filter(Boolean).join(' · ');
  q('grade').hidden = !g;
  if (g) { q('grade').dataset.g = g.grade; q('gradeLetter').textContent = g.grade; q('grade').setAttribute('aria-label', `Dataset grade ${g.grade}`); }
}

function renderReport(root, data, { name = '', meta = [], showEvent = null } = {}) {
  root.innerHTML = SHEET;
  const q = k => root.querySelector(`[data-r="${k}"]`);
  const sm = data.summary || {}, t = sm.totals || {}, g = data.grade;
  renderHead(q, g, name, meta);
  // the map, its wiring and the cards that point at it, drawn again when the reader shows all links or fewer
  const am = data.availability?.graph?.available && data.graph?.agent_graph?.layout ? agentMapModel(data) : null;
  let selectAgent = null;
  const drawMap = all => {
    if (am) {
      renderAgentMap(q, data, am);
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

  const gaps = gapsOf(data);
  q('gaps').innerHTML = gaps.map(gap => `<dt>${esc(gap.label.charAt(0).toUpperCase() + gap.label.slice(1))}</dt><dd>${esc(gap.effect || '')}</dd>`).join('');
  q('gapsSection').hidden = gaps.length === 0;
  q('notes').textContent = sm.notes || '';
}

const OVERVIEW = HEAD + `
  <div class="lead" data-r="lead">
    <p class="head" data-r="head"></p>
    <p class="sub" data-r="sub"></p>
  </div>
  <p class="totals" data-r="totals"></p>
  <section>
    <h3>Which workflows matter</h3>
    <p class="why">The workflows with the most spend a rule, a script or a cache could replace come first. Open one to see its map and what to do first.</p>
    <p class="keys" aria-hidden="true"><span class="key"><b></b>Spend</span><span class="key"><b class="rep"></b>Could be replaced</span></p>
    <table class="tbl wfs" data-r="workflows">
      <thead><tr><th>Workflow</th><th class="barh"><span class="sr">Spend that could be replaced</span></th><th class="num">Spend</th><th class="num">Could be replaced</th><th>Findings</th></tr></thead>
      <tbody></tbody>
    </table>
    <p class="why">Each call belongs to one workflow, so the rows add up to the totals above.</p>
  </section>`;

// the dataset's headline, by the same rules as a workflow's: the share first under $50
function renderOverviewLead(q, o) {
  const t = o.totals || {}, n = plural((o.workflows || []).length, 'workflow');
  const what = 'a rule, a script or a cache could replace';
  const found = `from ${plural(t.findings, 'finding')}, each call counted once`;
  if (!t.spend) {
    q('head').textContent = `No cost is recorded in this dataset, so the report can’t say how much of it went to calls ${what}.`;
    q('sub').textContent = '';
  } else if (!t.replaceable) {
    q('head').textContent = `The report found no calls in this dataset that ${what}.`;
    q('sub').textContent = `Its ${n} cost ${money(t.spend)}.`;
  } else if (t.replaceable >= 50) {
    q('head').innerHTML = `<b>${money(t.replaceable)}</b> of the ${money(t.spend)} this dataset cost went to calls ${what}.`;
    q('sub').textContent = `That is ${pct(t.replaceable, t.spend)} of the spend across ${n}, ${found}.`;
  } else {
    q('head').innerHTML = `<b>${pct(t.replaceable, t.spend)}</b> of this dataset’s spend went to calls ${what}.`;
    q('sub').textContent = `${money(t.replaceable)} of ${money(t.spend)} across ${n}, ${found}.`;
  }
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
      <td class="num" data-label="Could be replaced">${money(r.replaceable)}<span class="note">${r.spend ? pct(r.replaceable || 0, r.spend) : ''}</span></td>
      <td data-label="Findings">${count(r.findings)}${r.highest_risk ? `<span class="note">${esc(labels[r.highest_risk] || r.highest_risk)}</span>` : ''}</td></tr>`).join('');
}

function renderOverview(root, data, { name = '', meta = [], showEvent = null, workflow = null } = {}) {
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
    renderHead(q, data.grade, name, meta);
    renderOverviewLead(q, o);
    const runs = (o.workflows || []).reduce((n, r) => n + (r.runs || 0), 0);
    q('totals').innerHTML = `${money(o.totals?.spend)} <span class="k">across</span> ${plural(runs, 'run')} <span class="k">in</span> ${plural((o.workflows || []).length, 'workflow')}.`;
    renderOverviewRows(q, o, data.reports);
    q('workflows').addEventListener('click', e => {
      const b = e.target.closest('button[data-wf]');
      if (b) show(b.dataset.wf);
    });
    if (from != null) [...root.querySelectorAll('button[data-wf]')].find(b => b.dataset.wf === from)?.focus();
  });
  const show = wf => {
    if (!data.reports[wf]) return;
    redraw(() => {
      renderReport(root, data.reports[wf], { name: wf, meta: [plainName(name), ...meta].filter(Boolean), showEvent });
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

function render(root, data, opts = {}) {
  if (data && data.overview && data.reports) return renderOverview(root, data, opts);
  renderReport(root, data, opts);
  return { show() {}, overview() {} };
}

window.ProfilerReport = { render };
})();
