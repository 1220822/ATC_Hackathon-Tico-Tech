// The house in 3D: Three.js scene fed by /twin/state + /twin/stream (SSE).
// Only shows what get_home reports (rooms and devices); the layout only says where things are.
import * as THREE from "three";
import { OrbitControls } from "three/addons/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/addons/CSS2DRenderer.js";

// ------------------------------------------------------------------ constants / helpers
const WALL_T = 0.08;
const POLL_FALLBACK_MS = 2000;
const STREAM_STALE_MS = 20000;
const BALLOON_MS = 6500;
const nf1 = new Intl.NumberFormat("pt-PT", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const nf0 = new Intl.NumberFormat("pt-PT", { maximumFractionDigits: 0 });
const TYPE_PT = {
  temperature: "Sensor de temperatura", contact: "Sensor de contacto", light: "Lâmpada", plug: "Tomada (aquecedor)",
  smoke: "Detetor de fumo", hub: "Central de segurança", camera: "Câmara",
};
const MODE_PT = { DISARMED: "Desarmada", STAY: "Armada (em casa)", AWAY: "Armada (ausente)" };
const AREA_VAR = {
  "Segurança": "--a-seguranca", "Energia": "--a-energia", "Conforto": "--a-conforto", "Emergência": "--a-emergencia",
  "Coordenador": "--a-coordenador", "Sistema": "--a-sistema",
};
// events that only repeat a value already visible on a label: timeline yes, 3D balloon no
const QUIET = new Set(["temperature_changed", "humidity_changed", "plug_power_changed", "device_online"]);
const HISTORY = { temperature: ["temperature", "°C"], plug: ["power_w", "W"], contact: ["open", ""], light: ["on", ""] };

const $ = (id) => document.getElementById(id);
function el(tag, cls, ...children) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  for (const c of children) if (c != null) e.append(c);  // strings become text nodes, never HTML
  return e;
}
const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString("pt-PT", { hour: "2-digit", minute: "2-digit" });
const hhmmss = (ts) => new Date(ts * 1000).toLocaleTimeString("pt-PT");
function ago(ts) {
  if (!ts) return "nunca";
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "há instantes";
  if (s < 3600) return `há ${Math.round(s / 60)} min`;
  if (s < 86400) return `há ${Math.round(s / 3600)} h`;
  return new Date(ts * 1000).toLocaleString("pt-PT", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}
const areaColor = (area) => `var(${AREA_VAR[area] || "--a-sistema"})`;

// ------------------------------------------------------------------ renderer / scene
const host = $("cena");
const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "low-power" });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));  // light on integrated GPUs
renderer.setClearColor(0x0b0f15);
host.append(renderer.domElement);
const labels = new CSS2DRenderer();
Object.assign(labels.domElement.style, { position: "absolute", top: "0", left: "0", pointerEvents: "none" });
host.append(labels.domElement);

const scene = new THREE.Scene();
scene.fog = new THREE.Fog(0x0b0f15, 30, 60);
const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 200);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.maxPolarAngle = Math.PI * 0.47;
controls.minDistance = 2;
controls.maxDistance = 40;

scene.add(new THREE.HemisphereLight(0xdde6ff, 0x2a2f38, 1.6));
const sun = new THREE.DirectionalLight(0xffffff, 1.1);
sun.position.set(-6, 12, 9);
scene.add(sun);
const ground = new THREE.Mesh(new THREE.PlaneGeometry(200, 200), new THREE.MeshStandardMaterial({ color: 0x111720, roughness: 1 }));
ground.rotation.x = -Math.PI / 2;
ground.position.y = -0.01;
scene.add(ground);

function resize() {
  const w = host.clientWidth, h = host.clientHeight;
  renderer.setSize(w, h);
  labels.setSize(w, h);
  camera.aspect = w / Math.max(h, 1);
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(host);

// shared geometries / materials
const GEO = {
  box: new THREE.BoxGeometry(1, 1, 1),
  sphere: new THREE.SphereGeometry(1, 20, 14),
  cyl: new THREE.CylinderGeometry(1, 1, 1, 20),
  hit: new THREE.SphereGeometry(0.28, 10, 8),
};
const HIT_MAT = new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false });
const mat = (color, extra = {}) => new THREE.MeshStandardMaterial({ color, roughness: 0.55, ...extra });
function part(geo, material, sx, sy, sz, x = 0, y = 0, z = 0) {
  const m = new THREE.Mesh(geo, material);
  m.scale.set(sx, sy, sz);
  m.position.set(x, y, z);
  return m;
}
function label(text, cls = "rotulo") {
  const div = el("div", cls, text);
  const obj = new CSS2DObject(div);
  obj.userData.div = div;
  return obj;
}

// ------------------------------------------------------------------ world model
let world = null;  // built from a state; rebuilt when rooms/devices/layout change
let current = null;  // latest state
let selected = null;  // selected device id

function structureKey(st) {
  return JSON.stringify([st.rooms.map((r) => [r.id, r.name, r.x, r.z, r.width, r.depth, r.height, r.walls]),
    st.devices.map((d) => [d.id, d.role, d.position, d.rotation_y, d.opening, d.target, d.view_radius])]);
}

function disposeWorld() {
  if (!world) return;
  scene.remove(world.root);
  world.root.traverse((o) => {
    if (o.isCSS2DObject) o.element.remove();
    if (o.material && o.material !== HIT_MAT) o.material.dispose?.();
    if (o.geometry && !Object.values(GEO).includes(o.geometry)) o.geometry.dispose?.();
  });
}

function wallFrame(room, side) {
  // origin of the wall (start), direction along it, inward normal, length
  const { x, z, width: w, depth: d } = room;
  switch (side) {
    case "north": return { o: new THREE.Vector3(x, 0, z), dir: new THREE.Vector3(1, 0, 0), n: new THREE.Vector3(0, 0, 1), len: w };
    case "south": return { o: new THREE.Vector3(x, 0, z + d), dir: new THREE.Vector3(1, 0, 0), n: new THREE.Vector3(0, 0, -1), len: w };
    case "west": return { o: new THREE.Vector3(x, 0, z), dir: new THREE.Vector3(0, 0, 1), n: new THREE.Vector3(1, 0, 0), len: d };
    default: return { o: new THREE.Vector3(x + w, 0, z), dir: new THREE.Vector3(0, 0, 1), n: new THREE.Vector3(-1, 0, 0), len: d };
  }
}

function buildWorld(st) {
  disposeWorld();
  const root = new THREE.Group();
  const w = { root, rooms: {}, devices: {}, openings: {}, wallEdges: [], hits: [], floors: [] };
  const wallMat = new THREE.MeshStandardMaterial({ color: 0xc9d3e6, transparent: true, opacity: 0.16, depthWrite: false, side: THREE.DoubleSide });
  const edgeMat = new THREE.LineBasicMaterial({ color: 0x8592aa, transparent: true, opacity: 0.55 });
  w.edgeMat = edgeMat;

  for (const r of st.rooms) {
    const g = new THREE.Group();
    const floorMat = mat(0x394150, { roughness: 0.9 });
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(r.width, r.depth), floorMat);
    floor.rotation.x = -Math.PI / 2;
    floor.position.set(r.x + r.width / 2, 0.005, r.z + r.depth / 2);
    floor.userData.roomId = r.id;
    g.add(floor);
    w.floors.push(floor);

    for (const wall of r.walls) {
      const f = wallFrame(r, wall.side);
      const ops = [...wall.openings].sort((a, b) => a.offset - b.offset);
      const pieces = [];  // [start, end, y0, y1]
      let cursor = 0;
      for (const o of ops) {
        const a = o.offset - o.width / 2, b = o.offset + o.width / 2;
        if (a > cursor) pieces.push([cursor, a, 0, r.height]);
        if (o.sill > 0) pieces.push([a, b, 0, o.sill]);
        if (o.sill + o.height < r.height) pieces.push([a, b, o.sill + o.height, r.height]);
        cursor = b;
        buildOpening(w, g, r, f, o);
      }
      if (cursor < f.len) pieces.push([cursor, f.len, 0, r.height]);
      for (const [a, b, y0, y1] of pieces) {
        const len = b - a, h = y1 - y0;
        const geo = new THREE.BoxGeometry(f.dir.x ? len : WALL_T, h, f.dir.z ? len : WALL_T);
        const m = new THREE.Mesh(geo, wallMat);
        m.position.copy(f.o).addScaledVector(f.dir, (a + b) / 2);
        m.position.y = y0 + h / 2;
        g.add(m);
        const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geo), edgeMat);
        edges.position.copy(m.position);
        g.add(edges);
      }
    }

    // smoke: red volume + particles (hidden until needed)
    const fog = part(GEO.box, new THREE.MeshBasicMaterial({ color: 0xff2a2a, transparent: true, opacity: 0, depthWrite: false }),
      r.width - 0.1, r.height - 0.05, r.depth - 0.1, r.x + r.width / 2, r.height / 2, r.z + r.depth / 2);
    fog.visible = false;
    g.add(fog);
    const particles = smokeParticles(r);
    g.add(particles);

    const name = label(null, "rotulo-divisao");
    name.userData.div.append(r.name, el("small", null, ""));
    name.userData.div.addEventListener("click", () => focusRoom(r.id));
    name.position.set(r.x + r.width / 2, r.height + 0.45, r.z + r.depth / 2);
    g.add(name);
    w.rooms[r.id] = { group: g, floor, floorMat, fog, particles, name, data: r };
    root.add(g);
  }

  for (const d of st.devices) buildDevice(w, d, st);
  scene.add(root);
  world = w;
  w.key = structureKey(st);
  w.bounds = houseBounds(st);
}

function buildOpening(w, g, room, f, o) {
  const hinge = f.o.clone().addScaledVector(f.dir, o.offset - o.width / 2);
  hinge.y = o.sill;
  const pivot = new THREE.Group();
  pivot.position.copy(hinge);
  pivot.rotation.y = f.dir.z ? -Math.PI / 2 : 0;  // local +x runs along the wall
  const isWindow = o.type === "window";
  const leafMat = isWindow
    ? new THREE.MeshStandardMaterial({ color: 0x9fd0ff, transparent: true, opacity: 0.38, roughness: 0.1, depthWrite: false })
    : new THREE.MeshStandardMaterial({ color: 0x7d8798, transparent: true, opacity: 0.5, depthWrite: false });
  const leaf = part(GEO.box, leafMat, o.width, o.height, isWindow ? 0.03 : 0.05, o.width / 2, o.height / 2, 0);
  pivot.add(leaf);
  const frame = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(o.width, o.height, 0.04)),
    new THREE.LineBasicMaterial({ color: isWindow ? 0xcfe6ff : 0x9aa3b2 }));
  frame.position.set(o.width / 2, o.height / 2, 0);
  pivot.add(frame);
  g.add(pivot);
  // which swing direction goes into the room
  const base = pivot.rotation.y;
  const along = new THREE.Vector3(1, 0, 0).applyAxisAngle(new THREE.Vector3(0, 1, 0), base + 0.5);
  const swing = along.dot(f.n) > 0 ? 1 : -1;
  const open = isWindow ? 0 : swing * 1.35;  // doors drawn ajar, windows closed until a sensor says otherwise
  pivot.rotation.y = base + open;
  w.openings[o.id] = { pivot, leafMat, base, swing, target: base + open, type: o.type, baseColor: leafMat.color.clone() };
}

function smokeParticles(r) {
  const n = 140;
  const pos = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) {
    pos[i * 3] = r.x + 0.2 + Math.random() * (r.width - 0.4);
    pos[i * 3 + 1] = Math.random() * r.height;
    pos[i * 3 + 2] = r.z + 0.2 + Math.random() * (r.depth - 0.4);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const ctx = c.getContext("2d");
  const grd = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
  grd.addColorStop(0, "rgba(220,220,220,1)");
  grd.addColorStop(1, "rgba(220,220,220,0)");
  ctx.fillStyle = grd;
  ctx.fillRect(0, 0, 64, 64);
  const pts = new THREE.Points(geo, new THREE.PointsMaterial({
    size: 0.55, map: new THREE.CanvasTexture(c), transparent: true, opacity: 0.55, depthWrite: false, color: 0xbbbbbb,
  }));
  pts.visible = false;
  pts.userData.room = r;
  return pts;
}

function buildDevice(w, d, st) {
  const g = new THREE.Group();
  g.position.fromArray(d.position);
  g.rotation.y = THREE.MathUtils.degToRad(d.rotation_y || 0);
  const parts = new THREE.Group();
  g.add(parts);
  const white = mat(0xf2f4f8);
  const extra = {};
  switch (d.role) {
    case "temperature":
      parts.add(part(GEO.box, white, 0.14, 0.14, 0.035));
      parts.add(part(GEO.box, mat(0x2b3240), 0.09, 0.05, 0.01, 0, 0.02, 0.02));
      extra.label = label("—");
      extra.label.position.set(0, 0.32, 0);
      break;
    case "contact":
      parts.add(part(GEO.box, white, 0.05, 0.14, 0.035));
      parts.add(part(GEO.box, white, 0.05, 0.06, 0.035, 0.07, 0, 0));
      break;
    case "light": {
      const bulbMat = mat(0xffffff, { emissive: 0xffffff, emissiveIntensity: 1 });
      parts.add(part(GEO.cyl, mat(0x777f8e), 0.012, 0.12, 0.012, 0, 0.06, 0));
      parts.add(part(GEO.sphere, bulbMat, 0.1, 0.12, 0.1, 0, -0.06, 0));
      const light = new THREE.PointLight(0xffffff, 0, 9, 1.4);
      light.position.set(0, -0.25, 0);
      g.add(light);
      Object.assign(extra, { bulbMat, light });
      break;
    }
    case "plug": {
      parts.add(part(GEO.box, white, 0.11, 0.11, 0.03));
      const holes = mat(0x30363f);
      parts.add(part(GEO.cyl, holes, 0.012, 0.02, 0.012, -0.022, 0, 0.016).rotateX(Math.PI / 2));
      parts.add(part(GEO.cyl, holes, 0.012, 0.02, 0.012, 0.022, 0, 0.016).rotateX(Math.PI / 2));
      const glowMat = new THREE.MeshBasicMaterial({ color: 0xffa640, transparent: true, opacity: 0, depthWrite: false });
      const glow = part(GEO.sphere, glowMat, 0.2, 0.2, 0.2);
      g.add(glow);
      extra.glow = glow;
      extra.label = label("—");
      extra.label.position.set(0, 0.3, 0);
      break;
    }
    case "smoke": {
      parts.add(part(GEO.cyl, white, 0.13, 0.05, 0.13));
      const ledMat = new THREE.MeshBasicMaterial({ color: 0x3ccf7f });
      parts.add(part(GEO.sphere, ledMat, 0.015, 0.015, 0.015, 0.06, -0.026, 0));
      extra.ledMat = ledMat;
      break;
    }
    case "hub":
      parts.add(part(GEO.box, white, 0.26, 0.06, 0.18));
      parts.add(part(GEO.cyl, mat(0x6aa8ff, { emissive: 0x2a4f88 }), 0.05, 0.01, 0.05, 0, 0.035, 0));
      extra.label = label("—");
      extra.label.position.set(0, 0.32, 0);
      break;
    case "camera": {
      parts.add(part(GEO.box, mat(0x2b3038), 0.12, 0.09, 0.16));
      parts.add(part(GEO.cyl, mat(0x111111), 0.035, 0.03, 0.035, 0, 0, 0.09).rotateX(Math.PI / 2));
      if (d.target) {
        const from = new THREE.Vector3().fromArray(d.position), to = new THREE.Vector3().fromArray(d.target);
        const dir = to.clone().sub(from);
        const len = dir.length();
        // view_radius: the cone reaches the farthest corner of the room (the camera covers it all)
        const geo = new THREE.ConeGeometry(d.view_radius || len * 0.3, len, 28, 1, true);
        geo.translate(0, -len / 2, 0);  // apex at the camera
        const coneMat = new THREE.MeshBasicMaterial({ color: 0x8fb4e8, transparent: true, opacity: 0.08, depthWrite: false, side: THREE.DoubleSide });
        const cone = new THREE.Mesh(geo, coneMat);
        cone.position.copy(from);
        cone.quaternion.setFromUnitVectors(new THREE.Vector3(0, -1, 0), dir.normalize());
        w.root.add(cone);
        extra.coneMat = coneMat;
      }
      break;
    }
    default:
      parts.add(part(GEO.box, mat(0x9aa3b2), 0.12, 0.12, 0.12));
  }
  const hit = new THREE.Mesh(GEO.hit, HIT_MAT);
  hit.userData.deviceId = d.id;
  g.add(hit);
  w.hits.push(hit);
  if (extra.label) g.add(extra.label);
  const status = label("", "rotulo offline");
  status.position.set(0, -0.28, 0);
  status.visible = false;
  g.add(status);
  // remember original materials for the offline look
  const mats = [];
  parts.traverse((o) => { if (o.material) mats.push({ m: o.material, color: o.material.color.clone(), opacity: o.material.opacity, transparent: o.material.transparent }); });
  w.devices[d.id] = { group: g, parts, mats, status, data: d, ...extra };
  w.root.add(g);
}

function houseBounds(st) {
  const box = new THREE.Box3();
  for (const r of st.rooms) {
    box.expandByPoint(new THREE.Vector3(r.x, 0, r.z));
    box.expandByPoint(new THREE.Vector3(r.x + r.width, r.height, r.z + r.depth));
  }
  if (box.isEmpty()) box.set(new THREE.Vector3(-2, 0, -2), new THREE.Vector3(2, 2, 2));
  return box;
}

// ------------------------------------------------------------------ camera moves
let tween = null;
function moveCamera(target, position, ms = 800) {
  tween = { t0: performance.now(), ms, fromT: controls.target.clone(), toT: target, fromP: camera.position.clone(), toP: position };
}
function viewHouse(instant = false) {
  if (!world) return;
  const c = world.bounds.getCenter(new THREE.Vector3());
  const size = world.bounds.getSize(new THREE.Vector3());
  const span = Math.max(size.x, size.z, 4);
  const target = new THREE.Vector3(c.x, 0.6, c.z);
  const pos = new THREE.Vector3(c.x + span * 0.35, span * 0.95, c.z + span * 1.15);
  if (instant) { controls.target.copy(target); camera.position.copy(pos); controls.update(); }
  else moveCamera(target, pos);
}
function focusRoom(id) {
  const r = world?.rooms[id]?.data;
  if (!r) return;
  const c = new THREE.Vector3(r.x + r.width / 2, 0.8, r.z + r.depth / 2);
  const span = Math.max(r.width, r.depth);
  moveCamera(c, new THREE.Vector3(c.x + span * 0.3, span * 1.35, c.z + span * 1.4));
}
$("ver-casa").addEventListener("click", () => viewHouse());

// ------------------------------------------------------------------ applying state
// diverging scale (dataviz): cold blue <-> neutral grey at the comfort temperature <-> warm red.
const HEAT = { cold: new THREE.Color(0x3987e5), mid: new THREE.Color(0x3a404b), warm: new THREE.Color(0xe66767) };
function heatColor(t, disp) {
  if (t == null) return HEAT.mid.clone();
  if (t <= disp.heat_mid_c) {
    return HEAT.mid.clone().lerp(HEAT.cold, THREE.MathUtils.clamp((disp.heat_mid_c - t) / (disp.heat_mid_c - disp.heat_min_c), 0, 1));
  }
  return HEAT.mid.clone().lerp(HEAT.warm, THREE.MathUtils.clamp((t - disp.heat_mid_c) / (disp.heat_max_c - disp.heat_mid_c), 0, 1));
}

function applyState(st) {
  current = st;
  if (!world || world.key !== structureKey(st)) {
    const first = !world;
    buildWorld(st);
    if (first) viewHouse(true);
  }
  renderHeader(st);
  const disp = st.display;
  for (const r of st.rooms) {
    const wr = world.rooms[r.id];
    wr.data = r;
    const heat = r.temperature_c == null ? null : heatColor(r.temperature_c, disp);
    wr.floorMat.color.copy(heat ? heat.clone().multiplyScalar(0.62) : new THREE.Color(0x2a313c));
    wr.floorMat.emissive.copy(heat ? heat.clone().multiplyScalar(0.18) : new THREE.Color(0x000000));
    wr.smoke = r.smoke;
    wr.fog.visible = r.smoke;
    wr.particles.visible = r.smoke;
    wr.name.userData.div.lastChild.textContent = r.temperature_c == null ? "" : `${nf1.format(r.temperature_c)} °C`;
  }
  for (const d of st.devices) applyDevice(world.devices[d.id], d, st);
  if (selected) renderPanel();
}

function applyDevice(wd, d, st) {
  if (!wd) return;
  wd.data = d;
  const v = d.values || {};
  // offline look: grey and see-through, with the time of the last message
  for (const { m, color, opacity, transparent } of wd.mats) {
    if (d.online) { m.color.copy(color); m.opacity = opacity; m.transparent = transparent; }
    else { m.color.set(0x6b7280); m.opacity = 0.35; m.transparent = true; }
  }
  wd.status.visible = !d.online;
  wd.status.userData.div.textContent = `offline · ${d.last_seen ? hhmm(d.last_seen) : "sem dados"}`;

  switch (d.role) {
    case "temperature":
      wd.label.userData.div.textContent = v.temperature_c == null ? "—"
        : `${nf1.format(v.temperature_c)} °C · ${v.humidity_pct == null ? "—" : nf0.format(v.humidity_pct)} %`;
      break;
    case "contact": {
      const op = d.opening && world.openings[d.opening];
      if (op) {
        op.target = op.base + (v.open ? op.swing * 1.2 : 0);
        const amber = v.open && st.security.armed;
        op.leafMat.color.copy(amber ? new THREE.Color(0xfab219) : op.baseColor);
        op.leafMat.opacity = amber ? 0.7 : 0.38;
      }
      break;
    }
    case "light": {
      const on = d.online && v.on;
      const rgb = v.color_rgb || [255, 255, 255];
      const color = new THREE.Color(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255);
      const b = (v.brightness ?? 100) / 100;
      wd.light.color.copy(color);
      wd.light.intensity = on ? 0.6 + 7 * b : 0;
      if (d.online) {
        wd.bulbMat.color.copy(on ? color : new THREE.Color(0x9aa3b2));
        wd.bulbMat.emissive.copy(on ? color : new THREE.Color(0x000000));
        wd.bulbMat.emissiveIntensity = on ? 0.4 + 1.6 * b : 0;
      } else wd.bulbMat.emissiveIntensity = 0;
      break;
    }
    case "plug": {
      const p = v.power_w;
      wd.active = d.online && v.on && p != null && p > st.display.plug_active_w;
      wd.label.userData.div.textContent = `${v.on ? "Ligada" : "Desligada"} · ${p == null ? "—" : nf0.format(p)} W`;
      wd.label.userData.div.className = "rotulo" + (wd.active ? " quente" : "");
      break;
    }
    case "smoke":
      wd.alarm = !!v.alarm;
      if (!wd.alarm) wd.ledMat.color.set(0x3ccf7f);
      break;
    case "hub": {
      const s = st.security;
      const div = wd.label.userData.div;
      div.textContent = st.alarm.any ? "EM ALARME" : (MODE_PT[s.status] || s.status);
      div.className = "rotulo " + (st.alarm.any ? "modo-alarme" : s.armed ? "modo-armada" : "modo-desarmada");
      break;
    }
    case "camera":
      wd.active = d.online && (v.motion || v.sound);
      break;
  }
}

function renderHeader(st) {
  $("casa-nome").textContent = st.home_name || "";
  const modo = $("modo");
  modo.textContent = (st.mode === "simulado" ? "Casa simulada" : "Casa real (MCP)") + (st.dry_run ? " · só regista" : " · atua");
  modo.className = "modo" + (st.mode === "simulado" ? " simulado" : st.dry_run ? "" : " atua");
  modo.title = st.dry_run ? "Dry-run: o Coordenador regista as decisões mas não envia nada à casa."
    : "O Coordenador executa as decisões aprovadas na casa.";
  const seg = $("seguranca");
  const alarm = st.alarm;
  seg.textContent = alarm.any ? "Em alarme" : (MODE_PT[st.security.status] || st.security.status);
  seg.className = "seguranca " + (alarm.any ? "alarme" : st.security.armed ? "armada" : "desarmada");
  renderAlarm(st);
  renderAgents(st.agents || []);
  const av = $("avisos");
  av.hidden = !st.warnings.length;
  av.replaceChildren(...st.warnings.map((w) => el("div", null, w)));
}

// Alarm band: what is alarming, and the way out (the chat asks for the confirmation to disarm)
let alarmSince = null;
function renderAlarm(st) {
  const a = st.alarm;
  $("alarme").hidden = !a.any;
  if (!a.any) { alarmSince = null; return; }
  alarmSince ??= Date.now() / 1000;
  const room = a.smoke_room && st.rooms.find((r) => r.id === a.smoke_room);
  $("alarme-titulo").textContent = a.smoke ? `Fumo detetado${room ? ` · ${room.name}` : ""}`
    : a.intrusion ? "Possível intrusão: a casa está em alarme" : a.hub ? "A central de segurança está em alarme"
    : "A casa está em alarme";
  $("alarme-sub").textContent = `desde as ${hhmm(alarmSince)} · se foste tu, diz-me no chat ou desliga aqui`;
}
$("alarme-desligar").addEventListener("click", () => {
  window.dispatchEvent(new CustomEvent("tt:chat-send", { detail: "Foi falso alarme, desliga o alarme" }));
});

// LLM agents (A2A) as the Coordinator sees them: waiting, online, thinking, unavailable
function renderAgents(agents) {
  const box = $("agentes");
  box.replaceChildren(...agents.map((a) => {
    const cls = a.busy ? "ocupado" : a.online === true ? "online" : a.online === false ? "offline" : "espera";
    const label = a.busy ? "a pensar" : a.online === true ? (a.last_latency_s != null ? `${nf1.format(a.last_latency_s)} s` : "pronto")
      : a.online === false ? "indisponível" : "em espera";  // never called yet: nothing for it so far
    const chip = el("span", `agente ${cls}${a.name === "Coordenador" ? " coordenador" : ""}`, el("span", "id"), a.name, el("small", null, label));
    chip.style.setProperty("--area", areaColor(a.name));
    chip.title = a.last_error ? `${a.name} (A2A ${a.url}): ${a.last_error}`
      : a.online == null ? `${a.name} · A2A ${a.url} · ainda não recebeu nenhum evento que lhe diga respeito`
      : `${a.name} · A2A ${a.url}`;
    return chip;
  }));
}

// ------------------------------------------------------------------ device panel
function valueRows(d) {
  const v = d.values || {};
  const rows = [];
  const yes = (b, s, n) => (b == null ? "—" : b ? s : n);
  if ("temperature_c" in v) rows.push(["Temperatura", v.temperature_c == null ? "—" : `${nf1.format(v.temperature_c)} °C`]);
  if ("humidity_pct" in v) rows.push(["Humidade", v.humidity_pct == null ? "—" : `${nf0.format(v.humidity_pct)} %`]);
  if ("open" in v) {
    const op = d.opening && world && world.openings[d.opening];
    rows.push([op && op.type === "door" ? "Porta" : "Janela", yes(v.open, "Aberta", "Fechada")]);
  }
  if ("on" in v) rows.push(["Estado", yes(v.on, "Ligada", "Desligada")]);
  if ("power_w" in v) rows.push(["Potência", v.power_w == null ? "—" : `${nf0.format(v.power_w)} W`]);
  if ("today_kwh" in v && v.today_kwh != null) rows.push(["Consumo hoje", `${nf1.format(v.today_kwh)} kWh`]);
  if ("brightness" in v) rows.push(["Brilho", v.brightness == null ? "—" : `${nf0.format(v.brightness)} %`]);
  if (v.color_rgb) {
    const sw = el("span", null);
    sw.style.cssText = `display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;vertical-align:-1px;background:rgb(${v.color_rgb.join(",")})`;
    rows.push(["Cor", el("span", null, sw, `${v.color_rgb.join(", ")} (reportada ${v.color_rgb_reported ?? "—"})`)]);
  }
  if ("alarm" in v) rows.push(["Alarme", yes(v.alarm, "Sim", "Não")]);
  if ("motion" in v) rows.push(["Movimento", yes(v.motion, "Detetado", "Não")]);
  if ("sound" in v) rows.push(["Som", yes(v.sound, "Detetado", "Não")]);
  if ("tamper" in v) rows.push(["Manipulação", yes(v.tamper, "Detetada", "Não")]);
  if ("battery" in v) {
    const b = v.battery;
    rows.push(["Bateria", typeof b === "number" ? `${nf0.format(b)} %` : ({ low: "Baixa", medium: "Média", high: "Alta" }[b] || b)]);
  }
  return rows;
}

function renderPanel() {
  const d = current?.devices.find((x) => x.id === selected);
  if (!d) { closePanel(); return; }
  $("painel").hidden = false;
  $("painel-titulo").textContent = TYPE_PT[d.role] || d.type;
  $("painel-sub").textContent = `${d.name} · ${d.room_name}` + (d.placed === "default" ? " · posição por omissão" : "");
  $("painel-estado").replaceChildren(el("span", "ponto " + (d.online ? "on" : "off")),
    d.online ? `Online · última comunicação ${ago(d.last_seen)}` : `Offline desde ${d.last_seen ? new Date(d.last_seen * 1000).toLocaleString("pt-PT") : "—"}`);
  const dl = $("painel-valores");
  dl.replaceChildren();
  for (const [k, v] of valueRows(d)) dl.append(el("dt", null, k), el("dd", null, v));
  $("painel-acoes").querySelectorAll("button").forEach((b) => { if (!d.online) b.disabled = true; });
}

async function renderChart(d) {
  const box = $("painel-grafico");
  box.replaceChildren();
  const h = HISTORY[d.role];
  if (!h) return;
  const [metric, unit] = h;
  const now = Date.now() / 1000;
  let rows = [];
  try {
    const res = await fetch(`/historic/rooms/${encodeURIComponent(d.room)}/${metric}?start=${now - 6 * 3600}&end=${now + 1}`);
    if (res.ok) rows = (await res.json()).leituras.filter((r) => r.device_id === d.id);
  } catch { /* no history */ }
  if (selected !== d.id) return;
  const wrap = el("div", "grafico", el("div", "grafico-titulo", "Últimas 6 horas"));
  if (rows.length < 2) { wrap.append(el("div", "sub", "Ainda sem histórico suficiente.")); box.replaceChildren(wrap); return; }
  const xs = rows.map((r) => r.ts), ys = rows.map((r) => r.value);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const W = 300, H = 84, pad = 6;
  const px = (x) => pad + ((x - x0) / Math.max(x1 - x0, 1)) * (W - 2 * pad);
  const py = (y) => (y1 === y0 ? H / 2 : H - pad - ((y - y0) / (y1 - y0)) * (H - 2 * pad));
  const NS = "http://www.w3.org/2000/svg";
  const svgEl = (tag, attrs) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  };
  const SERIES = "#3987e5";
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img",
    "aria-label": `${TYPE_PT[d.role] || d.type}: histórico das últimas 6 horas` });
  for (const y of [pad, H / 2, H - pad]) {  // faint grid
    svg.append(svgEl("line", { x1: 0, x2: W, y1: y, y2: y, stroke: "rgba(133,145,166,.16)", "vector-effect": "non-scaling-stroke" }));
  }
  // contact / light are on-off: a step line; temperature / power: straight segments
  const step = !unit;
  const pts = [];
  rows.forEach((r, i) => {
    if (step && i) pts.push([px(r.ts), py(rows[i - 1].value)]);
    pts.push([px(r.ts), py(r.value)]);
  });
  const line = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  svg.append(svgEl("polygon", { points: `${pad},${H} ${line} ${px(x1).toFixed(1)},${H}`, fill: SERIES, "fill-opacity": "0.14" }));
  svg.append(svgEl("polyline", { points: line, fill: "none", stroke: SERIES, "stroke-width": "2",
    "stroke-linejoin": "round", "vector-effect": "non-scaling-stroke" }));
  const cross = svgEl("line", { y1: 0, y2: H, stroke: "rgba(230,235,243,.4)", "vector-effect": "non-scaling-stroke", visibility: "hidden" });
  svg.append(cross);
  const fmt = (y) => (unit ? `${metric === "power_w" ? nf0.format(y) : nf1.format(y)} ${unit}`
    : metric === "open" ? (y >= 0.5 ? "aberta" : "fechada") : (y >= 0.5 ? "ligada" : "desligada"));
  const last = rows[rows.length - 1];
  // endpoint: HTML dot over the SVG (the SVG is stretched, a circle in it would be an ellipse)
  const dot = el("span", null);
  dot.style.cssText = `position:absolute;width:8px;height:8px;border-radius:50%;background:${SERIES};`
    + `box-shadow:0 0 0 2px var(--panel-2);transform:translate(-50%,-50%);pointer-events:none`;
  const tip = el("div", "dica");
  tip.hidden = true;
  const plot = el("div", null);
  plot.style.position = "relative";
  plot.append(svg, dot, tip);
  const place = () => {
    const r = svg.getBoundingClientRect();
    dot.style.left = `${(px(last.ts) / W) * r.width}px`;
    dot.style.top = `${(py(last.value) / H) * r.height}px`;
  };
  requestAnimationFrame(place);
  svg.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect();
    const t = x0 + ((e.clientX - r.left) / r.width * W - pad) / (W - 2 * pad) * (x1 - x0);
    const near = rows.reduce((a, b) => (Math.abs(b.ts - t) < Math.abs(a.ts - t) ? b : a));
    const x = px(near.ts);
    cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.textContent = `${hhmm(near.ts)} · ${fmt(near.value)}`;
    tip.style.left = `${Math.min(Math.max((x / W) * r.width, 50), r.width - 50)}px`;
    tip.style.top = "-26px";
  });
  svg.addEventListener("pointerleave", () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); });
  wrap.append(plot, el("div", "eixo", el("span", null, `${hhmm(x0)} · mín ${fmt(y0)}`), el("span", null, `máx ${fmt(y1)} · agora ${fmt(last.value)}`)));
  box.replaceChildren(wrap);
}

// actions: a request to the Coordinator (POST /coordinator/request), never a direct write.
// Only what the server lists in state.actions (light/plug on/off); no security actions here.
function renderActions(d) {
  const box = $("painel-acoes");
  box.replaceChildren();
  const allowed = current.actions?.[d.role];
  if (!allowed) return;
  const note = el("div", "nota", current.dry_run
    ? "Pedido ao Coordenador. Dry-run ativo: a decisão fica registada, nada é enviado à casa."
    : "Pedido ao Coordenador, que valida antes de executar.");
  for (const action of allowed) {
    const b = el("button", null, action === "on" ? "Ligar" : "Desligar");
    b.type = "button";
    b.dataset.action = action;
    b.disabled = !d.online;
    b.addEventListener("click", async () => {
      box.querySelectorAll("button").forEach((x) => { x.disabled = true; });
      note.textContent = "A enviar o pedido ao Coordenador…";
      try {
        const res = await fetch("/coordinator/request", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ device_id: d.id, action, origin: "3D" }),
        });
        const body = await res.json();
        note.textContent = res.ok ? body.message : `Erro: ${body.detail || res.statusText}`;
      } catch (err) {
        note.textContent = `Erro: ${err.message}`;
      } finally {
        const now = current.devices.find((x) => x.id === d.id);
        box.querySelectorAll("button").forEach((x) => { x.disabled = !now?.online; });
      }
    });
    box.append(b);
  }
  box.append(note);
}

// camera: live feed (MJPEG from the RTSP stream) + "Analisar agora" with the vision model (qwen3.8)
const CATEGORY_PT = { empty: "sem pessoas", person: "pessoa", intrusion: "possível intrusão", smoke: "fumo", unclear: "pouco claro" };
async function renderCamera(d) {
  const box = $("painel-camara");
  box.replaceChildren();  // also drops the <img>, which closes the MJPEG connection
  if (d?.role !== "camera") return;
  let status = null;
  try { status = await (await fetch("/vision/live/status")).json(); } catch { /* API down */ }
  if (selected !== d.id) return;
  const feed = el("div", "camara-feed");
  if (status?.live) {
    const img = document.createElement("img");
    img.alt = "Imagem em direto da câmara";
    const off = el("div", "sem-sinal", "A ligar à câmara…");
    const connect = () => { img.src = `/vision/live.mjpg?t=${Date.now()}`; };
    img.addEventListener("load", () => off.remove());
    // the stream ends when the camera stops sending: show the server's reason and reconnect in 5 s
    img.addEventListener("error", async () => {
      if (!img.isConnected) return;  // panel closed or another device selected
      let why = "";
      try { why = (await (await fetch("/vision/live/status")).json()).error; } catch { /* API down */ }
      off.textContent = `${why || "Sem imagem da câmara."} A tentar de novo…`;
      if (!off.isConnected) feed.append(off);
      setTimeout(() => { if (img.isConnected) connect(); }, 5000);
    });
    connect();
    feed.append(img, el("span", "ao-vivo", "● ao vivo"), off);
  } else {
    feed.append(el("div", "sem-sinal", status?.vision === false ? "Visão desligada (VISION_ENABLED=false)."
      : "Sem câmara em direto: define CAMERA_RTSP_URL no .env."));
  }
  const result = el("div", null);
  const btn = el("button", null, "Analisar agora");
  btn.type = "button";
  btn.disabled = !status?.vision;
  const note = el("span", "sub", status?.model ? `modelo: ${status.model}` : "");
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    note.textContent = "A analisar a imagem…";
    try {
      const res = await fetch("/vision/analyze", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ divisao: d.room_name || null, contexto: "general" }) });
      const o = await res.json();
      if (!res.ok) throw new Error(o.detail || res.statusText);
      const box2 = el("div", "camara-analise");
      if (o.status !== "ok") box2.append(el("div", null, `Não consegui analisar (${o.status}): ${o.error || ""}`));
      else {
        box2.append(el("div", null, o.scene_note_pt || "Sem descrição."));
        const bits = [`${o.person_count} ${o.person_count === 1 ? "pessoa" : "pessoas"}`, CATEGORY_PT[o.category] || o.category];
        if (o.window_or_door_open != null) bits.push(`janela/porta ${o.window_or_door_open ? "aberta" : "fechada"}`);
        if (o.smoke_or_fire_visible) bits.push("fumo/fogo visível");
        bits.push(`confiança ${Math.round((o.overall_confidence || 0) * 100)} %`, `${(o.latency_ms / 1000).toFixed(1)} s`);
        box2.append(el("div", "meta", bits.join(" · ")));
      }
      result.replaceChildren(box2);
      note.textContent = o.model ? `modelo: ${o.model}` : "";
    } catch (err) {
      note.textContent = `Erro: ${err.message}`;
    } finally {
      btn.disabled = false;
    }
  });
  box.append(feed, el("div", "camara-acoes", btn, note), result);
}

function openPanel(id) {
  selected = id;
  renderPanel();
  const d = current.devices.find((x) => x.id === id);
  renderActions(d);
  renderChart(d);
  renderCamera(d);
  window.dispatchEvent(new CustomEvent("twin:device-selected", { detail: d }));
}
function closePanel() { selected = null; $("painel").hidden = true; $("painel-camara").replaceChildren(); }
$("painel-fechar").addEventListener("click", closePanel);

// ------------------------------------------------------------------ picking
const ray = new THREE.Raycaster();
const pointer = new THREE.Vector2();
let downAt = null;
renderer.domElement.addEventListener("pointerdown", (e) => { downAt = [e.clientX, e.clientY]; });
renderer.domElement.addEventListener("pointerup", (e) => {
  if (editMode) return;
  if (!downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 5 || !world) return;
  const rect = renderer.domElement.getBoundingClientRect();
  pointer.set(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
  ray.setFromCamera(pointer, camera);
  const dev = ray.intersectObjects(world.hits, false)[0];
  if (dev) { openPanel(dev.object.userData.deviceId); return; }
  const floor = ray.intersectObjects(world.floors, false)[0];
  if (floor) focusRoom(floor.object.userData.roomId);
});
renderer.domElement.addEventListener("pointermove", (e) => {
  if (!world) return;
  const rect = renderer.domElement.getBoundingClientRect();
  pointer.set(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
  ray.setFromCamera(pointer, camera);
  renderer.domElement.style.cursor = ray.intersectObjects(world.hits, false).length ? "pointer" : "";
});

// ------------------------------------------------------------------ layout edit mode (drag by hand)
// Opened by the "Ajustar 3D" button or by the chat ("Ajustar à mão"). Rooms snap to 0,25 m and carry
// their devices; devices snap to 5 cm. "Gravar" sends the changes to /layout/manual (validated there
// and stored as a layout preference, with history), "Desfazer última" restores the previous version.
let editMode = false;
let drag = null;
const movedRooms = {};
const movedDevices = new Set();
const groundPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
const snap = (v, s) => Math.round(v / s) * s;

function setEditMode(on) {
  editMode = on;
  $("edicao").hidden = !on;
  host.classList.toggle("editar", on);
  $("editar-3d").textContent = on ? "Sair da edição" : "Ajustar 3D";
  drag = null;
  controls.enabled = true;
  for (const k of Object.keys(movedRooms)) delete movedRooms[k];
  movedDevices.clear();
  if (on) viewHouse();
}
function resetWorld() {
  if (!current) return;
  disposeWorld();
  world = null;
  applyState(current);
}
function rayAt(e) {
  const rect = renderer.domElement.getBoundingClientRect();
  pointer.set(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
  ray.setFromCamera(pointer, camera);
}
function planePoint(plane) {
  const p = new THREE.Vector3();
  return ray.ray.intersectPlane(plane, p) ? p : null;
}
renderer.domElement.addEventListener("pointerdown", (e) => {
  if (!editMode || !world) return;
  rayAt(e);
  const hitDev = ray.intersectObjects(world.hits, false)[0];
  if (hitDev) {
    const id = hitDev.object.userData.deviceId;
    const g = world.devices[id].group;
    const plane = new THREE.Plane(new THREE.Vector3(0, 1, 0), -g.position.y);
    drag = { kind: "device", id, plane, start: planePoint(plane), orig: g.position.clone() };
  } else {
    const fl = ray.intersectObjects(world.floors, false)[0];
    if (!fl) return;
    const id = fl.object.userData.roomId;
    drag = { kind: "room", id, plane: groundPlane, start: planePoint(groundPlane), orig: world.rooms[id].group.position.clone(),
             devs: Object.values(world.devices).filter((d) => d.data.room === id).map((d) => [d, d.group.position.clone()]) };
  }
  if (!drag.start) { drag = null; return; }
  controls.enabled = false;  // capture phase: OrbitControls sees it disabled
  host.classList.add("arrastar");
}, true);
window.addEventListener("pointermove", (e) => {
  if (!drag || !world) return;
  rayAt(e);
  const p = planePoint(drag.plane);
  if (!p) return;
  const dx = p.x - drag.start.x, dz = p.z - drag.start.z;
  if (drag.kind === "room") {
    const sx = snap(dx, 0.25), sz = snap(dz, 0.25);
    const wr = world.rooms[drag.id];
    wr.group.position.set(drag.orig.x + sx, 0, drag.orig.z + sz);
    for (const [d, o] of drag.devs) d.group.position.set(o.x + sx, o.y, o.z + sz);
    movedRooms[drag.id] = { dx: wr.group.position.x, dz: wr.group.position.z };
  } else {
    const g = world.devices[drag.id].group;
    g.position.set(snap(drag.orig.x + dx, 0.05), drag.orig.y, snap(drag.orig.z + dz, 0.05));
    movedDevices.add(drag.id);
  }
});
window.addEventListener("pointerup", () => {
  if (!drag) return;
  drag = null;
  controls.enabled = true;
  host.classList.remove("arrastar");
});
async function saveEdit() {
  const rooms = {}, devices = {};
  for (const [id, m] of Object.entries(movedRooms)) {
    const r = world.rooms[id].data;
    rooms[id] = { x: r.x + m.dx, z: r.z + m.dz };
  }
  for (const id of movedDevices) {
    const p = world.devices[id].group.position;
    devices[id] = { position: [p.x, p.y, p.z] };
  }
  if (!Object.keys(rooms).length && !Object.keys(devices).length) { setEditMode(false); return; }
  const res = await fetch("/layout/manual", { method: "POST", headers: { "Content-Type": "application/json" },
                                             body: JSON.stringify({ rooms, devices }) });
  if (!res.ok) {
    const detail = (await res.json().catch(() => ({}))).detail || res.statusText;
    alert("Não gravei: " + detail);
    return;
  }
  setEditMode(false);
  poll();
}
$("ed-gravar").addEventListener("click", saveEdit);
$("ed-cancelar").addEventListener("click", () => { setEditMode(false); resetWorld(); });
$("ed-desfazer").addEventListener("click", async () => {
  await fetch("/layout/undo", { method: "POST" });
  setEditMode(false);
  resetWorld();
  poll();
});
$("editar-3d").addEventListener("click", () => { if (editMode) resetWorld(); setEditMode(!editMode); });
window.addEventListener("tt:layout-edit", () => setEditMode(true));
window.addEventListener("tt:layout-changed", () => setTimeout(poll, 300));

// ------------------------------------------------------------------ timeline + balloons
const seen = new Set();
const balloons = [];
// filter chips: one per area, in a fixed order (identity colour never follows rank)
const FILTERS = ["Todos", "Segurança", "Energia", "Conforto", "Coordenador"];
let filter = "Todos";
try { filter = localStorage.getItem("tt-linha-filtro") || "Todos"; } catch { /* storage blocked */ }
if (!FILTERS.includes(filter)) filter = "Todos";
function areaOf(it) {
  return it.kind === "decision" ? (AREA_VAR[it.agent] ? it.agent : it.area) : it.area;
}
function applyFilter(li) {
  li.hidden = filter !== "Todos" && li.dataset.area !== filter
    && !(filter === "Segurança" && li.dataset.area === "Emergência");
}
function renderFilters() {
  $("linha-filtros").replaceChildren(...FILTERS.map((name) => {
    const b = el("button", null);
    b.type = "button";
    if (name !== "Todos") {
      const id = el("span", "id");
      if (name === "Coordenador") id.style.transform = "rotate(45deg) scale(.9)";
      b.append(id);
      b.style.setProperty("--area", areaColor(name));
    }
    b.append(name);
    b.setAttribute("aria-pressed", String(name === filter));
    b.addEventListener("click", () => {
      filter = name;
      try { localStorage.setItem("tt-linha-filtro", name); } catch { /* storage blocked */ }
      renderFilters();
      $("linha-lista").querySelectorAll("li:not(.vazio)").forEach(applyFilter);
    });
    return b;
  }));
}
renderFilters();

function addTimeline(items, { animate }) {
  const list = $("linha-lista");
  list.querySelector(".vazio")?.remove();
  for (const it of items.sort((a, b) => a.ts - b.ts)) {
    if (seen.has(it.id)) continue;
    seen.add(it.id);
    const decision = it.kind === "decision";
    const area = areaOf(it);
    const cls = [animate ? "novo" : "", it.severity === "critical" ? "critical" : "", decision ? "decisao" : "",
      area === "Coordenador" ? "coordenador" : ""].filter(Boolean).join(" ");
    const li = el("li", cls);
    li.dataset.area = area;
    li.style.setProperty("--area", areaColor(area));
    const time = el("time", null, hhmmss(it.ts));
    time.dateTime = new Date(it.ts * 1000).toISOString();
    const meta = el("div", "meta", time, el("span", "area", decision ? it.agent : it.area),
      it.room_name ? el("span", null, `· ${it.room_name}`) : null,
      decision ? el("span", `estado ${it.status}`, it.status) : null);
    li.append(meta, el("div", null, it.message));
    applyFilter(li);
    list.prepend(li);
    if (animate && (decision || !QUIET.has(it.type))) showBalloon(it);
  }
  while (list.children.length > 80) list.lastChild.remove();
}

function anchorFor(it) {
  const wd = it.device_id && world?.devices[it.device_id];
  if (wd) return wd.group.position.clone().add(new THREE.Vector3(0, 0.55, 0));
  const r = it.room && world?.rooms[it.room]?.data;
  if (r) return new THREE.Vector3(r.x + r.width / 2, r.height + 0.9, r.z + r.depth / 2);
  const c = world ? world.bounds.getCenter(new THREE.Vector3()) : new THREE.Vector3();
  return c.setY((world?.bounds.max.y || 2.6) + 1.2);
}

function showBalloon(it) {
  if (!world) return;
  const pos = anchorFor(it);
  const decision = it.kind === "decision";
  const inner = el("div", null, el("b", null, `${decision ? it.agent : it.area}: `), it.message);
  const outer = el("div", "balao", inner);
  outer.style.setProperty("--area", areaColor(areaOf(it)));
  const obj = new CSS2DObject(outer);
  obj.position.copy(pos);
  scene.add(obj);
  const b = { obj, pos, inner };
  balloons.push(b);
  while (balloons.length > 5) removeBalloon(balloons[0]);
  setTimeout(() => { inner.style.opacity = "0"; }, BALLOON_MS - 800);
  setTimeout(() => removeBalloon(b), BALLOON_MS);
}
// Balloons on the same spot (e.g. an agent's proposal and the Coordinator's decision, which arrive
// together) stack upwards on screen, newest on top. Done every frame because a balloon only has a
// height once the CSS2DRenderer has put it in the page.
function layoutBalloons() {
  for (let i = 0; i < balloons.length; i++) {
    let below = 0;
    for (let j = 0; j < i; j++) {
      if (balloons[j].pos.distanceTo(balloons[i].pos) < 0.8) below += balloons[j].inner.offsetHeight + 6;
    }
    const t = `translateY(${-below}px)`;
    if (balloons[i].inner.style.transform !== t) balloons[i].inner.style.transform = t;
  }
}

function removeBalloon(b) {
  const i = balloons.indexOf(b);
  if (i < 0) return;
  balloons.splice(i, 1);
  scene.remove(b.obj);
  b.obj.element.remove();
}

// ------------------------------------------------------------------ data: SSE with polling fallback
let lastMessage = 0;
function setConnection(ok, text) {
  $("ligacao").className = "ligacao " + (ok ? "ok" : "erro");
  $("ligacao-texto").textContent = text;
}
function onSnapshot(st) {
  applyState(st);
  addTimeline([...(st.events || []), ...(st.decisions || [])], { animate: false });
}
async function poll() {
  try {
    const res = await fetch("/twin/state");
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    const st = await res.json();
    const fresh = [...st.events, ...st.decisions].filter((x) => !seen.has(x.id));
    const hadWorld = world != null;  // the first poll is history, not news
    applyState(st);
    addTimeline(fresh, { animate: hadWorld });
    setConnection(true, `atualização periódica · ${hhmmss(st.ts)}`);
  } catch (err) {
    setConnection(false, `sem dados: ${err.message}`);
  }
}
function connect() {
  if (!("EventSource" in window)) { setInterval(poll, POLL_FALLBACK_MS); poll(); return; }
  const es = new EventSource("/twin/stream");
  const touch = () => { lastMessage = Date.now(); };
  es.addEventListener("snapshot", (e) => { touch(); onSnapshot(JSON.parse(e.data)); setConnection(true, "em direto"); });
  es.addEventListener("state", (e) => {
    touch();
    const st = JSON.parse(e.data);
    applyState(st);
    setConnection(true, `em direto · ${hhmmss(st.ts)}`);
  });
  es.addEventListener("event", (e) => { touch(); addTimeline([JSON.parse(e.data)], { animate: true }); });
  es.addEventListener("decision", (e) => { touch(); addTimeline([JSON.parse(e.data)], { animate: true }); });
  es.onerror = () => setConnection(false, "ligação perdida, a tentar de novo…");
  // if the stream stays silent (proxy, sleep, server restart), fall back to polling every 2 s
  setInterval(() => { if (Date.now() - lastMessage > STREAM_STALE_MS) poll(); }, POLL_FALLBACK_MS);
}

// ------------------------------------------------------------------ animation loop
const clock = new THREE.Clock();
function animate() {
  requestAnimationFrame(animate);
  const dt = Math.min(clock.getDelta(), 0.1), t = clock.elapsedTime;
  if (tween) {
    const k = Math.min((performance.now() - tween.t0) / tween.ms, 1), e = 1 - Math.pow(1 - k, 3);
    controls.target.lerpVectors(tween.fromT, tween.toT, e);
    camera.position.lerpVectors(tween.fromP, tween.toP, e);
    if (k >= 1) tween = null;
  }
  controls.update();
  if (world && current) {
    for (const op of Object.values(world.openings)) op.pivot.rotation.y += (op.target - op.pivot.rotation.y) * Math.min(1, dt * 5);
    const alarm = current.alarm.any;
    const pulse = 0.5 + 0.5 * Math.sin(t * 6);
    world.edgeMat.color.set(alarm ? 0xff4a4a : 0x8591a6);
    world.edgeMat.opacity = alarm ? 0.45 + 0.55 * pulse : 0.55;
    for (const wr of Object.values(world.rooms)) {
      if (!wr.smoke) continue;
      wr.fog.material.opacity = 0.08 + 0.2 * pulse;
      wr.floorMat.color.setRGB(0.55 + 0.45 * pulse, 0.08, 0.08);
      wr.floorMat.emissive.setRGB(0.25 * pulse, 0, 0);
      const p = wr.particles.geometry.attributes.position, r = wr.data;
      for (let i = 0; i < p.count; i++) {
        let y = p.getY(i) + dt * (0.35 + (i % 7) * 0.05);
        if (y > r.height) y = 0.1;
        p.setY(i, y);
        p.setX(i, p.getX(i) + Math.sin(t + i) * dt * 0.05);
      }
      p.needsUpdate = true;
    }
    for (const wd of Object.values(world.devices)) {
      if (wd.glow) {
        wd.glow.material.opacity = wd.active ? 0.18 + 0.22 * (0.5 + 0.5 * Math.sin(t * 4)) : 0;
        const s = wd.active ? 0.2 + 0.05 * Math.sin(t * 4) : 0.2;
        wd.glow.scale.set(s, s, s);
      }
      if (wd.ledMat && wd.alarm) wd.ledMat.color.set(pulse > 0.5 ? 0xff2a2a : 0x330000);
      if (wd.coneMat) {
        wd.coneMat.color.set(wd.active ? 0xfab219 : 0x8fb4e8);
        wd.coneMat.opacity = wd.data.online ? (wd.active ? 0.16 + 0.12 * pulse : 0.08) : 0.03;
      }
    }
  }
  renderer.render(scene, camera);
  labels.render(scene, camera);
  layoutBalloons();
}

resize();
animate();
connect();
setConnection(false, "a ligar…");
