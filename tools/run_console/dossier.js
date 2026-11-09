// The run dossier views: Spec, Evidence, Engine and Calibration (docs/design/v2-run-console.md §15). Served by
// run_console.py at /dossier.js after run_console.js, whose helpers it uses (esc, num, T, ctx, wire, openFile).
// Every artifact value is untrusted: text goes through esc() or num(), attributes hold esc() values only.
"use strict";
const DOSSIER_VIEWS = ["spec", "evidence", "engine", "calibration"];
const DS = { run: null, data: null, at: 0, seq: 0, error: null, loading: false };
const DOSSIER_TTL_MS = 60000;   // the dossier's files rarely change: refetch at most once a minute
let DS_N = 0;

async function loadDossier() {
  const id = S.runId, seq = ++DS.seq;
  try {
    const d = await api("/api/dossier?" + q({ run: id }));
    if (seq !== DS.seq || id !== S.runId) return;
    DS.run = id; DS.data = d; DS.at = Date.now(); DS.error = null;
  } catch (e) {
    if (seq !== DS.seq || id !== S.runId) return;
    DS.run = id; DS.data = null; DS.at = Date.now(); DS.error = e.message;
  }
  if (DOSSIER_VIEWS.includes(S.view)) renderDossier();
}
function renderDossier() {
  const box = $("stream");
  if ((DS.run !== S.runId || Date.now() - DS.at > DOSSIER_TTL_MS) && !DS.loading) {
    DS.loading = true; loadDossier().finally(() => { DS.loading = false; });
  }
  if (DS.run !== S.runId) { box.innerHTML = '<div class="empty">Loading the run dossier…</div>'; return; }
  // Only a change of view or of content re-renders, so rows opened with "more" survive the 20-second refresh. The
  // key sits on the first element: any other view that replaced the content removed it.
  const key = S.runId + "|" + S.view + "|" + DS.at;
  if (box.firstElementChild && box.firstElementChild.dataset.dkey === key) return;
  const body = DS.error ? `<div class="card error"><div class="body">The dossier cannot be read: ${esc(DS.error)}</div></div>` : dossierHTML(S.view, (DS.data || {})[S.view]);
  box.innerHTML = `<div data-dkey="${esc(key)}">${body}</div>`;
  wire(box);
}

// ---- small formatting helpers (HTML-safe)
const naBox = (p) => `<div class="na">Not available: ${esc((p && p.why) || "no data")}</div>`;
const has = (p) => p && p.available !== false;
function val(x) {   // any artifact value as cell text: numbers formatted, lists joined, mappings as "k: v"
  if (x == null || x === "") return "—";
  if (typeof x === "number") return num(x);
  if (typeof x === "boolean") return x ? "yes" : "no";
  if (Array.isArray(x)) return x.length ? x.map(val).join(", ") : "—";
  if (typeof x === "object") return Object.entries(x).map(([k, v]) => `${esc(k)}: ${val(v)}`).join("; ") || "—";
  return esc(String(x));
}
const yr = (x) => (typeof x === "number" ? String(Number(x.toFixed(2))) : esc(String(x == null ? "—" : x)));
const span = (w) => (Array.isArray(w) && w.length === 2 ? `${yr(w[0])}–${yr(w[1])}` : val(w));
const money = (x) => (x == null ? "—" : typeof x !== "number" ? esc(String(x)) : Math.abs(x) < 1000 ? "$" + x.toFixed(2) : num(x, "usd"));
const sha12 = (s) => (s ? `<span class="mono" title="${esc(s)}">${esc(String(s).slice(0, 12))}</span>` : "—");
const rawBtn = (file) => (file ? `<button class="link" data-act="open" data-file="${esc(file)}" title="${esc("open " + file + " as text")}">raw</button>` : "");
function more(text, n) {   // long artifact text: the first n characters, the rest behind "more"
  const t = String(text == null ? "" : text);
  if (t.length <= n) return t ? `<span class="lt">${esc(t)}</span>` : "—";
  const cut = t.slice(0, n).replace(/\s+\S*$/, ""), id = "dsm" + (++DS_N);
  return `<span class="lt">${esc(cut)}</span><span class="lt" id="${esc(id)}" hidden>${esc(t.slice(cut.length))}</span> <button class="link" data-more="${esc(id)}">more</button>`;
}
function section(title, what, needs, file, body) {
  return `<div class="card dossier"><div class="head"><span class="t">${title}</span>${rawBtn(file)}</div>${ctx(what, needs)}${body}</div>`;
}
function kv(rows) {   // [[label html, value html]] -> a two-column table; rows with an undefined value are left out
  const r = rows.filter((x) => x && x[1] !== undefined);
  return r.length ? `<table class="t kvt">${r.map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join("")}</table>` : "";
}
function tbl(head, rows, cls) {   // head: [html] or [[html, "n"]] for a numeric column; rows: [[html]] (or {cls, cells})
  const h = head.map((x) => (Array.isArray(x) ? `<th class="n">${x[0]}</th>` : `<th>${x}</th>`)).join("");
  const nums = head.map((x) => Array.isArray(x));
  const b = rows.map((r) => { const cells = Array.isArray(r) ? r : r.cells, c = Array.isArray(r) ? "" : r.cls || "";
    return `<tr class="${esc(c)}">${cells.map((x, j) => (nums[j] ? `<td class="n">${x}</td>` : `<td>${x}</td>`)).join("")}</tr>`; }).join("");
  return rows.length ? `<table class="t ${esc(cls || "")}"><tr>${h}</tr>${b}</table>` : '<div class="empty small">None.</div>';
}
const stateCell = (s, why) => {
  const w = { match: '<span class="ok-i">✓ same</span>', changed: '<span class="bad-i">changed</span>', missing: '<span class="bad-i">missing</span>', refused: '<span class="bad-i">refused</span>' }[s] || esc(s || "—");
  return why ? `<span title="${esc(why)}">${w}</span>` : w;
};
function sig(x, p = 3) {   // p significant digits; scientific notation below 1e-3, so a small value never reads as 0
  if (typeof x !== "number" || !isFinite(x)) return num(x);
  if (x === 0) return "0";
  if (Math.abs(x) < 1e-3) return x.toExponential(p - 1).replace(/\.?0+e/, "e").replace("e+", "e");
  return num(Number(x.toPrecision(p)));
}
const tail = (p) => { const s = String(p || ""), i = s.lastIndexOf("/"); return i < 0 ? `<span class="lt">${esc(s)}</span>` : `<span class="lt"><span class="muted">${esc(s.slice(0, i + 1))}</span>${esc(s.slice(i + 1))}</span>`; };
const outcomeWord = (o) => (o === "dalys" ? T("DALYs") : esc(o || "—"));

function dossierHTML(tab, d) {
  if (!d) return naBox({ why: "the console server sent no " + tab + " section (restart it from this checkout)" });
  if (d.available === false && tab !== "calibration") return naBox(d);
  return { spec: specHTML, evidence: evidenceHTML, engine: engineHTML, calibration: calibrationHTML }[tab](d);
}

// ---- Spec
const STATUS = { signed: ["ok", "signed, and the spec has not changed since"], changed: ["bad", "changed since the last sign-off"], awaiting: ["warn", "awaiting your signature"], absent: ["", "no spec yet"] };
function specHTML(d) {
  const st = STATUS[d.status] || ["", d.status_error || d.status || "unknown"];
  const needs = d.status === "awaiting" ? "yes: sign it from the decision card in the Stream." : d.status === "changed" ? "yes: the spec or its derived data changed after the last sign-off." : "no.";
  const u = d.units || {};
  let h = section("Decision spec", "The question the run answers and what it optimises, as the owner signed it.", needs, d.file,
    kv([["status", `<span class="state ${esc(st[0])}">${esc(st[1])}</span>`], ["question", more(d.question, 260)],
      ["decision maker", val(d.decision_maker)], ["decision date", yr(d.decision_date)],
      ["simulated period", d.horizon ? `${yr(d.horizon.start)}–${yr(d.horizon.stop)}` : "—"], ["spec sha256", sha12(d.sha256)]]));
  const unitsLine = u.count != null ? `${num(u.count)} units${u.regions != null ? ` in ${num(u.regions)} regions` : ""}` : `<span class="bad-t">${esc(u.count_why || "not counted")}</span>`;
  h += section("Decision units", `Each unit gets one package. Engine attributes come from the units table by a ${T("binding")}; the rest are ${T("defaults accepted")} by name.`, "no.", u.csv,
    kv([["units", unitsLine], ["units table", tail(u.csv)], ["id column", val(u.id)], ["label column", val(u.label)], ["region column", val(u.region)]]) +
    `<div class="cmp-h">Engine attribute bindings</div>` + tbl(["attribute", "bound by", "columns or value", "source"],
      (d.bindings || []).map((b) => [esc(b.attribute), esc(b.how) + (b.split ? ` <span class="muted">(split on "${esc(b.split)}")</span>` : ""), more(b.detail, 120), more(b.source, 140)])) +
    `<div class="cmp-h">${T("defaults accepted", "Defaults accepted")}</div>` + tbl(["attribute", "reason"], (d.defaults || []).map((x) => [esc(x.name), more(x.reason, 220)])) +
    `<div class="cmp-h">Populations</div>` + tbl(["population", "column", "age groups"], (d.populations || []).map((p) => [esc(p.name), val(p.column), val(p.age_groups)]).concat((d.option_populations || []).map((p) => [esc(p.name), val(p.column), '<span class="muted">cost populations</span>']))));
  const pk = d.packages || {};
  h += section("Components and packages", "The interventions a package combines, the population each reaches, and what the engine runs for it.", "no.", d.file,
    tbl(["component", "group", "eligible population", "engine setting"], (d.components || []).map((c) => [esc(c.id), val(c.group), val(c.eligible), val(c.engine)])) +
    `<div class="sub">Packages: ${pk.generate ? "every combination of components, at most one per group (generated)" : (pk.list || []).map((p) => `${esc(p.name)} (${val(p.components)})`).join("; ") || "—"}.</div>`);
  const pr = d.prices || {};
  h += section("Costs", `Cost items in ${esc(pr.base_year || "base-year")} prices. The ${T("per person")} column is the cost of covering one person over the window, where the ${T("cost basis")} allows it.`, "no.", d.file,
    `<div class="sub">Base year ${esc(pr.base_year || "—")}${pr.index_name ? `; ${T("price rule", "price index")}: ${esc(pr.index_name)} (${(pr.index || []).map(([y, v]) => `${esc(y)}: ${num(v)}`).join(" · ")})` : ""}.</div>` +
    tbl(["item", "component", ["value"], "price year", T("price rule", "rule"), T("cost basis", "basis"), "includes", [`${esc(pr.base_year || "base")} value`], [T("per person", "per person, window")]],
      (d.cost_items || []).map((c) => [esc(c.id), esc(c.component), money(c.value), esc(c.price_year || "—"), esc(c.rule || "—"),
        esc(c.basis || "—") + (c.extra && c.extra.length ? `<div class="sub">${esc(c.extra.join("; "))}</div>` : ""), val(c.includes) + (c.not_double_counted || []).map((n) => `<div class="sub">not a double count with ${esc(n.item)}: ${more(n.reason, 160)}</div>`).join(""), money(c.base_value),
        c.per_person != null ? money(c.per_person) + `<div class="sub">${esc(c.per_label)}</div>` : `<span class="muted" title="${esc(c.why_no_per_person || "")}">${esc(c.per_label || "—")}</span>`])));
  const o = d.objective || {}, k = d.constraints || {}, cp = d.comparator || {};
  h += section("Objective and constraints", `What the plan maximises, within which budget, against which ${T("comparator")}.`, "no.", d.file,
    kv([["objective", `${esc(o.sense || "—")} ${outcomeWord(o.outcome)}`], ["outcome window", span(o.window)], ["delivery period", span(o.delivery)], ["discounting", val(o.discounting)],
      ["envelope", money(k.envelope_usd)], ...(k.fixed || []).map((f) => [`fixed cost: ${esc(f.name)}`, money(f.usd)]),
      ...(k.eligibility || []).map((e) => [`eligibility: ${esc(e.component)}`, esc(e.rule)]),
      ...Object.entries(k.other || {}).map(([a, b]) => [esc(a), esc(b)]),
      ["comparator", `${esc(cp.package || "—")}${cp.text ? ` <span class="muted">(${esc(cp.text)})</span>` : ""}`]]));
  const c = d.calibration || {}, tu = c.target_uncertainty || {}, tf = c.target_frame || {};
  h += section("Calibration settings", `How the run fits each unit to its target. Results are in the Calibration tab.`, "no.", d.file,
    kv([["method", c.method === "emulator_inversion" ? T("emulator inversion") : val(c.method)], ["calibrated attribute", `${val(c.attribute)}${c.scale ? ` <span class="muted">(${esc(c.scale)} scale)</span>` : ""}`],
      ["range", span(c.range)], ["target column", val(c.target_column)], ["target window", span(c.window)], ["metric", val(c.metric)],
      ...Object.entries(tu).map(([a, b]) => [`target uncertainty: ${esc(a)}`, more(b, 200)]), ...Object.entries(tf).map(([a, b]) => [`target frame: ${esc(a)}`, more(b, 200)])]));
  const un = d.uncertainty || {};
  h += section("Uncertainty", `What the ${T("UI")} draws vary. Cost items vary by a uniform multiplier in the stated range.`, "no.", d.file,
    kv([["level", un.level != null ? pct(un.level * 100, 0) : "—"], ["draws", val(un.draws)], ["seed", val(un.seed)], ["excluded", val(un.excluded)]]) +
    tbl(["source", "items"], (un.sources || []).map((s) => [esc(String(s.kind || "—").replace(/_/g, " ")), (s.items || []).map((i) => `${esc(i.id)} ×${Array.isArray(i.range) ? i.range.map(num).join("–") : val(i.range)}`).join(" · ") || '<span class="muted">all</span>'])));
  h += section(T("decision test", "Decision tests"), "Each re-solves the plan with one input at other values. The results are in the Stream.", "no.", d.file,
    tbl(["id", "type", "what is varied", ["values tested"], "question"], (d.decision_tests || []).map((t) => [esc(t.id), esc(String(t.type || "").replace(/_/g, " ")), more(t.varied, 140),
      (t.values || []).length ? (t.unit === "USD" ? (t.values || []).map(money).join(", ") : t.unit === "×" ? (t.values || []).map((x) => "×" + num(x)).join(", ") : (t.values || []).map((x) => num(x)).join(", ") + (t.unit ? ` <span class="muted">${esc(t.unit)}</span>` : "")) : "—", more(t.question, 160)])));
  h += signoffHTML(d);
  return h;
}
function signoffHTML(d) {
  const so = d.signoffs || [];
  const rows = so.map((s) => s.available === false ? { cls: "", cells: [esc(s.signed_at || "—"), "—", "—", `<span class="bad-t">${esc(s.why)}</span>`, s.current ? "current" : "archived"] } : {
    cls: s.current ? "cur" : "", cells: [`${esc(day(s.signed_at))} ${esc(hhmm(s.signed_at))}`, esc(s.signed_by || "—"), sha12(s.spec_sha256), more(s.note, 160),
      s.current ? (s.matches ? '<span class="ok-i">current; matches the spec</span>' : '<span class="bad-i">current; the spec changed since</span>') : `archived <span class="muted">${tail(s.folder)}</span>`] });
  let h = tbl(["signed", "by", "spec sha256", "note", "record"], rows);
  for (const s of so) {
    if (s.current || s.available === false) continue;
    const df = s.diff || {};
    h += `<div class="cmp-h">Changes after the sign-off of ${esc(day(s.signed_at))} ${esc(hhmm(s.signed_at))}${df.against_at ? `, to the sign-off of ${esc(day(df.against_at))} ${esc(hhmm(df.against_at))}` : df.against ? `, to ${esc(df.against)}` : ""}</div>`;
    if (!has(df)) { h += naBox(df); continue; }
    if (!(df.rows || []).length) { h += '<div class="empty small">No field changed.</div>'; continue; }
    let sec = null, body = "";
    for (const r of df.rows) {
      if (r.section !== sec) { sec = r.section; body += `<tr class="grp"><td colspan="3">${esc(sec)}</td></tr>`; }
      body += `<tr class="ch"><td>${esc(r.field.slice(sec.length + 1) || r.field)}</td><td>${r.before == null ? '<span class="muted">(absent)</span>' : more(r.before, 140)}</td><td>${r.after == null ? '<span class="muted">(removed)</span>' : more(r.after, 140)}</td></tr>`;
    }
    h += `<table class="cmp"><tr><th>field</th><th>before</th><th>after</th></tr>${body}</table>`;
  }
  return section("Sign-off history", "Who signed the spec, when and with what note; each archived sign-off is compared with the next one.", "no.", "decision_spec.signoff.json", so.length ? h : '<div class="empty small">The spec has not been signed.</div>');
}

// ---- Evidence
function evidenceHTML(d) {
  const reg = d.registry || {};
  let h = section("Evidence registry", "The parameters the evidence service fetched and matched to a source.", "no.", reg.file,
    !has(reg) ? naBox(reg) : (reg.stub ? `<div class="sub">The evidence service is a stub in this harness version: no parameter has been fetched or matched (status: ${esc(reg.status)}). Parameter sources are in the engine card (Engine tab) and the staged registries below.</div>` : `<div class="sub">Status: ${esc(reg.status || "—")}.</div>`) +
      ((reg.entries || []).length || !reg.stub ? tbl(["parameter", ["value"], "source", "citation", "used in", "status"], (reg.entries || []).map((e) => [esc(e.id), num(e.value) + (e.unit ? ` <span class="muted">${esc(e.unit)}</span>` : ""), more(e.source, 140), esc(e.citation || "—") + (e.citation_title ? `<div class="sub">${esc(e.citation_title)}</div>` : ""), val(e.used_in), esc(e.status || "—") + (e.more ? `<div class="sub">${more(e.more, 80)}</div>` : "")])) : ""));
  for (const r of d.param_registries || []) {
    h += section(`Staged parameter registry <span class="muted small">${tail(r.file)}</span>`, "Cost, envelope and coverage values staged with the inputs, each with its source. \"Used by the spec\" lists the spec fields that name it.", "no.", r.file,
      !has(r) ? naBox(r) : tbl(["name", ["value"], ["interval"], "kind", "source", "used by the spec"], (r.entries || []).map((e) => ({ cls: e.used_in.length ? "" : "dim", cells: [esc(e.name), num(e.value), e.ci ? e.ci.map(num).join("–") : "—", esc(e.kind || "—"),
        esc(e.source || "—") + (e.citation_title ? ` <span class="muted">${esc(e.citation_title)}</span>` : "") + (e.subgroup ? `<div class="sub">${more(e.subgroup, 120)}</div>` : ""), e.used_in.length ? e.used_in.map(esc).join("<br>") : '<span class="muted">not named</span>'] }))));
  }
  const ci = d.citations || {};
  h += section("Citations", "Sources staged with the inputs. Parameters cite them by id.", "no.", (ci.files || [])[0],
    !has(ci) ? naBox(ci) : tbl(["id", "title", "value", "source", "DOI or URL", "cited by"], (ci.entries || []).map((c) => [esc(c.id), esc(c.title), more((c.fields || {}).Value, 120), more((c.fields || {}).Source, 100),
      more((c.fields || {})["DOI/PMCID"] || (c.fields || {})["URL verified"] || (c.fields || {}).DOI || (c.fields || {}).URL, 90), val(c.cited_by)])) + ((ci.errors || []).length ? `<div class="na">${ci.errors.map(esc).join("<br>")}</div>` : ""));
  const inp = d.inputs || {};
  h += section("Staged inputs", "The files staged into data/inputs, with the hash recorded when they were staged and whether the file is still the same.", "no.", inp.file,
    !has(inp) ? naBox(inp) : tbl(["file", "staged from", ["size"], "recorded sha256", "now", "use"], (inp.rows || []).map((r) => [tail(r.path), esc(r.source || "—"), r.bytes != null ? num(r.bytes) + " B" : "—", sha12(r.sha256), stateCell(r.state, r.why),
      r.read_by.length ? "read by " + r.read_by.map(esc).join(", ") : r.ignored ? `<span class="muted">${more("ignored: " + r.ignored, 90)}</span>` : '<span class="muted">not read by a derivation</span>'])) +
      ((inp.cited || []).length ? `<div class="cmp-h">Cited outputs</div>` + tbl(["file", "recorded sha256", "now"], inp.cited.map((r) => [tail(r.path), sha12(r.sha256), stateCell(r.state, r.why)])) : ""));
  const dv = d.derivations || {};
  let dh = !has(dv) ? naBox(dv) : "";
  for (const r of dv.rows || []) {
    const rec = r.record || {};
    dh += `<div class="cmp-h">${esc(r.id)}: ${(r.inputs || []).length} input file${(r.inputs || []).length === 1 ? "" : "s"} → <span class="mono">${esc(r.script || "—")}</span> ${stateCell(r.script_state, r.script_why)} → ${(r.outputs || []).map((o) => `${tail(o.path)} ${stateCell(o.state, o.why)} <span class="muted">(${num((o.columns || []).length)} columns)</span>`).join(", ")}</div>`;
    dh += has(rec) ? `<div class="sub">Record: ${rec.ok ? '<span class="ok-i">ran ok</span>' : '<span class="bad-i">failed</span>'}${rec.runs != null ? `, ${esc(rec.runs)} runs` : ""}${rec.runtime_s != null ? `, ${esc(rec.runtime_s)} s` : ""}${rec.sandbox ? `, sandbox ${esc(rec.sandbox)}` : ""}${rec.python ? `, Python ${esc(rec.python)}` : ""}${Object.keys(rec.libraries || {}).length ? `, ${val(rec.libraries)}` : ""}${rec.reason ? `; ${esc(rec.reason)}` : ""}.</div>` : naBox(rec);
    dh += tbl(["input file", "now", "columns used", "columns ignored"], (r.inputs || []).map((i) => {
      const id = "dsi" + (++DS_N);
      const ign = i.ignores.length ? `${num(i.ignores.length)} <button class="link" data-more="${esc(id)}">show</button><div id="${esc(id)}" hidden>${tbl(["column", "reason"], i.ignores.map((g) => [esc(g.column), esc(g.reason)]))}</div>` : "—";
      return [tail(i.path), stateCell(i.state, i.why), i.uses.length ? i.uses.map(esc).join(", ") : "—", ign];
    }));
    for (const o of r.outputs || []) if ((o.columns || []).length) dh += `<div class="sub">Output columns of ${tail(o.path)}: ${o.columns.map(esc).join(", ")}</div>`;
  }
  if ((dv.ignored_inputs || []).length) dh += `<div class="cmp-h">Staged inputs no derivation reads</div>` + tbl(["file", "reason"], dv.ignored_inputs.map((x) => [tail(x.path), more(x.reason, 160)]));
  h += section("Derivations", "Scripts that turn the staged inputs into the units table, run in a sandbox and recorded with hashes. For each input: the columns used and the columns ignored, with reasons.", "no.", dv.file, dh);
  const ic = d.input_checks || {};
  h += section("Input checks", "The geometry check of the units' coordinates and areas against the boundaries.", has(ic) && (ic.findings || []).some((f) => f.severity === "HIGH" && !f.accepted) ? "yes: a HIGH finding is open; fix the data or accept it by name in the spec." : "no.", ic.file,
    !has(ic) ? naBox(ic) : kv([["status", esc(ic.status || "—") + (ic.reason ? ` <span class="muted">(${esc(ic.reason)})</span>` : "")], ...Object.entries(ic.counts || {}).map(([a, b]) => [esc(a.replace(/_/g, " ")), num(b)])]) +
      tbl(["severity", "code", "units", "claim", "accepted"], (ic.findings || []).map((f) => [T(f.severity || "—"), esc(f.code || "—"), val(f.units), more(f.claim, 160), f.accepted ? more(f.accepted, 80) : '<span class="muted">no</span>'])) +
      ((ic.acceptance_problems || []).length ? `<div class="na">${ic.acceptance_problems.map(esc).join("<br>")}</div>` : ""));
  return h;
}

// ---- Engine
function engineHTML(d) {
  const ch = d.choice || {}, card = d.card || {}, pa = d.parameters || {}, bm = d.benchmarks || {}, rs = d.restamp || {}, id = d.identity || {};
  let h = section("Engine choice", "The engines the spec allowed, why each was or was not eligible, and the one chosen.", "no.", ch.file,
    !has(ch) ? naBox(ch) : kv([["chosen", `<span class="mono">${esc(ch.chosen || "—")}</span>`], ["why", more(ch.why, 300)], ["decided", ch.at ? `${esc(day(ch.at))} ${esc(hhmm(ch.at))}` : "—"]]) +
      tbl(["engine", "eligible", "why not", ["mechanisms"], ["benchmarks"]], (ch.candidates || []).map((c) => ({ cls: c.ref === ch.chosen ? "cur" : "", cells: [`${esc(c.name || "—")} ${esc(c.version || "")}<div class="sub mono lt">${esc(c.ref || "")}</div>`,
        c.eligible ? '<span class="ok-i">yes</span>' : '<span class="muted">no</span>', c.why_not.length ? more(c.why_not.join("; "), 140) + ` <span class="muted">(${num(c.why_not.length)})</span>` : "—", num(c.mechanisms), num(c.benchmarks)] }))));
  h += section("Engine identity", `Which engine and code produced this run's results. The ${T("code fingerprint")} names the code of the stored batches.`, "no.", "analysis/engine.yaml",
    !has(id) ? naBox(id) : kv([["engine", `${esc(id.name || "—")} ${esc(id.version || "")}`], ["card sha256", sha12(id.card_sha256)], ["engine choice sha256", sha12(id.engine_choice_sha256)],
      [T("code fingerprint"), (id.code_fp || []).length ? id.code_fp.map(sha12).join(", ") : "—"], ["engine code sha256", id.code_sha256 ? sha12(id.code_sha256) + ' <span class="muted">(from the benchmark results file)</span>' : '<span class="muted">not shown (see the re-stamp line)</span>']]));
  if (!has(card)) return h + section("Engine card", "What the engine models, what it needs from each unit, and what it returns.", "no.", null, naBox(card));
  h += section(`Engine card: ${esc(card.name || "")} ${esc(card.version || "")}`, "What the engine models, what it needs from each unit, and what it returns. From the harness's snapshot of the card.", "no.", card.file,
    `<div class="cmp-h">Mechanisms</div><ul class="mech">${(card.mechanisms || []).map((m) => `<li>${esc(m)}</li>`).join("")}</ul>` +
    `<div class="cmp-h">Components</div>` + tbl(["component", "levels"], (card.components || []).map((c) => [esc(c.name), val(c.levels)])) +
    `<div class="cmp-h">Unit attributes</div>` + tbl(["attribute", "what it is", "valid range", T("binding", "binding in this spec")], (card.attributes || []).map((a) => [esc(a.name), more(a.desc, 120), a.range ? span(a.range) : "—", more(a.binding, 120)])) +
    `<div class="cmp-h">Outputs</div>` + kv([...Object.entries(card.outputs || {}).map(([k, v]) => [esc(k.replace(/_/g, " ")), val(v)]), ["age groups", val(card.age_groups)]]) +
    ((card.notes || []).length ? `<div class="cmp-h">Notes</div><ul class="mech">${card.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""));
  h += section("Parameters", `Each parameter's value and ${T("prior range")}; ${T("fitted")} ones with their fit box and the benchmarks they were fitted to.`, "no.", pa.file,
    !has(pa) ? naBox(pa) : tbl(["parameter", "kind", ["value"], "units", ["prior range"], "fit", "verified", "source"], (pa.rows || []).map((p) => [esc(p.name), p.kind === "fitted" ? T("fitted") : esc(p.kind || "—"), num(p.value), esc(p.units || "—"),
      p.lo != null || p.hi != null ? `${num(p.lo)}–${num(p.hi)}` : "—", p.box ? `<span class="nw">box ${span(p.box)}</span>${(p.fitted_to || []).length ? `<div class="sub">to ${p.fitted_to.map(esc).join(", ")}</div>` : ""}` : "—",
      p.verified === "matched" ? '<span class="ok-i">matched</span>' : esc(p.verified || "—"), more(p.source, 110)])));
  let rline;
  if (!has(rs)) rline = `Re-stamp status not shown: ${esc(rs.why)}.`;
  else if (rs.state === "recomputed") rline = `The benchmark numbers were computed by this engine code (${esc(rs.new_version || "?")}); no re-stamp.`;
  else rline = `The benchmark numbers were computed by ${rs.computed_by ? "v" + esc(rs.computed_by) : "an earlier version (not recorded)"}; ${T("re-stamped by proof", rs.state === "proof" ? "re-stamped by proof" : "re-stamped")}${rs.state === "proof" ? " (not recomputed)" : ""} to ${(rs.restamped_to || []).map((v) => "v" + esc(v)).join(", ") || "?"}. Proof identical: ${rs.proof_identical ? "yes" : "no"}. ${T("validate_once")} confirmation: <b class="${rs.validate_once === "pending" || rs.validate_once === "not recorded" ? "bad-i" : "ok-i"}">${esc(rs.validate_once)}</b>${rs.evidence ? ` (${esc(rs.evidence)})` : ""}.`;
  h += section(T("benchmark", "Benchmarks"), `Published results the engine is run against. ${T("held out")}: not used to fit. Rows outside their band are flagged.`, has(bm) && bm.out_of_band ? `no, but ${num(bm.out_of_band)} benchmark${bm.out_of_band === 1 ? " is" : "s are"} out of band.` : "no.", bm.file,
    !has(bm) ? naBox(bm) : `<div class="sub">${num(bm.total)} benchmarks: ${num(bm.in_band)} in band, <b class="${bm.out_of_band ? "bad-i" : ""}">${num(bm.out_of_band)} out of band</b>; held out: ${num(bm.held_out_in_band)} of ${num(bm.held_out)} in band.</div><div class="sub">${rline}</div>` +
      tbl(["id", "what it tests", "role", ["model"], "criterion", "status", "source"], (bm.rows || []).map((b) => ({ cls: b.flag ? "flag" : "", cells: [esc(b.id), more(b.metric, 110), b.role === "held_out" ? T("held out") : esc(b.role || "—"), b.kind === "compare" ? sig(b.computed, 6) : num(b.computed), criterion(b),
        b.status === "in_band" ? '<span class="ok-i">✓ in band</span>' : `<span class="bad-i">${esc(String(b.status || "unknown").replace(/_/g, " "))}</span>`, more(b.source, 60)] }))));
  return h;
}

const CMP = { eq: "equal to the reference within the tolerance", le: "at most the reference", differ: "differs from the reference" };
const tol = (x) => (x && typeof x === "object" && !Array.isArray(x) ? Object.entries(x).map(([k, v]) => `${esc(k)}: ${sig(v)}`).join("; ") || "—" : sig(x));
function criterion(b) {   // a band benchmark: target and band; an equation or identity one: reference, compare, tolerance, errors
  if (b.kind !== "compare") return `target ${num(b.target)}<div class="sub">band ${span(b.band)}</div>`;
  return `reference ${sig(b.reference, 6)} <span class="muted">(${esc(CMP[b.compare] || b.compare)})</span><div class="sub">tolerance ${tol(b.tolerance)}; error ${sig(b.error_abs)} abs${b.error_rel != null ? `, ${sig(b.error_rel)} rel` : ""}</div>`;
}

// ---- Calibration
function calibrationHTML(d) {
  if (d.available !== false && d.method !== "emulator_inversion") return fitHTML(d);
  const s = d.settings || {};
  const set = kv([["method", s.method === "emulator_inversion" ? T("emulator inversion") : val(s.method)], ["calibrated attribute", `${val(s.attribute)}${s.scale ? ` <span class="muted">(${esc(s.scale)} scale)</span>` : ""}`],
    ["range", span(s.range)], ["metric", val(s.metric)], ["target column", val(s.target_column)], ["target window", span(s.window)],
    ...Object.entries(d.target_uncertainty || {}).map(([a, b]) => [`target uncertainty: ${esc(a)}`, more(b, 160)])]);
  if (d.available === false) return section("Calibration", "How each unit was fitted to its target.", "no.", "analysis/calibration.yaml", set + naBox(d));
  const g = d.diagnostics || {}, idf = d.identifiability || {}, em = d.emulator || {};
  const clamped = g.clamped_units != null ? g.clamped_units : (d.units || []).filter((u) => u.clamped).length;
  const needs = d.needs_owner ? `yes: ${[clamped ? `${num(clamped)} unit${clamped === 1 ? " is" : "s are"} ${T("clamped")}` : "", idf.status && idf.status !== "well_identified" ? `${T("identifiability")} is ${esc(String(idf.status).replace(/_/g, " "))}` : ""].filter(Boolean).join("; ")}.` : "no.";
  let h = section("Calibration", `By ${T("emulator inversion")}: each unit's attribute is set so that the ${T("emulator")} reproduces its target.`, needs, d.file,
    set + kv([["units fitted", num(g.units != null ? g.units : (d.units || []).length)], [T("clamped", "clamped units"), `<b class="${clamped ? "bad-i" : ""}">${num(clamped)}</b>`], ["grid points", num(g.grid_points)],
      ["largest target residual (logit)", sig(g.target_logit_residual_max, 2)], [T("slope", "slope, median / smallest"), `${sig(g.slope_median)} / ${sig(g.slope_min)}`],
      [T("identifiability"), `${esc(String(idf.status || "—").replace(/_/g, " "))}${idf.design ? ` <span class="muted">(${esc(idf.design)})</span>` : ""}${(idf.units_unidentified || []).length ? `; unidentified: ${val(idf.units_unidentified)}` : ""}`]]) +
      `<div class="sub">Model value per unit: ${esc(d.model_per_unit)}.</div>`);
  const tid = "dsu" + (++DS_N);
  h += section("By region", `Targets and calibrated values (z: the attribute on the calibration scale) summarised by region. Target means are unweighted means of the units' targets.`, "no.", d.file,
    (d.join_why ? `<div class="na">Targets and regions not joined: ${esc(d.join_why)}</div>` : "") +
    tbl(["region", ["units"], ["target, mean"], ["z, range"], [T("clamped")], [T("slope", "smallest slope")]], (d.regions || []).map((r) => ({ cls: r.clamped ? "flag" : "", cells: [esc(r.region), num(r.n), sig(r.target_mean), r.z_min != null ? `${sig(r.z_min)} – ${sig(r.z_max)}` : "—", num(r.clamped), sig(r.slope_min)] }))) +
    `<div class="sub"><button class="link" data-more="${esc(tid)}">all ${num((d.units || []).length)} units</button></div><div id="${esc(tid)}" hidden>` +
    tbl(["unit", "region", ["target"], ["z"], ["slope"], "clamped"], (d.units || []).map((u) => ({ cls: u.clamped ? "flag" : "", cells: [esc(u.id), esc(u.region == null ? "—" : u.region), sig(u.target), sig(u.z), sig(u.slope), u.clamped ? '<span class="bad-i">yes</span>' : "no"] }))) + "</div>");
  h += section(`${T("emulator", "Emulator")}: held-out accuracy`, "How well the emulator predicts engine runs it was not fitted to.", "no.", em.file,
    !has(em) ? naBox(em) : kv([["held-out points", num(em.holdout_points)], ["training points", num(em.training_points)], ["calibration metric RMSE (logit)", sig(em.cal_logit_rmse)],
      ["R², median / 10th percentile", `${sig(em.reduction_r2_median)} / ${sig(em.reduction_r2_p10)}`], ["MAE, median", sig(em.reduction_mae_median)], ["base outcome RMSE (log)", sig(em.base_log_rmse)],
      ...Object.entries(em.error_model || {}).map(([a, b]) => [`error model: ${esc(a.replace(/_/g, " "))}`, typeof b === "number" ? sig(b) : more(b, 160)]),
      ["features", val(em.features)], ["fit time", em.fit_seconds != null ? `${num(Math.round(em.fit_seconds))} s` : "—"]]));
  return h;
}
function fitHTML(d) {   // likelihood or posterior calibration: a fit to a panel of observed counts
  const s = d.settings || {}, g = d.diagnostics || {}, idf = d.identifiability || {};
  if (d.method === "none") return section("Calibration", "The spec calibrates nothing.", "no.", d.file, kv([["method", "none"]]));
  const bound = g.at_bound || [];
  const why = [g.converged !== true ? "the fit did not converge" : "", bound.length ? `${num(bound.length)} parameter${bound.length === 1 ? " is" : "s are"} at a bound` : "",
    g.unsupported_targets ? `${num(g.unsupported_targets)} observed target${g.unsupported_targets === 1 ? " has" : "s have"} no expected count` : "",
    idf.status && idf.status !== "well_identified" ? `${T("identifiability")} is ${esc(String(idf.status).replace(/_/g, " "))}` : ""].filter(Boolean);
  const label = d.method === "posterior" ? "posterior (a joint sample of the parameters and unit attributes)" : "likelihood (Poisson quasi-likelihood, the dispersion from the Pearson statistic)";
  let h = section("Calibration", `Fitted by ${esc(label)} to a panel of observed counts. Convergence, bound hits, Pearson residuals and identifiability decide whether the fit can be used.`, d.needs_owner ? `yes: ${why.join("; ")}.` : "no.", d.file,
    kv([["method", esc(d.method)], ["calibrated attribute", `${val(s.attribute)}${s.scale ? ` <span class="muted">(${esc(s.scale)} scale)</span>` : ""}`], ["range", span(s.range)],
      ["converged", g.converged === true ? '<span class="ok-i">yes</span>' : `<span class="bad-i">no</span>${g.message ? ` <span class="muted">(${esc(g.message)})</span>` : ""}`],
      ["iterations", num(g.iterations)], ["parameters at a bound", bound.length ? `<span class="bad-t">${bound.map(esc).join(", ")}</span>` : "none"],
      ["targets / parameters", `${num(g.n_targets)} / ${num(g.n_parameters)}`], ["Pearson χ² / df", g.pearson_chi2 != null ? `${sig(g.pearson_chi2)} / ${num(g.df)}` : "—"],
      ["dispersion", sig(g.dispersion)], ["largest Pearson residual", sig(g.pearson_residual_max)], ["targets with no expected count", num(g.unsupported_targets)],
      ...["sampler", "ess", "rhat_max", "chains", "draws_per_chain"].filter((k) => g[k] != null).map((k) => [esc(k.replace(/_/g, " ")), typeof g[k] === "number" ? sig(g[k]) : esc(g[k])]),
      [T("identifiability"), esc(String(idf.status || "—").replace(/_/g, " ")) + (idf.method ? ` <span class="muted">(${esc(idf.method)})</span>` : "")],
      ...((d.prior_dominated || []).length ? [["prior-dominated", val(d.prior_dominated)]] : [])]));
  h += section("Observed vs expected", "Each target of the panel against the fitted model's expected count. Pearson residual: (observed − expected) / √expected.", "no.", d.file,
    tbl(["unit", "period", "package", ["observed"], ["expected"], ["Pearson residual"]], (d.fit || []).map((r) => ({ cls: r.pearson != null && Math.abs(r.pearson) > 3 ? "flag" : "", cells: [esc(r.unit), val(r.t0), esc(r.package || "—"), sig(r.observed), sig(r.expected), sig(r.pearson)] }))));
  h += section(`${T("identifiability", "Identifiability")} by parameter`, "Whether the panel pins each fitted quantity down.", "no.", d.file,
    tbl(["parameter", "status", ["estimate"], ["se"], "profile interval", "why"], (d.parameters || []).map((p) => ({ cls: p.status && !/^(well_identified|identified)$/.test(p.status) ? "flag" : "", cells: [esc(p.name), esc(String(p.status || "—").replace(/_/g, " ")), sig(p.estimate), sig(p.se), p.profile_ci ? p.profile_ci.map((x) => sig(x)).join(" – ") : "—", more(p.explanation, 120)] }))) +
    `<div class="cmp-h">Fitted values</div>` + kv(Object.entries(d.theta || {}).map(([k, v]) => [esc(k), sig(v)])));
  return h;
}
