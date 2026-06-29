// Dashboard client. Talks to /api, renders whatever checks the backend exposes.
const CLASS_COLORS = {
  "Death Knight": "#C41E3A", "Demon Hunter": "#A330C9", "Druid": "#FF7C0A",
  "Evoker": "#33937F", "Hunter": "#AAD372", "Mage": "#3FC7EB", "Monk": "#00FF98",
  "Paladin": "#F48CBA", "Priest": "#FFFFFF", "Rogue": "#FFF468", "Shaman": "#0070DD",
  "Warlock": "#8788EE", "Warrior": "#C69B6D",
};

const state = { days: window.DEFAULT_DAYS || 14, category: "All", data: null,
                raids: [], encounter: null };

const $ = (sel) => document.querySelector(sel);
const overlay = $("#overlay");
const results = $("#results");

function showOverlay(msg) {
  $("#overlay-msg").textContent = msg || "Loading…";
  overlay.hidden = false;
}
function hideOverlay() { overlay.hidden = true; }

async function load(force = false) {
  showOverlay(force ? "Re-querying WarcraftLogs…" : "Summoning logs…");
  try {
    const res = await fetch(`/api/analyze?days=${state.days}&force=${force}`);
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      renderError(body.detail || `Request failed (${res.status})`);
      return;
    }
    state.data = await res.json();
    renderStats();
    renderFilters();
    renderCards();
  } catch (e) {
    renderError(String(e));
  } finally {
    hideOverlay();
  }
}

function renderError(msg) {
  results.innerHTML = "";
  const div = document.createElement("div");
  div.className = "banner";
  div.innerHTML = `<b>Couldn't load analytics.</b><br>${msg}<br><br>
    Check your <code>.env</code> (credentials + guild) and that the guild has public logs in range.`;
  results.before(div);
  // remove any stale banner duplicates
  document.querySelectorAll(".banner").forEach((b, i) => { if (i > 0) b.remove(); });
}

// Non-error info banner (e.g. backfill stopped early and needs another pass).
function renderNotice(html) {
  document.querySelectorAll(".notice").forEach((n) => n.remove());
  const div = document.createElement("div");
  div.className = "banner notice";
  div.innerHTML = html;
  results.before(div);
}

function renderStats() {
  const d = state.data;
  $("#statbar").innerHTML = `
    ${stat(d.report_count, "Reports")}
    ${stat(d.fight_count, "Boss pulls")}
    ${stat(d.player_count, "Players")}
    ${stat(d.checks.length, "Checks run")}
    ${stat(state.days ? state.days + "d" : "All", "Window")}`;
  const f = d.filters || {};
  const att = Math.round((f.min_attendance_pct || 0) * 100);
  const bits = [
    f.difficulty ? `${f.difficulty} difficulty` : null,
    f.mythic_plus_excluded ? "raid only (M+ excluded)" : null,
    att > 0 ? `core raiders ≥${att}% attendance` : null,
  ].filter(Boolean);
  $("#guild-sub").textContent = bits.join(" · ") || "Raid performance analytics";
}
const stat = (num, label) => `<div class="stat"><div class="num">${num}</div><div class="label">${label}</div></div>`;

function renderFilters() {
  const cats = ["All", ...new Set(state.data.checks.map((c) => c.category))];
  const nav = $("#category-filters");
  nav.innerHTML = "";
  cats.forEach((cat) => {
    const b = document.createElement("button");
    b.className = "chip" + (cat === state.category ? " active" : "");
    b.textContent = cat;
    b.onclick = () => { state.category = cat; renderFilters(); renderCards(); };
    nav.appendChild(b);
  });
}

const SEV_RANK = { critical: 0, warn: 1, good: 2, info: 3 };

function renderCards() {
  const checks = state.data.checks
    .filter((c) => state.category === "All" || c.category === state.category)
    .sort((a, b) => (SEV_RANK[a.severity] - SEV_RANK[b.severity]));
  results.innerHTML = "";
  if (!checks.length) {
    results.innerHTML = `<div class="empty">No checks in this category.</div>`;
    return;
  }
  checks.forEach((c) => results.appendChild(card(c)));
}

function card(c) {
  const el = document.createElement("article");
  el.className = `card ${c.severity}`;
  const showDetail = c.columns.length > 2;
  const rowsHtml = c.rows.length
    ? `<table><thead><tr><th class="rank">#</th>${c.columns
        .map((col) => `<th>${col}</th>`).join("")}</tr></thead><tbody>
        ${c.rows.map((r, i) => row(r, i, showDetail)).join("")}</tbody></table>`
    : `<div class="empty">No entries.</div>`;
  el.innerHTML = `
    <div class="card-head">
      <div class="cat">${c.category}</div>
      <h3>${c.name}</h3>
      <p class="headline">${c.headline}</p>
      <p class="desc">${c.description}</p>
    </div>
    ${rowsHtml}`;
  return el;
}

function row(r, i, showDetail = true) {
  const color = CLASS_COLORS[r.player_class] || "#8aa";
  const dot = `<span class="cdot" style="background:${color}"></span>`;
  return `<tr>
    <td class="rank">${i + 1}</td>
    <td class="player">${dot}${escapeHtml(r.player)}</td>
    <td class="val">${escapeHtml(r.display)}</td>
    ${showDetail ? `<td class="detail">${escapeHtml(r.detail || "")}</td>` : ""}
  </tr>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ── Boss drill-down ───────────────────────────────────────
async function loadBosses(force = false) {
  try {
    const res = await fetch(`/api/bosses?days=${state.days}&force=${force}`);
    if (!res.ok) return;
    const data = await res.json();
    state.raids = data.raids || [];
  } catch { state.raids = []; }
  renderRaidOptions();
}

function renderRaidOptions() {
  const sel = $("#raid-select");
  const prev = sel.value;
  sel.innerHTML = `<option value="">Overall — all bosses</option>` +
    state.raids.map((r, i) => `<option value="${i}">${escapeHtml(r.zone)}</option>`).join("");
  if (prev && state.raids[Number(prev)]) sel.value = prev; else renderBossOptions();
}

function renderBossOptions() {
  const raidIdx = $("#raid-select").value;
  const wrap = $("#boss-wrap");
  if (raidIdx === "") { wrap.hidden = true; switchToOverall(); return; }
  const raid = state.raids[Number(raidIdx)];
  const sel = $("#boss-select");
  sel.innerHTML = `<option value="">Select a boss…</option>` +
    raid.bosses.map((b) => `<option value="${b.encounter_id}">${escapeHtml(b.name)} (${b.pulls} pulls)</option>`).join("");
  wrap.hidden = false;
}

function switchToOverall() {
  state.encounter = null;
  $("#overall-view").hidden = false;
  $("#boss-panel").hidden = true;
  if (!state.data) load(false); else { renderStats(); renderFilters(); renderCards(); }
}

async function showBoss(encounterId, force = false) {
  state.encounter = encounterId;
  showOverlay("Crunching boss stats…");
  try {
    const res = await fetch(`/api/boss?encounter_id=${encounterId}&days=${state.days}&force=${force}`);
    const data = await res.json();
    if (!res.ok || data.error) { renderError((data && data.detail) || data.error || "Boss load failed"); return; }
    $("#overall-view").hidden = true;
    $("#boss-panel").hidden = false;
    renderBossPanel(data);
  } catch (e) { renderError(String(e)); }
  finally { hideOverlay(); }
}

function renderBossPanel(d) {
  const b = d.boss;
  const best = b.best_kill_s != null ? `${b.best_kill_s.toFixed(0)}s`
             : b.best_wipe_pct != null ? `${b.best_wipe_pct.toFixed(1)}%` : "—";
  const bestLbl = b.best_kill_s != null ? "Best kill" : "Best pull";
  // Same checks the overall page renders, just scoped to this encounter.
  const checks = (d.checks || []).slice().sort((a, c) => SEV_RANK[a.severity] - SEV_RANK[c.severity]);
  const panel = $("#boss-panel");
  panel.innerHTML = `
    <div class="boss-hero">
      <div>
        <h2>${escapeHtml(b.name)}</h2>
        <div class="zone">${escapeHtml(b.zone)} · ${d.timeframe_days ? "last " + d.timeframe_days + "d" : "all logs"}</div>
      </div>
      <div class="metrics">
        ${metric(b.pulls, "Pulls")}
        ${metric(b.kills, "Kills", "kills")}
        ${metric(b.wipes, "Wipes", "wipes")}
        ${metric(best, bestLbl)}
      </div>
    </div>
    <div class="results" id="boss-results"></div>`;
  const host = $("#boss-results");
  if (!checks.length) { host.innerHTML = `<div class="empty">No checks for this boss.</div>`; return; }
  checks.forEach((c) => host.appendChild(card(c)));
}

const metric = (num, lbl, cls = "") =>
  `<div class="metric ${cls}"><div class="m-num">${num}</div><div class="m-lbl">${lbl}</div></div>`;

// ── log sync ("Update Logs") ──────────────────────────────
function fmtAgo(ts) {
  if (!ts) return "never synced";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return "synced just now";
  const m = Math.round(s / 60);
  if (m < 90) return `synced ${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 36) return `synced ${h}h ago`;
  return `synced ${Math.round(h / 24)}d ago`;
}

async function refreshSyncStatus() {
  try {
    const res = await fetch("/api/status");
    const sync = (await res.json()).sync || {};
    const reports = sync.stored_reports ? ` · ${sync.stored_reports} reports` : "";
    $("#last-synced").textContent = fmtAgo(sync.last_synced) + reports;
  } catch { /* leave label as-is */ }
}

async function updateLogs() {
  const btn = $("#update-logs");
  btn.disabled = true;
  showOverlay("Updating logs from WarcraftLogs… this can take a minute.");
  try {
    const res = await fetch("/api/update-logs", { method: "POST" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      hideOverlay();
      renderError(body.detail || `Update failed (${res.status})`);
      btn.disabled = false;
      return;
    }
    await refreshSyncStatus();
    // Backfill batches incrementally; a rate-limit stops it early with work left.
    // Tell the user to run it again — stored reports are skipped, so it resumes.
    if (body.stopped_early && body.remaining > 0) {
      renderNotice(`<b>Backfill paused.</b> Fetched ${body.fetched} report(s); `
        + `${body.remaining} still to load (WarcraftLogs rate limit). `
        + `Wait a minute, then click <b>Update Logs</b> again to continue — `
        + `already-fetched reports are skipped.`);
    }
  } catch (e) {
    hideOverlay();
    renderError(String(e));
    btn.disabled = false;
    return;
  }
  btn.disabled = false;
  // Re-read the freshly-synced store (these manage their own overlay).
  loadBosses(false);
  reloadCurrentView(false);
}

// ── wire up controls ──────────────────────────────────────
function reloadCurrentView(force) {
  if (state.encounter) showBoss(state.encounter, force);
  else load(force);
}

$("#timeframe").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  state.days = Number(btn.dataset.days);
  document.querySelectorAll("#timeframe button").forEach((b) => b.classList.toggle("active", b === btn));
  loadBosses(false);
  reloadCurrentView(false);
});
$("#refresh").addEventListener("click", () => { loadBosses(true); reloadCurrentView(true); });
$("#update-logs").addEventListener("click", updateLogs);
$("#raid-select").addEventListener("change", renderBossOptions);
$("#boss-select").addEventListener("change", (e) => {
  const enc = e.target.value;
  if (enc) showBoss(Number(enc), false); else switchToOverall();
});

// set initial active timeframe button & go
document.querySelectorAll("#timeframe button").forEach((b) =>
  b.classList.toggle("active", Number(b.dataset.days) === state.days));
load(false);
loadBosses(false);
refreshSyncStatus();
