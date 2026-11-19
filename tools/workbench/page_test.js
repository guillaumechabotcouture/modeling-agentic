"use strict";
// Cases for the page script (static/app.js), run by `workbench.py --self-test` when node is installed:
//     node tools/workbench/page_test.js
// The script is loaded against a small stand-in for the browser's document, so what it builds can be read here.
const fs = require("fs"), path = require("path");

class Node {
  constructor(tag) {
    this.tag = tag; this.attrs = {}; this.kids = []; this.listeners = {}; this.style = {};
    this.classList = {toggle() {}, contains: c => String(this.attrs.class || "").split(" ").includes(c)};
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
  "renderView", "post"];
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
console.log(fails.length ? `FAILED: ${JSON.stringify(fails)}` : "OK: all page cases passed.");
process.exit(fails.length ? 1 : 0);
