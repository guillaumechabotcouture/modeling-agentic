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
const names = ["ticks", "checkCard", "checksView", "setGlossary", "gloss", "glossaryFoot", "state", "scanned"];
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
console.log(fails.length ? `FAILED: ${JSON.stringify(fails)}` : "OK: all page cases passed.");
process.exit(fails.length ? 1 : 0);
