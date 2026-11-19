"use strict";
// The review workbench page. It draws whatever a view returns ({checks, facts, tables, charts, notes}) and has no
// code for any one view. Text that comes from a run is only ever set as text, never parsed as markup.

const SVG = "http://www.w3.org/2000/svg";
function make(ns, tag, props, kids) {
  const n = ns ? document.createElementNS(ns, tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "text") n.textContent = v;
    else if (k === "on") for (const [e, f] of Object.entries(v)) n.addEventListener(e, f);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) if (c != null) n.append(c);
  return n;
}
const el = (tag, props, ...kids) => make(null, tag, props, kids);
const sv = (tag, props, ...kids) => make(SVG, tag, props, kids);

// Each status or state word: its symbol, its words, its colour role and what it asks of the reader.
const MARKS = {
  problem: ["✕", "Problem", "problem", "needs you"], look: ["◐", "Look", "look", "for you to judge"],
  not_available: ["–", "Not available", "na", "a file could not be read or recognised"],
  ok: ["✓", "OK", "ok", "nothing to do"], not_built: ["○", "Not built", "na", "this view does not exist yet"],
  same: ["✓", "same", "ok", "the file has its recorded hash"], changed: ["≠", "changed", "problem", "the file differs from its recorded hash"],
  missing: ["?", "missing", "look", "the file is not in the run folder"],
  not_recorded: ["–", "not recorded", "na", "no hash of this file was recorded, so it cannot be compared"],
  outside: ["↗", "outside", "na", "a harness file outside the run folder, not compared"]};
const ORDER = ["problem", "look", "not_available", "ok"];
const rank = s => (ORDER.indexOf(s) + ORDER.length + 1) % (ORDER.length + 1);
const tone = w => "t-" + (MARKS[w] ? MARKS[w][2] : "na");
function mark(word, bare) {
  const m = MARKS[word] || ["·", String(word), "na", ""];
  return el("span", {class: `mark ${tone(word)}${bare ? " bare" : ""}`, title: `${m[1]}: ${m[3]}`, text: bare ? m[0] : `${m[0]} ${m[1]}`});
}

const UNITS = [["_usd", "USD"], ["_minutes", "min"]];
function label(name, unit) {
  const s = String(name), u = UNITS.find(([end]) => s.endsWith(end));
  const words = (u ? s.slice(0, -u[0].length) : s).replace(/_/g, " ");
  return u && unit ? `${words} (${u[1]})` : words;
}
function fmt(v, name, unit) {
  if (v == null || v === "") return "–";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "string") return v.replace(/^(\d{4}-\d\d-\d\d)T(\d\d:\d\d:\d\d)(\.\d+)?(\+00:00|Z)$/, "$1 $2Z");   // a UTC time, compactly
  if (Array.isArray(v)) return v.length ? v.map(x => (typeof x === "number" ? String(Number(x.toPrecision(7))) : fmt(x))).join(", ") : "none";
  if (typeof v !== "number") return JSON.stringify(v);
  const n = String(name || ""), loc = (x, lo, hi) => x.toLocaleString("en-US", {minimumFractionDigits: lo, maximumFractionDigits: hi});
  if (n.endsWith("_usd")) return (unit ? "$" : "") + loc(v, 2, 2);
  if (n.endsWith("_minutes") && unit)
    return `${loc(v, 0, 1)} min` + (v >= 2880 ? ` (${loc(v / 1440, 0, 1)} days)` : v >= 120 ? ` (${loc(v / 60, 0, 1)} hours)` : "");
  if (Number.isInteger(v)) return Math.abs(v) < 10000 ? String(v) : loc(v, 0, 0);
  return Math.abs(v) >= 1000 ? loc(v, 0, 1) : String(Number(v.toPrecision(4)));
}
const clip = (s, n) => (s = String(s == null ? "–" : s), s.length > n ? "…" + s.slice(s.length - n + 1) : s);
const rowPairs = (t, r) => t.columns.map((c, i) => [label(c, true), fmt(r[i], c)]);

const hint = document.getElementById("tip");
function tip(node, pairs) {
  node.addEventListener("mouseenter", () => {
    hint.replaceChildren(...pairs().map(([k, v]) => el("div", null, el("span", {text: k}), el("span", {text: v}))));
    hint.hidden = false;
  });
  node.addEventListener("mousemove", e => {
    hint.style.left = Math.max(4, Math.min(e.clientX + 14, innerWidth - hint.offsetWidth - 8)) + "px";
    hint.style.top = Math.max(4, Math.min(e.clientY + 14, innerHeight - hint.offsetHeight - 8)) + "px";
  });
  node.addEventListener("mouseleave", () => { hint.hidden = true; });
  return node;
}

// ------------------------------------------------------------------------------------------------ glossary
// Codes the specification defines (a scorecard case, a gap, a goal, a few terms). Their meanings come from the server
// (/api/glossary, read from the specification's own tables); none is written here. A code is recognised only with
// its kind in front of it ("goal G2"), so a run's own name that looks like a code ("group G2") is left alone.
const GLOSS = {entries: [], source: "", why: null, re: null, by: new Map(), used: new Set()};
function setGlossary(g) {
  Object.assign(GLOSS, {entries: (g && g.entries) || [], source: (g && g.source) || "", why: (g && g.why) || null, re: null});
  GLOSS.by.clear(); GLOSS.used.clear();
  for (const e of GLOSS.entries) GLOSS.by.set((e.kind === "term" ? e.code : `${e.kind} ${e.code}`).toLowerCase(), e);
  const alts = [...GLOSS.by.keys()].sort((a, b) => b.length - a.length).map(k => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (alts.length) GLOSS.re = new RegExp(`\\b(${alts.join("|")})(?:e?s)?(?![A-Za-z0-9])`, "gi");
}
// A text as a list of strings and marked codes: each code the glossary defines carries its meaning on hover.
function gloss(text) {
  text = String(text == null ? "" : text);
  if (!GLOSS.re) return [text];
  const out = [];
  let last = 0;
  for (const m of text.matchAll(GLOSS.re)) {
    const e = GLOSS.by.get(m[1].toLowerCase());
    GLOSS.used.add(e);
    out.push(text.slice(last, m.index), el("abbr", {class: "term", title: `${e.meaning} (${e.section})`, text: m[0]}));
    last = m.index + m[0].length;
  }
  out.push(text.slice(last));
  return out.filter(x => x !== "");
}
// The glossary at the foot of a page: the codes that page used. Without a readable specification it says so.
function glossaryFoot() {
  if (!GLOSS.entries.length) return GLOSS.why ? el("section", {class: "glossary"}, el("h2", {text: "Glossary"}), el("p", {class: "muted",
    text: `The meanings of the codes on this page are not available (${GLOSS.why}), so each code is shown as it is.`})) : null;
  const used = GLOSS.entries.filter(e => GLOSS.used.has(e));
  if (!used.length) return null;
  return el("section", {class: "glossary"}, el("h2", {text: "Glossary"}),
    el("p", {class: "about", text: `The codes and terms this page uses, as ${GLOSS.source} defines them. Pointing at a dotted word shows the same text.`}),
    el("dl", {class: "terms"}, used.map(e => el("div", null, el("dt", {text: e.kind === "term" ? e.code : `${e.kind} ${e.code}`}),
      el("dd", {text: `${e.meaning} (${e.section})`})))));
}

// ------------------------------------------------------------------------------------------------ tables
const PAGE = 25, LONG = 60;
function compare(a, b) {
  if (a == null || b == null) return (a == null) - (b == null);
  return typeof a === "number" && typeof b === "number" ? a - b : String(a).localeCompare(String(b), "en", {numeric: true});
}
// A table that sorts on a header click, filters on one text box and pages long lists. opts.href(row) links a row.
function tableView(t, opts = {}) {
  let sort = null, desc = false, page = 0, q = "";
  const numeric = t.columns.map((_, i) => t.rows.some(r => typeof r[i] === "number") && t.rows.every(r => r[i] == null || typeof r[i] === "number"));
  const body = el("tbody"), info = el("span"), wide = t.columns.length > 10;     // a wide table is set smaller, its long texts on one line
  const step = d => () => { page += d; draw(); };
  const prev = el("button", {text: "Previous", on: {click: step(-1)}}), next = el("button", {text: "Next", on: {click: step(1)}});
  const heads = t.columns.map((c, i) => el("th", {class: numeric[i] ? "num" : null, title: "Click to sort by this column",
    on: {click: () => { desc = sort === i && !desc; sort = i; page = 0; draw(); }}}));
  const cell = (v, i, r) => {
    const c = t.columns[i], td = el("td", {class: numeric[i] ? "num" : fmt(v, c).length <= 20 ? "tight" : null});      // a short value stays on one line; a longer one wraps to fit the page
    if ((c === "status" || c === "state") && MARKS[v]) td.append(mark(v));
    else if (i === 0 && opts.href) td.append(el("a", {href: opts.href(r), text: fmt(v, c)}));
    else if (typeof v === "string" && v.length > (wide ? LONG : 3 * LONG)) {      // a long text: one line of it, the whole on hover, and a click to open it
      td.textContent = v; td.title = v; td.className = "clip";
      td.addEventListener("click", () => td.classList.toggle("open"));
    }
    else td.append(...gloss(fmt(v, c)));
    return td;
  };
  function draw() {
    let rows = t.rows.filter(r => !q || r.some((v, i) => fmt(v, t.columns[i]).toLowerCase().includes(q)));
    if (sort != null) rows = [...rows].sort((a, b) => compare(a[sort], b[sort]) * (desc ? -1 : 1));
    const pages = Math.max(1, Math.ceil(rows.length / PAGE));
    page = Math.min(Math.max(page, 0), pages - 1);
    heads.forEach((th, i) => {       // the column's name, and under it the words the view gives for it (table.labels)
      const words = t.labels && t.labels[t.columns[i]];
      th.replaceChildren(label(t.columns[i], true) + (sort === i ? (desc ? " ▼" : " ▲") : ""), words ? el("span", {class: "sub", text: words}) : "");
    });
    body.replaceChildren(...rows.slice(page * PAGE, page * PAGE + PAGE).map(r => el("tr", null, r.map((v, i) => cell(v, i, r)))));
    const shown = rows.length === t.rows.length ? `${rows.length} rows` : `${rows.length} of ${t.rows.length} rows match`;
    info.textContent = (pages > 1 ? `${shown}; showing ${page * PAGE + 1} to ${Math.min(rows.length, page * PAGE + PAGE)}` : shown).replace(/^1 rows/, "1 row");
    prev.disabled = page === 0;
    next.disabled = page >= pages - 1;
  }
  const box = el("input", {type: "search", placeholder: "Filter these rows", "aria-label": "Filter these rows",
    on: {input: e => { q = e.target.value.trim().toLowerCase(); page = 0; draw(); }}});
  const long = t.rows.length > PAGE;
  const node = el("div", null,
    el("div", {class: "tools"}, t.rows.length > 8 && !opts.noBox ? box : null, info, long ? prev : null, long ? next : null),
    el("div", {class: "scroll"}, el("table", {class: wide ? "wide" : null}, el("thead", null, el("tr", null, heads)), body)));
  node.filter = text => { q = text.trim().toLowerCase(); page = 0; draw(); };
  draw();
  // A table still wider than the page after wrapping says so: its right-hand columns are reached by scrolling it.
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => {
    const box = node.lastChild;
    if (box.scrollWidth > box.clientWidth + 2) node.insertBefore(el("p", {class: "about small",
      text: "This table is wider than the page: scroll it sideways for the columns on the right."}), box);
  });
  return node;
}

// ------------------------------------------------------------------------------------------------ charts
const W = 860;
function scale(lo, hi, a, b, log) {
  const g = v => (log ? Math.log10(v) : v), l = g(lo), h = g(hi) === g(lo) ? g(lo) + 1 : g(hi);
  return v => a + (g(v) - l) / (h - l) * (b - a);
}
function ticks(lo, hi, log) {
  const out = [];
  if (log) { for (let e = Math.ceil(Math.log10(lo)); e <= Math.floor(Math.log10(hi)); e++) out.push(10 ** e); return out.length ? out : [lo, hi]; }
  const raw = (hi - lo) / 5 || 1, mag = 10 ** Math.floor(Math.log10(raw)), by = [1, 2, 5, 10].map(m => m * mag).find(s => s >= raw);
  for (let i = Math.ceil(lo / by - 1e-9); i * by <= hi + by * 1e-9; i++) out.push(Number((i * by).toPrecision(12)));     // counted in whole steps, so zero is 0
  return out;
}
function extent(values, log, zero) {
  const xs = values.filter(v => typeof v === "number" && (!log || v > 0));
  if (!xs.length) return null;
  let lo = Math.min(...xs), hi = Math.max(...xs);
  if (zero && !log) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  const pad = log || zero ? 0 : (hi - lo || Math.abs(hi) || 1) * 0.05;
  return [log ? lo / 1.15 : lo - pad, log ? hi * 1.15 : hi + pad];
}
// The bottom axis of a chart whose rows run down the page: grid lines, tick values and the column's name with its unit.
function axisX(f, [lo, hi], y0, y1, log, name, limits) {
  return [ticks(lo, hi, log).map(v => [sv("line", {class: "gridline", x1: f(v), x2: f(v), y1: y0, y2: y1}),
    sv("text", {x: f(v), y: y1 + 14, "text-anchor": "middle", text: fmt(v)})]),
    (limits || []).map(m => [sv("line", {class: "limit", x1: f(m.value), x2: f(m.value), y1: y0 - 3, y2: y1}),
      sv("text", {class: "limit", x: f(m.value), y: y0 - 6, "text-anchor": "middle", text: m.label})]),
    sv("text", {x: f(hi), y: y1 + 28, "text-anchor": "end", text: name + (log ? ", log scale" : "")})];
}
// What a chart calls a column's mark: the view's words for it (decl.labels), else the column's name.
const word = (c, k) => (c.decl.labels && c.decl.labels[c.names[k]]) || label(c.names[k], true);
const marks = c => (Array.isArray(c.decl.limits) ? c.decl.limits.filter(m => m && typeof m.value === "number" && (!c.log || m.value > 0)) : []);
const swatch = (...shapes) => sv("svg", {width: 22, height: 12, viewBox: "0 0 22 12"}, shapes);
const diamond = (x, y, r, cls) => sv("path", {class: cls, d: `M${x - r} ${y}L${x} ${y - r}L${x + r} ${y}L${x} ${y + r}Z`});
const key = items => el("div", {class: "legend"}, items.map(([shape, text]) => el("span", null, shape, " ", text)));
// Frame for charts with one row of marks per table row: names down the left, a hover target per row.
function rowChart(t, rows, c, right, drawRow, under) {
  const L = 230, top = marks(c).length ? 20 : 6, h = 24, bottom = top + rows.length * h, g = sv("svg", {class: "chart", viewBox: `0 0 ${W} ${bottom + (under ? 34 : 4)}`, role: "img"});
  g.append(...(under ? under(L, W - right, top, bottom) : []).flat(Infinity));
  rows.forEach((r, k) => {
    const y = top + k * h + h / 2;
    if (r.header != null) return g.append(sv("text", {class: "name", x: 0, y: y + 4, "font-weight": 600, text: clip(r.header, 60)}));
    g.append(sv("text", {class: "name", x: L - 10, y: y + 4, "text-anchor": "end", text: clip(fmt(r[c.x]), 34)}), ...drawRow(r, y, L, W - right));
    g.append(tip(sv("rect", {class: "hit", x: 0, y: y - h / 2, width: W, height: h}), () => rowPairs(t, r)));
  });
  return g;
}
const DRAW = {
  bars(t, c) {
    const rows = t.rows.filter(r => typeof r[c.y] === "number"), ext = extent([...rows.map(r => r[c.y]), ...marks(c).map(m => m.value)], c.log, true);
    if (!ext || !rows.length) return null;
    if (!c.log) { const pad = (ext[1] - ext[0]) * 0.04; ext[0] -= ext[0] < 0 ? pad : 0; ext[1] += ext[1] > 0 ? pad : 0; }
    let f;
    const zero = () => f(c.log ? ext[0] : 0);
    return [rowChart(t, rows, c, 170, (r, y) => [
      sv("rect", {class: "c1", x: Math.min(f(r[c.y]), zero()), y: y - 7, width: Math.max(1, Math.abs(f(r[c.y]) - zero())), height: 14, rx: 3}),
      sv("text", {class: "name", x: Math.max(f(r[c.y]), zero()) + 6, y: y + 4, text: fmt(r[c.y], c.names.y, true)})],
      (a, b, y0, y1) => (f = scale(ext[0], ext[1], a, b, c.log), axisX(f, ext, y0, y1, c.log, word(c, "y"), marks(c))))];
  },
  interval_points(t, c) {
    const cap = 80, has = k => c[k] != null, all = t.rows.filter(r => ["y", "model"].some(k => has(k) && typeof r[c[k]] === "number"));
    const ext = extent([...all.flatMap(r => ["y", "low", "high", "model", "model_alt"].filter(has).map(k => r[c[k]])), ...marks(c).map(m => m.value)], c.log);
    if (!ext) return null;
    let rows = all.slice(0, cap), f;
    if (has("group")) {
      const groups = [...new Set(rows.map(r => r[c.group]))];
      rows = groups.flatMap(gname => [{header: `${label(c.names.group)}: ${fmt(gname)}`}, ...rows.filter(r => r[c.group] === gname)]);
    }
    const num = v => typeof v === "number" && (!c.log || v > 0);
    const chart = rowChart(t, rows, c, 30, (r, y) => [
      has("low") && num(r[c.low]) && num(r[c.high]) ? sv("line", {class: "c1 whisker", x1: f(r[c.low]), x2: f(r[c.high]), y1: y, y2: y}) : null,
      has("model_alt") && num(r[c.model_alt]) ? diamond(f(r[c.model_alt]), y, 5, "c2 hollow") : null,
      has("model") && num(r[c.model]) ? diamond(f(r[c.model]), y, 5, "c2") : null,
      num(r[c.y]) ? sv("circle", {class: "c1", cx: f(r[c.y]), cy: y, r: 4}) : null],
      (a, b, y0, y1) => (f = scale(ext[0], ext[1], a, b, c.log), axisX(f, ext, y0, y1, c.log, word(c, "y"), marks(c))));
    return [key([[swatch(has("low") ? sv("line", {class: "c1 whisker", x1: 1, x2: 21, y1: 6, y2: 6}) : null, sv("circle", {class: "c1", cx: 11, cy: 6, r: 4})),
      word(c, "y") + (has("low") ? `, with its interval (${label(c.names.low)} to ${label(c.names.high)})` : "")],
      has("model") ? [swatch(diamond(11, 6, 5, "c2")), word(c, "model")] : null,
      has("model_alt") ? [swatch(diamond(11, 6, 5, "c2 hollow")), word(c, "model_alt")] : null].filter(Boolean)), chart,
      all.length > cap ? el("p", {class: "about small", text: `The first ${cap} of ${all.length} rows are drawn; the table below holds them all.`}) : null];
  },
  ranges(t, c) {
    const ok = r => [c.outer_low, c.outer_high, c.low, c.high].every(i => typeof r[i] === "number") && r[c.outer_high] > r[c.outer_low];
    const rows = t.rows.filter(ok);
    if (!rows.length) return null;
    // A wide range that spans a factor of 100 or more above zero is drawn on a log scale, and its row says so.
    const isLog = r => r[c.outer_low] > 0 && r[c.outer_high] / r[c.outer_low] >= 100;
    const chart = rowChart(t, rows, c, 345, (r, y, a, b) => {
      const lg = isLog(r), g = scale(r[c.outer_low], r[c.outer_high], a, b, lg);
      const f = v => Math.max(a, Math.min(b, g(lg ? Math.max(v, r[c.outer_low]) : v))), x1 = f(r[c.low]), x2 = f(r[c.high]);
      return [sv("rect", {class: "track", x: a, y: y - 3, width: b - a, height: 6, rx: 3}),
        sv("rect", {class: "c1", x: Math.min(x1, x2), y: y - 6, width: Math.max(2, Math.abs(x2 - x1)), height: 12, rx: 3}),
        c.y != null && typeof r[c.y] === "number" ? diamond(f(r[c.y]), y, 5, "c2") : null,
        sv("text", {x: b + 8, y: y + 4, text: `${fmt(r[c.low])} to ${fmt(r[c.high])} within ${fmt(r[c.outer_low])} to ${fmt(r[c.outer_high])}` + (lg ? ", log scale" : "")})];
    });
    const span = (k1, k2) => (c.decl.labels && c.decl.labels[c.names[k1]]) || `${label(c.names[k1])} to ${label(c.names[k2])}`;
    return [key([[swatch(sv("rect", {class: "track", x: 1, y: 3, width: 20, height: 6, rx: 3})), `${span("outer_low", "outer_high")} (each row on its own scale)`],
      [swatch(sv("rect", {class: "c1", x: 1, y: 1, width: 20, height: 10, rx: 3})), span("low", "high")],
      c.y != null ? [swatch(diamond(11, 6, 5, "c2")), word(c, "y")] : null].filter(Boolean)), chart];
  },
  lines(t, c) {
    const rows = t.rows.filter(r => typeof r[c.x] === "number" && typeof r[c.y] === "number" && (!c.log || r[c.y] > 0));
    const more = ["model", "low", "high"].filter(k => c[k] != null), band = c.low != null && c.high != null;
    const ex = extent(rows.map(r => r[c.x])), ey = extent([...rows.flatMap(r => [r[c.y], ...more.map(k => r[c[k]])]), ...marks(c).map(m => m.value)], c.log);
    if (!ex || !ey) return null;
    const H = 320, fx = scale(ex[0], ex[1], 64, W - 20), fy = scale(ey[0], ey[1], H - 40, 10, c.log);
    const g = sv("svg", {class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img"}, ticks(ey[0], ey[1], c.log).map(v => [
      sv("line", {class: "gridline", x1: 64, x2: W - 20, y1: fy(v), y2: fy(v)}), sv("text", {x: 58, y: fy(v) + 4, "text-anchor": "end", text: fmt(v)})]),
      ticks(ex[0], ex[1]).map(v => sv("text", {x: fx(v), y: H - 24, "text-anchor": "middle", text: fmt(v)})),
      sv("line", {class: "axis", x1: 64, x2: W - 20, y1: H - 40, y2: H - 40}),
      marks(c).map(m => [sv("line", {class: "limit", x1: 64, x2: W - 20, y1: fy(m.value), y2: fy(m.value)}),
        sv("text", {class: "limit", x: W - 22, y: fy(m.value) - 4, "text-anchor": "end", text: m.label})]),
      sv("text", {x: W - 20, y: H - 6, "text-anchor": "end", text: word(c, "x")}),
      sv("text", {x: 4, y: 10, text: word(c, "y") + (c.log ? ", log scale" : "")}));
    const ok = v => typeof v === "number" && (!c.log || v > 0), path = (pts, k) => pts.filter(r => ok(r[c[k]])).map((r, i) => `${i ? "L" : "M"}${fx(r[c.x])} ${fy(r[c[k]])}`).join("");
    const groups = c.group == null ? [null] : [...new Set(rows.map(r => r[c.group]))];
    groups.forEach((name, k) => {
      const pts = rows.filter(r => c.group == null || r[c.group] === name).sort((a, b) => a[c.x] - b[c.x]), cls = `c${Math.min(k, 5) + 1}`;
      if (band) { const in2 = pts.filter(r => ok(r[c.low]) && ok(r[c.high]));
        if (in2.length) g.append(sv("path", {class: "c2 band", d: in2.map((r, i) => `${i ? "L" : "M"}${fx(r[c.x])} ${fy(r[c.high])}`).join("") + [...in2].reverse().map(r => `L${fx(r[c.x])} ${fy(r[c.low])}`).join("") + "Z"})); }
      if (c.model != null) g.append(sv("path", {class: "c2 line", d: path(pts, "model")}));
      else g.append(sv("path", {class: `${cls} line`, d: path(pts, "y")}));
      for (const r of pts) g.append(sv("circle", {class: cls, cx: fx(r[c.x]), cy: fy(r[c.y]), r: 3}),
        tip(sv("circle", {class: "hit", cx: fx(r[c.x]), cy: fy(r[c.y]), r: 9}), () => rowPairs(t, r)));
    });
    if (c.model != null) return [key([[swatch(sv("circle", {class: "c1", cx: 11, cy: 6, r: 3})), word(c, "y")],
      [swatch(sv("line", {class: "c2 line", x1: 1, x2: 21, y1: 6, y2: 6})), word(c, "model")],
      band ? [swatch(sv("rect", {class: "c2 band", x: 1, y: 1, width: 20, height: 10})), `${word(c, "low")} (${label(c.names.low)} to ${label(c.names.high)})`] : null,
      groups.length === 1 && groups[0] != null ? [null, `${label(c.names.group)}: ${fmt(groups[0])}`] : null].filter(Boolean)), g];
    return [groups.length > 1 ? key(groups.map((name, k) => [swatch(sv("line", {class: `c${Math.min(k, 5) + 1} line`, x1: 1, x2: 21, y1: 6, y2: 6})),
      `${label(c.names.group)}: ${fmt(name)}`])) : null, g];
  },
  // Links run left to right: a node sits one column to the right of the furthest-right node it depends on.
  graph(t, c, tables) {
    const nt = tables[c.decl.nodes], state = nt ? nt.columns.indexOf(c.decl.group) : -1, nodes = new Map();
    const add = (id, row) => { if (!nodes.has(id)) nodes.set(id, {id, row, from: [], to: [], word: row && state >= 0 ? row[state] : null}); return nodes.get(id); };
    for (const r of nt ? nt.rows : []) add(r[0], r);
    const links = t.rows.map(r => ({a: add(r[c.x]), b: add(r[c.y]), r}));
    for (const l of links) { l.a.to.push(l); l.b.from.push(l); }
    if (!nodes.size) return null;
    const depth = (n, seen = new Set()) => n.depth ?? (seen.has(n) ? 0 : (seen.add(n), n.depth = n.from.length ? 1 + Math.max(...n.from.map(l => depth(l.a, seen))) : 0));
    const layers = [];
    for (const n of nodes.values()) (layers[depth(n)] = layers[depth(n)] || []).push(n);
    layers.forEach((layer, d) => {
      const at = n => (d ? n.from.reduce((s, l) => s + l.a.pos, 0) / n.from.length : rank(n.word === "same" ? "ok" : n.word === "outside" ? "not_available" : "problem"));
      layer.map(n => [at(n), n]).sort((p, q) => p[0] - q[0] || compare(p[1].id, q[1].id)).forEach(([, n], k) => { n.pos = (k + 0.5) / layer.length; n.k = k; });
    });
    const bw = 142, bh = 20, gap = 6, tall = Math.max(...layers.map(l => l.length)) * (bh + gap), wide = Math.max(W, layers.length * (bw + 24) - 20);
    const g = sv("svg", {class: "chart", viewBox: `0 0 ${wide} ${tall + 8}`, role: "img"});
    g.style.maxWidth = wide + "px";      // drawn at its own size when there is room, never enlarged
    layers.forEach((layer, d) => layer.forEach(n => {
      n.x = layers.length > 1 ? d * (wide - bw - 2) / (layers.length - 1) + 1 : 1;
      n.y = 4 + (tall - layer.length * (bh + gap)) / 2 + n.k * (bh + gap);
    }));
    for (const l of links) {
      const x1 = l.a.x + bw, y1 = l.a.y + bh / 2, x2 = l.b.x, y2 = l.b.y + bh / 2, mid = (x1 + x2) / 2;
      l.path = sv("path", {class: `edge ${tone(l.r[c.group])}`, d: `M${x1} ${y1}C${mid} ${y1} ${mid} ${y2} ${x2} ${y2}`});
      g.append(l.path);
    }
    const tail = (id, k) => String(id).split("/").slice(-k).join("/"), seen = {};
    for (const n of nodes.values()) seen[tail(n.id, 1)] = (seen[tail(n.id, 1)] || 0) + 1;
    const short = id => tail(id, seen[tail(id, 1)] > 1 ? 2 : 1);     // two parts of the path where one is not unique
    for (const n of nodes.values()) {
      n.box = sv("g", {class: `node ${tone(n.word)}`}, sv("rect", {x: n.x, y: n.y, width: bw, height: bh, rx: 4}),
        sv("text", {class: "glyph", x: n.x + 7, y: n.y + 14, "font-weight": 700, text: (MARKS[n.word] || ["·"])[0]}),
        sv("text", {class: "name", x: n.x + 22, y: n.y + 14, text: clip(short(n.id), 20)}));
      tip(n.box, () => [...(n.row ? rowPairs(nt, n.row) : [[label(c.names.x), n.id]]),
        ["computed from", `${n.from.length} file(s)`], ["used by", `${n.to.length} file(s)`]]);
      const light = on => () => { g.classList.toggle("focus", on); for (const l of [...n.from, ...n.to]) for (const p of [l.path, l.a.box, l.b.box]) p.classList.toggle("hot", on); };
      n.box.addEventListener("mouseenter", light(true));
      n.box.addEventListener("mouseleave", light(false));
      g.append(n.box);
    }
    const words = [...new Set([...nodes.values()].map(n => n.word))].filter(w => w != null);
    return [key(words.map(w => [mark(w), (MARKS[w] || [])[3] || ""])), g,
      el("p", {class: "about small", text: "A box shows the last part of a file's path; point at it for the full path and to pick out its links."})];
  }};
const COLUMN_KEYS = ["x", "y", "low", "high", "group", "model", "model_alt", "outer_low", "outer_high", "panels"];
const PANEL_ROWS = 80;      // panels holding more rows than this together are shown one at a time
function chartView(decl, tables) {
  const fig = el("figure", null, el("figcaption", {text: decl.title || `${decl.kind} of ${decl.table}`}),
    el("p", {class: "about"}, gloss(decl.how_to_read || "This chart does not say how to read it.")));
  const cannot = why => (fig.append(el("p", {class: "muted", text: `This chart cannot be drawn: ${why}.`})), fig);
  let t = tables[decl.table];
  const c = {names: {}, decl, log: decl.scale === "log"};
  if (!DRAW[decl.kind]) return cannot(`the page has no chart of kind "${decl.kind}"`);
  if (!t || !Array.isArray(t.columns)) return cannot(`the view has no table "${decl.table}"`);
  for (const k of COLUMN_KEYS) if (decl[k] != null) {
    c[k] = t.columns.indexOf(decl[k]);
    c.names[k] = decl[k];
    if (c[k] < 0) return cannot(`table "${decl.table}" has no column "${decl[k]}"`);
  }
  const need = {ranges: ["x", "low", "high", "outer_low", "outer_high"]}[decl.kind] || ["x", "y"];
  if (need.some(k => c[k] == null)) return cannot(`a ${decl.kind} chart needs the columns ${need.join(", ")}`);
  const only = Object.entries(decl.where || {});       // {column: value}: the rows drawn
  for (const [col] of only) if (!t.columns.includes(col)) return cannot(`table "${decl.table}" has no column "${col}"`);
  if (only.length) t = {...t, rows: t.rows.filter(r => only.every(([col, v]) => r[t.columns.indexOf(col)] === v))};
  const said = only.length ? `, rows with ${only.map(([col, v]) => `${label(col)} ${fmt(v)}`).join(" and ")}` : "";
  const from = el("p", {class: "about small", text: `Drawn from the table "${label(decl.table)}" below${said}. Point at a mark to see its row.`});
  if (c.panels == null) {
    const parts = DRAW[decl.kind](t, c, tables);
    if (!parts) return cannot(`table "${decl.table}" has no rows with numbers to plot`);
    fig.append(...parts.filter(Boolean), from);
    return fig;
  }
  // One chart per value of the panels column, each on its own axis; a long set is shown one panel at a time.
  const panels = [...new Set(t.rows.map(r => r[c.panels]))].map(v => [v, {...t, rows: t.rows.filter(r => r[c.panels] === v)}]);
  if (!panels.length) return cannot(`table "${decl.table}" has no rows with numbers to plot`);
  const head = ([v, sub]) => `${label(decl.panels)}: ${fmt(v)} (${sub.rows.length} row${sub.rows.length === 1 ? "" : "s"})`;
  const one = (p, legend) => [el("h4", {text: head(p)}), ...(DRAW[decl.kind](p[1], c, tables) || [el("p", {class: "muted", text: "No rows with numbers to plot."})])
    .filter(n => n && (legend || !(n.classList && n.classList.contains("legend"))))];
  if (panels.length > 1 && t.rows.length > PANEL_ROWS) {
    const holder = el("div"), show = i => holder.replaceChildren(...one(panels[i], true));
    fig.append(el("div", {class: "tools"}, el("label", null, `Show one ${label(decl.panels)}: `,
      el("select", {on: {change: e => show(Number(e.target.value))}}, panels.map((p, i) => el("option", {value: i, text: head(p).replace(/^[^:]*: /, "")})))),
      el("span", {text: `${panels.length} panels hold ${t.rows.length} rows; one is drawn at a time, starting with the first in the table's order.`})), holder, from);
    show(0);
  } else fig.append(...panels.flatMap((p, i) => one(p, i === 0)), from);
  return fig;
}

// ------------------------------------------------------------------------------------------------ a view
const state = {runs: null, over: null, turn: 0};
const link = (run, view) => `#run=${encodeURIComponent(run)}&view=${encodeURIComponent(view)}`;
const viewName = id => ((state.runs.views.find(v => v.id === id) || {}).label || id);

// A check that asks something of the reader (problem, look) is an open card. One that asks nothing (ok, not available)
// is one line with its title, which opens to the same card.
function checkCard(c) {
  const read = (c.evidence || []).map(e => (e.field ? `${e.file} (${e.field})` : e.file)).join("; ");
  const table = c.rows || c.table, n = table && table.rows ? table.rows.length : 0, quiet = c.status === "ok" || c.status === "not_available";
  const head = [mark(c.status), el("span", {class: "title", text: c.title || c.id}), quiet ? el("span", {class: "brief", text: c.says}) : null,
    el("span", {class: "case"}, gloss((c.case ? `scorecard case ${c.case} · ` : "") + c.id))];
  const body = [el("p", {class: "says"}, gloss(c.says)), read ? el("p", {class: "read", text: `Read from: ${read}`}) : null,
    n ? el("details", null, el("summary", {text: `Show the ${n} row${n > 1 ? "s" : ""} behind this check`}), tableView(table)) : null];
  return quiet ? el("details", {class: `card check line ${tone(c.status)}`}, el("summary", {class: "head", title: "Click to open this check"}, head), body)
    : el("div", {class: `card check ${tone(c.status)}`}, el("div", {class: "head"}, head), body);
}
function checksView(checks, run, here) {
  const groups = new Map();
  for (const c of checks) groups.set(c.step, [...(groups.get(c.step) || []), c]);
  const quiet = checks.filter(c => c.status === "ok" || c.status === "not_available").length;
  const out = [el("h2", {text: "Checks"}),
    el("p", {class: "about", text: "Each check is a rule in code, run on this run's files." + (groups.size > 1 ? " Views are in reading order; within a view the most serious check is first." : " Most serious first.")
      + (quiet ? ` The ${quiet} with nothing for you to do ${quiet === 1 ? "is one line" : "are one line each"}: click a line for its sentence and the file it read.` : "")}),
    key(ORDER.map(s => [mark(s), MARKS[s][3]]))];
  for (const [step, cs] of groups) {        // in the order given: the views' reading order, whatever their statuses
    if (groups.size > 1 || (step && step !== here))
      out.push(el("h3", null, `${viewName(step)} · `, el("a", {href: link(run, step), text: "open this view for the evidence"})));
    out.push(...[...cs].sort((a, b) => rank(a.status) - rank(b.status)).map(checkCard));
  }
  return out;
}
function renderView(data, run, here) {
  const out = [], facts = Object.entries(data.facts || {}), notes = Object.entries(data.notes || {}), tables = data.tables || {};
  if ((data.checks || []).length) out.push(...checksView(data.checks, run, here));
  if (facts.length) out.push(el("h2", {text: "Facts"}), el("dl", {class: "facts"}, facts.map(([k, v]) => {
    const text = fmt(v, k, true);
    return el("div", {class: text.length > 44 ? "wide" : null}, el("dt", {text: label(k)}), el("dd", null, gloss(text)));
  })));
  if (notes.length) out.push(el("h2", {text: "Notes"}), notes.map(([k, v]) => el("p", null, el("strong", {text: `${label(k).replace(/^./, ch => ch.toUpperCase())}. `}), gloss(fmt(v)))));
  if ((data.charts || []).length) out.push(el("h2", {text: "Charts"}), data.charts.map(d => chartView(d, tables)));
  if (Object.keys(tables).length) out.push(el("h2", {text: "Tables"}),
    el("p", {class: "about", text: "Click a column name to sort. Units are in the column names."}));
  const none = Object.keys(tables).filter(k => tables[k] == null);      // present with the value null: not recorded
  for (const [name, t] of Object.entries(tables)) {
    if (t == null) continue;
    out.push(el("h3", {text: label(name)}), t.about ? el("p", {class: "about"}, gloss(t.about)) : null);
    out.push(Array.isArray(t.columns) && Array.isArray(t.rows) ? (t.rows.length ? tableView(t) : el("p", {class: "muted", text: "No rows."}))
      : el("p", {class: "muted", text: "Not available: this table has no columns and rows."}));
  }
  if (none.length) out.push(el("h3", {text: "Not recorded"}), el("p", {class: "muted",
    text: `This run's files record nothing for ${none.length === 1 ? "this table" : "these tables"}: ${none.map(k => label(k)).join(", ")}. The notes above say why.`}));
  return out;
}

// ------------------------------------------------------------------------------------------------ pages
const main = document.getElementById("main"), rail = document.getElementById("rail"), where = document.getElementById("where");
async function get(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
// What the run list holds, in words: run folders given by name, and the folders searched, each only when there are any.
function scanned(r) {
  const dirs = r.run_dirs || [], roots = r.roots || [];
  return [dirs.length ? `the run folder${dirs.length > 1 ? "s" : ""} given by name (${dirs.join(", ")})` : "",
    roots.length ? `every folder holding a run_state.json under ${roots.join(", ")}` : ""].filter(Boolean).join(", and ") || "no folder";
}
function showRuns() {
  const cols = ["run", "status", "stage", "pipeline", "round", "model_spend_usd", "last_file_written", "question"], ids = new Map();
  const table = rows => {
    const t = {columns: cols, rows: rows.map(r => {
      const row = [r.label, r.paused ? `paused: ${r.paused}` : r.status, r.state, r.pipeline, r.round, r.spent_usd, r.last_written_utc ? `${r.last_written_utc}, ${r.last_written_ago} ago` : null, r.question.length > 90 ? r.question.slice(0, 90) + "…" : r.question];
      ids.set(row, r.id);
      return row;
    })};
    return tableView(t, {noBox: true, href: row => link(ids.get(row), "overview")});
  };
  const live = state.runs.runs.filter(r => !r.copy), copies = state.runs.runs.filter(r => r.copy), parts = [table(live), table(copies)];
  rail.replaceChildren();
  where.textContent = "";
  main.replaceChildren(el("h1", {text: "Runs"}),
    el("p", {class: "about", text: `This list holds ${scanned(state.runs)}; the run whose files were written most recently is first. ` +
      "Status is how the run ended; stage is the last state of its stage machine; model spend is language-model spend only; last file written is the " +
      "newest modification time among the run's files, which tells a run still at work from one that stopped. Choose a run to verify it."}),
    el("div", {class: "tools"}, el("input", {type: "search", placeholder: "Search runs", "aria-label": "Search runs", autofocus: true,
      on: {input: e => parts.forEach(p => p.filter(e.target.value))}})),
    parts[0], copies.length ? el("details", null, el("summary", {text: `${copies.length} stored copies and backups of runs`}), parts[1]) : "");
}
function showRail(run, here) {
  const over = state.over.data.tables.views, at = k => (over ? over.columns.indexOf(k) : -1);
  const status = id => { const r = over && over.rows.find(x => x[at("view")] === id); return r ? r[at("status")] : null; };
  const heads = {spec: "Verify the run", steps: "Inspect and track"};
  rail.replaceChildren(...state.runs.views.flatMap(v => [heads[v.id] ? el("div", {class: "railhead", text: heads[v.id]}) : null,
    el("a", {href: link(run.id, v.id), class: (v.id === here ? "here " : "") + (v.built ? "" : "unbuilt"), "aria-current": v.id === here ? "page" : null},
      mark(v.built ? status(v.id) || "not_available" : "not_built", true), el("span", {text: v.label}),
      v.built ? null : el("span", {class: "goal"}, gloss(`goal ${v.goal}`)))]).filter(Boolean));
}
async function route() {
  const p = new URLSearchParams(location.hash.slice(1)), id = p.get("run"), here = p.get("view") || "overview", turn = ++state.turn;
  hint.hidden = true;
  try {
    if (!state.runs) {
      state.runs = await get("/api/runs");
      setGlossary(await get("/api/glossary").catch(e => ({entries: [], why: `the glossary could not be fetched: ${e.message}`})));
    }
    if (!id) return showRuns();
    const run = state.runs.runs.find(r => r.id === id);
    if (!run) throw new Error("This run is not under the folders the workbench was started on.");
    const url = v => `/api/view?run=${encodeURIComponent(id)}&view=${encodeURIComponent(v)}`;
    if (!state.over || state.over.id !== id) { main.replaceChildren(el("p", {class: "muted", text: "Reading the run's files…"})); state.over = {id, data: await get(url("overview"))}; }
    const data = here === "overview" ? state.over.data : await get(url(here));
    if (turn !== state.turn) return;
    GLOSS.used.clear();
    showRail(run, here);
    where.textContent = run.label;
    main.replaceChildren(el("h1", {text: viewName(here)}),
      el("p", {class: "about"}, `Run ${run.label}, in `, el("code", {text: run.dir}), ". ",
        el("button", {text: "Read the files again", on: {click: () => { state.runs = state.over = null; route(); }}})),
      ...renderView(data, id, here).flat(Infinity).filter(Boolean));
    main.append(glossaryFoot() || "");
    scrollTo(0, 0);
  } catch (e) {
    main.replaceChildren(el("div", {class: "card check t-na"}, mark("not_available"), el("p", {class: "says", text: e.message})));
  }
}
addEventListener("hashchange", route);
route();
