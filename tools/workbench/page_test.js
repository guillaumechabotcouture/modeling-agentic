"use strict";
// Cases for the page script (static/app.js), run by `workbench.py --self-test` when node is installed:
//     node tools/workbench/page_test.js
// The script is loaded against a small stand-in for the browser's document, so what it builds can be read here.
const fs = require("fs"), path = require("path");

class Node {
  constructor(tag) {
    this.tag = tag; this.attrs = {}; this.kids = []; this.listeners = {}; this.style = {};
    const words = () => String(this.attrs.class || "").split(" ").filter(Boolean);
    this.classList = {contains: c => words().includes(c),
      toggle: (c, on) => { const add = on === undefined ? !words().includes(c) : on; this.attrs.class = [...words().filter(w => w !== c), ...(add ? [c] : [])].join(" "); }};
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  append(...k) { this.kids.push(...k); }
  replaceChildren(...k) { this.kids = k; }
  addEventListener(e, f) { this.listeners[e] = f; }
  set textContent(v) { this.kids = [String(v)]; }
  get textContent() { return this.kids.map(k => (k instanceof Node ? k.textContent : String(k))).join(""); }
  set title(v) { this.attrs.title = v; }
  set className(v) { this.attrs.class = v; }
  set hidden(v) { this.attrs.hidden = v; }
  all(tag) { return this.kids.flatMap(k => (k instanceof Node ? [...(k.tag === tag ? [k] : []), ...k.all(tag)] : [])); }
}
const byId = {};
global.document = {createElement: t => new Node(t), createElementNS: (_, t) => new Node(t),
  getElementById: id => (byId[id] = byId[id] || new Node("#" + id))};
global.location = {hash: ""};
global.addEventListener = () => {};
global.scrollTo = () => {};
global.innerWidth = global.innerHeight = 1000;
global.fetch = () => new Promise(() => {});      // the page's first request never answers: nothing is drawn at load

const src = fs.readFileSync(path.join(__dirname, "static", "app.js"), "utf8");
const names = ["ticks", "checkCard", "checksView", "setGlossary", "gloss", "glossaryFoot", "state", "scanned", "actionForm", "tableView",
  "renderView", "post", "imagesView", "imageBox", "chartView"];
const page = new Function(`${src}\n;return {${names.map(n => `${n}: typeof ${n} === "undefined" ? undefined : ${n}`).join(", ")}};`)();

const fails = [];
function ok(name, fn) {
  let why = null;
  try { const r = fn(); if (r !== true) why = String(r); } catch (e) { why = `${e.name}: ${e.message}`; }
  console.log(`  ${why == null ? "ok  " : "FAIL"} ${name}${why == null ? "" : ": " + why.slice(0, 300)}`);
  if (why != null) fails.push(name.split(" ")[0]);
}

ok("P1 an axis that crosses zero has a tick at exactly 0, never a rounding remainder such as 6.9e-18", () => {
  for (const [lo, hi] of [[-0.3, 0.3], [-4.95, 2.5], [-0.15, 0.1], [-0.07, 0.03], [-1.2, 0.4]]) {
    const t = page.ticks(lo, hi, false);
    if (!t.includes(0) || t.some(v => v !== 0 && Math.abs(v) < 1e-9)) return `ticks(${lo}, ${hi}) = ${t.join(", ")}`;
  }
  return true;
});
const check = status => ({id: "v.name", step: "v", title: "The check's title", status, says: "What it found.", case: "C13",
  evidence: [{file: "a.yaml", field: "data.x"}], rows: null});
ok("P2 an ok or not_available check is one line that still shows its title and opens to the full card", () => {
  for (const s of ["ok", "not_available"]) {
    const c = page.checkCard(check(s)), line = c.all("summary")[0];
    if (c.tag !== "details" || !line) return `${s}: drawn as <${c.tag}>, not a line that opens`;
    if (!line.textContent.includes("The check's title")) return `${s}: the line is "${line.textContent}"`;
    if (!c.textContent.includes("What it found.") || !c.textContent.includes("a.yaml (data.x)")) return `${s}: the card inside lacks the sentence or the file read`;
  }
  return true;
});
ok("P3 a problem or look check stays an open card", () => {
  for (const s of ["problem", "look"]) {
    const c = page.checkCard(check(s));
    if (c.tag !== "div" || c.all("summary").length || !c.textContent.includes("What it found.")) return `${s}: drawn as <${c.tag}>`;
  }
  return true;
});
ok("P4 the checks of several views stay in the order given, whatever their statuses; within a view the worst is first", () => {
  page.state.runs = {views: [{id: "a", label: "View A"}, {id: "b", label: "View B"}]};
  const cs = [{...check("ok"), step: "a", title: "a-ok"}, {...check("look"), step: "a", title: "a-look"}, {...check("problem"), step: "b", title: "b-problem"}];
  const text = page.checksView(cs, "run", "overview").flat(Infinity).filter(n => n instanceof Node).map(n => n.textContent).join(" | ");
  const at = w => text.indexOf(w);
  return (at("View A") >= 0 && at("View A") < at("a-look") && at("a-look") < at("a-ok") && at("a-ok") < at("View B") && at("View B") < at("b-problem")) || text;
});
const glossary = {source: "the spec", why: null, entries: [
  {code: "C13", kind: "scorecard case", meaning: "meaning of C13", section: "§8"},
  {code: "G2", kind: "goal", meaning: "meaning of G2", section: "§10"},
  {code: "H3", kind: "gap", meaning: "meaning of H3", section: "§7"},
  {code: "workbench threshold", kind: "term", meaning: "meaning of the term", section: "§3"}]};
ok("P5 a code the glossary defines carries its meaning on hover; a run's own name that looks like a code does not", () => {
  page.setGlossary(glossary);
  const parts = page.gloss("Group G2 is flagged (scorecard case C13, goal G2, goal G2b) against two workbench thresholds.");
  const marked = parts.filter(p => p instanceof Node).map(p => [p.textContent, p.attrs.title]);
  const want = [["scorecard case C13", "meaning of C13 (§8)"], ["goal G2", "meaning of G2 (§10)"], ["workbench thresholds", "meaning of the term (§3)"]];     // the hover also says where it is defined
  return (JSON.stringify(marked) === JSON.stringify(want) && parts.map(p => (p instanceof Node ? p.textContent : p)).join("") ===
    "Group G2 is flagged (scorecard case C13, goal G2, goal G2b) against two workbench thresholds.") || JSON.stringify(marked);
});
ok("P6 the glossary at the foot of the page lists the codes that page used, each with its meaning", () => {
  const foot = page.glossaryFoot();
  const text = foot ? foot.textContent : "";
  return (text.includes("scorecard case C13") && text.includes("meaning of C13") && text.includes("meaning of the term")
    && !text.includes("meaning of H3") && text.includes("the spec")) || text;
});
ok("P7 fail closed: with no glossary the text is shown as it is, and the foot says the meanings are not available", () => {
  page.setGlossary({entries: [], source: "the spec", why: "the spec could not be read"});
  const parts = page.gloss("scorecard case C13");
  const foot = page.glossaryFoot();
  return (parts.length === 1 && parts[0] === "scorecard case C13" && !!foot && foot.textContent.includes("could not be read")) || JSON.stringify(parts);
});
ok("P8 the run list says what was scanned: run folders given by name, and the roots searched, each only when there are any", () => {
  const a = page.scanned({roots: [], run_dirs: ["/x/run1"]}), b = page.scanned({roots: ["/keep"], run_dirs: []});
  return (a.includes("/x/run1") && !a.includes("under") && b.includes("under /keep") && !b.includes("given")) || `${a} | ${b}`;
});
// ---- the one write: a table's `actions` (workbench.py's docstring), drawn by generic code
const actions = {post: "/api/note", key: "id", label: "Review", about: "Saved as one line of the notes file.", fields: [
  {name: "disposition", label: "Disposition", choices: ["open", "declared"]}, {name: "same_as", label: "Same as row", rows: true},
  {name: "note", label: "Note", text: true}],
  add: {label: "Add a row", fields: [{name: "kind", label: "Kind", choices: ["issue", "assumption"], required: true},
    {name: "statement", label: "Statement", text: true, required: true}, {name: "note", label: "Note", text: true}]}};
const button = (node, words) => node.all("button").find(b => b.textContent === words);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
ok("P9 a row's form sends the row's key and only the fields filled in; another row is offered by its key, as text", () => {
  const sent = [], form = page.actionForm(actions, "A-1", ["B-2", "<b>C</b>"], body => (sent.push(body), Promise.resolve()), () => {});
  const [disposition, sameAs] = form.all("select"), note = form.all("textarea")[0];
  const offered = sameAs.all("option").map(o => o.textContent);
  if (!same(offered.slice(1), ["B-2", "<b>C</b>"]) || sameAs.all("b").length) return `the other rows are offered as ${JSON.stringify(offered)}`;
  if (!form.textContent.includes("A-1") || !form.textContent.includes("Saved as one line")) return `the form does not name its row or say what Save does: ${form.textContent}`;
  disposition.value = "declared"; note.value = "  accepted as a limit  ";
  button(form, "Save").listeners.click();
  return same(sent, [{row: "A-1", disposition: "declared", note: "accepted as a limit"}]) || JSON.stringify(sent);
});
ok("P10 a form with nothing filled in sends nothing and says so", () => {
  const sent = [], form = page.actionForm(actions, "A-1", [], body => (sent.push(body), Promise.resolve()), () => {});
  button(form, "Save").listeners.click();
  return (sent.length === 0 && /nothing/i.test(form.textContent)) || `${JSON.stringify(sent)} | ${form.textContent}`;
});
ok("P11 the form that adds a row sends row null; a field that is needed and empty stops it, with the field named", () => {
  const sent = [], form = page.actionForm(actions.add, null, [], body => (sent.push(body), Promise.resolve()), () => {});
  const kind = form.all("select")[0], statement = form.all("textarea")[0];
  kind.value = "assumption";
  button(form, "Save").listeners.click();
  if (sent.length || !form.textContent.includes("Statement is needed")) return `sent ${JSON.stringify(sent)}; says "${form.textContent}"`;
  statement.value = "The two rounds share one frame.";
  button(form, "Save").listeners.click();
  return same(sent, [{row: null, kind: "assumption", statement: "The two rounds share one frame."}]) || JSON.stringify(sent);
});
const noted = {columns: ["id", "statement"], rows: [["A-1", "first <i>statement</i>"], ["B-2", "second"]], actions};
ok("P12 a table that declares actions has a button on each row and one to add a row, only when the page knows the run; "
  + "the button opens that row's form", () => {
  const plain = page.tableView({...noted, actions: undefined}, {run: "r1"}), unknown = page.tableView(noted, {});
  if (plain.all("button").length || unknown.all("button").length) return "a table with no actions, or with no run to post for, has buttons";
  const t = page.tableView(noted, {run: "r1"}), rows = t.all("button").filter(b => b.textContent === "Review");
  if (rows.length !== 2 || !button(t, "Add a row")) return `${rows.length} row buttons; add: ${!!button(t, "Add a row")}`;
  if (t.all("select").length) return "a form is open before any button is pressed";
  rows[1].listeners.click();
  const form = t.all("tr").find(r => r.all("select").length);
  if (!form || !form.textContent.includes("B-2")) return "the second row's button does not open its form";
  const offered = form.all("select")[1].all("option").map(o => o.textContent).slice(1);
  return (same(offered, ["A-1"]) && t.all("i").length === 0) || `offers ${JSON.stringify(offered)}; markup from a row's text was built`;
});
ok("P13 a save posts JSON to the declared address for this run, with the token the server put in the page", () => {
  const calls = [];
  global.document.querySelector = q => (q === 'meta[name="workbench-token"]' ? {getAttribute: () => "tok-1"} : null);
  global.fetch = (url, o) => (calls.push([url, o]), Promise.resolve({ok: true, json: () => Promise.resolve({ok: true})}));
  const t = page.tableView(noted, {run: "r 1", done: () => calls.push("done")});
  t.all("button").filter(b => b.textContent === "Review")[0].listeners.click();
  const form = t.all("tr").find(r => r.all("select").length);
  form.all("select")[0].value = "open";
  button(form, "Save").listeners.click();
  const [url, o] = calls[0] || [];
  global.fetch = () => new Promise(() => {});
  return (url === "/api/note?run=r%201" && o.method === "POST" && o.headers["X-Workbench-Token"] === "tok-1"
    && o.headers["Content-Type"] === "application/json" && o.body === JSON.stringify({row: "A-1", disposition: "open"})) || JSON.stringify(calls);
});
ok("P14 a table marked lead is drawn straight after the facts, before the notes and the charts; the others stay under Tables", () => {
  const data = {checks: [], facts: {n_open: 2}, notes: {terms: "What the words mean."}, charts: [{kind: "bars", table: "by_x", x: "x", y: "n",
    title: "Chart title", how_to_read: "How to read it."}], tables: {rows: {columns: ["id"], rows: [["A-1"]], about: "The leading table.", lead: true},
    by_x: {columns: ["x", "n"], rows: [["a", 1]], about: "The other table."}}};
  const text = page.renderView(data, "r1", "v").flat(Infinity).filter(n => n instanceof Node).map(n => n.textContent).join(" | ");
  const at = w => text.indexOf(w), order = ["Facts", "The leading table.", "What the words mean.", "Chart title", "Tables", "The other table."].map(at);
  return (order.every(i => i >= 0) && same(order, [...order].sort((a, b) => a - b)) && text.split("The leading table.").length === 2) || `${order} in ${text}`;
});
ok("P16 a column the table names in wrap holds its whole text; another long text in a wide table is cut to one line; a long name may break only after a separator", () => {
  const long = "word ".repeat(40).trim(), cols = ["id", "a", "b", "c", "d", "e", "f", "g", "h", "i", "statement", "other"];
  const t = page.tableView({columns: cols, rows: [["A-1", 1, 2, 3, 4, 5, 6, 7, 8, 9, long, long]], wrap: ["statement"]});
  const cells = t.all("td"), whole = cells[10], cut = cells[11];
  const name = page.tableView({columns: ["id"], rows: [["kind_of:a_long/name,with:parts"]]}).all("td")[0];
  if (name.textContent !== "kind_of:a_long/name,with:parts" || name.all("wbr").length !== 6) return `a long name is ${name.textContent} with ${name.all("wbr").length} places to break`;
  return (whole.attrs.class === "whole" && whole.textContent === long && cut.attrs.class === "clip" && cut.attrs.title === long) || `${whole.attrs.class} | ${cut.attrs.class}`;
});
ok("P15 the add button opens the form that adds a row, and closes it again", () => {
  const t = page.tableView(noted, {run: "r1"}), add = button(t, "Add a row");
  add.listeners.click();
  const open = t.all("textarea").length;
  add.listeners.click();
  return (open === 2 && t.all("textarea").length === 0) || `${open} text boxes when open, ${t.all("textarea").length} when closed`;
});
// ---- images (§5.12): a view's `images` declaration and a chart's `image`, drawn by generic code
const sha = n => String(n).repeat(64), shots = {columns: ["name", "file", "sha256", "title", "caption", "part", "state", "view"], rows: [
  ["a", "pics/a b.png", sha(1), "Title of <b>A</b>", "Caption of A.", "first_part", "same", "b"],
  ["b", "pics/b.png", sha(2), null, null, "second_part", "changed", "nowhere"],
  ["c", "pics/c.png", sha(3), "Title of C", "Caption of C.", "first_part", "same", "b"]]};
const decl = {table: "shots", path: "file", hash: "sha256", title: "title", caption: "caption", group: "part", details: ["state", "view"]};
const drawn = (d, t = shots) => { const box = new Node("div"); box.append(...page.imagesView(d, {shots: t}, "r 1").flat(Infinity).filter(Boolean)); return box; };
ok("P17 images: one figure per row with its title, its caption and the image asked of the file endpoint by run, path and hash; "
  + "gathered under each value of the declared column; text from a run is never markup", () => {
  page.state.runs = {views: [{id: "a", label: "View A"}, {id: "b", label: "View B"}]};
  const box = drawn(decl), figs = box.all("figure"), imgs = box.all("img"), heads = box.all("h3").map(h => h.textContent);
  if (figs.length !== 3 || imgs.length !== 3) return `${figs.length} figures, ${imgs.length} images`;
  if (!same(heads, ["part: first part (2 images)", "part: second part (1 image)"])) return JSON.stringify(heads);
  if (imgs[0].attrs.src !== `/api/file?run=r%201&path=pics%2Fa%20b.png&sha=${sha(1)}`) return imgs[0].attrs.src;
  const text = box.textContent, at = w => text.indexOf(w), order = ["part: first part", "Title of <b>A</b>", "Caption of A.", "Title of C", "part: second part", "pics/b.png"].map(at);
  if (!order.every(i => i >= 0) || !same(order, [...order].sort((a, b) => a - b)) || box.all("b").length) return `${order} in ${text}`;
  return (imgs[0].attrs.alt === "Title of <b>A</b>" && imgs[2].attrs.alt === "pics/b.png") || `alt: ${imgs.map(i => i.attrs.alt)}`;
});
ok("P18 the columns named in details are listed with each image, before it, with their names; a state word has its mark, and a view's id links to that view", () => {
  const figs = drawn(decl).all("figure"), first = figs[0].all("dl")[0], last = figs[2];
  if (!first || !same(first.all("dt").map(x => x.textContent), ["state", "view"])) return first ? first.textContent : "no list with the image";
  if (figs[0].kids.indexOf(first) > figs[0].kids.findIndex(k => k instanceof Node && k.all("img").length)) return "the details come after the image, where a tall image hides them";
  const jump = drawn(decl).all("button").map(b => b.textContent);
  if (!same(jump, ["first part (2 images)", "second part (1 image)"]) || drawn({...decl, group: undefined}).all("button").length) return `buttons to each group: ${JSON.stringify(jump)}`;
  const marks = first.all("span").filter(x => x.classList.contains("mark")), links = first.all("a");
  if (marks.length !== 1 || !marks[0].classList.contains("t-ok") || links.length !== 1 || links[0].textContent !== "View B" || links[0].attrs.href !== "#run=r%201&view=b") return first.textContent;
  const bare = drawn(decl, {...shots, rows: [["a", "pics/a.png", sha(1), "T", "C", "p", null, "b"], ["b", "pics/b.png", sha(2), "T", "C", "p", null, null]]}).all("figure");
  if (!same(bare[0].all("dt").map(x => x.textContent), ["view"]) || bare[1].all("dl").length) return "a detail the run does not record (null) is listed all the same";
  return (last.all("a").length === 0 && last.textContent.includes("nowhere") && last.all("span").some(x => x.classList.contains("t-problem"))) || last.textContent;
});
ok("P19 a click shows an image at full width and another puts it back", () => {
  const img = drawn(decl).all("img")[0], box = page.imageBox("r1", "pics/a.png", sha(1), "A"), own = box.all("img")[0];
  if (!img.listeners.click || !/full width/.test(img.attrs.title)) return "the image does not say that a click enlarges it";
  own.listeners.click();
  const wide = box.classList.contains("full");
  own.listeners.keydown({key: "Enter"});
  return (wide && !box.classList.contains("full")) || `after a click: ${wide}; after Enter: ${box.classList.contains("full")}`;
});
ok("P21 a row with no hash or no path, a table that is not there and a column that is not there each say so, and ask the server for nothing", () => {
  const holes = {...shots, rows: [["a", "pics/a.png", null, "T", "C", "p", "same", "b"], ["b", null, sha(2), "T", "C", "p", "same", "b"]]};
  const box = drawn(decl, holes), none = drawn({...decl, table: "absent"}), wrong = drawn({...decl, hash: "absent"}), empty = drawn(decl, {...shots, rows: []});
  if (box.all("img").length || box.all("figure").length !== 2 || !box.textContent.includes("names no hash for pics/a.png") || !box.textContent.includes("names no file")) return box.textContent;
  return (!none.all("img").length && none.textContent.includes('no table "absent"') && !wrong.all("img").length && /no column/.test(wrong.textContent)
    && empty.textContent === "No image.") || `${none.textContent} | ${wrong.textContent} | ${empty.textContent}`;
});
const bars = {kind: "bars", table: "by_x", x: "x", y: "n", title: "Chart title", how_to_read: "How to read it."}, byX = {by_x: {columns: ["x", "n"], rows: [["a", 1]]}};
ok("P22 a chart that carries an image shows the harness figure under the chart, asked of the file endpoint; a chart without one shows none; "
  + "a chart that cannot be drawn still shows its figure", () => {
  const image = {path: "pics/a.png", hash: sha(1), title: "Figure title"};
  const withIt = page.chartView({...bars, image}, byX, "r1"), without = page.chartView(bars, byX, "r1"), undrawn = page.chartView({...bars, table: "absent", image}, byX, "r1");
  const img = withIt.all("img")[0];
  if (!img || img.attrs.src !== `/api/file?run=r1&path=pics%2Fa.png&sha=${sha(1)}` || !withIt.textContent.includes("Figure title") || !withIt.all("svg").length) return withIt.textContent;
  if (withIt.textContent.indexOf("Drawn from the table") > withIt.textContent.indexOf("The harness figure")) return "the figure is not under the chart";
  return (without.all("img").length === 0 && undrawn.all("img").length === 1 && /cannot be drawn/.test(undrawn.textContent)
    && page.chartView({...bars, image: {path: "pics/a.png"}}, byX, "r1").textContent.includes("names no hash")) || `${without.all("img").length} ${undrawn.textContent}`;
});
ok("P23 a view's images are drawn after the facts and before the notes, charts and tables, and their table is kept closed; a view with none has no such section", () => {
  const data = {checks: [], facts: {n_images: 3}, notes: {terms: "What the words mean."}, charts: [bars], images: [decl], tables: {...byX, shots: {...shots, about: "The table of images."}}};
  const text = v => page.renderView(v, "r1", "v").flat(Infinity).filter(n => n instanceof Node).map(n => n.textContent).join(" | ");
  const at = w => text(data).indexOf(w), order = ["Facts", "Images", "Title of C", "What the words mean.", "Chart title", "The table of images."].map(at);
  const closed = page.renderView(data, "r1", "v").flat(Infinity).filter(n => n instanceof Node && n.tag === "details");
  if (closed.length !== 1 || !closed[0].textContent.includes("The table of images.") || !/behind the images above \(3 rows\)/.test(closed[0].all("summary")[0].textContent)) return "the table behind the images is not kept closed under one line";
  return (order.every(i => i >= 0) && same(order, [...order].sort((a, b) => a - b)) && !text({...data, images: []}).includes("Images |")
    && !text({...data, images: undefined}).includes("full width")) || `${order} in ${text(data)}`;
});
// ---- the reader's findings of the walkthrough (specification §12): generic page behaviour, no view named
const centre = d => { const m = /^M(-?[\d.]+) (-?[\d.]+)L(-?[\d.]+)/.exec(d); return [Number(m[3]), Number(m[2])]; };      // a diamond's centre from its path
const ranged = {kind: "ranges", table: "r", x: "id", y: "v", outer_low: "lo", outer_high: "hi", low: "smin", high: "smax", title: "T", how_to_read: "H"};
ok("P24 a value outside its range is drawn at its true position beyond the range, not at its edge; a row inside its range fills the row as before", () => {
  const t = {r: {columns: ["id", "lo", "hi", "smin", "smax", "v"], rows: [["in", 10, 20, 12, 14, 13], ["above", 10, 20, 22, 26, 25], ["below", 10, 20, 4, 6, 5]]}};
  const fig = page.chartView(ranged, t, "r1"), tracks = fig.all("rect").filter(r => r.attrs.class === "track" && r.attrs.height === "6").slice(1);      // the first is the legend's
  const at = fig.all("path").filter(q => q.attrs.class === "c2").slice(1).map(q => centre(q.attrs.d)[0]);
  const edge = k => [Number(tracks[k].attrs.x), Number(tracks[k].attrs.x) + Number(tracks[k].attrs.width)];
  if (tracks.length !== 3 || at.length !== 3) return `${tracks.length} tracks, ${at.length} value marks`;
  if (!(at[0] > edge(0)[0] && at[0] < edge(0)[1])) return `a value inside its range is drawn at ${at[0]}, outside the track ${edge(0)}`;
  if (!(at[1] > edge(1)[1] + 20)) return `a value above its range is drawn at ${at[1]}, not beyond the track's end ${edge(1)[1]}`;
  if (!(at[2] < edge(2)[0] - 20)) return `a value below its range is drawn at ${at[2]}, not before the track's start ${edge(2)[0]}`;
  const ends = fig.all("line").filter(q => q.attrs.class === "rangeend").map(q => Number(q.attrs.x1));
  if (ends.length !== 4 || Math.abs(ends[1] - edge(1)[1]) > 1 || Math.abs(ends[2] - edge(2)[0]) > 1) return `the ends of a range that a row reaches beyond are not marked (${ends})`;
  return (edge(0)[1] - edge(0)[0] > edge(1)[1] - edge(1)[0] && /beyond/.test(fig.textContent)) || "the row does not say that a value lies beyond its range";
});
ok("P25 an observed value and a model value at the same place are both visible: the two marks are set apart, and the chart says so", () => {
  const t = {t: {columns: ["id", "obs", "lo", "hi", "mod"], rows: [["same", 5, 4, 6, 5], ["apart", 5, 4, 6, 9]]}};
  const fig = page.chartView({kind: "interval_points", table: "t", x: "id", y: "obs", low: "lo", high: "hi", model: "mod", title: "T", how_to_read: "H"}, t, "r1");
  const dots = fig.all("circle").filter(q => q.attrs.class === "c1" && q.attrs.r === "4").slice(-2), marks = fig.all("path").filter(q => q.attrs.class === "c2").slice(-2).map(q => centre(q.attrs.d));
  if (dots.length !== 2 || marks.length !== 2) return `${dots.length} dots, ${marks.length} diamonds`;
  const gap = k => Math.abs(Number(dots[k].attrs.cy) - marks[k][1]);
  return (gap(0) >= 6 && gap(1) === 0 && /cover each other/.test(fig.textContent)) || `the marks are ${gap(0)} apart where they coincide and ${gap(1)} where they do not`;
});
ok("P26 a chart or an image block that names a table in `after` is drawn directly after that table, wherever the table is; "
  + "a chart says whether its table is above or below it; with no such table it stays with the other charts", () => {
  const chart = (title, more) => ({...bars, title, ...more});
  const data = {checks: [], facts: {n: 1}, notes: {terms: "What the words mean."}, images: [{...decl, after: "by_x"}],
    charts: [chart("Chart after lead", {table: "rows2", after: "rows2"}), chart("Chart after other", {after: "by_x"}), chart("Chart loose"), chart("Chart lost", {after: "absent"})],
    tables: {rows2: {columns: ["x", "n"], rows: [["a", 1]], about: "The leading table.", lead: true}, by_x: {columns: ["x", "n"], rows: [["a", 1]], about: "The other table."},
      shots: {...shots, about: "The table of images."}}};
  const nodes = page.renderView(data, "r1", "v").flat(Infinity).filter(n => n instanceof Node), text = nodes.map(n => n.textContent).join(" | ");
  const at = w => text.indexOf(w), order = ["The leading table.", "Chart after lead", "What the words mean.", "Charts", "Chart loose", "Chart lost", "The other table.", "Chart after other", "Title of C"].map(at);
  if (!order.every(i => i >= 0) || !same(order, [...order].sort((a, b) => a - b))) return `${order} in ${text}`;
  const says = title => nodes.find(n => n.tag === "figure" && n.textContent.includes(title)).textContent;
  if (!/"rows2" above/.test(says("Chart after lead")) || !/"by x" above/.test(says("Chart after other")) || !/"by x" below/.test(says("Chart loose"))) return "a chart does not say where its table is";
  const cards = nodes.filter(n => n.tag === "figure" && String(n.attrs.class).startsWith("image") && n.textContent.includes("Title of C")).length;
  return (!text.includes("Images |") && cards === 1) || `the image block placed after a table is drawn ${cards} times, or in a section of its own`;
});
ok("P27 a table the run does not record is listed with the reason the view gives for it, and without one the page says the view gives none", () => {
  const data = {checks: [], facts: {}, charts: [], notes: {gone: "The run has no file for it.", terms: "What the words mean."}, tables: {gone: null, silent: null, by_x: byX.by_x}};
  const text = page.renderView(data, "r1", "v").flat(Infinity).filter(n => n instanceof Node).map(n => n.textContent).join(" | ");
  return (/Not recorded/.test(text) && /gone: The run has no file for it\./i.test(text) && /silent: this view gives no reason/i.test(text) && !/The notes above say why/.test(text)
    && text.split("The run has no file for it.").length === 2) || text;
});
const flagged = {columns: ["name", "file", "sha256", "title", "caption", "part", "state", "why"], rows: [
  ["a", "pics/a.png", sha(1), "Title of A", "Title of A; drawn from its sources.", "first", "same", null],
  ["b", "pics/b.png", sha(2), "Title of B", "Title of B.", "first", "changed", null],
  ["c", null, null, null, null, "first", null, "it needs a file the run does not have"],
  ["d", "pics/d.png", sha(4), "Title of D", "What D shows.", "second", "same", null]]};
const fdecl = {table: "shots", path: "file", hash: "sha256", title: "title", caption: "caption", group: "part", details: ["state"], flag: "state", reason: "why"};
ok("P28 images: a row whose flag column holds a word that is not ok is first in its group and has a button at the top that goes to it; "
  + "a row with no image and a reason is a card that gives the reason and asks the server for nothing", () => {
  const box = drawn(fdecl, flagged), figs = box.all("figure"), names = figs.map(f => f.all("figcaption")[0].textContent);
  if (!same(names, ["Title of B", "Title of A", "c", "Title of D"])) return `the cards are in the order ${JSON.stringify(names)}`;
  const go = box.all("button").map(b => b.textContent);
  if (!go.some(w => /Title of B/.test(w) && /changed/.test(w))) return `no button goes to the flagged image: ${JSON.stringify(go)}`;
  if (box.all("img").length !== 3 || !figs[2].textContent.includes("No image: it needs a file the run does not have") || figs[2].all("img").length) return figs[2].textContent;
  return /1 image.*needs a look|needs a look.*1 image/i.test(box.textContent) || box.textContent.slice(0, 300);
});
ok("P29 a caption that only repeats the title is not shown; one that begins with the title shows what it adds; another is shown whole", () => {
  const figs = drawn(fdecl, flagged).all("figure"), cap = f => f.all("p").filter(q => q.attrs.class === "caption").map(q => q.textContent);
  return (same(cap(figs[0]), []) && same(cap(figs[1]), ["Drawn from its sources."]) && same(cap(figs[3]), ["What D shows."])) || JSON.stringify(figs.map(cap));
});
ok("P30 the glossary reads on its own: each kind of code is said in a sentence, and an entry says where it is defined in words", () => {
  page.setGlossary({...glossary, kinds: {"scorecard case": "A scorecard case is a kind of problem the page must show early."}});
  const parts = page.gloss("scorecard case C13 and gap H3"), foot = page.glossaryFoot().textContent;
  const hover = parts.find(q => q instanceof Node).attrs.title;
  page.setGlossary(glossary);
  return (foot.includes("A scorecard case is a kind of problem the page must show early.") && hover.startsWith("A scorecard case is a kind of problem")
    && /defined in/.test(foot) && !/as the spec defines them\. Pointing/.test(foot)) || `${hover} | ${foot}`;
});
// An image that does not load: the cases below wait for the page's request for the server's reason.
async function later(name, fn) {
  let why = null;
  try { const r = await fn(); if (r !== true) why = String(r); } catch (e) { why = `${e.name}: ${e.message}`; }
  console.log(`  ${why == null ? "ok  " : "FAIL"} ${name}${why == null ? "" : ": " + why.slice(0, 300)}`);
  if (why != null) fails.push(name.split(" ")[0]);
}
(async () => {
  await later("P20 an image that cannot be loaded is replaced by a sentence naming the file, then by the server's reason; never a broken picture", async () => {
    const asked = [];
    global.fetch = url => (asked.push(url), Promise.resolve({ok: false, text: () => Promise.resolve("not served: the file no longer has that hash")}));
    const box = page.imageBox("r1", "pics/a.png", sha(1), "A");
    box.all("img")[0].listeners.error();
    const first = box.textContent, gone = box.all("img").length === 0;
    await new Promise(r => setTimeout(r, 0));
    global.fetch = () => new Promise(() => {});
    return (gone && first.includes("pics/a.png could not be loaded") && same(asked, [`/api/file?run=r1&path=pics%2Fa.png&sha=${sha(1)}`])
      && box.textContent.includes("not served: the file no longer has that hash") && box.all("p")[0].attrs.role === "status") || `${first} | ${box.textContent}`;
  });
  await later("P31 an image that cannot be loaded says where the fault lies: the run's file (changed, gone, not an image), this page (out of date) or the server (busy, not answering)", async () => {
    const said = [];
    for (const answer of [{status: 409}, {status: 404}, {status: 415}, {status: 403}, {status: 503}, null]) {
      global.fetch = () => (answer ? Promise.resolve({ok: false, status: answer.status, text: () => Promise.resolve("not served: the reason")}) : Promise.reject(new Error("no answer")));
      const box = page.imageBox("r1", "pics/a.png", sha(1), "A");
      box.all("img")[0].listeners.error();
      await new Promise(r => setTimeout(r, 0));
      said.push(box.textContent);
    }
    global.fetch = () => new Promise(() => {});
    const want = [/the run's file/i, /the run's file/i, /the run's file/i, /this page/i, /the server/i, /the server/i];
    return (want.every((w, i) => w.test(said[i])) && said.slice(0, 5).every(x => x.includes("not served: the reason")) && /Read the files again/.test(said[3])) || JSON.stringify(said);
  });
  console.log(fails.length ? `FAILED: ${JSON.stringify(fails)}` : "OK: all page cases passed.");
  process.exit(fails.length ? 1 : 0);
})();
