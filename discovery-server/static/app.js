import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

// --------------------------------------------------------------------------
// State
// --------------------------------------------------------------------------
let CONFIG = { num_boxes: 12, default_probes: 8, node_id: "" };
const boxes = {};            // box number -> { mesh, edges, label, A:{}, B:{} }
const history = {};          // box number -> [{ts, avg}]
const logRows = [];          // full reading log for the table + CSV
let selectedBox = null;
let trendChart = null, probeChart = null;

const TEMP_MIN = 15, TEMP_MAX = 80;   // °C range used for the color ramp

// --------------------------------------------------------------------------
// Three.js scene: a 4 x 3 grid of 12 rack boxes, each with 2 sensor markers
// --------------------------------------------------------------------------
const wrap = document.getElementById("canvas-wrap");
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0d1117);

const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 1000);
camera.position.set(9, 7, 12);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);
wrap.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.target.set(0, 0, 0);

scene.add(new THREE.AmbientLight(0xffffff, 0.6));
const dir = new THREE.DirectionalLight(0xffffff, 0.6);
dir.position.set(5, 10, 7);
scene.add(dir);
scene.add(new THREE.GridHelper(20, 20, 0x223040, 0x162028));

const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();

function tempColor(t) {
  // blue -> green -> yellow -> red
  const x = Math.max(0, Math.min(1, (t - TEMP_MIN) / (TEMP_MAX - TEMP_MIN)));
  const stops = [
    [0.0, [0x2f, 0x6d, 0xf7]],
    [0.4, [0x3f, 0xb9, 0x50]],
    [0.7, [0xf0, 0xc0, 0x00]],
    [1.0, [0xf8, 0x51, 0x49]],
  ];
  for (let i = 0; i < stops.length - 1; i++) {
    const [a, ca] = stops[i], [b, cb] = stops[i + 1];
    if (x >= a && x <= b) {
      const k = (x - a) / (b - a);
      const c = ca.map((v, j) => Math.round(v + (cb[j] - v) * k));
      return new THREE.Color(`rgb(${c[0]},${c[1]},${c[2]})`);
    }
  }
  return new THREE.Color(0x888888);
}

function makeTextSprite(text) {
  const cv = document.createElement("canvas");
  cv.width = 128; cv.height = 64;
  const ctx = cv.getContext("2d");
  ctx.fillStyle = "#e6edf3";
  ctx.font = "bold 40px sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, 64, 32);
  const tex = new THREE.CanvasTexture(cv);
  const spr = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true }));
  spr.scale.set(1.2, 0.6, 1);
  return spr;
}

function buildRack() {
  const cols = 4, rows = Math.ceil(CONFIG.num_boxes / cols);
  const gap = 3.2, w = 2, h = 1.6, d = 2;
  let n = 0;
  for (let r = 0; r < rows && n < CONFIG.num_boxes; r++) {
    for (let c = 0; c < cols && n < CONFIG.num_boxes; c++) {
      n++;
      const x = (c - (cols - 1) / 2) * gap;
      const z = (r - (rows - 1) / 2) * gap;

      const geo = new THREE.BoxGeometry(w, h, d);
      const mat = new THREE.MeshStandardMaterial({
        color: 0x30363d, transparent: true, opacity: 0.18,
      });
      const mesh = new THREE.Mesh(geo, mat);
      mesh.position.set(x, h / 2, z);
      mesh.userData.box = n;
      scene.add(mesh);

      const edges = new THREE.LineSegments(
        new THREE.EdgesGeometry(geo),
        new THREE.LineBasicMaterial({ color: 0x3fb950 })
      );
      edges.position.copy(mesh.position);
      scene.add(edges);

      // two sensor markers (slot A front-left, slot B back-right)
      const mk = (dx, dz, color) => {
        const s = new THREE.Mesh(
          new THREE.SphereGeometry(0.16, 16, 16),
          new THREE.MeshStandardMaterial({ color, emissive: color, emissiveIntensity: 0.4 })
        );
        s.position.set(x + dx, h / 2, z + dz);
        scene.add(s);
        return s;
      };
      const A = mk(-w / 2 + 0.3, -d / 2 + 0.3, 0x888888);
      const B = mk(w / 2 - 0.3, d / 2 - 0.3, 0x888888);

      const label = makeTextSprite("#" + n);
      label.position.set(x, h + 0.7, z);
      scene.add(label);

      boxes[n] = { mesh, edges, label, A: { marker: A, probes: null },
                   B: { marker: B, probes: null } };
      history[n] = [];
    }
  }
}

function updateBoxVisual(boxNum) {
  const box = boxes[boxNum];
  if (!box) return;
  const temps = [];
  for (const slot of ["A", "B"]) {
    if (box[slot].probes) temps.push(...box[slot].probes);
  }
  if (!temps.length) return;
  const avg = temps.reduce((a, b) => a + b, 0) / temps.length;
  const col = tempColor(avg);
  box.mesh.material.color = col;
  box.mesh.material.opacity = 0.28;
  box.edges.material.color = col;
  for (const slot of ["A", "B"]) {
    if (box[slot].probes) {
      const sAvg = box[slot].probes.reduce((a, b) => a + b, 0) / box[slot].probes.length;
      box[slot].marker.material.color = tempColor(sAvg);
      box[slot].marker.material.emissive = tempColor(sAvg);
    }
  }
}

// --------------------------------------------------------------------------
// Resize / render loop
// --------------------------------------------------------------------------
function resize() {
  const w = wrap.clientWidth, h = wrap.clientHeight;
  renderer.setSize(w, h);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
window.addEventListener("resize", resize);

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}

// click to open analytics
renderer.domElement.addEventListener("click", (e) => {
  const rect = renderer.domElement.getBoundingClientRect();
  pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hits = raycaster.intersectObjects(
    Object.values(boxes).map((b) => b.mesh)
  );
  if (hits.length) openDrawer(hits[0].object.userData.box);
});

// --------------------------------------------------------------------------
// Analytics drawer
// --------------------------------------------------------------------------
const drawer = document.getElementById("drawer");
document.getElementById("drawer-close").onclick = () => {
  drawer.classList.add("hidden");
  selectedBox = null;
};

function openDrawer(boxNum) {
  selectedBox = boxNum;
  document.getElementById("drawer-title").textContent = `Box #${boxNum}`;
  drawer.classList.remove("hidden");
  renderDrawer();
}

function renderDrawer() {
  if (selectedBox == null) return;
  const box = boxes[selectedBox];
  const all = [];
  for (const slot of ["A", "B"]) if (box[slot].probes) all.push(...box[slot].probes);

  const grid = document.getElementById("stat-grid");
  if (!all.length) {
    grid.innerHTML = `<div class="stat"><div class="label">status</div>
      <div class="val">no data</div></div>`;
  } else {
    const avg = (all.reduce((a, b) => a + b, 0) / all.length).toFixed(1);
    const max = Math.max(...all).toFixed(1);
    const min = Math.min(...all).toFixed(1);
    grid.innerHTML = `
      <div class="stat"><div class="label">avg °C</div><div class="val">${avg}</div></div>
      <div class="stat"><div class="label">max °C</div><div class="val">${max}</div></div>
      <div class="stat"><div class="label">min °C</div><div class="val">${min}</div></div>
      <div class="stat"><div class="label">probes</div><div class="val">${all.length}</div></div>`;
  }

  // trend chart (avg over time)
  const hist = history[selectedBox] || [];
  const labels = hist.map((p) => new Date(p.ts * 1000).toLocaleTimeString());
  const data = hist.map((p) => p.avg);
  trendChart = upsertChart(trendChart, "trend-chart", {
    type: "line",
    data: { labels, datasets: [{ label: "avg °C", data,
      borderColor: "#2f81f7", backgroundColor: "rgba(47,129,247,.15)",
      fill: true, tension: 0.3, pointRadius: 0 }] },
    options: chartOpts("avg over time"),
  });

  // per-probe bar (latest)
  probeChart = upsertChart(probeChart, "probe-chart", {
    type: "bar",
    data: {
      labels: all.map((_, i) => "P" + (i + 1)),
      datasets: [{ label: "°C", data: all,
        backgroundColor: all.map((t) => "#" + tempColor(t).getHexString()) }],
    },
    options: chartOpts("per-probe latest"),
  });
}

function chartOpts(title) {
  return {
    responsive: true,
    plugins: { legend: { display: false }, title: { display: false } },
    scales: {
      x: { ticks: { color: "#8b949e", maxTicksLimit: 6 }, grid: { color: "#1c2430" } },
      y: { ticks: { color: "#8b949e" }, grid: { color: "#1c2430" } },
    },
  };
}

function upsertChart(chart, canvasId, cfg) {
  if (chart) {
    chart.data = cfg.data;
    chart.update("none");
    return chart;
  }
  return new Chart(document.getElementById(canvasId), cfg);
}

// --------------------------------------------------------------------------
// Log table + CSV
// --------------------------------------------------------------------------
const tbody = document.querySelector("#log-table tbody");

function addLogRow(r) {
  logRows.push(r);
  const avg = (r.probes.reduce((a, b) => a + b, 0) / r.probes.length).toFixed(1);
  const max = Math.max(...r.probes).toFixed(1);
  const tr = document.createElement("tr");
  tr.className = "flash";
  tr.innerHTML = `
    <td>${logRows.length}</td>
    <td>${new Date(r.ts * 1000).toLocaleTimeString()}</td>
    <td>${r.box}</td>
    <td>${r.slot}</td>
    <td>${r.sensor_id}</td>
    <td>${r.probes.map((p) => p.toFixed(1)).join(", ")}</td>
    <td>${avg}</td>
    <td>${max}</td>`;
  tbody.prepend(tr);
  // cap DOM rows so a long session doesn't bloat the page
  while (tbody.children.length > 500) tbody.removeChild(tbody.lastChild);
  document.getElementById("row-count").textContent = `${logRows.length} readings`;
}

document.getElementById("export-csv").onclick = () => {
  const head = "idx,time,box,slot,sensor_id,probes,avg,max\n";
  const lines = logRows.map((r, i) => {
    const avg = (r.probes.reduce((a, b) => a + b, 0) / r.probes.length).toFixed(2);
    const max = Math.max(...r.probes).toFixed(2);
    const probes = `"${r.probes.map((p) => p.toFixed(2)).join(" ")}"`;
    return [i + 1, new Date(r.ts * 1000).toISOString(), r.box, r.slot,
            r.sensor_id, probes, avg, max].join(",");
  });
  const blob = new Blob([head + lines.join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "hvac_log.csv";
  a.click();
};

// --------------------------------------------------------------------------
// Apply an incoming reading
// --------------------------------------------------------------------------
function applyReading(r) {
  const box = boxes[r.box];
  if (box && (r.slot === "A" || r.slot === "B")) {
    box[r.slot].probes = r.probes;
    updateBoxVisual(r.box);
    const all = [];
    for (const s of ["A", "B"]) if (box[s].probes) all.push(...box[s].probes);
    const avg = all.reduce((a, b) => a + b, 0) / all.length;
    const h = history[r.box];
    h.push({ ts: r.ts, avg });
    if (h.length > 200) h.shift();
    if (selectedBox === r.box) renderDrawer();
  }
  addLogRow(r);
}

// --------------------------------------------------------------------------
// Long-poll loop (idle until data shows up)
// --------------------------------------------------------------------------
function setConn(ok) {
  const dot = document.getElementById("conn");
  const txt = document.getElementById("conn-text");
  dot.className = "dot " + (ok ? "on" : "off");
  txt.textContent = ok ? "live" : "reconnecting…";
}

async function pollLoop() {
  while (true) {
    try {
      const res = await fetch("/poll");
      setConn(true);
      const { reading } = await res.json();
      if (reading) applyReading(reading);
    } catch (e) {
      setConn(false);
      await new Promise((r) => setTimeout(r, 2000));
    }
  }
}

// --------------------------------------------------------------------------
// View switching
// --------------------------------------------------------------------------
document.querySelectorAll("nav button").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll("nav button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("view-" + btn.dataset.view).classList.add("active");
    if (btn.dataset.view === "mesh") resize();
  };
});

// --------------------------------------------------------------------------
// Boot
// --------------------------------------------------------------------------
async function boot() {
  try {
    CONFIG = await (await fetch("/config")).json();
    document.getElementById("node-id").textContent = "node: " + CONFIG.node_id;
  } catch (_) {}
  buildRack();
  resize();
  animate();

  // backfill recent history so the view isn't empty on load
  try {
    const { readings } = await (await fetch("/history?limit=200")).json();
    readings.reverse().forEach(applyReading);
  } catch (_) {}

  pollLoop();
}

boot();
