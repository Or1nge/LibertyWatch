const paths = {
  alert:
    '<path d="M12 9v4"/><path d="M12 17h.01"/><path d="M10.3 3.7 2.5 17.2A2 2 0 0 0 4.2 20h15.6a2 2 0 0 0 1.7-2.8L13.7 3.7a2 2 0 0 0-3.4 0Z"/>',
  arrow:
    '<path d="m9 18 6-6-6-6"/>',
  bell:
    '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M10 21h4"/>',
  book:
    '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"/>',
  check:
    '<path d="m5 12 4 4L19 6"/>',
  chevrondown:
    '<path d="m6 9 6 6 6-6"/>',
  close:
    '<path d="M18 6 6 18M6 6l12 12"/>',
  database:
    '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
  down:
    '<path d="M12 5v14M19 12l-7 7-7-7"/>',
  flask:
    '<path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.8 3h10.4a2 2 0 0 0 1.8-3l-5-9V3"/><path d="M7.7 15h8.6"/>',
  grid:
    '<rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/>',
  info:
    '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  layers:
    '<path d="m12 2 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/>',
  linechart:
    '<path d="M3 3v18h18"/><path d="m7 16 4-5 3 3 5-7"/>',
  list:
    '<path d="M8 6h13M8 12h13M8 18h13"/><path d="M3 6h.01M3 12h.01M3 18h.01"/>',
  menu:
    '<path d="M4 7h16M4 12h16M4 17h16"/>',
  more:
    '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  note:
    '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M8 13h8M8 17h6"/>',
  radar:
    '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><path d="M12 12 19 5M12 3v2M21 12h-2M12 21v-2M3 12h2"/>',
  refresh:
    '<path d="M20 7h-5V2"/><path d="M20 7a8 8 0 1 0 1 8"/>',
  search:
    '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
  shield:
    '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/>',
  target:
    '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  trend:
    '<path d="m3 17 6-6 4 4 8-9"/><path d="M15 6h6v6"/>',
  up:
    '<path d="M12 19V5M5 12l7-7 7 7"/>',
  wallet:
    '<path d="M20 7V5a2 2 0 0 0-2-2H5a3 3 0 0 0 0 6h15v12H5a3 3 0 0 1-3-3V6"/><path d="M16 14h.01"/>'
};

export function icon(name, className = "") {
  const body = paths[name] ?? paths.info;
  const classAttribute = ` class="app-icon${className ? ` ${className}` : ""}"`;
  return `<svg${classAttribute} viewBox="0 0 24 24" aria-hidden="true">${body}</svg>`;
}

export function hydrateIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((element) => {
    if (element.dataset.iconReady === "true") return;
    element.innerHTML = icon(element.dataset.icon);
    element.dataset.iconReady = "true";
  });
}
