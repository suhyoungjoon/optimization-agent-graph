"use strict";
// optimization-agent-graph 화면. 서버(/api)는 기록을 읽어 주기만 하고, 승인·반려는 그래프의 interrupt를 재개한다.

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 3) => (typeof v === "number" ? v.toFixed(d) : esc(v ?? "-"));
const pct = (v) => (v == null ? "-" : `${(v * 100).toFixed(0)}%`);
const signed = (v, d = 4) => (v == null ? "-" : `${v >= 0 ? "+" : ""}${v.toFixed(d)}`);
const time = (iso) => (iso ? new Date(iso).toLocaleTimeString("ko-KR", { hour12: false }) + "." + String(new Date(iso).getMilliseconds()).padStart(3, "0") : "");
const SVG = "http://www.w3.org/2000/svg";
const ACTOR_COLOR = { ai: "var(--ai)", code: "var(--code)", human: "var(--human)", system: "var(--system)" };
// 같은 열 안의 세로 순서 (주 경로를 위쪽에)
const ROW_PRIORITY = ["__start__", "execute", "perspective", "analyze", "synthesize", "propose", "validate",
  "await_approval", "retry", "auto_reject", "approval", "apply", "reject", "__end__"];

const S = { graph: null, runs: [], runId: null, detail: null, cps: [], cpIndex: null, node: null, timer: null };

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}

// --- 그래프 레이아웃: 역방향 엣지(재시도 루프)를 빼고 최장 경로로 열을 정한다 -------------------------

function layout(graph) {
  const out = {};
  graph.edges.forEach((e) => (out[e.source] ||= []).push(e.target));
  const back = new Set(), state = {}, order = [];
  (function dfs(n) {
    state[n] = 1;
    for (const m of out[n] || []) {
      if (state[m] === 1) back.add(`${n}>${m}`);
      else if (!state[m]) dfs(m);
    }
    state[n] = 2; order.push(n);
  })("__start__");
  const layer = {};
  order.reverse().forEach((n) => (layer[n] ??= 0));
  for (const n of order) for (const m of out[n] || []) if (!back.has(`${n}>${m}`)) layer[m] = Math.max(layer[m] ?? 0, layer[n] + 1);
  const cols = {};
  graph.nodes.forEach((n) => (cols[layer[n.id] ?? 0] ||= []).push(n.id));
  Object.values(cols).forEach((c) => c.sort((a, b) => ROW_PRIORITY.indexOf(a) - ROW_PRIORITY.indexOf(b)));
  return { layer, cols, back };
}

// 체크포인트로부터 이번 실행이 지난 노드·엣지와 현재 위치를 구한다
function runPath(cps, detail) {
  const visits = {}, edges = {}, failed = new Set();
  const executed = cps.map((c) => c.tasks.filter((t) => t.result != null || t.error).map((t) => t.name));
  executed.forEach((names) => names.forEach((n) => (visits[n] = (visits[n] || 0) + (n === "perspective" ? 0 : 1))));
  if (executed.some((ns) => ns.includes("perspective"))) visits.perspective = 1;
  for (let i = 0; i < executed.length - 1; i++) {
    const a = [...new Set(executed[i])], b = [...new Set(executed[i + 1])];
    for (const s of a) for (const t of b) edges[`${s}>${t}`] = (edges[`${s}>${t}`] || 0) + 1;
  }
  const last = cps[cps.length - 1];
  const pending = last ? last.next : [];
  const paused = last ? last.tasks.filter((t) => t.interrupts.length).map((t) => t.name) : [];
  const lastExec = [...executed].reverse().find((ns) => ns.length) || [];
  if (last && !pending.length) lastExec.forEach((n) => (edges[`${n}>__end__`] = 1));
  (detail?.timings || []).forEach((t) => t.error && failed.add(t.node));
  return { visits, edges, pending, paused, failed };
}

function drawGraph() {
  const svg = $("graph"), g = S.graph;
  if (!g) return;
  const { layer, cols, back } = layout(g);
  const path = runPath(S.cps, S.detail);
  const perspectives = S.detail?.run?.analysis?.perspectives || [];
  const W = 124, GAP = 30, H = 46, VGAP = 18, PAD = 14, EDGE_W = 58;   // EDGE_W: start·end 노드 폭
  const widthOf = (id) => (id === "__start__" || id === "__end__" ? EDGE_W : W);
  const heightOf = (id) => (id === "perspective" && perspectives.length ? H + perspectives.length * 18 : H);
  const pos = {};
  let maxY = 0, x = PAD;
  Object.keys(cols).map(Number).sort((a, b) => a - b).forEach((c) => {
    const ids = cols[c], colW = Math.max(...ids.map(widthOf));
    let y = PAD;
    ids.forEach((id) => { pos[id] = { x, y, w: widthOf(id), h: heightOf(id) }; y += heightOf(id) + VGAP; });
    x += colW + GAP; maxY = Math.max(maxY, y);
  });
  const width = x - GAP + PAD;
  const height = maxY + 40;
  // 화면 폭에 맞춰 줄이되 글자가 읽히도록 75%까지만 줄이고, 그보다 넓으면 가로 스크롤
  const avail = svg.parentElement.clientWidth || width;
  const scale = Math.max(0.75, Math.min(1, avail / width));
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", width * scale); svg.setAttribute("height", height * scale);
  svg.innerHTML = "";
  const meta = Object.fromEntries(g.nodes.map((n) => [n.id, n]));
  const el = (tag, attrs, parent = svg) => { const e = document.createElementNS(SVG, tag); Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v)); parent.appendChild(e); return e; };

  // 엣지
  g.edges.forEach((e) => {
    const a = pos[e.source], b = pos[e.target];
    if (!a || !b) return;
    const key = `${e.source}>${e.target}`, on = path.edges[key];
    let d;
    if (back.has(key)) {   // 재시도처럼 되돌아가는 엣지는 아래로 돌아 그린다
      const y = height - 14, x1 = a.x + a.w / 2, x2 = b.x + b.w / 2;
      d = `M${x1},${a.y + a.h} C${x1},${y} ${x2},${y} ${x2},${b.y + b.h}`;
    } else {
      const x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = b.x, y2 = b.y + b.h / 2, mx = (x1 + x2) / 2;
      d = `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
    }
    // 실패 시 END로 빠지는 엣지는 지나간 경우가 아니면 흐리게 (구조는 보이되 주 경로를 가리지 않게)
    const failExit = e.target === "__end__" && e.conditional && !on;
    el("path", { d, class: `edge${e.conditional ? " cond" : ""}${on ? " on" : ""}${failExit ? " exit" : ""}` });
    if (on > 1 || (back.has(key) && on)) {
      const tx = back.has(key) ? (a.x + a.w / 2 + b.x + b.w / 2) / 2 : (a.x + a.w + b.x) / 2;
      const ty = back.has(key) ? height - 18 : (a.y + b.y) / 2 + 12;
      const t = el("text", { x: tx, y: ty, class: "count", "text-anchor": "middle" });
      t.textContent = `${e.target === "propose" ? "retry " : ""}×${on}`;
    }
  });

  // 노드
  g.nodes.forEach((n) => {
    const p = pos[n.id];
    const isStartEnd = n.id === "__start__" || n.id === "__end__";
    const visited = path.visits[n.id] || (n.id === "__start__" && S.cps.length) || (n.id === "__end__" && path.edges[Object.keys(path.edges).find((k) => k.endsWith(">__end__"))]);
    const isPending = path.pending.includes(n.id), isPaused = path.paused.includes(n.id), isFailed = path.failed.has(n.id);
    const grp = el("g", { class: `node${!visited && !isPending ? " faded" : ""}${S.node === n.id ? " sel" : ""}`, transform: `translate(${p.x},${p.y})` });
    el("rect", { class: "box", width: p.w, height: p.h, rx: isStartEnd ? 23 : 8, stroke: ACTOR_COLOR[n.actor] || "var(--system)" }, grp);
    el("rect", { width: 6, height: p.h, rx: 3, fill: ACTOR_COLOR[n.actor] || "var(--system)" }, grp);
    const label = el("text", { x: isStartEnd ? p.w / 2 : 14, y: isStartEnd ? 27 : 19, "text-anchor": isStartEnd ? "middle" : "start" }, grp);
    label.textContent = isStartEnd ? n.id.replace(/_/g, "") : n.label;
    const stage = el("text", { x: 14, y: 35, class: "stage" }, grp); stage.textContent = isStartEnd ? "" : n.id;   // 기획의 단계 이름은 노드 상세 제목에 보인다
    let mark = "", color = "var(--ok)";
    if (isFailed) { mark = "✗"; color = "var(--bad)"; }
    else if (isPaused) { mark = "⏸"; color = "var(--pause)"; }
    else if (isPending) { mark = "●"; color = "var(--code)"; }
    else if (visited && !isStartEnd) mark = path.visits[n.id] > 1 ? `✓×${path.visits[n.id]}` : "✓";
    if (mark) { const m = el("text", { x: p.w - 8, y: 19, "text-anchor": "end", class: `mark${isPending && !isPaused ? " pulse" : ""}`, fill: color }, grp); m.textContent = mark; }
    if (n.id === "perspective" && perspectives.length) {
      perspectives.forEach((pp, i) => {
        const r = S.detail?.stages?.[`2_analyze.${pp.id}`];
        const t = el("text", { x: 14, y: 54 + i * 18, class: "stage" }, grp);
        t.textContent = `${r ? (r.error ? "✗" : "✓") : "·"} ${pp.prefix} ${pp.name}`;
      });
    }
    grp.addEventListener("click", () => { S.node = n.id; drawGraph(); renderNode(); });
  });
}

// --- 실행 목록·통계·모델 ------------------------------------------------------------------

async function loadRuns() {
  const { runs, stats } = await api("/api/runs");
  S.runs = runs;
  const u = stats.usage;
  $("stats").innerHTML = [
    `<span class="stat"><b>${stats.runs}</b>실행</span>`,
    ...Object.entries(stats.by_status).map(([k, v]) => `<span class="stat"><b>${v}</b>${esc(k)}</span>`),
    `<span class="stat"><b>${pct(stats.approval_rate)}</b>승인율</span>`,
    `<span class="stat"><b>${pct(stats.retry_rate)}</b>재시도율</span>`,
    `<span class="stat"><b>${u.llm_calls}</b>LLM 호출 · ${u.input_tokens + u.output_tokens} 토큰 · ${u.cost_usd == null ? "비용 -" : "$" + u.cost_usd.toFixed(4)}</span>`,
  ].join("");
  $("runs").innerHTML = runs.map((r) => `
    <li data-id="${esc(r.run_id)}" class="${r.run_id === S.runId ? "sel" : ""}">
      <span class="id">${esc(r.run_id)}</span>
      <span><span class="status ${esc(r.status)}">${esc(r.status)}</span> ${esc(r.model_version)} · ${esc(r.scenario_set)} · ${esc(r.analysis)}${r.retries ? ` · 재시도 ${r.retries}` : ""} · ${r.llm.rehearsal ? "리허설" : esc(r.llm.model)}</span>
    </li>`).join("") || `<li class="hint">아직 실행이 없다</li>`;
  document.querySelectorAll("#runs li[data-id]").forEach((li) => li.addEventListener("click", () => selectRun(li.dataset.id)));
}

async function loadModels() {
  const m = await api("/api/models");
  $("models").innerHTML = `<div class="models">
    <div class="hint">엔진 ${esc(m.engine)} · 챔피언 <b>v${esc(m.champion)}</b></div>
    ${m.versions.map((c) => {
      const val = c.validation?.sets?.validation?.mean_gain?.assignment_rate;
      return `<div class="ver ${c.version === m.champion ? "champ" : ""}">${c.version === m.champion ? "★ " : ""}${esc(c.model_version)}
        <span class="hint">부모 ${c.parent ? "v" + c.parent : "-"} · ${esc(c.source?.kind)} ${esc(c.source?.title || "")}
        ${val != null ? `· 검증셋 할당 ${signed(val)}` : ""}${c.approval?.by ? ` · 승인 ${esc(c.approval.by)}` : ""}</span></div>`;
    }).join("")}
    <div class="hint">이력: ${m.history.map((h) => `v${h.version}(${esc(h.action)})`).join(" → ")}</div>
    <div class="hint">되돌리기는 CLI: python -m workflow rollback --note "사유"</div></div>`;
}

async function loadScenarios() {
  const s = await api("/api/scenarios");
  $("start-scenario").innerHTML = s.names.map((n) => `<option ${n === s.default ? "selected" : ""}>${esc(n)}</option>`).join("");
  $("start-analysis").value = s.analysis_default;
}

// --- 실행 상세 ------------------------------------------------------------------------------

async function selectRun(id) {
  S.runId = id; S.cpIndex = null; S.node = null;
  await refreshRun();
  loadRuns();
}

async function refreshRun() {
  if (!S.runId) return;
  const [detail, cps] = await Promise.all([api(`/api/runs/${S.runId}`), api(`/api/runs/${S.runId}/checkpoints`)]);
  S.detail = detail; S.cps = cps.checkpoints; S.reducers = cps.reducers;
  $("empty").hidden = true; $("detail").hidden = false;
  renderHead(); drawGraph(); renderTimeline(); renderState(); renderNode();
  clearTimeout(S.timer);
  const live = detail.running || (!detail.terminal && !detail.pending.length);
  if (live) S.timer = setTimeout(() => refreshRun().then(loadRuns), 1200);
}

function renderHead() {
  const { run, pending, interrupted_decision: crashed } = S.detail;
  const sset = run.scenario_set || {};
  $("run-head").innerHTML = `
    <h2>${esc(run.run_id)} <span class="status ${esc(run.status)}">${esc(run.status)}</span></h2>
    <div class="chips">
      <span class="chip">모델 ${esc(run.model_version)}</span>
      <span class="chip">시나리오 세트 ${esc(sset.name)} (학습 ${(sset.train || []).map((s) => s.seed).join("·")} / 검증 ${(sset.validation || []).map((s) => s.seed).join("·") || "-"})</span>
      <span class="chip">분석 ${esc(run.analysis?.mode || "single")}</span>
      <span class="chip">LLM ${run.llm.rehearsal ? "리허설(가짜)" : esc(run.llm.model)}</span>
      <span class="chip">thread_id = ${esc(run.run_id)}</span>
      <span class="chip">다음 노드 ${pending.length ? esc(pending.join(", ")) : "없음 (끝)"}</span>
      <span class="chip">코어 ${esc((run.core?.commit || "").slice(0, 7))}</span>
    </div>
    ${run.error ? `<p class="err">오류: ${esc(run.error.message)}</p>` : ""}`;
  const banner = $("banner");
  if (crashed) {
    banner.innerHTML = `<div class="banner warn"><b>반영 도중 중단됨</b> — 체크포인트가 <code>apply</code> 앞에 남아 있다. 같은 결정(${esc(crashed.proposal_id)})으로 이어서 실행하면 이미 등록된 버전은 다시 등록하지 않는다.
      <div class="row"><input id="rc-by" placeholder="승인자 이름"><button id="rc-go">이어서 실행</button></div></div>`;
    $("rc-go").onclick = () => decide("approve", { approver: $("rc-by").value, proposal_id: crashed.proposal_id, note: "중단 후 재개" });
  } else if (pending.includes("approval")) {
    const iv = S.cps.at(-1)?.tasks.find((t) => t.interrupts.length)?.interrupts[0] || {};
    banner.innerHTML = `<div class="banner pause"><b>⏸ interrupt — 사람의 결정을 기다린다</b>
      <span class="hint">승인·반려는 이 실행(thread)을 체크포인트에서 재개한다 (Command(resume=…))</span>
      <div class="hint">interrupt 값: 후보 ${esc((iv.eligible || []).join(", "))} · 모델 ${esc(iv.model_version)}</div>
      <div class="row">
        <select id="dc-prop">${(iv.eligible || []).map((p) => `<option>${esc(p)}</option>`).join("")}</select>
        <input id="dc-by" placeholder="승인자 이름 (필수)"><input id="dc-note" placeholder="메모">
        <button id="dc-ok">승인 → 새 버전 등록</button><button id="dc-no" class="secondary">반려</button>
      </div></div>`;
    $("dc-ok").onclick = () => decide("approve", { approver: $("dc-by").value, note: $("dc-note").value, proposal_id: $("dc-prop").value });
    $("dc-no").onclick = () => decide("reject", { approver: $("dc-by").value, note: $("dc-note").value });
  } else banner.innerHTML = "";
}

async function decide(action, body) {
  try {
    await api(`/api/runs/${S.runId}/${action}`, { method: "POST", body: JSON.stringify(body) });
  } catch (e) { alert(e.message); return; }
  await Promise.all([refreshRun(), loadRuns(), loadModels()]);
}

// --- 체크포인트 타임라인과 상태 -------------------------------------------------------------------

function renderTimeline() {
  const cps = S.cps, timings = S.detail.timings;
  $("thread").textContent = `thread_id ${S.runId} · 체크포인트 ${cps.length}개 · runs/checkpoints.sqlite`;
  if (!cps.length) { $("timeline").innerHTML = `<p class="hint">체크포인트 없음 (X1 이전 실행)</p>`; return; }
  const rows = cps.map((c, i) => {
    const next = cps[i + 1];
    const dur = next ? (new Date(next.created_at) - new Date(c.created_at)) / 1000 : null;
    const names = c.tasks.map((t) => t.name);
    const label = names.length > 1 ? `${names[0]} ×${names.length} <span class="hint">(병렬 Send)</span>` : esc(names[0] || "-");
    const paused = c.tasks.some((t) => t.interrupts.length);
    let gantt = "";
    if (names.length > 1) {   // 병렬 가지: 노드 시간 기록으로 겹쳐 그린다
      const ts = timings.filter((t) => names.includes(t.node));
      if (ts.length) {
        const t0 = Math.min(...ts.map((t) => t.started_at)), t1 = Math.max(...ts.map((t) => t.finished_at)), span = Math.max(t1 - t0, 1e-6);
        gantt = ts.map((t) => `<div title="${esc(t.task)} ${t.seconds.toFixed(3)}s" class="gantt"><span style="left:${((t.started_at - t0) / span) * 100}%;width:${Math.max((t.seconds / span) * 100, 2)}%"></span></div>`).join("")
          + `<div class="hint">${ts.map((t) => esc(t.task)).join(" · ")} — 겹친 구간이 동시에 실행된 시간</div>`;
      }
    }
    return `<tr data-i="${i}" class="${S.cpIndex === i ? "sel" : ""}">
      <td class="num">${c.step}</td><td>${time(c.created_at)}</td>
      <td>${paused ? "⏸ " : ""}${label}${gantt}</td>
      <td class="num">${dur == null ? "" : dur.toFixed(2) + "s"}</td><td>${esc(c.next.join(", ") || "끝")}</td></tr>`;
  }).join("");
  $("timeline").innerHTML = `<table class="tl"><thead><tr><th>step</th><th>시각</th><th>이 체크포인트에서 실행한 노드</th><th>소요</th><th>다음</th></tr></thead><tbody>${rows}</tbody></table>`;
  document.querySelectorAll(".tl tr[data-i]").forEach((tr) => tr.addEventListener("click", () => { S.cpIndex = Number(tr.dataset.i); renderTimeline(); renderState(); }));
}

function renderState() {
  const cps = S.cps;
  if (!cps.length) { $("state").innerHTML = ""; return; }
  const i = S.cpIndex ?? cps.length - 1, c = cps[i], prev = cps[i - 1];
  $("state-title").textContent = `step ${c.step} 체크포인트 ${c.checkpoint_id.slice(0, 13)}…${S.cpIndex == null ? " (최신)" : ""}`;
  const keys = Object.keys(c.values);
  const kv = keys.map((k) => {
    const changed = !prev || JSON.stringify(prev.values[k]) !== JSON.stringify(c.values[k]);
    const val = typeof c.values[k] === "object" ? JSON.stringify(c.values[k]) : String(c.values[k]);
    return `<div class="k ${changed ? "changed" : ""} ${S.reducers.includes(k) ? "reducer" : ""}">${esc(k)}</div><div>${esc(val.length > 220 ? val.slice(0, 220) + "…" : val)}</div>`;
  }).join("");
  const writes = c.tasks.map((t) => `<details><summary>${esc(t.name)}${t.interrupts.length ? " ⏸ interrupt" : ""}${t.error ? " ✗" : ""} 가 쓴 값</summary>
    <pre>${esc(JSON.stringify(t.interrupts.length ? { interrupt: t.interrupts } : t.result, null, 1))}</pre></details>`).join("");
  $("state").innerHTML = `<div class="kv">${kv}</div>
    <p class="hint">노란 칸: 직전 체크포인트에서 바뀐 필드. 이 시점 상태에서 아래 task가 실행되어 다음 체크포인트가 된다.</p>${writes}`;
}

// --- 노드 상세: 단계 결과 펼침 ------------------------------------------------------------------

function metricTable(sets) {
  const names = Object.keys(sets.train?.summary?.mean_before || sets.validation?.summary?.mean_before || {});
  const cell = (s, m) => {
    if (!s?.summary?.n) return "<td>-</td>";
    const b = s.summary.mean_before[m], a = s.summary.mean_after[m], g = s.summary.mean_gain[m];
    return `<td class="num">${fmt(b)} → ${fmt(a)} <span class="${g >= 0 ? "up" : "down"}">(${signed(g)})</span></td>`;
  };
  return `<table><thead><tr><th>지표 (평균)</th><th>학습셋 챔피언 → 도전자</th><th>검증셋 챔피언 → 도전자</th></tr></thead><tbody>
    ${names.map((m) => `<tr><td>${esc(m)}</td>${cell(sets.train, m)}${cell(sets.validation, m)}</tr>`).join("")}</tbody></table>`;
}

function seedTable(sets, metric) {
  const rows = ["train", "validation"].flatMap((name) => (sets[name]?.scenarios || []).map((s) =>
    `<tr><td>${name}</td><td>${s.seed}</td><td>${esc(s.faults.join(",") || "-")}</td>
     <td class="num">${fmt(s.before[metric])} → ${fmt(s.after[metric])}</td><td class="num">${s.violations_after}</td></tr>`));
  return `<details><summary>seed별 ${esc(metric)}</summary><table><thead><tr><th>세트</th><th>seed</th><th>결함</th><th>챔피언 → 도전자</th><th>위반</th></tr></thead><tbody>${rows.join("")}</tbody></table></details>`;
}

function nodeTiming(node) {
  const ts = S.detail.timings.filter((t) => t.node === node);
  const usage = Object.entries(S.detail.usage).filter(([k]) => k.split(/[:#]/)[0] === node);
  return `<p class="hint">${ts.length ? `실행 ${ts.length}회 · ${ts.map((t) => `${t.task ? t.task + " " : ""}${t.seconds.toFixed(3)}s`).join(", ")}` : "노드 시간 기록 없음"}
    ${usage.length ? ` · LLM ${usage.map(([k, u]) => `${esc(k)}: ${u.llm_calls}회 ${u.input_tokens}/${u.output_tokens} 토큰${u.cost_usd != null ? " $" + u.cost_usd.toFixed(4) : ""}`).join(", ")}` : ""}</p>`;
}

function renderNode() {
  const n = S.node, st = S.detail?.stages || {};
  if (!n) return;
  const meta = S.graph.nodes.find((x) => x.id === n) || { label: n, stage: "" };
  $("node-title").textContent = `노드 상세: ${meta.label} (${n}) — ${meta.stage}`;
  let html = nodeTiming(n);
  const ex = st["1_execute"], an = st["2_analyze"], pr = st["3_propose"], va = st["4_validate"], ap = st["5_apply"];
  if (n === "execute" && ex) {
    html += `<table><thead><tr><th>seed</th><th>결함</th><th>건수</th><th>할당률</th><th>희망시간 일치</th><th>위반</th><th>실패 사유</th></tr></thead><tbody>
      ${ex.scenarios.map((s) => `<tr><td>${s.seed}</td><td>${esc(s.faults.join(","))}</td><td class="num">${s.items}</td><td class="num">${fmt(s.metrics.assignment_rate)}</td><td class="num">${fmt(s.metrics.desired_time_match_rate)}</td><td class="num">${s.violations}</td><td>${esc(JSON.stringify(s.reason_counts))}</td></tr>`).join("")}
      </tbody></table><p class="hint">분석·개선안 도출은 seed ${ex.primary?.seed}로 한다.</p>`;
  } else if ((n === "analyze" || n === "synthesize" || n === "perspective") && an) {
    if (n === "perspective") {
      html += (S.detail.run.analysis?.perspectives || []).map((p) => {
        const r = st[`2_analyze.${p.id}`];
        if (!r) return `<p>${esc(p.name)}: 아직 없음</p>`;
        if (r.error) return `<p class="err">${esc(p.name)}: ${esc(r.error)}</p>`;
        return `<details open><summary><b>${esc(p.prefix)} ${esc(p.name)}</b> <span class="hint">도구 ${esc(p.tools.join(", "))}</span></summary>
          <ul>${r.findings.map((f) => `<li><b>${esc(f.id)}</b> ${esc(f.title)} — ${esc(f.description)}</li>`).join("")}</ul>
          ${r.dropped.length ? `<p class="hint">근거 없음으로 제외 ${r.dropped.length}건</p>` : ""}</details>`;
      }).join("");
    } else {
      html += `<p>${esc(an.summary)}</p><ul>${an.findings.map((f) => `<li><b>${esc(f.id)}</b> ${esc(f.title)} — ${esc(f.description)}
        ${f.sources ? `<span class="hint">출처 ${esc(f.sources.join(", "))}</span>` : ""}
        ${f.slice ? `<span class="hint">구간 ${esc(JSON.stringify(f.slice))}</span>` : ""}${f.metric ? `<span class="hint">지표 ${esc(f.metric.name)} ${esc(f.metric.direction)}</span>` : ""}</li>`).join("")}</ul>
        ${an.dropped.map((d) => `<p class="err">제외: ${esc(d.finding.title)} — ${esc(d.problems.join("; "))}</p>`).join("")}`;
    }
  } else if ((n === "propose" || n === "retry") && pr) {
    const attempts = Object.keys(st).filter((k) => k.startsWith("3_propose.attempt")).sort();
    const block = (title, p, v) => `<details ${v ? "" : "open"}><summary><b>${esc(title)}</b></summary><ul>${p.proposals.map((x) =>
      `<li><b>${esc(x.id)}</b> ${esc(x.proposal.title)} <span class="hint">${esc(x.proposal.kind)}</span>
       ${x.errors.length ? `<span class="err">— ${esc(x.errors.join("; "))}</span>` : `<span class="pass">— 허용 범위 통과</span>`}
       <div class="hint">${esc(x.proposal.rationale || "")}</div></li>`).join("")}</ul>
       ${v ? `<p class="hint">판정: ${v.candidates.map((c) => `${esc(c.id)} ${c.eligible ? "통과" : "탈락 — " + esc(c.reasons.join("; "))}`).join(" / ") || "판정 대상 없음"}</p>` : ""}</details>`;
    html += attempts.map((k, i) => block(`시도 ${i + 1} (탈락, 보관됨)`, st[k], st[`4_validate.attempt${i}`])).join("");
    html += block(attempts.length ? `시도 ${attempts.length + 1} (현재)` : "개선안", pr, null);
  } else if (n === "validate" && va) {
    html += `<p class="hint">${esc(va.method)} · 세트 ${esc(va.scenario_set)} · 기준: 목표 ${esc(va.criteria.target?.metric)} ${signed(va.criteria.target?.min_delta, 3)} 이상 (${esc((va.criteria.target?.sets || []).join(", "))}), 과반 (${esc((va.criteria.target?.seed_majority || []).join(", "))}), 부작용 한도 ${va.criteria.side_effects?.length ? va.criteria.side_effects.length + "개" : "없음(기록만)"}</p>`;
    html += va.candidates.map((c) => `<details open><summary><b>${esc(c.id)}</b> ${esc(c.title)} — ${c.eligible ? '<span class="pass">판정 통과 → 승인 후보</span>' : '<span class="fail">판정 탈락</span>'}</summary>
      <ul>${c.judgement.checks.map((k) => `<li class="${k.passed ? "pass" : "fail"}">${k.passed ? "통과" : "탈락"} ${esc(k.detail)}</li>`).join("")}</ul>
      ${metricTable(c.sets)}${seedTable(c.sets, va.criteria.target?.metric || "assignment_rate")}</details>`).join("") || `<p class="hint">판정 대상 후보 없음</p>`;
    if (va.skipped.length) html += `<p class="hint">검사 탈락으로 판정에서 제외: ${va.skipped.map((s) => esc(s.id)).join(", ")}</p>`;
  } else if (["await_approval", "approval"].includes(n)) {
    const it = S.cps.flatMap((c) => c.tasks).find((t) => t.name === "approval" && t.interrupts.length);
    html += `<p>그래프는 이 노드에서 <code>interrupt()</code>로 멈추고 체크포인트를 남긴다. 승인·반려는 다른 프로세스(CLI·화면)에서 같은 thread를 재개한다.</p>
      ${it ? `<pre>${esc(JSON.stringify(it.interrupts[0], null, 1))}</pre>` : ""}${ap ? `<pre>${esc(JSON.stringify({ decision: ap.decision, by: ap.by, note: ap.note, proposal_id: ap.proposal_id }, null, 1))}</pre>` : ""}`;
  } else if (["apply", "reject", "auto_reject"].includes(n) && ap) {
    html += `<pre>${esc(JSON.stringify(ap, null, 1))}</pre>`;
  } else {
    html += `<p class="hint">이 실행에서 아직 기록이 없다.</p>`;
  }
  $("node-detail").innerHTML = html;
}

// --- 시작 ------------------------------------------------------------------------------------

$("start-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = ev.target.querySelector("button");
  btn.disabled = true;
  try {
    const { run_id } = await api("/api/runs", { method: "POST", body: JSON.stringify({ scenario: $("start-scenario").value, analysis: $("start-analysis").value }) });
    await selectRun(run_id);
  } catch (e) { alert(e.message); } finally { btn.disabled = false; }
});

(async function init() {
  S.graph = await api("/api/graph");
  await Promise.all([loadRuns(), loadModels(), loadScenarios()]);
  if (S.runs.length) selectRun(S.runs[0].run_id);
  setInterval(() => loadRuns().catch(() => {}), 4000);
  window.addEventListener("resize", () => drawGraph());
})();
