// The run console page script (served by run_console.py at /run_console.js; the CSP allows no inline script).
"use strict";
const TOKEN = document.querySelector('meta[name="console-token"]').content;
const S = { runs: [], run: null, runId: null, filter: null, anchor: null, verdict: null, meta: null, live: false, liveTimer: null, tab: "runs", view: "stream", cmp: null, loadSeq: 0, loading: false };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const unesc = (s) => String(s).replace(/&(amp|lt|gt|quot|#39);/g, (m, k) => ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" }[k]));
// Artifact text is untrusted (agents write it). Every value in an HTML attribute goes through esc(); values a
// control acts on (anchors, the run and the ticket a decision card was shown for) stay in JS and the attribute
// holds only ref(), an index this page made.
const REFS = {};
let REF_N = 0;
function ref(obj) { const k = "r" + (++REF_N); REFS[k] = obj; return k; }
const getRef = (k) => REFS[k] || {};
let TOG_N = 0;
function toast(msg) { const t = $("toast"); t.textContent = msg; t.classList.add("on"); clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove("on"), 4000); }
// Errors are never swallowed: a failed fetch or render shows one line at the top of the page.
function fail(where, e) { $("banner-text").textContent = `${where} failed: ${e && e.message ? e.message : e}`; $("banner").hidden = false; console.error(where, e); }
window.addEventListener("error", (e) => fail("Page script", e.error || e.message));
window.addEventListener("unhandledrejection", (e) => fail("Page script", e.reason));
async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-Console-Token": TOKEN }, body: JSON.stringify(body) };
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), body === undefined ? 60000 : 600000);
  let r;
  try { r = await fetch(path, { ...opt, signal: ctl.signal }); }
  catch (e) { throw new Error(e.name === "AbortError" ? `${path.split("?")[0]}: no answer from the console server within ${body === undefined ? 60 : 600} s` : `${path.split("?")[0]}: ${e.message} (is the console server running?)`); }
  finally { clearTimeout(timer); }
  const j = await r.json().catch(() => ({ error: "not JSON (" + r.status + ")" }));
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}
const q = (o) => Object.entries(o).map(([k, v]) => k + "=" + encodeURIComponent(v)).join("&");

// ---- words: stage labels, run state, short title, times
const STAGE = { SPEC: "Spec", SIGN: "Sign", EVIDENCE: "Evidence", ENGINE_CHOICE: "Engine", CONFIGURE: "Configure", ANALYZE: "Analyze",
  CRITIQUE: "Critique", GATE: "Gate", FIX_LOOP: "Fix loop", OWNER_DECISION: "Owner", WRITE: "Write", PRESENTATION: "Present", FINAL: "Final" };
const stage = (s) => STAGE[s] || String(s || "").toLowerCase().replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
const FAIL = ["run_failed", "completed_gate_error", "completed_gate_not_accepted", "completed_scope_declined"];
function runState(r) {   // r: a run summary or a run's state; returns [class, words]
  const status = r.status, fail = r.fail_statuses || FAIL;
  if (r.error) return ["bad", "Unreadable"];
  if (status && fail.includes(status)) return ["bad", "Failed"];
  if (status) return [status === "completed" || status === "completed_with_report_restored" ? "ok" : "warn", "Completed"];
  if (r.paused) return ["warn", "Waiting for you"];
  if (r.finished || r.state === "FINAL") return ["", "Ended"];
  return ["", "At " + stage(r.state)];
}
function shortTitle(s) {   // the first sentence, at most ~90 characters, cut at a word
  const t = String(s || "").trim().split(/\n/)[0], m = t.match(/^.*?[.?!](?=\s|$)/), f = m ? m[0] : t;
  return f.length <= 90 ? f : f.slice(0, 90).replace(/\s+\S*$/, "") + "…";
}
const day = (at) => { const d = new Date(at); return isNaN(d) ? "" : d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" }); };
const hhmm = (at) => (at && /T\d\d:\d\d/.test(at)) ? at.slice(11, 16) : "";
const when = (at) => at ? `<span class="tm" title="${esc(at)}">${esc(day(at))} ${esc(hhmm(at))}</span>` : "";
const usd = (x) => "$" + Number(x || 0).toFixed(2);
function num(v, unit) {   // 12.34M, 86.6M, 147.1K, $260.7M; small values as is with separators; HTML-safe
  if (v == null || v === "" || typeof v === "boolean" || isNaN(v)) return v == null ? "—" : esc(String(v));
  v = Number(v);
  const a = Math.abs(v), pre = unit === "usd" ? "$" : "";
  const c = a >= 1e9 ? (v / 1e9).toFixed(2) + "B" : a >= 1e6 ? (v / 1e6).toFixed(2) + "M" : a >= 1e4 ? (v / 1e3).toFixed(1) + "K"
    : Number.isInteger(v) ? v.toLocaleString("en-US") : Number(v.toPrecision(6)).toLocaleString("en-US", { maximumFractionDigits: 6 });
  return pre + c;
}
const UNIT = { dalys: "DALYs", usd: "", units: "units" };
const unitWord = (u) => (u in UNIT ? UNIT[u] : u);
const pct = (x, d = 1) => x == null ? "—" : `${Number(x).toFixed(d)}%`;
function uiText(lo, hi, level) {   // "95% UI 11.29–13.65M"
  const sfx = (v) => { const a = Math.abs(v); return a >= 1e9 ? "B" : a >= 1e6 ? "M" : a >= 1e4 ? "K" : ""; };
  const s = sfx(hi), f = (v) => num(v).replace(/[BMK]$/, "");
  return `${Math.round((level || 0.95) * 100)}% ${T("UI")} ${f(lo)}–${f(hi)}${s && sfx(lo) === s ? s : ""}`;
}
function T(key, label) {   // a glossary term: dotted underline, the definition and its source on hover
  const g = S.meta && S.meta.glossary && S.meta.glossary[key];
  return g ? `<span class="term" tabindex="0" title="${esc(g.text + " (" + g.source + ")")}">${esc(label || key)}</span>` : esc(label || key);
}
function bench(text) {     // benchmark ids in run text, defined by the run's engine card
  const B = (S.run && S.run.benchmarks) || {};
  return esc(text).replace(/\bB\d+[a-z]?\b/g, (id) => B[id] ? `<span class="term" tabindex="0" title="${esc(`${id}: ${B[id].metric || ""}. Role ${String(B[id].role || "?").replace("_", " ")}; target ${B[id].target} (band ${(B[id].band || []).join("–")}); engine ${B[id].computed}; ${String(B[id].status || "").replace(/_/g, " ")}.`)}">${id}</span>` : id);
}
const clip = (t, n) => (t.length <= n ? t : t.slice(0, n).replace(/\s+\S*$/, "") + "…");
const ctx = (what, needs) => `<div class="ctx">${what} <b>Needs you: ${needs}</b></div>`;
const fmtS = (s) => s >= 3600 ? (s / 3600).toFixed(1) + " h" : s >= 60 ? Math.round(s / 60) + " min" : s + " s";

// ---- markdown (small, local) with [[file#anchor]] citations
function inline(s) {
  s = esc(s);
  s = s.replace(/\[\[([^\]#]+)(?:#([^\]]*))?\]\]/g, (m, f, a) => `<button class="cite" data-file="${esc(unesc(f.trim()))}" data-anchor="${esc(unesc((a || "").trim()))}">${f.trim()}${a ? "#" + a.trim() : ""}</button>`);
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<i>$2</i>");
  return s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1");   // links render as text: nothing leaves the page
}
const NUMCELL = /^[-+]?[$]?\d[\d,]*(\.\d+)?%?$/;
function cellNum(c) { const t = c.trim(); if (!NUMCELL.test(t)) return null; const v = Number(t.replace(/[$,%]/g, "")); return /\./.test(t) || t.includes("%") || Math.abs(v) < 10000 ? t : t.replace(/[\d.]+/, (x) => Number(x).toLocaleString("en-US")); }
function md(src, doc) {
  const L = String(src || "").split("\n"), out = []; let i = 0;
  while (i < L.length) {
    let l = L[i];
    if (/^```/.test(l)) { const b = []; i++; while (i < L.length && !/^```/.test(L[i])) b.push(L[i++]); i++; out.push("<pre>" + esc(b.join("\n")) + "</pre>"); continue; }
    let m = l.match(/^(#{1,4})\s+(.*)/);
    if (m && doc && m[1].length === 1) { out.push(`<div class="doc-h1" data-h="${esc(m[2].trim())}">${inline(m[2])}</div>`); i++; continue; }
    if (m) { out.push(`<h${m[1].length} data-h="${esc(m[2].trim())}">${inline(m[2])}</h${m[1].length}>`); i++; continue; }
    if (/^\s*\|/.test(l)) { const rows = []; while (i < L.length && /^\s*\|/.test(L[i])) rows.push(L[i++]);
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const body = rows.slice(1).filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r)).map(cells), ncol = cells(rows[0]).length;
      const isNum = [...Array(ncol).keys()].map((j) => body.length && body.every((r) => r[j] == null || r[j] === "" || cellNum(r[j]) != null));
      let h = '<table class="t">'; rows.forEach((r, k) => { if (/^\s*\|?\s*:?-{2,}/.test(r)) return; const tag = k === 0 ? "th" : "td"; h += "<tr>" + cells(r).map((c, j) => `<${tag}${isNum[j] ? ' class="n"' : ""}>${isNum[j] && k ? esc(cellNum(c) || c) : inline(c)}</${tag}>`).join("") + "</tr>"; });
      out.push(h + "</table>"); continue; }
    if (/^\s*([-*]|\d+\.)\s+/.test(l)) { const ord = /^\s*\d+\./.test(l), it = [];
      while (i < L.length && /^\s*([-*]|\d+\.)\s+/.test(L[i])) { let t = L[i++].replace(/^\s*([-*]|\d+\.)\s+/, ""); while (i < L.length && /^\s{2,}\S/.test(L[i]) && !/^\s*([-*]|\d+\.)\s+/.test(L[i])) t += " " + L[i++].trim(); it.push("<li>" + inline(t) + "</li>"); }
      out.push((ord ? "<ol>" : "<ul>") + it.join("") + (ord ? "</ol>" : "</ul>")); continue; }
    if (/^>\s?/.test(l)) { const b = []; while (i < L.length && /^>\s?/.test(L[i])) b.push(L[i++].replace(/^>\s?/, "")); out.push("<blockquote>" + inline(b.join(" ")) + "</blockquote>"); continue; }
    if (!l.trim()) { i++; continue; }
    const p = []; while (i < L.length && L[i].trim() && !/^(#{1,4}\s|```|\s*\||\s*([-*]|\d+\.)\s+|>)/.test(L[i])) p.push(L[i++]);
    if (!p.length) p.push(L[i++]);
    out.push("<p>" + inline(p.join(" ")) + "</p>");
  }
  return '<div class="md">' + out.join("\n") + "</div>";
}

// ---- sidebar: runs and the learning view
const PAGE_NEEDS = ["runs", "projects", "kinds", "layers", "chat", "glossary", "sync"];   // RC17: /api/runs must carry these
async function loadRuns() {
  if (!S.meta) $("sidebar").innerHTML = '<div class="muted small" style="margin:0 8px">Loading runs…</div>';
  try {
    const j = await api("/api/runs"), missing = PAGE_NEEDS.filter((k) => !(k in j));
    if (missing.length) throw new Error(`/api/runs lacks ${missing.join(", ")}: restart the console server from this checkout`);
    S.runs = j.runs; S.meta = j;
  } catch (e) { $("sidebar").innerHTML = `<div class="card error small">${esc(e.message)}</div>`; fail("Loading runs", e); return; }
  $("banner").hidden = true;
  try { renderLoaded(); } catch (e) { fail("Showing runs", e); }
}
function renderLoaded() {
  if (!$("kind").options.length) {
    $("kind").innerHTML = S.meta.kinds.map((k) => `<option>${k}</option>`).join("");
    $("layer").innerHTML = S.meta.layers.map((k) => `<option>${k}</option>`).join("");
    syncComposer();
  }
  renderChatSpend();
  renderSync();
  if (S.tab === "runs") renderRuns();
  const first = ((S.meta.projects || [])[0] || {}).lineage || [];
  if (!S.runId && S.runs.length) selectRun(first.length ? first[first.length - 1] : S.runs[0].id);
}
function renderSync() {   // the mirror's freshness: last successful sync, failures, stale after two missed
  const sy = S.meta.sync, el = $("syncline");
  if (!sy) { el.hidden = true; return; }
  el.hidden = false;
  const last = sy.last_ok_at ? `${day(sy.last_ok_at)} ${hhmm(sy.last_ok_at)} UTC` : "never";
  el.className = "hint" + (sy.stale ? " stale" : "");
  el.textContent = !sy.configured ? `Mirror: ${sy.why}.` : `Mirror synced ${last}${sy.failures ? ` · ${sy.failures} failed since` : ""}${sy.stale ? ` · stale: ${sy.why}; remote approval is off` : ""}`;
  el.title = sy.last_error ? "last error: " + sy.last_error : "";
}
function renderChatSpend() {
  const c = S.meta && S.meta.chat; if (!c) return;
  $("chatspend").textContent = c.key ? `chat ${usd(c.spent)} of ${usd(c.cap)}` : "Chat is off (no API key). Record only still works.";
  $("send").disabled = !c.key; $("send").title = c.key ? "Send (Ctrl or Cmd + Enter)" : "Chat is off: ANTHROPIC_API_KEY is unset or a placeholder.";
}
const runLabel = (r) => r.label || r.name;
const CHIP = { completed: "ok", failed: "bad", stopped: "", running: "live", waiting: "warn", ended: "" };
function runRow(r, sub) {
  const cls = CHIP[r.chip] != null ? CHIP[r.chip] : runState(r)[0], words = r.chip || runState(r)[1];
  const eng = r.engine_id && r.engine_id.name ? `${r.engine_id.name} ${r.engine_id.version || ""}`.trim() : (r.engine || "");
  const on = r.id === S.runId, cmp = S.cmp === r.id;
  const det = on ? `<div class="det">
      ${eng ? `<span>engine</span><b>${esc(eng)}</b>` : ""}
      ${r.ended ? `<span>ended</span><b>${esc(r.ended)}</b>` : ""}
      ${r.headline_number ? `<span>outcome</span><b>${esc(r.headline_number)}${r.ui_vals ? " (" + uiText(...r.ui_vals) + ")" : r.ui ? " (95% UI " + esc(r.ui.replace(/([0-9.]+)M-/, "$1–")) + ")" : ""}</b>` : ""}
      ${r.shift ? `<span>largest shifts</span><b>${(r.shifts || []).length ? r.shifts.map((x) => `${esc(x.name)}: ${x.before} → ${x.after} units`).join("<br>") : esc(r.shift)}<br><span class="muted">vs ${esc(r.shift_vs)}</span></b>` : ""}
      ${r.spent_usd != null ? `<span>model spend</span><b>${usd(r.spent_usd)}</b>` : ""}
      ${r.spec_sha ? `<span>spec</span><b>${esc(r.spec_sha.slice(0, 10))}</b>` : ""}
      ${r.signed_at ? `<span>signed</span><b>${esc(day(r.signed_at))} ${esc(hhmm(r.signed_at))} by ${esc(r.signed_by || "?")}</b>` : ""}
      ${r.launch_commit ? `<span>commit</span><b>${esc(r.launch_commit)}</b>` : ""}
      ${r.signoff_note ? `<span>sign-off note</span><b>${esc(r.signoff_note.length > 80 ? r.signoff_note.slice(0, 80).replace(/\s+\S*$/, "") + "…" : r.signoff_note)} <button class="link" data-note="${esc(r.id)}">full note</button></b>` : ""}
      <span>folder</span><b>${esc(r.full_label || r.label || r.name)}</b><span>path</span><b class="tail" title="${esc(r.id)}">${esc(r.id)}</b></div>` : "";
  return `<div class="runitem ${on ? "on" : ""} ${cmp ? "cmp" : ""}" data-run="${esc(r.id)}">
    <div class="top"><span class="l">${esc(runLabel(r))}</span><span class="state ${esc(cls)}">${esc(words)}</span>${r.headline_number ? `<span class="h">${esc(r.headline_number)}</span>` : ""}</div>
    <div class="sc" title="${esc(r.signoff_note || "")}">${r.mirror ? '<span class="chip">live mirror</span> ' : ""}${esc(sub)}</div>${det}</div>`;
}
function renderRuns() {
  const by = {}; S.runs.forEach((r) => (by[r.id] = r));
  const pjs = (S.meta && S.meta.projects) || [{ question: "", lineage: S.runs.map((r) => r.id), drafts: [] }];
  let h = "";
  for (const pj of pjs) {
    h += `<div class="project" title="${esc(pj.question)}">${esc(pj.title || shortTitle(pj.question) || "Runs")}</div>`;
    for (const id of [...pj.lineage].reverse()) {   // newest on top
      const r = by[id]; if (!r) continue;
      const eng = r.engine_id && r.engine_id.name ? `${r.engine_id.name} ${r.engine_id.version || ""}`.trim() : "";
      const sub = r.relation ? `${r.relation}${r.prev && by[r.prev] ? " " + runLabel(by[r.prev]) : ""} · ${r.scenario}` : [eng, r.ended, r.started ? day(r.started) : ""].filter(Boolean).join(" · ");
      h += runRow(r, sub);
    }
    if (pj.drafts.length) h += `<details class="drafts"><summary>Drafts and staging copies (${pj.drafts.length})</summary>${pj.drafts.map((id) => by[id] ? runRow(by[id], by[id].draft || "") : "").join("")}</details>`;
  }
  h += S.runs.length > 1 ? '<div class="hint">Shift-click a second run to compare.</div>' : "";
  $("sidebar").innerHTML = S.runs.length ? h : '<div class="muted small">No runs under the roots.</div>';
  document.querySelectorAll(".runitem[data-run]").forEach((el) => (el.onclick = (e) => {
    if (e.shiftKey && S.runId && el.dataset.run !== S.runId) { S.cmp = el.dataset.run; renderRuns(); renderCompare(); return; }
    $("app").classList.remove("nav"); S.cmp = null; selectRun(el.dataset.run);
  }));
  document.querySelectorAll("[data-note]").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); const r = S.runs.find((x) => x.id === b.dataset.note); side("sign-off note · " + runLabel(r), `<p>${esc(r.signoff_note)}</p><p class="small muted">${esc(r.signed_by || "")} · ${esc(r.signed_at || "")} · decision_spec.signoff.json</p>`); }));
  const d = document.querySelector("details.drafts"); if (d && d.querySelector(".runitem.on")) d.open = true;
}
async function renderLearning() {
  let j; try { j = await api("/api/learning"); } catch (e) { $("sidebar").innerHTML = `<div class="card error">${esc(e.message)}</div>`; return; }
  let h = `<div class="small muted" style="margin:0 8px">${j.n} feedback item(s) across ${S.runs.length} run(s), by layer.</div><div style="margin:8px"><button id="export">Export digest</button></div>`;
  for (const [layer, items] of Object.entries(j.by_layer)) {
    if (!items.length) continue;
    h += `<div class="group">${esc(layer)} (${items.length})</div>`;
    for (const r of items) h += `<div class="runitem" title="${esc(r.run)}"><div class="q"><b>${esc(r.kind)}</b>${r.verdict ? " · " + esc(r.verdict) : ""}: ${esc(r.text)}</div>
      ${r.proposed_change ? `<div class="s">→ ${esc(r.proposed_change)}</div>` : ""}</div>`;
  }
  if (j.errors.length) h += `<div class="card error small">${j.errors.map(esc).join("<br>")}</div>`;
  $("sidebar").innerHTML = h;
  $("export").onclick = async () => { try { const r = await api("/api/export", {}); toast("Digest written: " + r.path); } catch (e) { toast(e.message); } };
}

// ---- one run: header, stepper, stream
async function selectRun(id) {   // the composer's anchor and snapshot belong to the run they were taken in
  if (id !== S.runId) setAnchor(null, null, null);
  S.runId = id; S.filter = null; S.cmp = null; await refresh(); if (S.tab === "runs") renderRuns();
}
async function refresh() {
  // Only the answer to the latest request is shown: a slower answer for an earlier request, or for a run no
  // longer selected, is dropped (request sequence). Decisions are disabled until the answer is in.
  if (!S.runId) return;
  const seq = ++S.loadSeq, id = S.runId;
  S.loading = true; markLoading();
  let run;
  try { run = await api("/api/run?" + q({ run: id })); } catch (e) {
    if (seq !== S.loadSeq) return;
    S.loading = false;
    $("stream").innerHTML = `<div class="card error"><div class="head"><span class="t">The run cannot be read</span></div><div class="body">${esc(e.message)}</div></div>`; fail("Loading the run", e); return;
  }
  if (seq !== S.loadSeq || id !== S.runId) return;
  S.run = run; S.loading = false;
  try { renderHeader(); renderStepper(); renderStream(); } catch (e) { fail("Showing the run", e); }
}
function renderHeader() {
  const r = S.run, st = r.state || {}, c = r.compute || {}, sp = r.stepper || { stages: [] };
  const summary = S.runs.find((x) => x.id === S.runId) || {};
  const question = st.question || "";
  $("crumb").textContent = runLabel(summary.label ? summary : r);
  $("title").textContent = shortTitle(question) || r.name;
  $("title").title = question;
  $("qbox").hidden = !question; $("qtext").textContent = question;
  // at most four items: state and round, engine batch, spend, compute used
  const [cls0, words] = runState({ ...st, status: r.status || st.status, finished: r.finished, error: r.error, fail_statuses: r.fail_statuses });
  const failed = sp.stages.find((s) => s.status === "failed"), cls = summary.chip === "stopped" ? "" : cls0;
  const calls = c.calls || [], k = calls.find((x) => x.live) || calls[0];
  const items = [];
  items.push(`<span class="state ${esc(cls)} ${c.live ? "live" : ""}" title="${esc(r.error || (failed && failed.why) || r.status || "")}">${esc(failed ? "Failed in " + stage(failed.state) : c.live ? "Running · " + stage(st.state) : (summary.chip === "stopped" || !summary.chip) && r.stop ? "Stopped in " + stage(st.state).toLowerCase() : words)}${st.round != null ? ` <span class="muted">· round ${esc(st.round)} of ${esc(st.max_rounds)}</span>` : ""}</span>`);
  if (k && k.done != null) items.push(k.live ? `<span>Engine batch <b>${k.done}/${esc(k.n)}</b>${k.eta_s != null ? " · ETA " + fmtS(k.eta_s) : ""}</span>`
                                           : `<span title="the run is not live; this progress record was left behind">Engine batch interrupted at <b>${k.done}/${esc(k.n)}</b></span>`);
  items.push(`<span><b>${usd(st.spent_usd)}</b>${st.budget_usd != null ? " of " + usd(st.budget_usd) : ""} spent</span>`);
  const reused = (summary.batches || r.batches || []).filter((b) => b.reused);
  items.push(`<span title="${esc((c.cap_source || "") + (c.error ? " · " + c.error : ""))}">this run <b>${esc(c.used_core_h)}</b> of ${esc(c.cap_core_h)} core-h${reused.map((b) => ` · plus ${num(b.reused)} reused points${b.reused_from ? " computed in " + esc(b.reused_from) : " from an earlier batch"}`).join("")}</span>`);
  const task = (r.stop || "").split(": ")[1];
  const where = (r.stop || "").split(":")[0].replace(/ \(round (\d+) of \d+\)/, ", round $1");
  $("stopline").innerHTML = r.stop ? `<span title="${esc(task || "")}">Stopped ${esc(where)}; no reason recorded.</span>${r.operator_note ? ` <button class="link" id="b-note">note</button>` : ""}` : "";
  if ($("b-note")) $("b-note").onclick = () => side("operator note · progress.md", `<p>${esc(r.operator_note)}</p><p class="small muted">The latest note in progress.md, written after this run's last transition.</p>`);
  $("stopline").hidden = !r.stop;
  $("meta").innerHTML = items.join("");
  $("acts").innerHTML = `<button class="link" id="b-results">Results</button><button class="link" id="b-compute">Compute</button><button class="link" id="b-build">Build on this run</button>`;
  $("b-results").onclick = () => openDoc();
  $("b-compute").onclick = () => openCompute();
  $("b-build").onclick = buildOn;
}
function renderStepper() {
  const sp = S.run.stepper;
  if (!sp) { $("stepper").innerHTML = ""; $("stepnote").textContent = "No stage history."; return; }
  const tip = { done: "done", current: "current", waiting: "waiting for you", failed: "failed", skipped: "not reached", todo: "not reached" };
  const ended = sp.stages.some((s) => s.status === "failed");   // a failed run's FINAL is not a step reached
  $("stepper").innerHTML = sp.stages.map((s) => `<div class="st ${esc(ended && s.state === "FINAL" ? "todo" : s.status)} ${S.filter === s.state ? "sel" : ""}" data-s="${esc(s.state)}" title="${esc(stage(s.state) + ": " + (s.why || tip[s.status] || s.status))}"><i></i><span>${esc(stage(s.state))}</span></div>`).join("");
  const note = [];
  if (sp.fix_rounds) note.push(`${sp.fix_rounds} fix-loop round${sp.fix_rounds > 1 ? "s" : ""}`);
  if (S.filter) note.push(`showing ${esc(stage(S.filter))} <button class="link" id="nofilter">show all</button>`);
  $("stepnote").innerHTML = note.join(" · "); $("stepnote").hidden = !note.length;
  document.querySelectorAll(".st").forEach((el) => (el.onclick = () => { S.filter = S.filter === el.dataset.s ? null : el.dataset.s; renderStepper(); renderStream(); }));
  if ($("nofilter")) $("nofilter").onclick = () => { S.filter = null; renderStepper(); renderStream(); };
}
$("v-stream").onclick = () => setView("stream");
$("v-figs").onclick = () => setView("figs");
function setView(v) { S.view = v; S.cmp = null; $("v-stream").classList.toggle("on", v === "stream"); $("v-figs").classList.toggle("on", v === "figs"); renderStream(); renderRuns(); $("streambox").scrollTop = 0; }
function renderFigures() {
  const figs = S.run.cards.filter((c) => c.kind === "figure"), sp = S.run.stepper || { stages: [] };
  $("v-figs").textContent = figs.length ? `Figures (${figs.length})` : "Figures";
  if (!figs.length) {
    const an = sp.stages.find((s) => s.state === "ANALYZE") || {};
    const why = an.status === "failed" ? "the run ended in ANALYZE before it wrote any"
      : an.status === "current" || an.status === "waiting" ? "ANALYZE is still running"
      : an.status === "done" ? "ANALYZE wrote none to figures/" : "the run has not reached ANALYZE";
    return `<div class="empty">No figures yet: ${why}.</div>`;
  }
  return `<div class="figs">${figs.map((c) => `<div class="fig" data-act="open" data-file="${esc(c.file)}"><img loading="lazy" src="/api/file?${q({ run: S.runId, path: c.file })}" alt="">
    <div class="c">${esc(c.body || c.title)}</div><div class="p">${esc(c.producer || c.title)}${c.at ? " · " + esc(day(c.at)) + " " + esc(hhmm(c.at)) : ""}</div></div>`).join("")}</div>`;
}
async function renderCompare() {
  const order = ((S.meta.projects || []).find((p) => p.lineage.includes(S.runId)) || { lineage: [] }).lineage;
  const [a, b] = order.indexOf(S.cmp) >= 0 && order.indexOf(S.cmp) < order.indexOf(S.runId) ? [S.cmp, S.runId] : [S.runId, S.cmp];   // older on the left
  let j; try { j = await api("/api/compare?" + q({ a, b })); } catch (e) { toast(e.message); return; }
  const val = (v) => { if (v == null) return "—"; try { const x = JSON.parse(v); if (Array.isArray(x)) return x.length === 2 && x.every((n) => typeof n === "number") ? `${x[0]}–${x[1]}` : x.join(", "); return String(x); } catch (e) { return String(v); } };
  const [A, B] = j.labels, row = (k, a, b, ch) => `<tr class="${ch ? "ch" : ""}"><td>${k}</td><td>${a}</td><td>${b}</td></tr>`;
  const eng = (e) => e && e.name ? esc(`${e.name} ${e.version || ""}`) : "—";
  const head = `<table class="cmp"><tr><th></th><th>${esc(A)}</th><th>${esc(B)}</th></tr>`;
  let h = `<div class="cmp-h" style="margin-top:4px">Compare ${esc(A)} and ${esc(B)} <button class="link" id="cmp-close">close</button></div>` + head +
    row("status", esc(j.chips[0].join(" · ")), esc(j.chips[1].join(" · ")), false) +
    row("engine", eng(j.engines[0]), eng(j.engines[1]), eng(j.engines[0]) !== eng(j.engines[1])) +
    row("outcome", ...[0, 1].map((k) => esc(j.headline[k][0] || "—") + ((j.ui_vals || [])[k] ? " (" + uiText(...j.ui_vals[k]) + ")" : j.headline[k][1] ? " (95% UI " + esc(j.headline[k][1].replace(/([0-9.]+)M-/, "$1–")) + ")" : "")), j.headline[0][0] !== j.headline[1][0]) +
    row("model spend", usd(j.spent[0]), usd(j.spent[1]), false) + "</table>";
  h += `<div class="cmp-h">Spec${j.same_spec ? " (the same file in both runs)" : ""}</div>` + (!j.same_spec && !j.spec_diff.length ? `<div class="empty" style="padding:4px 0">The spec files differ (sha256 ${esc(String((j.spec_shas || [])[0] || "?").slice(0, 10))} and ${esc(String((j.spec_shas || [])[1] || "?").slice(0, 10))}) outside the compared fields.</div>` : "") + (j.spec_diff.length ? head + j.spec_diff.map((d) => row(esc(d.field.replace("decision_units.", "")), esc(val(d.before)), esc(val(d.after)), true)).join("") + "</table>" : (j.same_spec ? '<div class="empty" style="padding:4px 0">Same spec.</div>' : ""));
  const pk = Object.entries(j.packages);
  h += `<div class="cmp-h">Units per package component</div>` + (pk.length ? `<table class="cmp"><tr><th></th><th class="n">${esc(A)}</th><th class="n">${esc(B)}</th><th class="n">change</th></tr>` + pk.map(([k, [a, b]]) => `<tr class="${a !== b ? "ch" : ""}"><td>${esc(k)}</td><td class="n">${a}</td><td class="n">${b}</td><td class="n">${b - a > 0 ? "+" : ""}${b - a || ""}</td></tr>`).join("") + "</table>" : '<div class="empty" style="padding:4px 0">No allocation.csv in either run.</div>');
  const ts = Object.entries(j.tests), v = (x) => x == null ? "—" : DT_WORD[x] || DT_WORD[DT_STATE_OF[x]] || DT_WORD.error;
  h += `<div class="cmp-h">Decision tests: does the plan change?</div>` + (ts.length ? head + ts.map(([k, [a, b]]) => row(esc(k), esc(v(a)), esc(v(b)), a !== b)).join("") + "</table>" : '<div class="empty" style="padding:4px 0">No decision tests in either run.</div>');
  $("stream").innerHTML = h; $("streambox").scrollTop = 0;
  $("cmp-close").onclick = () => { S.cmp = null; renderRuns(); renderStream(); };
}
function renderStream() {
  if (S.cmp) return renderCompare();
  if (S.view === "figs") { $("stream").innerHTML = renderFigures(); wire($("stream")); return; }
  renderFigures();
  const box = $("streambox"), atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  const cards = S.run.cards.filter((c) => !S.filter || c.stage === S.filter), items = [], seen = {};
  for (const c of cards) {   // blockers of one round and all decision tests read as one table each
    const g = c.kind === "blocker" ? "b" + (c.round == null ? "" : c.round) : c.kind === "decision_test" ? "dt" : null;
    if (!g) { items.push(c); continue; }
    if (!seen[g]) { seen[g] = { kind: g === "dt" ? "dt_table" : "blocker_table", round: c.round, at: c.at, rows: [] }; items.push(seen[g]); }
    seen[g].rows.push(c);
  }
  const top = !S.filter ? summaryBlock() : "";
  const saved = saveForms();
  $("stream").innerHTML = top + (items.map(card).join("") || '<div class="muted small" style="padding:12px 0">Nothing here for this stage.</div>');
  restoreForms(saved);
  wire($("stream"));
  markLoading();
  if (atBottom) box.scrollTop = box.scrollHeight;
}
function summaryBlock(forDoc) {
  const sm = (S.run && S.run.summary) || [];
  if (!sm.length) return "";
  const cite = (it) => `<button class="s" data-act="open" data-file="${esc(it.source)}" data-anchor-id="${esc(it.key.split(".").pop())}" title="${esc(it.source + " · " + it.key)}">${esc(it.source.split("/").pop())}</button>`;
  const tiles = sm.map((it, i) => {
    const label = it.unit === "dalys" ? T("DALYs") + " averted" : it.label === "allocatable" ? T("allocatable", "Allocatable") : esc(it.label.replace(/^./, (x) => x.toUpperCase()));
    const v = it.unit === "units" ? `${num(it.value)}${it.of ? ` <span class="d">of ${num(it.of)} units</span>` : ""}` : num(it.value, it.unit);
    const ui = it.ui ? `<div class="d">${uiText(it.ui[0], it.ui[1], it.level)} · median ${num(it.median)}</div>` : "";
    return `<div class="stat ${i === 0 && it.ui ? "wide" : ""}"><div class="k">${label}</div><div class="v ${i === 0 && it.ui ? "hero" : ""}">${v}</div>${ui}<div>${cite(it)}${it.ui_source ? " " + cite({ source: it.ui_source, key: "fixed_plan" }) : ""}</div></div>`;
  }).join("");
  const covers = (sm[0] && sm[0].covers) || [];
  return `<div class="stats">${tiles}</div>${covers.length ? `<div class="ctx">Point plan, ${T("comparator")}-based. The ${T("UI")} covers ${covers.map((c) => c === "calibration_partly_clamped" ? T("calibration partly clamped") : esc(c.replace(/_/g, " "))).join(", ")}. <b>Needs you: no.</b></div>` : ""}`;
}
function statusCell(p) { return p === true ? '<span class="ok-i">✓ pass</span>' : p === false ? '<span class="bad-i">✗ fail</span>' : '<span class="muted">? unknown</span>'; }
function gateCard(c, head, open, comment) {
  const dw = c.done_when || [];
  const rows = dw.map((r) => {
    const checks = r.checks.length ? r.checks : ["(no done-when test recorded)"], n = checks.length;
    return `<tr class="grp"><td colspan="5"><b>${esc(r.blocker)}</b> ${r.severity ? T(r.severity) : ""} · ${esc(r.reviewer || "")} — ${bench(clip((r.claim || "").split("\n")[0], 150))}</td></tr>` +
      checks.map((ch, k) => `<tr><td>${esc(r.blocker)}.${k + 1}</td><td${(r.raw || [])[k] ? ` title="${esc("pattern: " + r.raw[k])}"` : ""}>${bench(ch)}</td>${k === 0 ? `<td rowspan="${esc(n)}">${statusCell(r.passes)}</td><td rowspan="${esc(n)}">${esc((r.detail || "").replace(/^(FAIL|PASS)\s*/, ""))}</td><td rowspan="${esc(n)}">${r.mechanical ? T("mechanical") : T("judged")}</td>` : ""}</tr>`).join("");
  }).join("");
  const act = c.action || "", rule = c.rule != null ? `rule ${c.rule}` : "";
  const needs = act === "AWAIT_OWNER" ? "yes, answer the decision card below." : "no.";
  return `<div class="card">${head(`${T("gate", "Gate")} round ${esc((c.title.match(/round (\d+)/) || [])[1] || "?")}: ${T(act)} <span class="muted small">(${T(rule)})</span>`)}
    ${ctx(`${T("gate", "The gate")} decides whether the run may write its report. ${T("ACCEPT")} needs no unresolved ${T("HIGH")} ${T("blocker")} and no missing required artifact.`, needs)}
    <div class="body">${esc(c.body)}</div>
    ${rows ? `<div class="cmp-h">${T("done-when", "Done-when")} tests, by blocker</div><table class="t fixed"><colgroup><col style="width:11%"><col style="width:45%"><col style="width:9%"><col style="width:24%"><col style="width:11%"></colgroup><tr><th>criterion</th><th>what it checks</th><th>status</th><th>detail</th><th>test</th></tr>${rows}</table>` : ""}
    <div class="acts">${open}${comment}</div></div>`;
}
function blockerTable(g) {
  const rows = g.rows.map((c, i) => {
    const tog = "bt" + (++TOG_N);
    return `<tr class="row" data-tog="${esc(tog)}"><td>${esc(c.blocker_id)}</td><td>${T(c.severity)}</td><td>${esc(c.reviewer || "")}</td><td>${esc(stage(c.target_stage))}</td><td class="clip">${bench(String(c.claim || "").split("\n")[0])}</td><td>${c.resolved ? '<span class="ok-i">✓ resolved</span>' : "open"}</td></tr>
      <tr class="more" id="${esc(tog)}" hidden><td colspan="6"><div>${bench(c.claim || "")}</div>${c.evidence ? `<div class="sub">Evidence: ${bench(c.evidence)}</div>` : ""}${c.fix_requires ? `<div class="sub">Fix requires: ${bench(c.fix_requires)}</div>` : ""}
      <div class="acts"><button data-act="agree" data-ref="${ref({ anchor: c.anchor || {}, evidence: c.snapshot })}">Agree</button><button data-act="disagree" data-ref="${ref({ anchor: c.anchor || {}, evidence: c.snapshot })}">Disagree</button><button data-act="comment" data-ref="${ref({ anchor: c.anchor || {}, evidence: c.snapshot })}">Comment</button><button data-act="open" data-file="${esc(c.anchor.file)}" data-anchor-id="${esc(c.blocker_id)}">Open</button></div></td></tr>`;
  }).join("");
  const high = g.rows.filter((c) => c.severity === "HIGH" && !c.resolved).length;
  return `<div class="card"><div class="head"><span class="t">Critique${g.round != null ? " round " + esc(g.round) : ""}: ${g.rows.length} ${T("blocker", "blockers")}, ${high} open ${T("HIGH")}</span>${when(g.at)}</div>
    ${ctx(`Findings from the critics. An open ${T("HIGH")} blocks ${T("ACCEPT")}; each has a ${T("done-when")} test. Click a row for the full claim.`, "no. Agree or disagree to record feedback.")}
    <table class="t"><tr><th>id</th><th>severity</th><th>reviewer</th><th>${T("target stage")}</th><th>claim</th><th>status</th></tr>${rows}</table></div>`;
}
// Four states, never folded: a test that did not run, or whose result cannot be read, is not "no change".
const DT_STATE_OF = { CHANGES_PLAN: "changed", NO_CHANGE: "unchanged", NOT_RUN: "not_run" };
const DT_WORD = { changed: "changes plan", unchanged: "no change", not_run: "not run", error: "error: cannot be read" };
const DT_CELL = { changed: '<span class="bad-i">yes</span>', unchanged: "no", not_run: '<span class="muted">not run</span>', error: '<span class="bad-i">error</span>' };
function dtTable(g) {
  g.rows.forEach((c) => { if (!c.state) c.state = DT_STATE_OF[c.verdict] || "error"; c.test_id = c.test_id || (c.anchor || {}).id; });   // a server from before the four states
  const thrs = [...new Set(g.rows.map((c) => c.threshold_pct))], thr = thrs.length === 1 ? thrs[0] : undefined;
  const first = (t) => { const m = String(t).match(/^.*?[.?!](?=\s|$)/); return m ? m[0] : String(t); };
  const rows = g.rows.map((c, i) => { const body = c.body || "", lead = first(body), rest = body.slice(lead.length).trim();
    return `<tr><td><button class="link" data-act="open" data-file="${esc(c.anchor.file)}" data-anchor-id="${esc(c.test_id)}">${esc(c.test_id).replace(/_/g, "_<wbr>")}</button></td>
    <td>${bench(lead)}${rest ? ` <button class="link" data-more="${esc("dtm" + i)}">more</button><div id="${esc("dtm" + i)}" hidden class="sub">${bench(rest)}</div>` : ""}${c.parameter ? `<div class="sub">${esc(c.parameter)}</div>` : ""}</td>
    <td class="n">${(c.values || []).map((v) => esc(typeof v === "number" ? num(v) : v)).join(", ") || "—"}${c.unit && (c.values || []).some((v) => typeof v === "number") ? ` <span class="muted">${esc(c.unit)}</span>` : ""}</td>
    <td${c.why ? ` title="${esc(c.why)}"` : ""}>${DT_CELL[c.state] || DT_CELL.error}</td><td class="n">${pct(c.share_pct)}${c.threshold_pct != null && c.threshold_pct !== thr ? ` <span class="muted">(threshold ${pct(c.threshold_pct, 0)})</span>` : ""}</td>
    <td class="n">${(c.objective_vs_point_pct || []).map((x) => x == null ? "—" : (x > 0 ? "+" : "") + pct(x, 2)).join(", ") || "—"}</td></tr>`; }).join("");
  const n = (st) => g.rows.filter((c) => c.state === st).length, nr = n("not_run"), er = n("error");
  return `<div class="card"><div class="head"><span class="t">${T("decision test", "Decision tests")}: ${n("changed")} of ${g.rows.length} change the plan${nr ? `, ${nr} not run` : ""}${er ? `, ${er} cannot be read` : ""}</span>${when(g.at)}</div>
    ${ctx(`Each test re-solves the plan with one input at other values. It changes the plan when more than ${thr != null ? pct(thr, 0) : "the threshold share"} of units change package.`, "no.")}
    <table class="t"><tr><th>id</th><th style="width:44%">what it tests</th><th class="n">value tested</th><th>plan changes?</th><th class="n">units changed</th><th class="n"><span class="term" tabindex="0" title="The objective of the plan re-solved at the tested value, relative to the point plan's objective evaluated at the same value: how much re-optimising gains (models/decision_tests/*.yaml: objective vs point_plan_objective_under_test).">re-solved vs point plan</span></th></tr>${rows}</table></div>`;
}
function card(c) {
  const comment = c.anchor ? `<button data-act="comment" data-ref="${ref({ anchor: c.anchor, evidence: c.snapshot })}">Comment</button>` : "";
  const open = c.anchor && c.anchor.file ? `<button data-act="open" data-file="${esc(c.anchor.file)}" data-anchor-id="${esc(c.anchor.id || "")}">Open</button>` : "";
  const head = (t, extra) => `<div class="head"><span class="t">${t}</span>${extra || ""}${when(c.at)}</div>`;
  const line = (cls, txt) => `<div class="line ${esc(cls || "")}"><span class="x">${txt}</span>${when(c.at)}</div>`;
  switch (c.kind) {
    case "blocker_table": return blockerTable(c);
    case "dt_table": return dtTable(c);
    case "stage": return line(c.stage === "FINAL" && /fail/i.test(c.body || "") ? "bad" : "", `<b>${esc(c.title)}</b>${c.body ? " · " + esc(c.body) : ""}${c.spent_usd != null ? ` · ${usd(c.spent_usd)}` : ""}`);
    case "fix_loop": return line("", `<b>Fix loop</b> · ${esc(c.body)}`);
    case "batch": return line("", `<b>Engine batch</b> · ${c.done == null ? "?" : c.done}/${esc(c.n)} done${c.live ? (c.eta_s != null ? " · ETA " + fmtS(c.eta_s) : "") : c.done != null ? " · interrupted" : ""}${c.error ? " · " + esc(c.error) : ""}`);
    case "figure": return `<div class="card">${head(esc(c.body || c.title))}${ctx("A figure from the analysis.", "no.")}<img class="thumb" loading="lazy" src="/api/file?${q({ run: S.runId, path: c.file })}" data-act="open" data-file="${esc(c.file)}">
      <div class="sub">${esc(c.title)}${c.producer ? ` · ${esc(c.producer)}` : ""}</div>${c.provenance_error ? `<div class="sub" style="color:var(--bad)">provenance unreadable: ${esc(c.provenance_error)}</div>` : ""}<div class="acts">${open}${comment}</div></div>`;
    case "gate": return gateCard(c, head, open, comment);
    case "decision_needed": return decisionCard(c, head);
    case "chat": {
      const body = c.role === "assistant" ? md(c.text) : esc(c.text).replace(/\n/g, "<br>");
      const unc = (c.uncited || []).length ? `<div class="uncited">Uncited: ${c.uncited.map(esc).join(" · ")}</div>` : "";
      const unchk = (c.citations || []).filter((x) => x.status !== "checked");
      const chk = unchk.length ? `<div class="uncited">Citations not checked against what Claude was shown: ${unchk.map((x) => esc(x.file + (x.anchor ? "#" + x.anchor : "") + " (" + x.status + ")")).join(" · ")}</div>` : "";
      const cost = c.usd != null ? `<span class="tm" title="${esc(c.fallback ? "a refusal fallback answered" + (c.model ? " (" + c.model + ")" : "") + ": charged at the reserved worst case" : c.model && c.model !== "claude-opus-5-5" ? "served by " + c.model + ", priced at Opus 5.5 rates" : "")}">$${Number(c.usd).toFixed(3)}</span>` : "";
      return `<div class="card chat ${esc(c.role)}"><div class="head"><span class="t">${c.role === "assistant" ? "Claude" : "You"}</span>${cost}${when(c.at)}</div><div class="body">${body}</div>${unc}${chk}</div>`;
    }
    case "feedback": return `<div class="card chat feedback">${head("Feedback: " + esc(c.fb_kind) + (c.verdict ? " · " + esc(c.verdict) : ""), `<span class="tm">${esc(c.layer)}</span>`)}<div class="body">${esc(c.text)}</div>${c.evidence && c.evidence.excerpt ? `<div class="sub" title="${esc("sha256 " + (c.evidence.sha256 || "") + " · " + (c.evidence.at || ""))}">Seen then (round ${esc(c.evidence.round == null ? "?" : c.evidence.round)}, ${esc(String(c.evidence.sha256 || "").slice(0, 10))}): ${esc(clip(String(c.evidence.excerpt), 300))}</div>` : ""}${c.anchor && c.anchor.file ? `<div class="sub">on ${esc(c.anchor.file)}${c.anchor.id ? "#" + esc(c.anchor.id) : ""}</div>` : ""}${c.proposed_change ? `<div class="sub">Proposed: ${esc(c.proposed_change)}</div>` : ""}</div>`;
    case "error": return `<div class="card error">${head(esc(c.title))}<div class="body small">${esc(c.body)}</div><div class="acts">${open}</div></div>`;
    default: return `<div class="card">${head(esc(c.title || c.kind))}<div class="body">${esc(c.body || "")}</div></div>`;
  }
}
function remoteBlock() {   // a remote answer needs a fresh mirror: why not, or ""
  const sy = S.meta && S.meta.sync;
  return S.run && S.run.mirror && sy && sy.stale ? `The mirror is stale (${sy.why}): remote approval is off until a sync succeeds.` : "";
}
function decisionCard(c, head) {
  // The controls act on the run and the content this card was rendered for (ref), never on whatever run is
  // selected when the button is pressed; the server checks the ticket and the sign command the content.
  const k = ref({ run: c.run_id, ticket: c.ticket, sub: c.sub, sha: c.spec_sha256, dsha: c.derivations_sha256, ssha: c.sheet_sha256, fps: c.fingerprints || [] });
  const block = remoteBlock() || (c.sub === "spec" && c.sheet_stale ? `The sign-off sheet is stale (${c.sheet_stale}); approval waits for the regenerated sheet.` : ""), dis = block ? " disabled" : "";
  const blockNote = block ? `<div class="small" style="color:var(--bad)">${esc(block)}</div>` : "";
  if (c.sub === "spec") return `<div class="card need">${head(esc(c.title))}${ctx("The run waits for the owner to sign its decision spec.", "yes, answer below.")}<div class="body">${md(c.body)}</div>
    <div class="small muted">Signing binds to decision_spec.yaml sha256 ${esc(String(c.spec_sha256 || "?").slice(0, 12))}; a spec changed since this page loaded is refused.</div>
    <div class="answer" data-ref="${esc(k)}" data-key="${esc(c.ticket || "")}"><textarea class="note" placeholder="Your note (required): what you checked, what you accept"></textarea>
    <label class="inline"><input type="checkbox" class="confirm"> I have read the sheet and sign the spec as the owner</label><button class="primary" data-act="sign-spec"${dis}>Sign</button>${blockNote}<div class="small result"></div></div></div>`;
  const items = (c.items || []).map((it, i) => `<div style="margin:12px 0"><b>${esc(it.finding)}</b>
      <div class="sub">Why it bears on the decision: ${esc(it.reason)}</div><div class="sub">Gate finding: ${esc(it.claim)}</div>
      ${(it.declarations || []).map((d) => `<div class="sub">Declaration ${esc(d.id)}: ${inline(d.test)}</div>`).join("") || '<div class="sub">Not declared, and untested.</div>'}
      <div style="margin-top:6px"><label class="inline"><input type="radio" name="${esc(k + "_" + i)}" value="accept" data-f="${esc(it.fingerprint)}" ${it.answer === "accept" ? "checked" : ""}> accept</label>
      <label class="inline"><input type="radio" name="${esc(k + "_" + i)}" value="decline" data-f="${esc(it.fingerprint)}" ${it.answer === "decline" ? "checked" : ""}> decline</label>${it.answer ? `<span class="small muted">recorded: ${esc(it.answer)}</span>` : ""}</div></div>`).join("");
  return `<div class="card need">${head(esc(c.title), c.status ? `<span class="tm">${esc(c.status)}</span>` : "")}${ctx("Findings that bear on the decision; only the owner may accept them.", "yes, answer below.")}<div class="body">${esc(c.body)}</div>${items}
    <div class="answer" data-ref="${esc(k)}" data-key="${esc(c.ticket || "")}"><textarea class="note" placeholder="Your note (required)"></textarea>
    <label class="inline"><input type="checkbox" class="confirm"> I answer these declarations as the owner</label><button class="primary" data-act="scope"${dis}>Submit answers</button>${blockNote}<div class="small result"></div></div></div>`;
}
// An answer being written survives a refresh: its note, confirmation and choices are kept by the card's ticket
// (the run and the content shown). A changed request has a new ticket, so its form starts empty.
function saveForms() {
  const out = {};
  document.querySelectorAll("#stream .answer[data-key]").forEach((box) => {
    const card = box.closest(".card");
    out[box.dataset.key] = { note: box.querySelector(".note").value, confirm: box.querySelector(".confirm").checked,
      radios: [...card.querySelectorAll("input[type=radio]:checked")].map((r) => [r.dataset.f, r.value]),
      focus: document.activeElement === box.querySelector(".note"), result: box.querySelector(".result").textContent };
  });
  return out;
}
function restoreForms(saved) {
  document.querySelectorAll("#stream .answer[data-key]").forEach((box) => {
    const f = saved[box.dataset.key]; if (!f) return;
    const card = box.closest(".card");
    box.querySelector(".note").value = f.note; box.querySelector(".confirm").checked = f.confirm;
    box.querySelector(".result").textContent = f.result || "";
    for (const [fp, v] of f.radios) card.querySelectorAll("input[type=radio]").forEach((r) => { if (r.dataset.f === fp && r.value === v) r.checked = true; });
    if (f.focus) box.querySelector(".note").focus();
  });
}
function markLoading() {   // while a run loads, no decision can be sent
  document.querySelectorAll(".answer button.primary").forEach((b) => { b.disabled = S.loading || !!remoteBlock(); });
}
function wire(root) {
  root.querySelectorAll("[data-more]").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); const m = document.getElementById(b.dataset.more); if (m) { m.hidden = !m.hidden; b.textContent = m.hidden ? "more" : "less"; } }));
  root.querySelectorAll("tr[data-tog]").forEach((tr) => (tr.onclick = () => { const m = document.getElementById(tr.dataset.tog); if (m) m.hidden = !m.hidden; }));
  root.querySelectorAll(".cite").forEach((b) => (b.onclick = () => openFile(b.dataset.file, b.dataset.anchor)));
  root.querySelectorAll("[data-act]").forEach((b) => (b.onclick = () => act(b)));
}
async function act(b) {
  // The anchor and the evidence snapshot the card was rendered with (sealed by the server) travel together.
  const anchor = b.dataset.ref ? getRef(b.dataset.ref).anchor || null : null, shown = b.dataset.ref ? getRef(b.dataset.ref).evidence || null : null;
  switch (b.dataset.act) {
    case "open": return openFile(b.dataset.file, b.dataset.anchorId || "");
    case "comment": return setAnchor(anchor, null, shown);
    case "agree":
      try { await api("/api/feedback", { run: S.runId, kind: "approval", layer: "critique", verdict: "agree", anchor, evidence: shown, text: `Agree with ${anchor.id}`, stage: "CRITIQUE" }); toast(`Recorded: agree with ${anchor.id}`); refresh(); } catch (e) { toast(e.message); }
      return;
    case "disagree": setAnchor(anchor, "disagree", shown); $("kind").value = "correction"; $("layer").value = "critique"; syncComposer(); $("text").placeholder = `Why do you disagree with ${anchor.id}?`; return;
    case "sign-spec": case "scope": {
      const box = b.closest(".answer"), note = box.querySelector(".note").value, confirmed = box.querySelector(".confirm").checked;
      const res = box.querySelector(".result"), R = getRef(box.dataset.ref);
      if (S.loading || !S.run || !R.run || R.run !== S.run.run_dir || S.run.run_dir !== S.runId) { res.textContent = "The run is loading or another run is selected: wait for it to load, then answer."; return; }
      if (remoteBlock()) { res.textContent = remoteBlock(); return; }
      const body = { run: R.run, ticket: R.ticket, note, confirm: confirmed };
      if (b.dataset.act === "scope") { body.fingerprints = R.fps; body.answers = {}; b.closest(".card").querySelectorAll("input[type=radio]:checked").forEach((r) => (body.answers[r.dataset.f] = r.value)); }
      else { body.spec_sha256 = R.sha; body.derivations_sha256 = R.dsha; body.sheet_sha256 = R.ssha; }
      if (!confirmed) { res.textContent = "Tick the confirmation first."; return; }
      if (!window.confirm(b.dataset.act === "scope" ? "Submit these answers through scripts/scope_signoff.py sign?" : "Sign the decision spec through agents/stage_machine.py sign?")) return;
      try { const r = await api(b.dataset.act === "scope" ? "/api/scope" : "/api/sign_spec", body);
        res.innerHTML = r.refused ? `<span style="color:var(--bad)">Not sent: ${esc(r.refused)}</span>` : `<span style="color:${r.ok ? "var(--ok)" : "var(--bad)"}">exit ${esc(r.rc)}</span><pre>${esc(r.out)}</pre><div class="muted">${esc((r.cmd || []).slice(1).join(" "))}</div>`;
        if (r.ok) refresh(); } catch (e) { res.textContent = e.message; }
      return;
    }
  }
}

// ---- composer
function setAnchor(a, verdict, shown) { S.anchor = a; S.verdict = verdict; S.shown = shown || null; renderAnchor(); $("text").focus(); }
function renderAnchor() {
  $("anchor").innerHTML = S.anchor ? `<span class="chip">on ${esc(S.anchor.kind)} ${esc(S.anchor.id || "")}${S.verdict ? " · " + S.verdict : ""} <button class="link" id="unanchor">×</button></span>` : "";
  if ($("unanchor")) $("unanchor").onclick = () => setAnchor(null, null);
}
function syncComposer() { const steer = $("kind").value !== "question"; $("layer").style.display = steer ? "" : "none"; $("proposed").style.display = steer ? "" : "none"; $("record").style.display = steer ? "" : "none"; }
$("kind").onchange = syncComposer;
function curStage() { return S.filter || (S.run && S.run.state && S.run.state.state) || ""; }
$("record").onclick = async () => {
  const text = $("text").value.trim(); if (!text || !S.runId) return;
  try { await api("/api/feedback", { run: S.runId, kind: $("kind").value, layer: $("layer").value, text, anchor: S.anchor, evidence: S.shown, verdict: S.verdict, proposed_change: $("proposed").value, stage: curStage() });
    $("text").value = ""; $("proposed").value = ""; setAnchor(null, null); toast("Feedback recorded"); refresh(); } catch (e) { toast(e.message); }
};
$("send").onclick = async () => {
  const text = $("text").value.trim(); if (!text || !S.runId) return;
  const kind = $("kind").value, btn = $("send");
  btn.disabled = true; btn.textContent = "Thinking…";
  try {
    const body = { run: S.runId, text, anchor: S.anchor, evidence: S.shown, kind, layer: $("layer").value, proposed_change: $("proposed").value, stage: curStage(), verdict: S.verdict };
    const r = await api("/api/chat", body);
    if (!r.ok) toast(r.error + (r.feedback ? " (the feedback was recorded)" : ""));
    else { $("text").value = ""; $("proposed").value = ""; setAnchor(null, null); S.meta.chat.spent = r.spent; }
    refresh();
  } catch (e) { toast(e.message); } finally { btn.textContent = "Send"; renderChatSpend(); }
};
$("text").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("send").click(); });

// ---- side panel: artifacts, results, compute, build-on
function side(title, html) { $("side-title").textContent = title; $("side-content").innerHTML = html; $("side").classList.add("open"); wire($("side-content")); }
$("side-close").onclick = () => { $("side").classList.remove("open"); stopLive(); };
async function openFile(file, anchor) {
  if (!S.runId) return;
  const url = "/api/file?" + q({ run: S.runId, path: file });
  if (/\.(png|jpe?g|svg)$/i.test(file)) {
    const fig = (S.run.cards || []).find((c) => c.kind === "figure" && c.file === file);
    side(file, `<img src="${esc(url)}">${fig ? `<p>${esc(fig.body)}</p><p class="small muted">${esc(fig.producer || "")} ${fig.sources ? "from <code>" + fig.sources.map(esc).join("</code>, <code>") + "</code>" : ""}</p>` : ""}<div class="acts"><button data-act="comment" data-ref="${ref({ anchor: { kind: "figure", id: file, file }, evidence: fig && fig.snapshot })}">Comment</button></div>`);
    return;
  }
  let text;
  try { const r = await fetch(url); text = await r.text(); if (!r.ok) throw new Error(JSON.parse(text).error); } catch (e) { side(file, `<div class="card error"><div class="body">${esc(e.message)}</div></div>`); return; }
  if (/\.md$/i.test(file)) {
    side(file + (anchor ? "#" + anchor : ""), md(text));
    if (anchor) { const h = [...$("side-content").querySelectorAll("[data-h]")].find((e) => e.dataset.h.toLowerCase().includes(anchor.toLowerCase())); if (h) { h.classList.add("hit"); h.scrollIntoView({ block: "start" }); } }
  } else {
    const lines = text.split("\n"), key = (anchor || "").toLowerCase();
    let hit = -1;
    if (key) hit = lines.findIndex((l) => new RegExp(`(^|[\\s"'{:\\-])${key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}($|[\\s"',:}])`, "i").test(l));
    side(file + (anchor ? "#" + anchor : ""), `<div class="lines">${lines.map((l, k) => `<div class="${k >= hit && hit >= 0 && k < hit + 12 ? "hit" : ""}" ${k === hit ? 'id="hit"' : ""}>${esc(l) || " "}</div>`).join("")}</div>`);
    if ($("hit")) $("hit").scrollIntoView({ block: "center" });
  }
}
function openDoc() {
  const d = S.run.docs || {}, names = Object.keys(d);
  if (!names.length) { side("results", '<div class="muted">No results.md, manuscript.md or report.md yet.</div>'); return; }
  side(names.join(" · "), summaryBlock() + names.map((n) => (d[n].error ? `<div class="card error">${esc(d[n].error)}</div>` : md(d[n].text, true))).join("<hr>"));
}
async function openCompute() {
  const c = S.run.compute || {};
  const calls = (c.calls || []).map((k) => `<tr><td><code>${esc(k.fn)}</code></td><td>${esc(k.purpose || "")}</td><td>${k.done == null ? "?" : k.done}/${esc(k.n)}</td><td>${k.core_s == null ? "" : Math.round(k.core_s)}</td><td>${k.live ? "live" : "interrupted"}</td><td>${k.eta_s == null ? "" : fmtS(k.eta_s)}</td><td>${esc(k.error || "")}</td></tr>`).join("");
  let h = `<h3>This run</h3><table class="kv"><tr><td>core-hours used</td><td>${esc(c.used_core_h)} of ${esc(c.cap_core_h)} <span class="muted small">(settled in the ledger${c.live ? ", plus live calls" : ""}; cap from ${esc(c.cap_source)})</span></td></tr><tr><td>finished calls</td><td>${esc(c.done_calls)}</td></tr></table>${c.error ? `<div class="card error">${esc(c.error)}</div>` : ""}
    ${calls ? `<table class="kv"><tr class="muted"><td>fn</td><td>purpose</td><td>done/n</td><td>core-s</td><td>state</td><td>ETA</td><td></td></tr>${calls}</table>` : '<p class="muted small">No compute call in flight.</p>'}
    <h3>Coiled</h3><label class="inline"><input type="checkbox" id="live" ${S.live ? "checked" : ""}> live (refreshes at most once a minute; each refresh attaches to the cluster read-only)</label><div id="cloud"></div>`;
  side("compute", h);
  $("live").onchange = () => { S.live = $("live").checked; if (S.live) { cloud(); S.liveTimer = setInterval(cloud, 60000); } else stopLive(); };
  cloud(false);
}
function stopLive() { S.live = false; clearInterval(S.liveTimer); S.liveTimer = null; }
async function cloud(force) {
  let j; try { j = await api("/api/cloud?" + q({ live: (force === false ? false : S.live) ? 1 : 0 })); } catch (e) { if ($("cloud")) $("cloud").innerHTML = `<div class="card error">${esc(e.message)}</div>`; return; }
  if (!$("cloud")) return;
  if (!j.available) { $("cloud").innerHTML = `<p class="muted">${esc(j.why)}</p>`; return; }
  const s = j.snapshot;
  if (!s) { $("cloud").innerHTML = '<p class="muted small">No snapshot yet: turn on live.</p>'; return; }
  const cl = s.clusters ? Object.entries(s.clusters).map(([k, v]) => `<tr><td>${k}</td><td><code>${esc(v.name || "")}</code></td><td><b>${esc(v.state)}</b></td></tr>`).join("") : "";
  $("cloud").innerHTML = `<p class="small muted">snapshot ${when(s.at)}${j.next_refresh_s ? " · next refresh in " + j.next_refresh_s + " s" : ""}</p>${cl ? `<table class="kv">${cl}</table>` : ""}` +
    ["status", "health", "report-cost"].map((k) => `<h4>${k} <span class="muted small">exit ${esc(s[k].rc)}</span></h4><pre>${esc(s[k].out)}</pre>`).join("");
}
async function buildOn() {
  if (!window.confirm("Write a draft directory that reuses this run's assets? Nothing is launched.")) return;
  try {
    const r = await api("/api/build_on", { run: S.runId });
    const b = (r.batches || []).map((x) => `<tr><td><code>${esc(x.out)}</code></td><td><b>${x.reused ? "reused" : "not reused"}</b></td><td>${esc(x.why)}</td></tr>`).join("");
    side("build on this run", `<p>Draft written: <code>${esc(r.draft_dir)}</code></p><h3>Commands (run them yourself; they spend money)</h3>
      <p class="small">Continue this run:</p><pre>${esc(r.commands.continue_this_run)}</pre><p class="small">New run on the same question:</p><pre>${esc(r.commands.new_run)}</pre>
      <h3>Reused assets</h3><p class="small">spec ${esc(r.spec.copied_from || "none")} · sign-off ${esc(r.spec.signoff_status)} · engine <code>${esc(r.engine || "")}</code></p><p class="small">${(r.copied || []).map((x) => `<code>${esc(x)}</code>`).join(" ")}</p>
      <h3>Stored engine batches</h3>${b ? `<table class="kv">${b}</table>` : '<p class="muted small">No stored batch.</p>'}
      <h3>What needs harness support</h3><ul>${(r.harness_support || []).map((x) => `<li class="small">${esc(x)}</li>`).join("")}</ul>`);
  } catch (e) { toast(e.message); }
}

// ---- tabs, theme, phone nav, refresh
$("tab-runs").onclick = () => { S.tab = "runs"; $("tab-runs").classList.add("on"); $("tab-learn").classList.remove("on"); renderRuns(); };
$("tab-learn").onclick = () => { S.tab = "learn"; $("tab-learn").classList.add("on"); $("tab-runs").classList.remove("on"); renderLearning(); };
$("theme").onclick = () => { const d = document.documentElement, dark = d.dataset.theme ? d.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches; d.dataset.theme = dark ? "light" : "dark"; };
$("banner-close").onclick = () => ($("banner").hidden = true);
$("banner-retry").onclick = () => { $("banner").hidden = true; loadRuns(); refresh(); };
$("navbtn").onclick = (e) => { e.stopPropagation(); $("app").classList.toggle("nav"); };
document.querySelector("main").addEventListener("click", () => $("app").classList.remove("nav"));
{ const t = new URLSearchParams(location.search).get("theme"); if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; }
loadRuns();
setInterval(() => { if (!document.hidden) { refresh(); loadRuns(); } }, 20000);
