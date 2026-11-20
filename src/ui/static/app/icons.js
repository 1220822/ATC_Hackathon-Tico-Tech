// Line icons (24×24, stroke), drawn in the Lucide style. Local: works without internet.
const P = {
  thermometer: ["M14 4v10.54a4 4 0 1 1-4 0V4a2 2 0 0 1 4 0Z"],
  droplet: ["M12 2.7 17.66 8.36a8 8 0 1 1-11.31 0Z"],
  window: ["M4 3h16v18H4Z", "M12 3v18", "M4 12h16"],
  door: ["M5 21V3h11v18", "M3 21h18", "M13 12h.01"],
  plug: ["M12 22v-5", "M9 8V2", "M15 8V2", "M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"],
  flame: ["M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.07-2.14-.22-4.05 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.15.43-2.29 1-3a2.5 2.5 0 0 0 2.5 2.5Z"],
  bulb: ["M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5", "M9 18h6", "M10 22h4"],
  shield: ["M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1Z"],
  shieldCheck: ["M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1Z", "m9 12 2 2 4-4"],
  camera: ["M16 13 21.2 16.5a.5.5 0 0 0 .8-.4V7.9a.5.5 0 0 0-.8-.4L16 11", "M2 6h14v12H2Z"],
  smoke: ["M3 5h18", "M5 5v2a7 7 0 0 0 14 0V5", "M9 21c0-1.2 1.2-1.2 1.2-2.4S9 17.4 9 16.2", "M15 21c0-1.2 1.2-1.2 1.2-2.4S15 17.4 15 16.2"],
  hub: ["M3 14h18v6H3Z", "M7 17h.01", "M11 17h.01", "M12 14V9", "M8.5 7.5a5 5 0 0 1 7 0", "M6 5a8.5 8.5 0 0 1 12 0"],
  bolt: ["M13 2 3 14h9l-1 8 10-12h-9l1-8Z"],
  home: ["M3 10 12 3l9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1Z"],
  cube: ["M21 8 12 3 3 8v8l9 5 9-5Z", "M3 8l9 5 9-5", "M12 13v8"],
  grid: ["M3 3h7v7H3Z", "M14 3h7v7h-7Z", "M3 14h7v7H3Z", "M14 14h7v7h-7Z"],
  user: ["M20 21a8 8 0 0 0-16 0", "M12 13a5 5 0 1 0 0-10 5 5 0 0 0 0 10Z"],
  check: ["M20 6 9 17l-5-5"],
  x: ["M18 6 6 18", "M6 6l12 12"],
  alert: ["M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z", "M12 9v4", "M12 17h.01"],
  clock: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z", "M12 7v5l3 2"],
  arrow: ["M5 12h14", "M13 6l6 6-6 6"],
  send: ["M22 2 11 13", "M22 2 15 22l-4-9-9-4Z"],
  trash: ["M3 6h18", "M8 6V4h8v2", "M19 6l-1 14H6L5 6"],
  move: ["M5 9l-3 3 3 3", "M9 5l3-3 3 3", "M15 19l-3 3-3-3", "M19 9l3 3-3 3", "M2 12h20", "M12 2v20"],
  focus: ["M3 7V3h4", "M17 3h4v4", "M21 17v4h-4", "M7 21H3v-4"],
  wifiOff: ["M2 2l20 20", "M8.5 16.5a5 5 0 0 1 7 0", "M5 12.9a10 10 0 0 1 5.2-2.8", "M19 12.9a10 10 0 0 0-2.1-1.5", "M12 20h.01"],
  spark: ["M12 3v4", "M12 17v4", "M3 12h4", "M17 12h4", "M5.6 5.6l2.8 2.8", "M15.6 15.6l2.8 2.8", "M5.6 18.4l2.8-2.8", "M15.6 8.4l2.8-2.8"],
  chat: ["M21 12a8 8 0 0 1-11.6 7.1L3 21l1.9-6.4A8 8 0 1 1 21 12Z"],
};

const NS = "http://www.w3.org/2000/svg";
export function icon(name, cls = "") {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", `i ${cls}`.trim());
  svg.setAttribute("aria-hidden", "true");
  for (const d of P[name] || P.grid) {
    const p = document.createElementNS(NS, "path");
    p.setAttribute("d", d);
    svg.append(p);
  }
  return svg;
}

// <svg data-icon="name"> placeholders in static HTML become real icons
export function hydrateIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((n) => {
    const s = icon(n.dataset.icon, n.getAttribute("class") || "");
    n.replaceWith(s);
  });
}

// device role -> icon and Portuguese name (never the raw MCP name, which is in English)
export const ROLE = {
  temperature: { icon: "thermometer", name: "Sensor de temperatura" },
  contact: { icon: "window", name: "Sensor da janela" },
  light: { icon: "bulb", name: "Luz" },
  plug: { icon: "plug", name: "Tomada do aquecedor" },
  smoke: { icon: "smoke", name: "Detetor de fumo" },
  hub: { icon: "hub", name: "Central de segurança" },
  camera: { icon: "camera", name: "Câmara" },
};
