/* KORA dashboard: a thin client over the KORA API. Every permission is enforced by the
   server; the UI only hides buttons a persona could not use anyway. No external requests. */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const main = $("#main");

const S = { personas: [], cur: null, me: null, sel: new Set(), filters: { q: "", type: "", show: "all" }, open: new Set(), blobs: new Map() };

/* ------------------------------------------------------------ formatting */
function fmtB(n) {
  n = Number(n || 0);
  if (n < 1000) return `${n}\u00a0B`;
  const u = ["kB", "MB", "GB", "TB"]; let i = -1;
  do { n /= 1000; i++; } while (n >= 1000 && i < u.length - 1);
  return `${n >= 100 ? n.toFixed(0) : n >= 10 ? n.toFixed(1) : n.toFixed(2)}\u00a0${u[i]}`;
}
const pct = (x) => `${(100 * (x || 0)).toFixed(x && x < 0.1 ? 1 : 0)}\u00a0%`;
const pct1 = (x) => `${(100 * (x || 0)).toFixed(x >= 0.95 && x < 1 ? 1 : 0)}\u00a0%`;   // 98.8 %, never a rounded-up 100 %
/* long file names break after _ - . instead of mid-word */
const brk = (v) => esc(v).replace(/([_\-.])/g, "$1<wbr>");
const when = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "");
const mmss = (t) => { t = Math.round(t || 0); return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`; };

const CLASS = {
  EXACT_COPY: "Exact copy", CONTAINER_VARIANT: "Same content, re-saved", SUPERSEDED_REVISION: "Superseded revision",
  CONTAINED_SUBSET: "Excerpt of a larger deck", MEDIA_VARIANT: "Same recording, other encoding",
  RECORDING_DISTILLED: "Recording to capsule", COMPRESSED: "Compressed in place", MANUAL_DELETE: "Chosen by a person",
  PARTIAL_OVERLAP: "Decks sharing slides", NEAR_DUPLICATE: "Near-duplicate", KNOWLEDGE_OVERLAP: "Same topic",
  REVISION_OF: "Revision of", PRESENTED_IN: "Shown in a recording",
};
const ACTION = {
  QUARANTINE_AND_STUB: "Move to quarantine, leave a pointer", COMPACT_TO_SLIDE_STORE: "Keep unique slides, remove file",
  MOVE_TO_ARCHIVE: "Move original to archive", REPLACE_WITH_RENDITION: "Replace with smaller version", DELETE: "Delete",
};
const KIND = {
  duplicates: "Duplicate copies", revisions: "Superseded revisions", subsets: "Excerpts", media: "Recording variants",
  recordings: "Recordings to capsules", compress: "Compression", manual: "Chosen by a person", departure: "Leaver's files",
};
const STEPS = [["draft", "Draft"], ["pending_approval", "Waiting for approval"], ["approved", "Approved"],
  ["executing", "Executing"], ["quarantined", "In quarantine"], ["purged", "Purged"]];
const STATUS_TEXT = { draft: "Draft", pending_approval: "Waiting for approval", approved: "Approved", executing: "Executing",
  quarantined: "In quarantine", purged: "Purged", cancelled: "Cancelled", rejected: "Rejected", completed: "Completed" };

/* ------------------------------------------------------------ API */
async function api(method, path, body) {
  const r = await fetch(path, {
    method, headers: { Authorization: `Bearer ${S.cur.key}`, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) {
    const msg = (data && (data.detail || data.error)) || r.statusText;
    const e = new Error(typeof msg === "string" ? msg : JSON.stringify(msg)); e.status = r.status; throw e;
  }
  return data;
}
async function blobUrl(key) {
  if (!key) return null;
  if (S.blobs.has(key)) return S.blobs.get(key);
  const p = fetch(`/v1/blobs/${key}`, { headers: { Authorization: `Bearer ${S.cur.key}` } })
    .then((r) => (r.ok ? r.blob() : null)).then((b) => (b ? URL.createObjectURL(b) : null));
  S.blobs.set(key, p);
  return p;
}
async function hydrateImages(root) {
  for (const img of $$("img[data-blob]", root)) {
    const u = await blobUrl(img.dataset.blob);
    if (u) img.src = u; else img.replaceWith(Object.assign(document.createElement("div"), { className: "noimg" }));
  }
}
function toast(msg, err = false) {
  const t = Object.assign(document.createElement("div"), { className: `toast${err ? " err" : ""}`, textContent: msg });
  $("#toasts").append(t);
  setTimeout(() => t.remove(), err ? 7000 : 3500);
}
const can = (...roles) => !!S.me && roles.some((r) => S.me.roles.includes(r));

/* ------------------------------------------------------------ personas */
function loadPersonas() {
  const m = location.hash.match(/^#p=([A-Za-z0-9_-]+)/);
  if (m) {
    try {
      const b64 = m[1].replace(/-/g, "+").replace(/_/g, "/");
      const json = decodeURIComponent(escape(atob(b64 + "===".slice((b64.length + 3) % 4))));
      sessionStorage.setItem("kora.personas", json);
    } catch { /* ignore a broken link */ }
    history.replaceState(null, "", "#/overview");
  }
  try { S.personas = JSON.parse(sessionStorage.getItem("kora.personas") || "[]"); } catch { S.personas = []; }
  const want = sessionStorage.getItem("kora.persona");
  S.cur = S.personas.find((p) => p.id === want) || S.personas[0] || null;
  const sel = $("#persona");
  sel.innerHTML = S.personas.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
  if (S.cur) sel.value = S.cur.id;
}
async function selectPersona(id) {
  S.cur = S.personas.find((p) => p.id === id);
  sessionStorage.setItem("kora.persona", id);
  S.me = await api("GET", "/v1/me");
  $("#persona-roles").textContent = `Roles: ${S.me.roles.join(", ")}`;
  if (!S.badge) { S.badge = true; liveBadge(); }
  S.sel.clear();
  refreshBadges();
  route();
}
$("#persona").addEventListener("change", (e) => selectPersona(e.target.value));

function noIdentity() {
  main.innerHTML = `<h1>Choose who you are</h1>
    <p class="lede">The dashboard acts with an API key, so every action is permission-checked and audited.
    Start it with <code>kora dashboard</code> to get demo identities, or paste a key that an administrator created
    with <code>kora key &lt;person&gt; --roles analyst</code>.</p>
    <form id="keyform" class="row"><input type="text" id="keyin" placeholder="kora_…" size="48" autocomplete="off" aria-label="API key">
    <button class="btn primary">Use this key</button></form>`;
  $("#keyform").addEventListener("submit", async (e) => {
    e.preventDefault();
    const key = $("#keyin").value.trim();
    S.cur = { id: "key", name: "My key", key };
    try {
      const me = await api("GET", "/v1/me");
      S.personas = [{ id: me.id, name: me.name, roles: me.roles, key }];
      sessionStorage.setItem("kora.personas", JSON.stringify(S.personas));
      loadPersonas(); selectPersona(me.id);
    } catch (err) { toast(`That key was not accepted: ${err.message}`, true); }
  });
}

/* ------------------------------------------------------------ router */
const VIEWS = { overview, pipeline, recordings, recording: recordingDetail, files, plans, plan: planDetail, compression, versions, reviews, knowledge, audit: auditView };
async function route() {
  if (!S.cur) return noIdentity();
  const [, view = "overview", arg] = (location.hash.match(/^#\/([a-z]+)(?:\/([A-Za-z0-9]+))?/) || []);
  let name = view === "plans" && arg ? "plan" : view === "recordings" && arg ? "recording" : view;
  if (name === "overview" && !can("analyst", "approver", "operator", "auditor", "dpo")) { location.hash = "#/files"; return; }
  $$("#nav a").forEach((a) => (a.dataset.view === view ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  clearTimeout(S.poll);
  main.innerHTML = `<p class="loading">Loading…</p>`;
  try {
    await (VIEWS[name] || overview)(arg);
  } catch (e) {
    if (e.status === 403) {
      main.innerHTML = `<h1>Not available for this role</h1><p class="lede">${esc(S.cur.name)} cannot open this page: ${esc(e.message)}.
        Switch identity in the sidebar to see how the same data looks to an analyst, approver or operator.</p>`;
    } else {
      main.innerHTML = `<h1>Something went wrong</h1><p class="lede">${esc(e.message)}</p>`;
    }
  }
  main.focus({ preventScroll: true });
}
window.addEventListener("hashchange", route);
async function refreshBadges() {
  try {
    const r = await api("GET", "/v1/reviews");
    $("#nav-reviews").textContent = r.reviews.length || "";
  } catch { $("#nav-reviews").textContent = ""; }
}

/* ------------------------------------------------------------ CSP-safe inline styles
   The page's CSP forbids style attributes; dynamic geometry is applied through the CSSOM. */
function applyStyles(root) {
  if (root.nodeType !== 1) return;
  const els = root.matches("[data-style]") ? [root, ...$$("[data-style]", root)] : $$("[data-style]", root);
  for (const el of els) { el.style.cssText = el.dataset.style; el.removeAttribute("data-style"); }
}
new MutationObserver((ms) => ms.forEach((m) => m.addedNodes.forEach(applyStyles))).observe(document.body, { childList: true, subtree: true });

/* ------------------------------------------------------------ tooltip */
document.addEventListener("mouseover", (e) => {
  const t = e.target.closest("[data-tip]"); const tip = $("#tip");
  if (!t) { tip.hidden = true; return; }
  tip.textContent = t.dataset.tip; tip.hidden = false;
  const r = t.getBoundingClientRect();
  tip.style.left = `${Math.min(window.innerWidth - 290, Math.max(8, r.left + r.width / 2 - 100))}px`;
  tip.style.top = `${r.top - tip.offsetHeight - 8 < 0 ? r.bottom + 8 : r.top - tip.offsetHeight - 8}px`;
});

/* ============================================================ OVERVIEW */
async function overview() {
  const o = await api("GET", "/v1/overview");
  const sm = o.savings;
  const freed = Object.values(o.realized_by_class).reduce((a, b) => a + b, 0);
  const segs = [
    { k: "freed", label: "Freed already", v: freed, c: "var(--ink-2)", note: "executed plans (quarantined or purged)" },
    { k: "removable", label: "Removable copies and drafts", v: sm.bytes_removable, c: "var(--s-removable)", note: "another copy, a newer revision or a larger deck keeps the content" },
    { k: "compressed", label: "Saved by compression", v: sm.bytes_saved_by_compression, c: "var(--s-compressed)", note: "smaller version passed the measured quality budget" },
    { k: "archive", label: "Recordings to archive", v: sm.bytes_movable_to_archive, c: "var(--s-archive)", note: "validated capsule replaces the hot copy" },
  ];
  const total = sm.active_bytes + freed;
  const change = segs.reduce((a, s) => a + s.v, 0);
  segs.push({ k: "kept", label: "Remains afterwards", v: Math.max(0, total - change), c: "var(--s-kept)", note: "the smaller versions that replace originals, plus everything unique, current or not proven safe" });
  const waiting = [
    ["Plans waiting for approval", o.plans.pending_approval || 0, "#/plans"],
    ["Approved plans to execute", o.plans.approved || 0, "#/plans"],
    ["Plans in quarantine (restorable)", o.plans.quarantined || 0, "#/plans"],
    ...Object.entries(o.open_reviews).map(([k, n]) => [`Review: ${k.replace(/_/g, " ")}`, n, "#/reviews"]),
  ];
  const relOrder = ["EXACT_COPY", "CONTAINER_VARIANT", "REVISION_OF", "CONTAINED_SUBSET", "MEDIA_VARIANT", "PARTIAL_OVERLAP", "NEAR_DUPLICATE", "KNOWLEDGE_OVERLAP", "PRESENTED_IN"];
  const rels = relOrder.filter((k) => o.relations[k]).map((k) => [CLASS[k] || k, o.relations[k], k]);
  const maxRel = Math.max(1, ...rels.map((r) => r[1]));
  const kinds = Object.entries(o.potential);

  main.innerHTML = `
  <section class="hero">
    <h1>${fmtB(total)} across ${o.inventory.active_files} files. ${freed
      ? `${fmtB(freed)} freed so far${change - freed > 0 ? `, ${fmtB(change - freed)} more can go` : ""}, each with proof.`
      : `${fmtB(change)} can go, each with proof.`}</h1>
    <p>Sizes are the files as they are now, before KORA changes anything. Every byte in colour has evidence: a surviving copy,
    a revision that rebuilds page for page, or a smaller version whose quality was measured. Nothing changes until someone
    other than the planner approves (two people for anything riskier than an exact copy), and it stays restorable for the grace period.
    ${sm.kora_overhead_bytes ? `KORA keeps ${fmtB(sm.kora_overhead_bytes)} of knowledge capsules for these files, so the net saving is
    ${fmtB(Math.max(0, change - sm.kora_overhead_bytes))}.` : ""}</p>
    <div class="sbar" role="img" aria-label="${esc(segs.map((s) => `${s.label} ${fmtB(s.v)}`).join(", "))}">
      <div class="sbar-track">${segs.filter((s) => s.v > 0).map((s) => `<div class="sbar-seg" data-style="flex:${s.v};background:${s.c}"
        data-tip="${esc(s.label)}\n${fmtB(s.v)} (${pct(s.v / total)})\n${esc(s.note)}"></div>`).join("")}</div>
      <div class="sbar-legend">${segs.map((s) => `<div data-style="--c:${s.c}">
        <b>${fmtB(s.v)}</b><span>${esc(s.label)}, ${pct(s.v / total)}</span></div>`).join("")}</div>
    </div>
  </section>
  <div class="grid2">
    <section class="panel"><h2>Waiting for a person</h2>
      <ul class="todo">${waiting.map(([l, n, href]) => `<li><a href="${href}">${esc(l)}</a><b>${n}</b></li>`).join("")}</ul>
      <p class="small muted" data-style="margin-top:12px">Audit chain ${o.audit.ok ? "verified" : "BROKEN"} over ${o.audit.events} events.
        ${o.lineages} document families tracked.</p>
    </section>
    <section class="panel"><h2>What KORA found</h2>
      ${rels.length ? `<div class="hbar">${rels.map(([l, n, k]) => `<div class="lab">${esc(l)}</div><div class="trk">
        <div class="bar" data-style="width:${(85 * n) / maxRel}%" data-tip="${esc(l)}\n${n} relation${n === 1 ? "" : "s"}"></div>
        <span class="val" data-style="left:${(85 * n) / maxRel}%">${n}</span></div>`).join("")}</div>` : `<p class="empty">No relations yet. Ingest files first.</p>`}
      <p class="small muted" data-style="margin-top:12px">Near-duplicates, shared slides and same-topic links are never deleted automatically.</p>
    </section>
  </div>
  <section class="panel"><div class="row"><h2 data-style="margin:0">Plans KORA can prove now</h2><span class="spacer"></span>
    <span class="small muted">Computed ${when(new Date(o.computed_at * 1000).toISOString())}</span>
    <button class="btn sm" id="refresh">Recompute</button></div>
    <div class="tablewrap" data-style="margin-top:12px"><table><thead><tr><th>Plan type</th><th class="r">Ready</th><th class="r">Frees</th>
      <th class="r">Blocked</th><th>Why blocked</th><th></th></tr></thead><tbody>
      ${kinds.map(([k, p]) => `<tr><td>${esc(KIND[k] || k)}</td><td class="r num">${p.deletable_items}</td>
        <td class="r num">${fmtB(p.deletable_bytes)}</td><td class="r num">${p.blocked_items}</td>
        <td class="small">${Object.entries(p.blocked_by || {}).map(([n, c]) => `<span class="chip">${esc(n)} ${c}</span>`).join(" ") || '<span class="muted">nothing</span>'}</td>
        <td class="r">${can("analyst") ? `<button class="btn sm" data-create="${k}" ${p.deletable_items ? "" : "disabled"}>Create plan</button>` : ""}</td></tr>`).join("")}
    </tbody></table></div>
    <p class="small muted" data-style="margin-top:10px">Rows can overlap (a recording can be compressed or archived). The bar above counts every file once: removal first, then archive, then compression.</p></section>`;
  $("#refresh").onclick = async () => { await api("GET", "/v1/overview?refresh=true"); overview(); };
  scenarioPanel();
  $$("[data-create]").forEach((b) => (b.onclick = () => createPlan(b.dataset.create)));
}
/* scenario calculator: measured ratios x a storage mix the audience chooses */
const PRESETS = {
  site: { label: "Team site, 1 TB", tb: 1, rec: 60, dup: 12, rev: 10, mode: "compress",
    note: "typical project site: recordings dominate, download copies and revision chains in the decks" },
  archive: { label: "Old meetings, 10 TB", tb: 10, rec: 90, dup: 3, rev: 2, mode: "capsule",
    note: "recordings older than a year: keep the knowledge capsule, move the video to the archive tier" },
  leaver: { label: "Leaver's OneDrive, 50 GB", tb: 0.05, rec: 30, dup: 35, rev: 15, mode: "compress",
    note: "copies of team files are removed; unique files are handed over, recordings compressed" },
};
async function scenarioPanel() {
  let ratios;
  try { ({ ratios } = await api("GET", "/v1/scenarios")); } catch { return; }
  const box = document.createElement("section");
  box.className = "panel"; box.dataset.style = "margin-top:18px";
  const st = { ...PRESETS.site };
  box.innerHTML = `<div class="row"><h2 data-style="margin:0">Scenarios</h2><span class="spacer"></span>
      ${Object.entries(PRESETS).map(([k, p]) => `<button class="btn sm" data-preset="${k}">${esc(p.label)}</button>`).join("")}</div>
    <p class="small muted">What would this save on a bigger store? The savings ratios are measured: video compression ${pct1(ratios.video_compression.ratio)}
      (${ratios.video_compression.files || "Ericsson's"} videos), PDF ${pct1(ratios.pdf_compression.ratio)} (${ratios.pdf_compression.files || "Ericsson's"} PDFs),
      recording to knowledge capsule ${pct1(ratios.capsule_with_voice.ratio)} (speech kept; the video moves to the archive tier),
      superseded revision ${pct1(ratios.superseded_revision.ratio)} (the SD-WAN deck's Rev B, if only the current revision were kept).
      The storage mix is an assumption: set it to the real mix of the site you have in mind.</p>
    <div class="scen"><div class="sliders" id="sl"></div><div id="sout"></div></div>`;
  main.append(box);
  const draw = () => {
    const other = Math.max(0, 100 - st.rec - st.dup - st.rev);
    const recR = st.mode === "capsule" ? ratios.capsule_with_voice.ratio : ratios.video_compression.ratio;
    const parts = [
      ["Recordings", st.rec, recR, "var(--s-compressed)", st.mode === "capsule" ? "knowledge capsule, video to archive" : "compressed in place"],
      ["Duplicate copies", st.dup, 1, "var(--s-removable)", "removed, a verified copy stays"],
      ["Superseded revisions", st.rev, ratios.superseded_revision.ratio, "var(--s-archive)", "unique slides kept"],
      ["Other documents", other, 0.5 * ratios.pdf_compression.ratio, "var(--s-kept)", "half assumed to be image-heavy PDFs"],
    ];
    const saved = parts.reduce((a, [, sh, r]) => a + (sh / 100) * r, 0);
    const tbStr = (x) => (x >= 1 ? `${x.toFixed(x >= 10 ? 0 : 1)}\u00a0TB` : `${Math.round(x * 1000)}\u00a0GB`);
    $("#sl", box).innerHTML = `
      <label>Storage size <b>${tbStr(st.tb)}</b><input type="range" min="0.05" max="20" step="0.05" value="${st.tb}" data-k="tb"></label>
      <label>Recordings <b>${st.rec}\u00a0%</b><input type="range" min="0" max="100" value="${st.rec}" data-k="rec"></label>
      <label>Duplicate copies <b>${st.dup}\u00a0%</b><input type="range" min="0" max="60" value="${st.dup}" data-k="dup"></label>
      <label>Superseded revisions <b>${st.rev}\u00a0%</b><input type="range" min="0" max="60" value="${st.rev}" data-k="rev"></label>
      <div class="row small"><label class="switch"><input type="radio" name="mode" value="compress" ${st.mode === "compress" ? "checked" : ""}> Recordings stay, compressed</label>
        <label class="switch"><input type="radio" name="mode" value="capsule" ${st.mode === "capsule" ? "checked" : ""}> Recordings become capsules</label></div>
      <p class="small muted">${esc(st.note || "custom mix")}</p>`;
    $("#sout", box).innerHTML = `<div class="scen-big"><b>−${pct(saved)}</b><span>${tbStr(st.tb * saved)} of ${tbStr(st.tb)} freed</span></div>
      <div class="hbar">${parts.map(([l, sh, r, c, how]) => `<div class="lab">${esc(l)} (${Math.round(sh)}\u00a0%)</div><div class="trk">
        <div class="bar" data-style="width:${Math.max(0.5, sh * r * 0.85)}%;background:${c}" data-tip="${esc(l)}: ${Math.round(sh)} % of the store, ${pct(r)} of it freed\n${esc(how)}"></div>
        <span class="val" data-style="left:${Math.max(0.5, sh * r * 0.85)}%">${tbStr((st.tb * sh * r) / 100)}</span></div>`).join("")}</div>`;
    $$("input[type=range]", box).forEach((el) => (el.oninput = () => {
      st[el.dataset.k] = Number(el.value); st.note = "";
      if (st.rec + st.dup + st.rev > 100) st[el.dataset.k] -= st.rec + st.dup + st.rev - 100;
      draw();
    }));
    $$("input[name=mode]", box).forEach((el) => (el.onchange = () => { st.mode = el.value; draw(); }));
  };
  $$("[data-preset]", box).forEach((b) => (b.onclick = () => { Object.assign(st, PRESETS[b.dataset.preset]); draw(); }));
  draw();
}

async function createPlan(kind, scope = {}, title) {
  try {
    const p = await api("POST", "/v1/plans", { kind, scope, title });
    toast(`Draft plan created: ${p.summary.deletable} of ${p.summary.items} items can go`);
    location.hash = `#/plans/${p.id}`;
  } catch (e) { toast(e.message, true); }
}

/* ============================================================ PIPELINE (live) */
const KIND_TEXT = {
  "source.ingest_folder": "Copying the folder in", "source.scan": "Listing files", "file.hash": "Fingerprinting",
  "content.extract": "Reading (pages, slides, frames, OCR, speech)", "content.relate": "Finding duplicates and versions",
  "content.capsule": "Building the knowledge capsule", "content.index": "Indexing for search",
  "content.optimize": "Making and measuring a smaller version", "capsule.rebuild": "Recreating the video",
  "plan.quarantine_item": "Moving a file to quarantine (approved plan)", "plan.restore_item": "Restoring a file from quarantine",
  "plan.purge_item": "Deleting for good after the grace period",
};
const clock = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const secs = (s) => {
  const t = Math.round(s);
  return t < 90 ? `${t} s` : t < 3600 ? `${Math.floor(t / 60)} min ${t % 60} s` : `${Math.floor(t / 3600)} h ${Math.floor((t % 3600) / 60)} min`;
};
async function pipeline() {
  const p = await api("GET", "/v1/pipeline");
  const busy = p.running.length + p.queued;
  const states = p.files_by_state;
  main.innerHTML = `<div class="row"><h1>Pipeline</h1><span class="spacer"></span>
      <span class="status ${busy ? "executing" : "approved"}">${busy ? `Working: ${p.running.length} running, ${p.queued} waiting` : "Idle: everything processed"}</span></div>
    <p class="lede">Every file goes through these stages as background jobs. This page refreshes itself every two seconds,
    so you can add a folder and watch it being read, fingerprinted, related, distilled and indexed.</p>
    <div class="stages">${p.stages.map((st) => {
      const total = st.queued + st.running + st.succeeded + st.dead;
      return `<div class="stage ${st.running ? "on" : ""}" data-tip="${esc(KIND_TEXT[st.kind] || st.label)}">
        <b>${esc(st.label)}</b>
        <div class="stage-bar" aria-hidden="true"><i data-style="width:${total ? (100 * st.succeeded) / total : 0}%"></i></div>
        <span class="small">${st.succeeded} done${st.running ? `, ${st.running} running` : ""}${st.queued ? `, ${st.queued} waiting` : ""}${st.dead ? `, <span class="bad">${st.dead} failed</span>` : ""}</span></div>`;
    }).join("")}</div>
    <div class="grid2" data-style="margin-top:18px">
      <section class="panel"><h2>Running now</h2>
        ${p.running.length ? `<ul class="todo">${p.running.map((j) => `<li><span><b class="jk">${esc(KIND_TEXT[j.kind] || j.kind)}</b><br><span class="small muted">${esc(j.what)}${j.attempt > 1 ? `, attempt ${j.attempt}` : ""}</span>
          ${j.progress ? `<br><span class="small">Now: ${esc(j.progress.step)}${j.progress.total ? ` (${j.progress.unit === "s" ? `${clock(j.progress.done)} of ${clock(j.progress.total)}` : `${j.progress.done} of ${j.progress.total}`})` : ""}</span>
          ${j.progress.total ? `<span class="stage-bar jp" aria-hidden="true"><i data-style="width:${(100 * j.progress.done) / j.progress.total}%"></i></span>` : ""}` : ""}</span>
          <span class="num small">${secs(j.seconds)}</span></li>`).join("")}</ul>` : `<p class="muted">Nothing running.</p>`}
        <p class="small muted" data-style="margin-top:10px">Files: ${Object.entries(states).map(([k, n]) => `${n} ${esc(k)}`).join(", ") || "none yet"}.</p></section>
      <section class="panel"><h2>Just finished</h2>
        ${p.recent.length ? `<ul class="todo">${p.recent.slice(0, 12).map((j) => `<li><span>${j.status === "dead" ? '<span class="bad">failed</span> ' : ""}${esc(KIND_TEXT[j.kind] || j.kind)}<br><span class="small muted">${esc(j.what)}</span></span>
          <span class="num small">${secs(j.seconds)}</span></li>`).join("")}</ul>` : `<p class="muted">Nothing yet.</p>`}</section>
    </div>
    ${p.dead.length ? `<section class="panel" data-style="margin-top:18px"><h2>Failed after retries</h2><ul class="todo">${p.dead.map((j) => `<li><span><b>${esc(KIND_TEXT[j.kind] || j.kind)}</b>: ${esc(j.what)}<br><span class="small muted">${esc(j.error)}</span></span>
      ${can("operator") ? `<button class="btn sm" data-retry="${j.id}">Retry</button>` : ""}</li>`).join("")}</ul></section>` : ""}
    <div class="grid2" data-style="margin-top:18px">
      <section class="panel"><h2>Add a folder</h2>
        <p class="small muted">A folder or Drive .zip on this computer. KORA copies it into its own workspace first, so nothing
        it does can touch your originals.</p>
        <form id="ingest" class="formstack">
          <label class="small" for="ip">Folder or .zip path</label>
          <input type="text" id="ip" placeholder="/Users/you/Downloads/drive-download-…" autocomplete="off" value="${esc(S.me?.import_hint || "")}">
          ${S.me?.import_hint ? `<p class="small muted">Running in Docker: your folder is mounted at <code>${esc(S.me.import_hint)}</code> (set <code>KORA_IMPORT_DIR</code> in <code>.env</code>).</p>` : ""}
          <div class="row"><div><label class="small" for="in">Name</label><br><input type="text" id="in" value="ericsson-drive" size="18"></div>
          <div><label class="small" for="io">Owner</label><br><input type="text" id="io" value="${esc((S.me.id || "me").replace(/[^A-Za-z0-9_.-]/g, ""))}" size="14"></div>
          <div><label class="small" for="is">Looks like</label><br><select class="in" id="is"><option value="sharepoint">SharePoint</option><option value="onedrive">OneDrive</option><option value="play">Play</option></select></div></div>
          <div><button class="btn primary" ${can("analyst") ? "" : "disabled"}>Copy in and process</button></div>
        </form></section>
      <section class="panel"><h2>Sources</h2>
        <table><tbody>${p.sources.map((x) => `<tr><td><div class="name">${esc(x.name)}</div><div class="path">${esc(x.system)}: ${brk(x.root || "")}</div></td>
          <td class="r num">${x.files} files</td><td class="r">${can("analyst") ? `<button class="btn sm" data-rescan="${esc(x.name)}">Scan again</button>` : ""}</td></tr>`).join("")
          || `<tr><td class="muted">No sources yet. Add a folder.</td></tr>`}</tbody></table></section>
    </div>`;
  $("#ingest").onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("POST", "/v1/ingest-folder", { path: $("#ip").value.trim(), name: $("#in").value.trim(), owner: $("#io").value.trim(), system: $("#is").value });
      toast("Copying the folder in. Watch the stages fill up.");
    } catch (err) { toast(err.message, true); }
  };
  $$("[data-rescan]").forEach((b) => (b.onclick = async () => {
    try { await api("POST", `/v1/sources/${b.dataset.rescan}/scan`); toast("Scanning again"); } catch (err) { toast(err.message, true); }
  }));
  $$("[data-retry]").forEach((b) => (b.onclick = async () => {
    try { await api("POST", `/v1/jobs/${b.dataset.retry}/retry`); toast("Retrying"); } catch (err) { toast(err.message, true); }
  }));
  // live refresh, but never while someone is typing in the form
  S.poll = setTimeout(function again() {
    if (location.hash !== "#/pipeline") return;
    if (document.activeElement && document.activeElement.closest("#ingest")) { S.poll = setTimeout(again, 2000); return; }
    pipeline().catch(() => {});
  }, 2000);
}
async function liveBadge() {
  try {
    const p = await api("GET", "/v1/pipeline");
    const n = p.running.length + p.queued;
    $("#nav-live").textContent = n ? n : "";
  } catch { $("#nav-live").textContent = ""; }
  setTimeout(liveBadge, 4000);
}

/* ============================================================ RECORDINGS */
const SPK = ["var(--s-removable)", "var(--s-compressed)", "var(--s-archive)", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"];
function spkChip(name, all) {
  if (!name) return "";
  const i = all.indexOf(name);
  return `<span class="chip spk" data-style="--c:${SPK[i % SPK.length]}"><i></i>${esc(name)}</span>`;
}
function transcriptNote(t) {
  if (!t.source) return "no transcript";
  if (t.source.startsWith("sidecar")) return "platform transcript (Teams/Stream) with speaker names";
  const eng = t.engine || t.source.replace("asr:", "");
  return `machine transcript (${eng}), ${t.review === "approve" ? "approved by a person" : t.review === "reject" ? "rejected by a person" : "waiting for review"}`;
}
async function recordings() {
  const { recordings } = await api("GET", "/v1/recordings");
  main.innerHTML = `<h1>Recordings</h1>
    <p class="lede">Each recording becomes text and pictures: what was said (with speaker turns), one picture per slide,
    each linked to the document and revision it shows. That knowledge capsule is what remains searchable
    and AI-ready when the video itself is compressed, archived or deleted.</p>
    <div class="stack">${recordings.map((r) => {
      const cap = r.capsule;
      const red = cap ? 1 - cap.bytes / r.size : 0;
      const redNoAudio = cap ? 1 - (cap.bytes - cap.audio_bytes) / r.size : 0;
      return `<a class="panel rec" href="#/recordings/${r.content}">
        <div><h2 data-style="margin:0 0 4px">${brk(r.name)}</h2>
          <p class="small muted" data-style="margin:0">${mmss(r.duration_s)} long, ${r.width || "?"}×${r.height || "?"}. ${esc(transcriptNote(r.transcript))}.</p>
          <div class="chips" data-style="margin-top:8px">${r.transcript.speakers.slice(0, 6).map((sp) => spkChip(sp, r.transcript.speakers)).join("")}
            ${Object.keys(r.transcript.languages || {}).map((l) => `<span class="chip">${esc(l)}</span>`).join("")}
            ${r.documents.map((d) => `<span class="chip blue"><i></i>shows ${esc(d.name)}</span>`).join("")}</div></div>
        <div class="recnums"><div><b>${fmtB(r.size)}</b><span>video</span></div>
          <div><b>${cap ? fmtB(cap.bytes - cap.audio_bytes) : "–"}</b><span>text + ${r.frames} slide pictures${cap ? `, −${pct(redNoAudio)}` : ""}</span></div>
          <div><b>${cap ? fmtB(cap.bytes) : "–"}</b><span>with the voice${cap ? `, −${pct(red)}` : ""}</span></div></div></a>`;
    }).join("") || `<div class="panel empty">No recordings yet.</div>`}</div>`;
}
async function recordingDetail(sha) {
  const r = await api("GET", `/v1/recordings/${sha}`);
  const m = r.summary; const spk = m.transcript.speakers;
  S.recSel = S.recSel && r.frames.some((f) => f.n === S.recSel) ? S.recSel : (r.frames[Math.min(1, r.frames.length - 1)] || {}).n;
  const draw = () => {
    const fr = r.frames.find((f) => f.n === S.recSel) || r.frames[0];
    const inFrame = (sg) => fr && sg.t >= (fr.t || 0) - 0.5 && sg.t < (fr.end || 1e9);
    main.innerHTML = `<p class="small"><a href="#/recordings">Recordings</a></p>
      <h1>${brk(m.name)}</h1>
      <p class="lede">${mmss(m.duration_s)}, ${r.segments.length} transcript passages, ${r.frames.length} slide pictures. ${esc(transcriptNote(m.transcript))}.
        ${spk.length ? `Speaker turns are anonymous labels; a person can name them, KORA never guesses names from voices.` : ""}</p>
      <div class="strip" role="list">${r.frames.map((f) => `<button class="thumb ${f.n === S.recSel ? "on" : ""}" data-fr="${f.n}" role="listitem"
        aria-label="Slide picture at ${mmss(f.t)}"><img data-blob="${f.blob}" alt=""><span>${mmss(f.t)}</span></button>`).join("")}</div>
      <div class="recgrid">
        <section class="panel">${fr ? `<img class="bigframe" data-blob="${fr.blob}" alt="Slide picture at ${mmss(fr.t)}">
          <p class="small" data-style="margin:8px 0 0">${mmss(fr.t)} to ${mmss(fr.end)}${fr.slide ? `. Shows <b>${esc(fr.slide.doc_number || fr.slide.name)}</b>${fr.slide.revision ? ` rev ${esc(fr.slide.revision)}` : ""}, slide ${fr.slide.page}${fr.slide.current ? " (current revision)" : ""}` : ". Not linked to a document in the corpus"}.</p>
          <div class="actions" data-style="margin-top:10px"><button class="btn" id="describe">Describe this slide as text</button></div>
          <div id="spec"></div>` : `<p class="empty">No slide pictures.</p>`}</section>
        <section class="panel"><h2>What was said here</h2>
          <div class="segs">${r.segments.filter(inFrame).map((sg) => `<div class="seg"><span class="t">${mmss(sg.t)}</span>${spkChip(sg.speaker, spk)}<p>${esc(sg.text)}</p></div>`).join("")
            || `<p class="muted small">No speech while this slide was on screen.</p>`}</div>
          <details data-style="margin-top:12px"><summary class="small">Whole transcript (${r.segments.length} passages)</summary>
            <div class="segs">${r.segments.map((sg) => `<div class="seg"><button class="t linkish" data-goto="${sg.frame ?? ""}" data-t="${sg.t}">${mmss(sg.t)}</button>${spkChip(sg.speaker, spk)}<p>${esc(sg.text)}</p></div>`).join("")}</div></details>
        </section></div>
      <section class="panel" data-style="margin-top:18px"><div class="row"><h2 data-style="margin:0">Recreate the recording from the capsule</h2><span class="spacer"></span>
        <button class="btn primary" id="rebuild">${m.rebuilt ? "Recreate again" : "Recreate as video"}</button></div>
        <p class="small muted">Slide pictures, the speech track and the transcript as subtitles: nothing from the original video file is used.</p>
        <div id="rebuilt">${m.rebuilt ? `<p class="small">Recreated: ${fmtB(m.rebuilt.bytes)} instead of ${fmtB(m.size)}. Loading…</p>` : ""}</div></section>`;
    $$("[data-fr]").forEach((b) => (b.onclick = () => { S.recSel = Number(b.dataset.fr); draw(); }));
    $$("[data-goto]").forEach((b) => (b.onclick = () => {
      const t = Number(b.dataset.t); const f = r.frames.filter((x) => (x.t || 0) <= t + 0.5).pop();
      if (f) { S.recSel = f.n; draw(); }
    }));
    const d = $("#describe");
    if (d) d.onclick = async () => {
      d.disabled = true; d.textContent = "Describing…";
      try {
        const sp = await api("GET", `/v1/units/${fr.id}/spec`);
        const ms = sp.measured;
        $("#spec").innerHTML = `<div class="specgrid"><div><p class="small muted">Recreated from the text alone</p><img class="bigframe" data-blob="${sp.image}" alt="Slide recreated from its text description"></div>
          <div><p class="small muted">The description (${fmtB(ms.prompt_bytes)} of text instead of a ${fmtB(ms.frame_bytes)} picture)</p>
          <pre class="block small-pre">${esc(sp.prompt)}</pre>
          <p class="small">OCR finds ${ms.text_recall != null ? pct(ms.text_recall) : "–"} of the slide's ${ms.words} words in the recreation.</p></div></div>`;
        hydrateImages($("#spec"));
      } catch (e) { toast(e.message, true); }
      d.disabled = false; d.textContent = "Describe this slide as text";
    };
    $("#rebuild").onclick = async () => {
      try { await api("POST", `/v1/recordings/${sha}/rebuild`); toast("Recreating the video from the capsule…"); pollRebuild(sha, m.rebuilt?.at); }
      catch (e) { toast(e.message, true); }
    };
    if (m.rebuilt) showRebuilt(sha, m.rebuilt);
    hydrateImages(main);
  };
  draw();
}
async function showRebuilt(sha, rb) {
  const box = $("#rebuilt"); if (!box) return;
  const [vurl, vtt] = await Promise.all([blobUrl(rb.key), fetch(`/v1/recordings/${sha}/transcript.vtt`, { headers: { Authorization: `Bearer ${S.cur.key}` } }).then((x) => x.text())]);
  const turl = URL.createObjectURL(new Blob([vtt], { type: "text/vtt" }));
  box.innerHTML = `<p class="small">Recreated: <b>${fmtB(rb.bytes)}</b>, ${rb.keyframes} slide pictures${rb.audio ? ", speech" : ""}${rb.subtitles ? ", subtitles" : ""}.</p>
    <video class="bigframe" controls preload="metadata" src="${vurl}"><track kind="subtitles" srclang="en" label="Transcript" src="${turl}" default></video>`;
}
function pollRebuild(sha, before) {
  clearTimeout(S.poll);
  S.poll = setTimeout(async () => {
    const r = await api("GET", `/v1/recordings/${sha}`);
    if (r.summary.rebuilt && r.summary.rebuilt.at !== before) { toast("Video recreated"); showRebuilt(sha, r.summary.rebuilt); }
    else pollRebuild(sha, before);
  }, 2000);
}

/* ============================================================ FILES */
function fileChips(f) {
  const c = [];
  if (f.legal_hold) c.push(`<span class="chip hold">legal hold</span>`);
  if (f.copies > 1) c.push(`<span class="chip blue" data-tip="Identical bytes exist in ${f.copies - 1} other place(s)"><i></i>${f.copies} copies</span>`);
  if (f.version) {
    const v = f.version;
    const rev = v.revision ? `Rev ${esc(v.revision)}` : "Version";
    c.push(v.is_head ? `<span class="chip head" data-tip="Current revision of ${esc(v.doc_number || "its document family")}\nOrder from ${esc(v.confidence)}-confidence evidence">${rev}, current</span>`
      : `<span class="chip old" data-tip="A newer revision of ${esc(v.doc_number || "this document")} exists">${rev}</span><span class="chip">superseded</span>`);
  }
  const seen = new Set();
  for (const r of f.relations) {
    let t = null;
    if (r.kind === "NEAR_DUPLICATE") t = "near-duplicate";
    else if (r.kind === "CONTAINED_SUBSET") t = r.role === "a" ? "excerpt" : "contains an excerpt";
    else if (r.kind === "MEDIA_VARIANT") t = "other encoding exists";
    else if (r.kind === "CONTAINER_VARIANT") t = "re-saved copy";
    else if (r.kind === "PARTIAL_OVERLAP") t = "shares slides";
    if (t && !seen.has(t)) { seen.add(t); c.push(`<span class="chip" data-tip="${esc(CLASS[r.kind])} of ${esc(r.other)}">${t}</span>`); }
  }
  if (f.compressible) c.push(`<span class="chip orange" data-tip="${esc(f.compressible.op)}: ${fmtB(f.size)} to ${fmtB(f.compressible.to)}, quality budget passed"><i></i>compress −${pct(f.compressible.saving)}</span>`);
  if (f.rendition_of) c.push(`<span class="chip orange"><i></i>compressed</span>`);
  if (f.freshness) {
    const fr = f.freshness; const lbl = fr.label || fr.best_before;
    c.push(fr.past ? `<span class="chip stale" data-tip="Speaker notes: Best Before ${esc(lbl)} on ${fr.slides_past} of ${fr.slides} slides${fr.review ? `\nReviewed: ${esc(OPTION_TEXT[fr.review] || fr.review)}` : ""}">${fr.review === "retire" ? "retired: past best-before" : "past best-before"}</span>`
      : `<span class="chip" data-tip="Speaker notes: Best Before ${esc(lbl)}">valid until ${esc(lbl)}</span>`);
  }
  if (f.plan) c.push(`<a class="chip plan" href="#/plans/${esc(f.plan.plan)}" data-tip="${esc(KIND[f.plan.kind] || f.plan.kind)} plan, ${esc(STATUS_TEXT[f.plan.status] || f.plan.status)}">in a plan</a>`);
  return c.join("");
}
async function files() {
  const { files } = await api("GET", "/v1/inventory");
  S.files = files;
  const types = [...new Set(files.map((f) => f.type).filter(Boolean))].sort();
  main.innerHTML = `<h1>Files</h1>
    <p class="lede">Everything KORA has read, with what it found about each file. Select files to delete them yourself:
    KORA shows the impact first, keeps their knowledge searchable, and two approvers decide.</p>
    <div class="toolbar">
      <input type="search" id="fq" placeholder="Filter by name or path" value="${esc(S.filters.q)}" aria-label="Filter files">
      <select class="in" id="ft" aria-label="Type"><option value="">All types</option>${types.map((t) => `<option ${S.filters.type === t ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>
      <select class="in" id="fs" aria-label="Show">
        ${[["all", "All files"], ["findings", "With findings"], ["compressible", "Compressible"], ["stale", "Past best-before"], ["unique", "Unique (no copy)"], ["gone", "Removed or in quarantine"]]
          .map(([v, l]) => `<option value="${v}" ${S.filters.show === v ? "selected" : ""}>${l}</option>`).join("")}
      </select><span class="spacer"></span><span class="small muted" id="fcount"></span>
    </div>
    <div class="tablewrap"><table><thead><tr><th data-style="width:34px"><input type="checkbox" id="fall" aria-label="Select all shown"></th>
      <th>File</th><th>Where</th><th class="r">Size</th><th>What KORA found</th><th>Owner</th></tr></thead><tbody id="fbody"></tbody></table></div>
    <div id="selbar"></div>`;
  const draw = () => {
    const q = S.filters.q.toLowerCase();
    const shown = files.filter((f) => (!q || `${f.name} ${f.path}`.toLowerCase().includes(q)) && (!S.filters.type || f.type === S.filters.type) && ({
      all: true, findings: f.copies > 1 || f.relations.length || (f.version && !f.version.is_head) || f.compressible || (f.freshness && f.freshness.past),
      compressible: !!f.compressible, stale: !!(f.freshness && f.freshness.past), unique: f.copies <= 1 && !f.relations.some((r) => ["CONTAINED_SUBSET", "CONTAINER_VARIANT", "MEDIA_VARIANT"].includes(r.kind)),
      gone: f.status !== "active" }[S.filters.show]));
    $("#fcount").textContent = `${shown.length} of ${files.length} files`;
    $("#fbody").innerHTML = shown.map((f) => {
      const live = f.status === "active";
      return `<tr class="${S.sel.has(f.id) ? "sel" : ""} ${live ? "" : "gone"}">
        <td><input type="checkbox" data-id="${f.id}" ${S.sel.has(f.id) ? "checked" : ""} ${live ? "" : "disabled"} aria-label="Select ${esc(f.name)}"></td>
        <td class="fcol"><div class="name">${brk(f.name)}</div><div class="path">${brk(f.path)}${live ? "" : ` (${esc(f.status)})`}</div></td>
        <td class="small">${esc(f.system)}<div class="path">${esc(f.site || "")}</div></td>
        <td class="r num nowrap">${fmtB(f.size)}${f.duration_s ? `<div class="path">${mmss(f.duration_s)}</div>` : ""}</td>
        <td><div class="chips">${fileChips(f) || '<span class="muted small">unique</span>'}</div></td>
        <td class="small">${esc(f.owner || "")}</td></tr>`;
    }).join("") || `<tr><td colspan="6" class="empty">No files match. Clear the filter to see everything.</td></tr>`;
    $$("#fbody input[type=checkbox]").forEach((cb) => (cb.onchange = () => { cb.checked ? S.sel.add(cb.dataset.id) : S.sel.delete(cb.dataset.id); draw(); }));
    $("#fall").checked = shown.length > 0 && shown.filter((f) => f.status === "active").every((f) => S.sel.has(f.id));
    $("#fall").onchange = (e) => { shown.filter((f) => f.status === "active").forEach((f) => (e.target.checked ? S.sel.add(f.id) : S.sel.delete(f.id))); draw(); };
    const chosen = files.filter((f) => S.sel.has(f.id));
    $("#selbar").innerHTML = chosen.length ? `<div class="selbar"><span><b>${chosen.length}</b> file${chosen.length === 1 ? "" : "s"} selected, ${fmtB(chosen.reduce((a, f) => a + f.size, 0))}</span>
      <span class="spacer"></span><button class="btn sm" id="sclear">Clear</button><button class="btn" id="sdel">Review deletion</button></div>` : "";
    if (chosen.length) {
      $("#sclear").onclick = () => { S.sel.clear(); draw(); };
      $("#sdel").onclick = async () => {
        await createPlan("manual", { file_ids: chosen.map((f) => f.id) }, `${S.me.name.split(" (")[0]} deletes ${chosen.length} file${chosen.length === 1 ? "" : "s"}`);
        S.sel.clear();
      };
    }
  };
  $("#fq").oninput = (e) => { S.filters.q = e.target.value; draw(); };
  $("#ft").onchange = (e) => { S.filters.type = e.target.value; draw(); };
  $("#fs").onchange = (e) => { S.filters.show = e.target.value; draw(); };
  draw();
}

/* ============================================================ PLANS */
async function plans() {
  const { plans } = await api("GET", "/v1/plans");
  main.innerHTML = `<div class="row"><h1>Plans</h1><span class="spacer"></span>
    ${can("analyst") ? `<select class="in" id="pk" aria-label="Plan type">${Object.entries(KIND).filter(([k]) => !["manual", "departure"].includes(k))
      .map(([k, l]) => `<option value="${k}">${esc(l)}</option>`).join("")}</select><button class="btn primary" id="pnew">Create draft plan</button>` : ""}</div>
    <p class="lede">A plan is a proposal with evidence. Drafts change nothing. The person who creates a plan can never approve it,
    and execution re-checks every item against the live source.</p>
    ${plans.length ? `<div class="tablewrap"><table><thead><tr><th>Plan</th><th>Status</th><th class="r">Items ready</th><th class="r">Frees</th>
      <th class="r">Approvals</th><th>Created by</th></tr></thead><tbody>${plans.map((p) => `<tr>
      <td><a href="#/plans/${p.id}" class="name">${esc(p.title)}</a><div class="path">${esc(KIND[p.kind] || p.kind)}</div></td>
      <td><span class="status ${esc(p.status)}">${esc(STATUS_TEXT[p.status] || p.status)}</span></td>
      <td class="r num">${p.summary?.deletable ?? 0} / ${p.summary?.items ?? 0}</td><td class="r num">${fmtB(p.summary?.bytes_reclaimable)}</td>
      <td class="r num">${p.approvals.length} / ${p.required_approvals}</td><td class="small">${esc(p.created_by)}</td></tr>`).join("")}
      </tbody></table></div>` : `<div class="panel empty">No plans yet. Create one above, or select files on the Files page.</div>`}`;
  const b = $("#pnew");
  if (b) b.onclick = () => createPlan($("#pk").value);
  if (can("analyst")) leaverPanel();
}

/* someone leaves: delete only what has a surviving copy, hand over the rest */
async function leaverPanel() {
  const { files } = await api("GET", "/v1/inventory");
  const owners = [...new Set(files.map((f) => f.owner).filter(Boolean))].sort();
  const box = document.createElement("section");
  box.className = "panel"; box.dataset.style = "margin-top:22px";
  box.innerHTML = `<h2>Someone is leaving</h2>
    <p class="small muted">KORA deletes a leaver's file only if an equivalent copy survives in an approved repository with an
    active owner, and everyone keeps access. Everything unique gets a new owner, and open action items are listed.</p>
    <div class="row"><select class="in" id="lv" aria-label="Person">${owners.map((o) => `<option>${esc(o)}</option>`).join("")}</select>
    <button class="btn" id="lgo">Assess</button></div><div id="lres"></div>`;
  main.append(box);
  $("#lgo").onclick = async () => {
    const out = $("#lres"); out.innerHTML = `<p class="loading">Assessing…</p>`;
    try {
      const r = await api("POST", `/v1/departures/${encodeURIComponent($("#lv").value)}/assess`);
      const m = r.summary; const bk = r.buckets;
      const list = (k, label) => (bk[k] || []).length ? `<h3 data-style="margin-top:14px">${label}</h3><ul class="reasons">${bk[k].map((e) =>
        `<li>${brk(e.path)}${e.proposed_owner ? ` <span class="muted">to ${esc(e.proposed_owner)}</span>` : ""}${e.note ? ` <span class="muted">(${esc(e.note)})</span>` : ""}${e.blocked_by ? ` <span class="muted">blocked: ${esc(e.blocked_by[0])}</span>` : ""}</li>`).join("")}</ul>` : "";
      out.innerHTML = `<div class="tiles" data-style="margin-top:14px">
          <div class="tile"><b>${m.files_owned}</b><span>files owned, ${fmtB(m.bytes_owned)}</span></div>
          <div class="tile"><b>${m.deletable_duplicates}</b><span>deletable duplicates, ${fmtB(m.deletable_bytes)}</span></div>
          <div class="tile"><b>${m.unique_documents + m.unique_recordings + m.versioned_documents}</b><span>unique files to hand over${m.proposed_new_owner ? ` to ${esc(m.proposed_new_owner)}` : ""}</span></div>
          <div class="tile"><b>${m.open_actions_and_decisions}</b><span>open actions and decisions</span></div></div>
        ${m.status === "active" ? `<div class="banner">${esc(m.display_name)} is still active, so no deletion plan was created. This is the preview.</div>` : ""}
        ${list("deletable_duplicates", "Can go: an equivalent copy survives")}${list("blocked_by_retention", "Kept: hold or retention")}
        ${list("blocked_other", "Kept: other checks failed")}${list("unique_documents", "Unique documents: hand over")}
        ${list("versioned_documents", "Document revisions: hand over")}${list("unique_recordings", "Unique recordings: hand over")}
        ${list("partially_shared", "Partly shared with other files: hand over")}
        ${(r.slide_ownership || []).length ? `<h3 data-style="margin-top:14px">Slides that name them as responsible (speaker notes)</h3><ul class="reasons">${r.slide_ownership.map((d) =>
          `<li>${brk(d.name)} <span class="muted">slides ${esc(d.slides.join(", "))}${d.proposed_owner ? `, to ${esc(d.proposed_owner)}` : ""}</span></li>`).join("")}</ul>` : ""}
        ${r.dependencies.length ? `<h3 data-style="margin-top:14px">Open actions and decisions mentioning them</h3>${r.dependencies.map((d) =>
          `<blockquote>${esc(d.text)} <span class="muted">(${esc(d.type.replace("_", " "))}, ${d.sources.length} source${d.sources.length === 1 ? "" : "s"})</span></blockquote>`).join("")}` : ""}
        ${r.plan_id ? `<p><a class="btn primary" href="#/plans/${r.plan_id}">Open the deletion plan</a></p>` : ""}`;
    } catch (e) { out.innerHTML = `<p class="empty">${esc(e.message)}</p>`; }
  };
}

function checksList(checks) {
  return `<ul class="checks">${checks.map((c) => {
    const cls = c.passed ? "ok" : c.hard ? "no" : "soft";
    return `<li class="${cls}"><span class="ic" aria-label="${c.passed ? "passed" : "failed"}">${c.passed ? "✓" : c.hard ? "✕" : "!"}</span>
      <span class="nm">${esc(c.name)}${c.hard ? "" : ' <span class="hard-tag">(advisory)</span>'}</span><span>${esc(c.detail)}</span></li>`;
  }).join("")}</ul>`;
}
async function planDetail(id) {
  const p = await api("GET", `/v1/plans/${id}`);
  const ev = p.evidence; const sm = ev.summary;
  const idx = STEPS.findIndex(([k]) => k === p.status);
  const mine = p.created_by === S.me.id;
  const approved = p.approvals.some((a) => a.principal === S.me.id);
  const grace = p.quarantine_until && new Date(p.quarantine_until) > new Date();
  const btn = [];
  if (p.status === "draft") {
    btn.push(`<button class="btn primary" data-act="submit" ${mine && sm.deletable ? "" : "disabled"}>Submit for approval</button>`);
    btn.push(`<button class="btn" data-act="cancel" ${mine ? "" : "disabled"}>Discard draft</button>`);
  }
  if (p.status === "pending_approval") {
    btn.push(`<button class="btn primary" data-act="approve" ${can("approver") && !mine && !approved ? "" : "disabled"}>Approve</button>`);
    btn.push(`<button class="btn" data-act="reject" ${can("approver") && !mine ? "" : "disabled"}>Reject</button>`);
  }
  if (p.status === "approved") btn.push(`<button class="btn danger" data-act="execute" ${can("operator") ? "" : "disabled"}>Execute</button>`);
  if (p.status === "quarantined") btn.push(`<button class="btn" data-act="purge" ${can("operator") && !grace ? "" : "disabled"}>${grace ? `Purge possible after ${when(p.quarantine_until)}` : "Purge"}</button>`);
  const hint = p.status === "draft" ? (mine ? "Nothing has been deleted. Read what KORA found for each file, then submit it for approval."
      : "Only the person who created this plan can submit it.")
    : p.status === "pending_approval" ? (mine ? "You created this plan, so someone else must approve it. Switch identity to an approver."
      : can("approver") ? (approved ? "You approved this plan. It needs another approver." : "Approving signs the evidence below; if anything changes, your approval no longer counts.")
      : "Waiting for approvers.")
    : p.status === "approved" ? (can("operator") ? "Execution re-checks every item against the live source, copies each original to quarantine, verifies it, and only then removes or replaces it." : "Approved. An operator executes it.")
    : p.status === "executing" ? "Executing item by item. This page updates by itself."
    : p.status === "quarantined" ? "Done. Originals are in quarantine and every item can be restored until the grace period ends." : "";
  main.innerHTML = `<p class="small"><a href="#/plans">Plans</a></p>
    <div class="row"><h1>${esc(p.title)}</h1></div>
    <div class="row"><span class="status ${esc(p.status)}">${esc(STATUS_TEXT[p.status] || p.status)}</span>
      <span class="small muted">${esc(KIND[p.kind] || p.kind)}. Created by ${esc(p.created_by)}. Policy ${esc(ev.policy.version)}, evidence ${esc(ev.evidence_hash.slice(0, 12))}</span></div>
    <ol class="steps">${STEPS.map(([k, l], i) => `<li class="${i < idx ? "done" : i === idx ? "now" : ""}">${l}</li>`).join("")}</ol>
    ${hint ? `<div class="banner ${p.status === "approved" ? "warn" : ""}">${esc(hint)}</div>` : ""}
    <div class="tiles">
      <div class="tile"><b>${sm.deletable} / ${sm.items}</b><span>items ready</span></div>
      <div class="tile"><b>${fmtB(sm.bytes_reclaimable)}</b><span>frees</span></div>
      <div class="tile"><b>${p.approvals.length} / ${p.required_approvals}</b><span>approvals${p.approvals.length ? `: ${esc(p.approvals.map((a) => a.principal).join(", "))}` : ""}</span></div>
      <div class="tile"><b>${Math.round(sm.readiness)}</b><span>readiness (weighted checks)</span></div>
    </div>
    <div class="actions">${btn.join("")}</div>
    <div id="reject" hidden class="panel" data-style="margin-top:12px"><label for="rr">Why reject?</label><textarea id="rr"></textarea>
      <button class="btn danger sm" id="rgo" data-style="margin-top:8px">Reject plan</button></div>
    <h2 data-style="margin-top:26px">Items</h2>
    <div class="tablewrap"><table><thead><tr><th>File</th><th>What and how</th><th>What stays</th><th class="r">Frees</th><th>Readiness</th><th>State</th><th></th></tr></thead>
    <tbody>${ev.items.map((r) => {
      const it = p.items.find((x) => x.id === r.item_id) || {};
      const open = S.open.has(r.item_id);
      const keeps = r.survivor ? `<div class="name small">${brk(r.survivor.path)}</div><div class="path">${esc(r.survivor.system)}:${esc(r.survivor.site || "")}</div>`
        : `<div class="small">${esc(r.recovery_meaning)}</div>`;
      return `<tr><td><div class="name">${brk(r.victim.path.split("/").pop())}</div><div class="path">${esc(r.victim.system)}: ${brk(r.victim.path)}</div></td>
        <td><div class="small"><b>${esc(CLASS[r.class] || r.class)}</b></div><div class="path">${esc(ACTION[r.action] || r.action)}</div></td>
        <td>${keeps}</td><td class="r num nowrap">${fmtB(r.bytes_reclaimable)}</td>
        <td class="nowrap"><span class="readiness"><i data-style="width:${r.readiness}%"></i></span> <span class="small num">${Math.round(r.readiness)}</span></td>
        <td class="small">${esc(it.state || "")}${!r.deletable ? `<div class="chips">${r.checks.filter((c) => c.hard && !c.passed).map((c) => `<span class="chip hold">${esc(c.name.replace(/_/g, " "))}</span>`).join("")}</div>` : ""}
          ${it.error ? `<div class="path">${esc(it.error)}</div>` : ""}</td>
        <td class="r"><button class="btn sm" data-open="${r.item_id}" aria-expanded="${open}">${open ? "Hide" : "Evidence"}</button>
          ${p.status === "quarantined" && it.state === "quarantined" && can("operator") ? `<button class="btn sm" data-restore="${r.item_id}">Restore</button>` : ""}</td></tr>
        ${open ? `<tr class="details-row"><td colspan="7">${r.survivor_reasons ? `<ul class="reasons">${r.survivor_reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
          ${r.note ? `<p class="small">${esc(r.note)}</p>` : ""}${checksList(r.checks)}</td></tr>` : ""}`;
    }).join("")}</tbody></table></div>`;
  $$("[data-open]").forEach((b) => (b.onclick = () => { S.open.has(b.dataset.open) ? S.open.delete(b.dataset.open) : S.open.add(b.dataset.open); planDetail(id); }));
  $$("[data-restore]").forEach((b) => (b.onclick = () => act(id, "restore", { item_ids: [b.dataset.restore] })));
  $$("[data-act]").forEach((b) => (b.onclick = () => {
    const a = b.dataset.act;
    if (a === "reject") { $("#reject").hidden = false; $("#rr").focus(); return; }
    act(id, a, a === "approve" ? { comment: "", expected_version: p.version } : a === "submit" || a === "cancel" || a === "execute" || a === "purge" ? undefined : {});
  }));
  const rg = $("#rgo");
  if (rg) rg.onclick = () => act(id, "reject", { reason: $("#rr").value || "rejected in dashboard" });
  if (p.status === "executing" || p.items.some((x) => ["quarantining", "restoring"].includes(x.state))) S.poll = setTimeout(() => planDetail(id), 1200);
}
async function act(id, a, body) {
  const label = { submit: "Submitted for approval", approve: "Approved", reject: "Rejected", cancel: "Draft discarded",
    execute: "Execution started", purge: "Purge started", restore: "Restore started" }[a];
  try {
    await api("POST", `/v1/plans/${id}/${a}`, body);
    toast(label);
    refreshBadges();
    a === "cancel" ? (location.hash = "#/plans") : planDetail(id);
  } catch (e) { toast(e.message, true); }
}

/* ============================================================ COMPRESSION */
async function compression() {
  const { renditions } = await api("GET", "/v1/renditions");
  renditions.sort((a, b) => (b.original - b.size) - (a.original - a.size));
  const passing = renditions.filter((r) => r.passed && !r.applied);
  const saved = renditions.filter((r) => r.passed).reduce((a, r) => a + (r.original - r.size), 0);
  main.innerHTML = `<div class="row"><h1>Compression</h1><span class="spacer"></span>
    ${can("analyst") ? `<button class="btn primary" id="cplan" ${passing.length ? "" : "disabled"}>Plan compression for ${passing.length} file${passing.length === 1 ? "" : "s"}</button>` : ""}</div>
    <p class="lede">KORA makes a smaller version of each large recording and PDF, then measures it against the policy's quality budget:
    same length and speech, picture similarity (SSIM), and whether OCR can still read the slides; for PDFs, identical text on every page.
    Drag across a picture to compare the original with the smaller version. ${fmtB(saved)} saved by versions that passed.</p>
    <div class="stack">${renditions.map((r, i) => `<section class="panel"><div class="rend">
      <div>${r.previews.length ? `<div class="compare" id="cmp${i}" data-style="--x:50%">
          <img data-blob="${r.previews[0].before}" alt="Original">
          <img class="after" data-blob="${r.previews[0].after}" alt="Smaller version">
          <span class="tag l">Original</span><span class="tag r">Smaller version</span><span class="handle"></span>
          <input type="range" min="0" max="100" value="50" aria-label="Compare original and smaller version"></div>
        <div class="compare-meta"><span id="cl${i}">${esc(r.previews[0].label)}</span>
          <span>${r.previews.length > 1 ? r.previews.map((pv, j) => `<button class="btn sm" data-pv="${i}:${j}">${j + 1}</button>`).join(" ") : ""}</span></div>`
        : `<div class="panel empty">No preview stored for this measurement.</div>`}</div>
      <div><h2 data-style="margin-bottom:2px">${brk(r.name)}</h2>
        <p class="small muted">${esc(r.op)}. ${r.applied ? "Applied: the file now holds the smaller version." : r.passed ? "Passed: ready to plan." : "Failed the quality budget: never offered."}</p>
        <div class="sizes"><b>${fmtB(r.original)}</b><span class="muted">to</span><b>${fmtB(r.size)}</b><span class="chip orange"><i></i>−${pct(r.saving)}</span></div>
        <div class="sizebar" aria-hidden="true"><i data-style="width:${Math.max(1, 100 * (r.size / r.original))}%"></i></div>
        ${checksList(r.checks.map((c) => ({ ...c, hard: true })))}</div></div></section>`).join("") || `<div class="panel empty">No compression measured yet. Run <code>kora optimize</code>.</div>`}</div>`;
  $$(".compare").forEach((el) => { $("input", el).oninput = (e) => el.style.setProperty("--x", `${e.target.value}%`); });
  $$("[data-pv]").forEach((b) => (b.onclick = async () => {
    const [i, j] = b.dataset.pv.split(":").map(Number); const pv = renditions[i].previews[j]; const el = $(`#cmp${i}`);
    $("img:not(.after)", el).src = await blobUrl(pv.before); $("img.after", el).src = await blobUrl(pv.after);
    $(`#cl${i}`).textContent = pv.label;
  }));
  const b = $("#cplan");
  if (b) b.onclick = () => createPlan("compress", {}, "Compress files within the quality budget");
  hydrateImages(main);
}

/* ============================================================ VERSIONS */
async function versions() {
  const { lineages } = await api("GET", "/v1/lineages");
  main.innerHTML = `<h1>Versions</h1>
    <p class="lede">Document families and their revision order. KORA reads document control (document number and revision
    in the file's properties or footer) before it trusts file names, and says how sure it is.</p>
    <div class="stack">${lineages.map((l) => {
      const main_ = l.versions.filter((v) => !v.variant_of);
      return `<section class="panel"><div class="row"><h2 data-style="margin:0">${esc(l.doc_number || l.title || l.key)}</h2>
        ${l.has_conflict ? '<span class="chip hold">revision conflict</span>' : ""}<span class="spacer"></span>
        <span class="small muted">order from ${esc(l.ordering.basis)}, ${esc(l.ordering.confidence)} confidence</span></div>
        ${l.title && l.doc_number ? `<p class="small muted" data-style="margin:2px 0 0">${esc(l.title)}</p>` : ""}
        <div class="chain">${main_.map((v, i) => `${i ? '<span class="arrow" aria-hidden="true">→</span>' : ""}<div class="rev ${v.is_head ? "head" : ""}"
          data-tip="${esc(v.name)}${v.diff_from_parent?.summary ? `\n${esc(v.diff_from_parent.summary)}` : ""}">
          <b>${esc(v.revision || "?")}</b><span>${v.is_head ? "current" : v.released ? "released" : "draft"}</span></div>`).join("")}
          ${l.versions.filter((v) => v.variant_of).map((v) => `<div class="rev variant" data-tip="${esc(v.name)}: same pages as another copy, different bytes">
          <b>${esc(v.revision || "?")}</b><span>variant</span></div>`).join("")}</div>
        <ul class="reasons">${main_.filter((v) => v.diff_from_parent?.summary).map((v) => `<li><b>${esc(v.revision)}</b>: ${esc(v.diff_from_parent.summary)}</li>`).join("")}</ul>
        </section>`;
    }).join("") || `<div class="panel empty">No document families found yet.</div>`}</div>`;
}

/* ============================================================ REVIEWS */
const ROLE_TEXT = { analyst: "a storage analyst", approver: "an approver", operator: "an IT operator",
  auditor: "an auditor", dpo: "the data protection officer (DPO)", viewer: "a viewer", admin: "an administrator" };
const PII_TEXT = { PHONE: ["phone number", "phone numbers"], EMAIL: ["e-mail address", "e-mail addresses"],
  PNR: ["personal identity number", "personal identity numbers"], IBAN: ["bank account number", "bank account numbers"],
  IPV4: ["IP address", "IP addresses"], NAME: ["name", "names"], NAME_IN_NOTES: ["name in speaker notes", "names in speaker notes"],
  AUTHOR_METADATA: ["author name in file properties", "author names in file properties"] };
const UNIT_TEXT = { message: "chat message", note: "speaker notes, slide", slide: "slide", page: "page",
  segment: "transcript line", frame: "recording frame" };
const OPTION_TEXT = { redact_for_ai_use: "Keep it redacted for AI use", not_personal_data: "It is not personal data",
  approve: "Approve", reject: "Reject", same_document: "Same document", different_documents: "Different documents",
  confirm_order: "One document, right order", order_is_wrong: "One document, wrong order",
  not_the_same_document: "Different documents", approve_erasure: "Approve the erasure",
  retire: "Retire it", ask_owner_to_update: "Ask the owner to update it", keep_as_is: "Keep it as it is" };
const PRIMARY = new Set(["approve", "confirm_order", "redact_for_ai_use", "ask_owner_to_update", "same_document"]);
const optionText = (o, ev) => {
  if (o.startsWith("authoritative:")) {
    const c = (ev?.candidates || []).find((x) => x.version === o.split(":")[1]);
    return `${c ? c.name : "This one"} is authoritative`;
  }
  return OPTION_TEXT[o] || o.replace(/_/g, " ");
};
const plural = (n, [one, many]) => `${n} ${n === 1 ? one : many}`;
const isHash = (v) => typeof v === "string" && /^[0-9a-f]{32,64}$/.test(v);
function evidenceHtml(ev) {
  const rows = []; const quotes = [];
  const row = (k, v) => rows.push(`<dt>${esc(k)}</dt><dd>${v}</dd>`);
  for (const [k, v] of Object.entries(ev || {})) {
    if (v === null || v === undefined || v === "" || isHash(v) || k === "a" || k === "b") continue;
    if (k === "counts" && typeof v === "object") {
      row("found", esc(Object.entries(v).map(([kind, n]) => plural(n, PII_TEXT[kind] || [kind.toLowerCase(), kind.toLowerCase()])).join(", ")));
    } else if (k === "samples" && Array.isArray(v)) {
      if (!v.length) continue;
      const groups = new Map();                       // the same masked value on several slides: one line
      v.forEach((x) => { const key = `${x.kind}|${x.masked}|${x.unit}`; groups.set(key, [...(groups.get(key) || []), x.ordinal]); });
      row("examples (masked)", `<ul class="ev">${[...groups].map(([key, ords]) => { const [kind, masked, unit] = key.split("|");
        return `<li><b>${esc((PII_TEXT[kind] || [kind])[0])}</b> <code>${esc(masked)}</code>
        <span class="muted">(${esc(UNIT_TEXT[unit] || unit)}${ords.length > 1 ? "s" : ""} ${esc(ords.join(", "))}${ords.length >= 5 ? ", …" : ""})</span></li>`; }).join("")}</ul>`);
    } else if (k === "names_in_directory") {
      row("names checked", `${esc(v)} people KORA knows by name (directory, speakers, slide owners)`);
    } else if (k === "sample" && Array.isArray(v)) {
      v.slice(0, 5).forEach((x) => quotes.push(`<blockquote>${esc(x.text)}${x.t != null ? ` <span class="muted">(${mmss(x.t)})</span>` : ""}</blockquote>`));
    } else if (k === "jaccard") {
      row("wording overlap", `${Math.round(v * 100)} %`);
    } else if (k === "candidates" && Array.isArray(v)) {
      row("candidates", `<ul class="ev">${v.map((x) => `<li>${brk(x.name)}</li>`).join("")}</ul>`);
    } else if (k === "versions" && Array.isArray(v)) {
      row("order KORA found", `<ol class="ev">${v.map((x) => `<li>${x.revision ? `Rev ${esc(x.revision)}: ` : ""}${brk(x.name)}</li>`).join("")}</ol>`);
    } else if (Array.isArray(v)) {
      row(k.replace(/_/g, " "), esc(v.map((x) => (typeof x === "object" ? Object.values(x).filter((y) => !isHash(y)).join(" ") : x)).join(", ")));
    } else if (typeof v === "object") {
      row(k.replace(/_/g, " "), esc(Object.entries(v).map(([a, b]) => `${a.replace(/_/g, " ")}: ${b}`).join(", ")));
    } else {
      row(k.replace(/_/g, " "), esc(v));
    }
  }
  return `${rows.length ? `<dl class="kv">${rows.join("")}</dl>` : ""}${quotes.join("")}`;
}
function reviewCard(t) {
  const mine = can(t.required_role);
  const who = S.personas.filter((p) => (p.roles || []).includes(t.required_role));
  const actions = mine
    ? `<label class="small" for="c-${t.id}">Comment (kept with the decision)</label><textarea id="c-${t.id}"></textarea>
       <div class="actions" data-style="margin-top:8px">${t.options.map((o) => `<button class="btn sm ${PRIMARY.has(o) ? "primary" : ""}"
         data-decide="${t.id}" data-opt="${esc(o)}">${esc(optionText(o, t.evidence))}</button>`).join("")}</div>`
    : `<div class="notice small">Only ${esc(ROLE_TEXT[t.required_role] || t.required_role)} can decide this, and ${esc(S.cur.name)} does not have that role.
        ${who.length ? `Act as: ${who.map((p) => `<button class="btn sm" data-as="${esc(p.id)}">${esc(p.name)}</button>`).join(" ")}` : ""}
        <div class="muted" data-style="margin-top:6px">Their choices: ${t.options.map((o) => esc(optionText(o, t.evidence))).join(" or ")}.</div></div>`;
  return `<article class="card"><h3 data-style="margin:0">${esc(t.question)}</h3>
    <p class="small muted" data-style="margin:4px 0 0">Who decides: ${esc(ROLE_TEXT[t.required_role] || t.required_role)}${t.blocks ? `. Waiting on this: ${esc(t.blocks)}` : ""}</p>
    ${evidenceHtml(t.evidence)}${actions}</article>`;
}
async function reviews() {
  const { reviews: list } = await api("GET", "/v1/reviews");     // not "reviews": that would hide this function
  const mine = list.filter((t) => can(t.required_role));
  const others = list.filter((t) => !can(t.required_role));
  main.innerHTML = `<h1>Reviews</h1>
    <p class="lede">What KORA will not decide alone: near-duplicates, uncertain revision order, machine transcripts, personal data
    (including names in speaker notes) and decks past their best-before date. Each task names who decides and what waits for the answer.</p>
    <h2 class="sect">Yours to decide <span class="muted">(${mine.length})</span></h2>
    ${mine.map(reviewCard).join("") || `<div class="panel empty">Nothing waiting for ${esc(S.cur.name)}.</div>`}
    ${others.length ? `<h2 class="sect">Waiting for someone else <span class="muted">(${others.length})</span></h2>${others.map(reviewCard).join("")}` : ""}`;
  $$("[data-decide]").forEach((b) => (b.onclick = async () => {
    try {
      await api("POST", `/v1/reviews/${b.dataset.decide}/decide`, { decision: b.dataset.opt, comment: $(`#c-${b.dataset.decide}`).value });
      toast(`Decided: ${optionText(b.dataset.opt)}`); refreshBadges(); reviews();
    } catch (e) { toast(e.message, true); }
  }));
  $$("[data-as]").forEach((b) => (b.onclick = () => { $("#persona").value = b.dataset.as; selectPersona(b.dataset.as); }));
}

/* ============================================================ SEARCH & AI */
async function knowledge() {
  const { sources } = await api("GET", "/v1/sources");
  main.innerHTML = `<h1>Search and AI</h1>
    <p class="lede">Search finds slides, keyframes, transcript passages and chat messages that this person may read, with the
    document number, revision and timestamp. The same evidence, with personal data removed, is what an AI assistant gets.</p>
    <form id="sform" class="row"><input type="search" id="sq" placeholder="e.g. edge data centre migration" size="44" aria-label="Search" value="${esc(S.lastQ || "")}">
      <button class="btn primary">Search</button>
      <label class="switch small"><input type="checkbox" id="aiv" ${S.aiView ? "checked" : ""}> Show what an AI assistant would receive</label></form>
    <div id="sres" data-style="margin-top:16px"></div>
    <div class="grid2" data-style="margin-top:22px">
      <section class="panel"><h2>Sources opted in for AI use</h2>
        <p class="small muted">A data-protection decision per source. Off by default; changes are audited.</p>
        ${can("dpo", "admin") ? `<label class="small" for="aireason">Reason for a change (kept in the audit log)</label>
          <input type="text" id="aireason" data-style="width:100%;margin:4px 0 8px" placeholder="e.g. DPIA approved for internal assistant">` : ""}
        <table><tbody>${sources.map((s) => `<tr><td><div class="name">${esc(s.name)}</div><div class="path">${esc(s.system)}</div></td>
          <td class="r"><label class="switch small"><input type="checkbox" data-ai="${esc(s.name)}" ${s.ai_use_allowed ? "checked" : ""} ${can("dpo", "admin") ? "" : "disabled"}>
          ${s.ai_use_allowed ? "allowed" : "not allowed"}</label></td></tr>`).join("")}</tbody></table>
        ${can("dpo", "admin") ? "" : `<p class="small muted">Only a data-protection officer can change this.</p>`}</section>
      <section class="panel"><h2>Use KORA from an AI assistant</h2>
        <p class="small">KORA runs as an MCP server, so assistants such as Claude can search this knowledge with one person's
        permissions, cite document numbers and revisions, and prefer current revisions. It is read-only: it cannot plan or delete.</p>
        <p class="small">In the VS Code terminal run <code>.venv/bin/kora mcp-config --as ui-analyst</code> (or the task
        <i>KORA: Connect an AI assistant</i>). It makes a read-only key and prints the exact settings for Claude Desktop and Claude Code.
        Answers only use sources the data protection officer opted in, under <i>Sources opted in for AI use</i>.</p>
        <pre class="block">{
  "mcpServers": {
    "kora": {
      "command": "/path/to/kora/.venv/bin/kora",
      "args": ["mcp"],
      "env": { "KORA_DATA_DIR": "/path/to/kora/.kora-data",
               "KORA_API_KEY": "kora_… (printed by mcp-config)" }
    }
  }
}</pre></section></div>`;
  const run = async () => {
    const q = $("#sq").value.trim(); S.lastQ = q; S.aiView = $("#aiv").checked;
    if (q.length < 2) return;
    const out = $("#sres"); out.innerHTML = `<p class="loading">Searching…</p>`;
    try {
      if (S.aiView) {
        const c = await api("GET", `/v1/ai/context?q=${encodeURIComponent(q)}&k=8`);
        out.innerHTML = `<div class="panel"><h2>Context pack for an assistant</h2>
          <p class="small">${c.chunks.length} cited passages. ${c.excluded.source_not_opted_in} result(s) left out because their source is not opted in for AI use.</p>
          <pre class="block">${esc(c.instructions)}\n\n${esc(c.context_block || "(nothing an assistant may use)")}</pre></div>`;
      } else {
        const r = await api("GET", `/v1/search?q=${encodeURIComponent(q)}&limit=10`);
        out.innerHTML = r.results.map((h) => {
          const where = h.locations?.length ? h.locations.map((l) => `${l.system}: ${l.path}`).join("; ") : h.original || "";
          const cite = h.type === "transcript" ? `${h.recording} at ${mmss(h.start_s)}${h.speaker ? `, ${h.speaker}` : ""}. ${h.transcript_quality}`
            : h.type === "frame" ? `Recording frame at ${mmss(h.t)}` : h.type === "message" ? `${h.document}, ${h.speaker || ""} ${h.time || ""}`
            : `${h.document || ""}, ${h.type === "speaker_notes" ? "speaker notes of slide" : "page"} ${h.page}${h.doc_number ? `. ${h.doc_number}` : ""}${h.version ? ` rev ${h.version.revision}${h.version.is_head ? " (current)" : `, ${h.version.in_latest || "superseded"}`}` : ""}`;
          const slide = (h.slide && h.slide.doc_number ? ` On screen: ${h.slide.doc_number} rev ${h.slide.revision || "?"}, slide ${h.slide.page}.` : "")
            + (h.past_best_before ? ` Past its best-before date (${h.best_before}).` : "");
          return `<article class="card hit">${h.thumb ? `<img data-blob="${h.thumb}" alt="">` : `<div class="noimg"></div>`}
            <div><div>${esc(h.text)}</div><div class="cite">${esc(cite)}${esc(slide)}</div><div class="cite">${esc(where)}</div></div></article>`;
        }).join("") || `<div class="panel empty">Nothing this person may read matches. Try fewer words.</div>`;
        hydrateImages(out);
      }
    } catch (e) { out.innerHTML = `<div class="panel empty">${esc(e.message)}</div>`; }
  };
  $("#sform").onsubmit = (e) => { e.preventDefault(); run(); };
  $("#aiv").onchange = run;
  $$("[data-ai]").forEach((cb) => (cb.onchange = async () => {
    const reason = ($("#aireason")?.value || "").trim();
    if (reason.length < 3) { cb.checked = !cb.checked; toast("Write a reason first: it is kept in the audit log.", true); $("#aireason")?.focus(); return; }
    try { await api("POST", `/v1/sources/${cb.dataset.ai}/ai-use`, { allowed: cb.checked, reason }); toast("Saved and audited"); knowledge(); }
    catch (e) { cb.checked = !cb.checked; toast(e.message, true); }
  }));
  if (S.lastQ) run();
}

/* ============================================================ AUDIT */
async function auditView() {
  const [v, { events }] = await Promise.all([api("GET", "/v1/audit/verify"), api("GET", "/v1/audit?limit=200")]);
  main.innerHTML = `<h1>Audit</h1>
    <p class="lede">Every decision and every change, hash-chained and signed. The log holds ids and hashes, not file names or
    people's data, so it never needs erasing.</p>
    <div class="banner ${v.ok ? "" : "warn"}">${v.ok ? `Chain verified: ${v.events} events, head ${esc(v.head_hash.slice(0, 16))}.` : `Chain broken at event ${v.first_bad_seq}: ${esc(v.reason)}`}</div>
    <div class="tablewrap"><table><thead><tr><th class="r">#</th><th>When</th><th>Who</th><th>What</th><th>Subject</th><th>Details</th></tr></thead><tbody>
    ${events.map((e) => `<tr><td class="r num">${e.seq}</td><td class="small">${when(e.at)}</td><td class="small">${esc(e.actor)}</td>
      <td class="small"><b>${esc(e.action)}</b></td><td class="path">${esc(e.subject)}</td>
      <td class="path">${esc(JSON.stringify(e.details).slice(0, 180))}</td></tr>`).join("")}</tbody></table></div>`;
}

/* ------------------------------------------------------------ boot */
loadPersonas();
if (S.cur) selectPersona(S.cur.id).catch((e) => { toast(`Identity not accepted: ${e.message}`, true); S.cur = null; noIdentity(); });
else noIdentity();
