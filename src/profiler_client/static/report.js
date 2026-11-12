/* The AI Agent Profiler report: one renderer for the web view and for `profiler-client open`.
 *
 * ProfilerReport.render(root, data, { name, meta }) draws a report JSON into `root` (an element with
 * class "sheet"); `meta` is a list of short lines for under the title. It reads nothing but `data`,
 * makes no request, and writes only inside `root`. The host page adds its own buttons to `.tools`.
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
};
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
const MAP_END = { '@prompt': 'Prompt', '@reply': 'Reply' };
const plainName = n => String(n).replace(HASH, 'h:$1');
function renderMap(q, data) {
  const avail = data.availability?.graph, graph = data.graph;
  if (!avail?.available || !graph) {
    const why = avail ? MAP_NONE[avail.code] || 'No map for this dataset.' : 'No map: this report was made before maps were added.';
    q('map').innerHTML = `<p class="none">${esc(why)}</p>`;
    q('mapWhy').textContent = '';
    return;
  }
  // the most-taken links (the server lists them most-taken first) and the steps they join
  const links = graph.edges.slice(0, MAP_LINKS), joined = new Set(links.flatMap(e => [e.from, e.to]));
  const byId = new Map(graph.nodes.map(n => [n.id, n])), shown = graph.nodes.filter(n => joined.has(n.id));

  // names: a pseudonymous step is "Tool A", "Tool B"… (most-called first); an LLM call is "after" what it read
  const letters = new Map();
  for (const n of shown) if (n.kind === 'step' && HAS_HASH.test(n.id)) letters.set(n.id, `Tool ${String.fromCharCode(65 + letters.size % 26)}${letters.size >= 26 ? Math.floor(letters.size / 26) : ''}`);
  const fit = (t, k) => t.length > k ? `${t.slice(0, k - 1)}…` : t;
  const label = id => MAP_END[id] ? MAP_END[id].toLowerCase() : letters.get(id) || plainName(id);
  const lines = n => {
    if (n.kind === 'llm_call') {
      const after = String(n.id).replace(/^llm_call ← /, '');
      return ['LLM call', `after ${fit(after === 'user_prompt' ? 'prompt' : label(after), 18)}`, `${count(n.occurrences)} · ${money(n.cost)}`];
    }
    return letters.has(n.id) ? [letters.get(n.id), fit(plainName(n.id), 20), plural(n.occurrences, 'call')]
      : [fit(plainName(n.id), 18), '', plural(n.occurrences, 'call')];
  };

  // places: walk out from the prompt along the busiest links; a step sits right of what started it,
  // and the LLM call that reads a first-row step's result sits under it
  const at = new Map(), taken = new Set(), key = (c, r) => `${c},${r}`;
  const put = (id, c, r) => { while (taken.has(key(c, r))) r++; at.set(id, { c, r }); taken.add(key(c, r)); };
  const starts = shown.filter(n => n.kind === 'start' || !links.some(e => e.to === n.id && e.from !== n.id));
  const queue = [];
  for (const n of starts.filter(n => n.id !== '@reply')) { put(n.id, 0, at.size ? Math.max(...[...at.values()].map(p => p.r)) + 1 : 0); queue.push(n.id); }
  while (queue.length) {
    const id = queue.shift(), p = at.get(id);
    for (const e of links.filter(e => e.from === id && e.to !== '@reply' && !at.has(e.to))) {
      if (e.kind === 'reads_result' && p.r === 0) put(e.to, p.c, 1); else put(e.to, p.c + 1, p.r);
      queue.push(e.to);
    }
  }
  for (const n of shown) if (!at.has(n.id) && n.id !== '@reply') put(n.id, 0, Math.max(0, ...[...at.values()].map(p => p.r)) + 1);
  if (joined.has('@reply')) put('@reply', Math.max(...[...at.values()].map(p => p.c)), 0);

  const W = 172, H = 68, PW = 96, PH = 44, CX = 236, RY = 200, PAD = 16, TOP = 40;
  const box = id => {
    const { c, r } = at.get(id), end = MAP_END[id], w = end ? PW : W, h = end ? PH : H;
    const x = PAD + c * CX + (W - w) / 2, y = TOP + r * RY + (H - h) / 2;
    return { x, y, w, h, cx: x + w / 2, cy: y + h / 2 };
  };
  const cols = Math.max(...[...at.values()].map(p => p.c)) + 1, rows = Math.max(...[...at.values()].map(p => p.r)) + 1;
  const width = PAD * 2 + (cols - 1) * CX + W, height = TOP + (rows - 1) * RY + H + 56;

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
      lx = (x1 + x2) / 2 + 20; ly = y2 - 8;  // over its level run
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
    const cls = `node${n.kind === 'llm_call' ? ' llm' : ''}${hot && n.id === hot.id ? ' hot' : ''}`;
    return `<g class="${cls}"><title>${esc(n.id)}</title><rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="6"/>` +
      `<text class="t" x="${b.x + 14}" y="${b.y + 26}">${esc(t)}</text>` +
      (s1 ? `<text class="s" x="${b.x + 14}" y="${b.y + 44}">${esc(s1)}</text>` : '') +
      `<text class="s" x="${b.x + 14}" y="${b.y + (s1 ? 59 : 50)}">${esc(s2)}${loop ? ` · ↻ ${esc(count(loop))}` : ''}</text></g>`;
  });
  q('map').innerHTML = `<div class="mapscroll"><svg viewBox="0 0 ${width} ${height}" style="min-width:${Math.min(width, 720)}px" role="img" aria-label="${esc(`Workflow map: ${plural(shown.length, 'step')} and ${plural(paths.length, 'link')} across ${plural(graph.run_count, 'run')}`)}">` +
    `<defs><marker id="mapArrow" ${ARROW}><path class="arrow" d="M0,0 L10,5 L0,10 z"/></marker>` +
    `<marker id="mapArrowHot" ${ARROW}><path class="arrow hot" d="M0,0 L10,5 L0,10 z"/></marker></defs>` +
    paths.join('') + nodes.join('') + '</svg></div>' +
    '<p class="legend"><span><i class="sw llm"></i>LLM call (has a cost)</span><span><i class="sw tool"></i>Tool call</span>' +
    (hot && total ? `<span><i class="sw hot"></i>Largest share of spend (${Math.round(100 * hot.cost / total)}%)</span>` : '') + '</p>';

  const left = graph.edges.length - links.length, unlinked = graph.nodes.filter(n => !joined.has(n.id)).length;
  const name = id => MAP_END[id] || (byId.get(id)?.kind === 'llm_call' ? lines(byId.get(id)).slice(0, 2).join(' ') : label(id));
  const way = (n, e) => {
    const read = String(n.id).replace(/^llm_call ← /, '');
    const what = e.to === '@reply' ? 'replies' : e.to === n.id ? 'repeats itself'
      : byId.get(e.to)?.kind === 'llm_call' ? `goes on to the ${name(e.to)}` : `calls ${label(e.to)}${e.to === read ? ' again' : ''}`;
    return `${what} ${plural(e.transition_count, 'time')} (${share(e)})`;
  };
  const branches = shown.filter(n => (out.get(n.id) || []).length > 1)
    .map(n => `${name(n.id)} goes ${out.get(n.id).length} ways: it ${out.get(n.id).map(e => way(n, e)).join(', ')}. `).join('');
  q('mapWhy').textContent = branches + (left ? `This shows the ${count(links.length)} most-taken of ${plural(graph.edges.length, 'link')}; ${plural(unlinked, 'step')} ${unlinked === 1 ? 'is' : 'are'} left out, and all are in the table below. ` : '') +
    `Arrow width is how many times a hop happened across ${plural(graph.run_count, 'run')}; where a step goes more than one way, the % is that way's share. Every link is recorded in the trace: an LLM call choosing a tool, the tool whose result the next LLM call read, the prompt that started a run, and the LLM call that replied.`;
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
      ? `<b>${money(repeated)}</b> (${share(repeated, cost)}) went on repeated calls: the same input and the same result as an earlier call. Reusing those results instead of calling again would save it.`
      : 'The report found no repeated calls.',
    data.mode === 'diagnose' ? ''
      : toTry ? `There ${toTry === 1 ? 'is 1 change' : `are ${count(toTry)} changes`} to try, on the rows marked <strong>Try:</strong> in the table below.`
      : 'There are no changes to try yet.',
    needRuns ? `For ${plural(needRuns, 'step')}, the report can’t tell yet whether calls repeat: it needs more runs of this workflow.` : '',
    unrecorded ? `For ${plural(unrecorded, 'step')}, the trace doesn’t record all of what the step saw, so the report can’t compare its calls.` : '',
    gaps ? `Your data is missing ${plural(gaps, 'kind')} of information, so the report may have missed some savings: see “What the report couldn’t see”.` : '',
  ].filter(Boolean).map(l => `<li>${l}</li>`).join('');

  q('steps').querySelector('tbody').innerHTML = shown.map(s => {
    const acts = tries(s);
    return `<tr class="${acts.length ? 'has-try' : ''}">
      <td data-label="Step">${stepName(s.node)}<span class="note">${plural(s.occurrences, 'call')}</span></td>
      <td class="barcell"><div class="spendbar"><i style="width:${max ? 100 * (s.total_cost || 0) / max : 0}%"></i><i class="rep" style="width:${max ? 100 * s.repeat / max : 0}%"></i></div></td>
      <td class="num" data-label="Cost">${money(s.total_cost)}<span class="note">${share(s.total_cost || 0, cost)}</span></td>
      <td data-label="Repeats">${repeats(s)}</td></tr>` +
      (acts.length ? `<tr class="try"><td colspan="4">${acts.map(a => `<strong>Try:</strong> ${a}`).join('<br>')}</td></tr>` : '');
  }).join('') + (steps.length > listed.length ? `<tr class="more"><td colspan="4">${showAllSteps
      ? `<button class="link" type="button" data-steps="less">Hide the steps with no cost and no repeats</button>`
      : `${plural(steps.length - listed.length, 'more step')} recorded no cost and no repeats. <button class="link" type="button" data-steps="all">Show them</button>`}</td></tr>` : '');
}

const SHEET = `
  <div class="sheet-head">
    <div class="title">
      <div class="grade mini" data-r="grade" hidden><b data-r="gradeLetter"></b></div>
      <div><h2 data-r="name"></h2><div class="when" data-r="meta"></div></div>
    </div>
    <div class="tools"></div>
  </div>
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
  </section>
  <section>
    <h3>Where the money went</h3>
    <p class="keys" aria-hidden="true"><span class="key"><b></b>Spend</span><span class="key"><b class="rep"></b>Spent on repeats</span></p>
    <table class="tbl" data-r="steps">
      <thead><tr><th>Step</th><th class="barh"><span class="sr">Share of spend</span></th><th class="num">Cost</th><th>Repeats</th></tr></thead>
      <tbody></tbody>
    </table>
    <p class="why">Repeats are calls with the same input and the same result as an earlier call.</p>
  </section>
  <section data-r="gapsSection">
    <h3>What the report couldn't see</h3>
    <dl class="gaps" data-r="gaps"></dl>
  </section>
  <p class="notes" data-r="notes"></p>`;

function render(root, data, { name = '', meta = [] } = {}) {
  root.innerHTML = SHEET;
  const q = k => root.querySelector(`[data-r="${k}"]`);
  const sm = data.summary || {}, t = sm.totals || {}, g = data.grade;
  q('name').textContent = plainName(name);  // a pseudonym shows as its first 7 characters
  q('meta').textContent = [...meta, g && `dataset grade ${g.grade} (${String(g.word || '').toLowerCase()})`].filter(Boolean).join(' · ');
  q('grade').hidden = !g;
  if (g) { q('grade').dataset.g = g.grade; q('gradeLetter').textContent = g.grade; q('grade').setAttribute('aria-label', `Dataset grade ${g.grade}`); }
  renderMap(q, data);
  q('totals').innerHTML = `${money(t.cost)} <span class="k">across</span> ${plural(t.runs, 'run')}<span class="k">,</span> ` +
    `${plural(t.llm_calls, 'LLM call')} <span class="k">and</span> ${plural(t.tool_calls, 'tool call')}<span class="k">,</span> ${count(t.tokens)} tokens.`;

  const workflows = data.observe?.workflows || [], wf = workflows.find(w => w.name === data.observe?.selected_workflow);
  const allRuns = workflows.reduce((n, w) => n + (w.num_runs || 0), 0);
  q('scope').innerHTML = wf && workflows.length > 1
    ? `This report covers one workflow, ${stepName(wf.name)}: ${plural(wf.num_runs, 'run')} of the ${count(allRuns)} in your dataset.` : '';
  q('scope').hidden = !(wf && workflows.length > 1);
  renderSteps(q, data, false);
  q('steps').addEventListener('click', e => {
    const b = e.target.closest('button[data-steps]');
    if (b) renderSteps(q, data, b.dataset.steps === 'all');
  });

  const gaps = gapsOf(data);
  q('gaps').innerHTML = gaps.map(gap => `<dt>${esc(gap.label.charAt(0).toUpperCase() + gap.label.slice(1))}</dt><dd>${esc(gap.effect || '')}</dd>`).join('');
  q('gapsSection').hidden = gaps.length === 0;
  q('notes').textContent = sm.notes || '';
}

window.ProfilerReport = { render };
})();
