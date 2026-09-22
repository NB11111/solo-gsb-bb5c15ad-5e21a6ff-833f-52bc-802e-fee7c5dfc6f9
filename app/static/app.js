/* 无人机电池循环健康评估 — 前端逻辑(原生 JS) */
"use strict";

let currentBatteryId = null;
let currentAssessment = null;

const $ = (sel) => document.querySelector(sel);

function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "show" + (isError ? " error" : "");
  setTimeout(() => { el.className = ""; }, 3200);
}

async function api(path, options = {}) {
  const resp = await fetch(path, options);
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try { detail = (await resp.json()).detail || detail; } catch (_) { /* ignore */ }
    throw new Error(detail);
  }
  return resp.json();
}

const fmtT = (s) => {
  s = Math.round(s);
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
};
const fmtNum = (v, d = 2) => (v === null || v === undefined) ? "—" : Number(v).toFixed(d);

/* ---------------------------------------------------------------- 电池列表 */
async function loadBatteries() {
  const list = await api("/api/batteries");
  const ul = $("#battery-list");
  ul.innerHTML = "";
  for (const b of list) {
    const li = document.createElement("li");
    li.className = b.id === currentBatteryId ? "active" : "";
    li.innerHTML = `<strong>${esc(b.name)}</strong>
      <span class="meta">${b.rated_capacity_ah}Ah · ${b.safe_vmin}–${b.safe_vmax}V · ${b.cycle_count} 循环 / ${b.log_count} 日志</span>`;
    li.onclick = () => selectBattery(b.id);
    ul.appendChild(li);
  }
  if (!list.length) ul.innerHTML = '<li style="cursor:default;color:#63739a">暂无电池</li>';
}

async function selectBattery(id) {
  currentBatteryId = id;
  $("#empty-tip").hidden = true;
  $("#detail").hidden = false;
  await Promise.all([loadBatteries(), loadDetail(), loadLogs(), loadAssessment()]);
}

async function loadDetail() {
  const b = await api(`/api/batteries/${currentBatteryId}`);
  $("#bat-name").textContent = b.name;
  const f = $("#settings-form");
  f.rated_capacity_ah.value = b.rated_capacity_ah;
  f.safe_vmin.value = b.safe_vmin;
  f.safe_vmax.value = b.safe_vmax;
}

/* ---------------------------------------------------------------- 日志 */
async function loadLogs() {
  const logs = await api(`/api/batteries/${currentBatteryId}/logs`);
  const table = $("#logs-table");
  const tbody = table.querySelector("tbody");
  tbody.innerHTML = "";
  table.hidden = logs.length === 0;
  for (const l of logs) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${l.id}</td><td>${esc(l.filename)}</td>
      <td>${l.row_count}${l.bad_rows ? ` (+${l.bad_rows} 坏行)` : ""}</td>
      <td>${fmtT(l.duration_s)}</td><td>${l.cycle_count}</td>
      <td><button class="danger" data-log="${l.id}">删除</button></td>`;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll("button").forEach((btn) => {
    btn.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`确认删除日志 ${btn.dataset.log} 及其循环数据?`)) return;
      await api(`/api/logs/${btn.dataset.log}`, { method: "DELETE" });
      toast("日志已删除");
      await Promise.all([loadLogs(), loadAssessment(), loadBatteries()]);
    };
  });
}

/* ---------------------------------------------------------------- 评估渲染 */
async function loadAssessment() {
  currentAssessment = await api(`/api/batteries/${currentBatteryId}/assessment`);
  renderSummary(currentAssessment.summary);
  renderCycles(currentAssessment.cycles);
}

function renderSummary(s) {
  const card = $("#summary-card");
  if (!s.cycle_count) { card.hidden = true; return; }
  card.hidden = false;

  const label = { good: "状态良好", attention: "需要关注", risk: "存在风险" }[s.health];
  const banner = $("#health-banner");
  banner.className = s.health;
  banner.innerHTML = `<strong>健康等级:${label}</strong>
    <ul>${s.health_reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>`;

  const stats = [
    [fmtNum(s.soh_latest_pct, 1) + (s.soh_latest_pct != null ? " %" : ""), "最新 SoH"],
    [s.soh_delta_pct != null ? (s.soh_delta_pct > 0 ? "+" : "") + fmtNum(s.soh_delta_pct, 1) + " %" : "—", "SoH 变化"],
    [s.cycle_count, "总循环数"],
    [s.unclosed_count, "未闭合循环"],
    [s.ir_avg_ohm != null ? fmtNum(s.ir_avg_ohm * 1000, 1) + " mΩ" : "—", "平均内阻"],
    [fmtNum(s.temp_max_ever_c, 1) + " °C", "历史最高温"],
    [s.capacity_slope_ah_per_cycle != null ? fmtNum(s.capacity_slope_ah_per_cycle, 4) + " Ah/循环" : "—", "容量衰减斜率"],
    [Object.entries(s.flag_counts).map(([k, v]) => `${k}×${v}`).join("  ") || "无", "判定统计"],
  ];
  $("#stat-grid").innerHTML = stats
    .map(([v, k]) => `<div class="stat"><div class="v">${v}</div><div class="k">${k}</div></div>`)
    .join("");

  const discharge = currentAssessment.cycles.filter((c) => c.kind === "discharge" && c.closed);
  drawChart($("#chart-soh"), discharge.map((c) => c.soh_pct), "%", "#5da9ff");
  drawChart($("#chart-ir"),
    currentAssessment.cycles.filter((c) => c.ir_ohm != null).map((c) => c.ir_ohm * 1000),
    "mΩ", "#ffb35d");
}

function renderCycles(cycles) {
  const card = $("#cycles-card");
  card.hidden = cycles.length === 0;
  $("#flag-detail").hidden = true;
  const tbody = $("#cycles-table tbody");
  tbody.innerHTML = "";
  cycles.forEach((c, idx) => {
    const tr = document.createElement("tr");
    const flags = c.flags.map((f) =>
      `<span class="badge ${f.severity === "critical" ? "crit" : "warn"}">${esc(f.title)}</span>`).join("");
    tr.innerHTML = `
      <td>${c.seq}</td>
      <td>${c.kind === "discharge" ? "放电" : "充电"}</td>
      <td>${fmtT(c.start_t)}–${fmtT(c.end_t)}</td>
      <td>${fmtT(c.duration_s)}</td>
      <td>${fmtNum(c.capacity_ah, 3)}</td>
      <td>${fmtNum(c.soh_pct, 1)}</td>
      <td>${c.ir_ohm != null ? fmtNum(c.ir_ohm * 1000, 1) : "—"}</td>
      <td>${fmtNum(c.temp_max, 1)}</td>
      <td>${fmtNum(c.v_min, 2)}–${fmtNum(c.v_max, 2)}</td>
      <td>${c.closed ? '<span class="badge ok">闭合</span>' : '<span class="badge warn">未闭合</span>'}</td>
      <td>${flags || '<span class="badge info">正常</span>'}</td>`;
    tr.onclick = () => {
      tbody.querySelectorAll("tr").forEach((r) => r.classList.remove("selected"));
      tr.classList.add("selected");
      showFlags(c);
    };
    tbody.appendChild(tr);
  });
}

function showFlags(c) {
  const box = $("#flag-detail");
  if (!c.flags.length) {
    box.hidden = false;
    box.innerHTML = `<div class="flag-item"><div class="title">第 ${c.seq} 循环</div>
      <div class="snippet">所有指标均在设定阈值内, 无异常判定。</div></div>`;
    return;
  }
  box.hidden = false;
  box.innerHTML = c.flags.map((f) => `
    <div class="flag-item ${f.severity}">
      <div class="title">[${f.severity === "critical" ? "严重" : "警告"}] ${esc(f.title)}
        <span class="badge info">${esc(f.code)}</span></div>
      <div class="snippet">${esc(f.snippet)}</div>
      <pre>${esc(JSON.stringify(f.evidence, null, 2))}</pre>
    </div>`).join("");
}

/* ---------------------------------------------------------------- 简易折线图 */
function drawChart(canvas, values, unit, color) {
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height, pad = 34;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = "#7d8db3";
  ctx.font = "11px sans-serif";
  if (!values.length || values.some((v) => v == null)) {
    ctx.fillText("暂无数据", W / 2 - 20, H / 2);
    return;
  }
  const min = Math.min(...values), max = Math.max(...values);
  const lo = min === max ? min - 1 : min, hi = min === max ? max + 1 : max;
  const x = (i) => pad + (values.length === 1 ? (W - 2 * pad) / 2 : i * (W - 2 * pad) / (values.length - 1));
  const y = (v) => H - pad - (v - lo) * (H - 2 * pad) / (hi - lo);

  ctx.strokeStyle = "#2a3a5f";
  ctx.beginPath();
  for (let g = 0; g <= 3; g++) {
    const gy = pad + g * (H - 2 * pad) / 3;
    ctx.moveTo(pad, gy); ctx.lineTo(W - pad, gy);
    ctx.fillText((hi - g * (hi - lo) / 3).toFixed(1), 4, gy + 4);
  }
  ctx.stroke();

  ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath();
  values.forEach((v, i) => { i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)); });
  ctx.stroke();
  ctx.fillStyle = color;
  values.forEach((v, i) => {
    ctx.beginPath(); ctx.arc(x(i), y(v), 3, 0, Math.PI * 2); ctx.fill();
    ctx.fillText(`${v.toFixed(1)}`, x(i) - 10, y(v) - 8);
  });
  ctx.fillStyle = "#7d8db3";
  ctx.fillText(unit, W - pad, 14);
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------------------------------------------------------- 事件绑定 */
$("#create-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    const b = await api("/api/batteries", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: f.name.value.trim(),
        rated_capacity_ah: parseFloat(f.rated_capacity_ah.value),
        safe_vmin: parseFloat(f.safe_vmin.value),
        safe_vmax: parseFloat(f.safe_vmax.value),
      }),
    });
    f.reset();
    toast(`电池「${b.name}」已创建`);
    await selectBattery(b.id);
  } catch (err) { toast(err.message, true); }
};

$("#settings-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    await api(`/api/batteries/${currentBatteryId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        rated_capacity_ah: parseFloat(f.rated_capacity_ah.value),
        safe_vmin: parseFloat(f.safe_vmin.value),
        safe_vmax: parseFloat(f.safe_vmax.value),
      }),
    });
    toast("设置已保存, 评估已更新");
    await Promise.all([loadAssessment(), loadBatteries(), loadDetail()]);
  } catch (err) { toast(err.message, true); }
};

$("#upload-btn").onclick = async () => {
  const input = $("#csv-file");
  if (!input.files.length) { toast("请先选择 CSV 文件", true); return; }
  const fd = new FormData();
  fd.append("file", input.files[0]);
  try {
    const r = await api(`/api/batteries/${currentBatteryId}/logs`, { method: "POST", body: fd });
    $("#upload-result").textContent =
      `✔ ${r.filename}: ${r.row_count} 行, 时长 ${fmtT(r.duration_s)}, 识别 ${r.cycle_count} 个循环` +
      (r.unclosed_count ? `(含 ${r.unclosed_count} 个未闭合)` : "") +
      (r.flipped_current ? "; 已自动翻转电流符号" : "");
    input.value = "";
    await Promise.all([loadLogs(), loadAssessment(), loadBatteries()]);
  } catch (err) { toast(err.message, true); }
};

$("#export-btn").onclick = () => {
  window.location = `/api/batteries/${currentBatteryId}/export`;
};

$("#delete-bat-btn").onclick = async () => {
  if (!confirm("确认删除该电池及其全部日志与循环数据?")) return;
  await api(`/api/batteries/${currentBatteryId}`, { method: "DELETE" });
  currentBatteryId = null;
  $("#detail").hidden = true;
  $("#empty-tip").hidden = false;
  await loadBatteries();
};

loadBatteries();
