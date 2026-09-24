/** Common UI icons (Lucide-style paths) and preset menu layouts for import. */
export const ICON_CATEGORIES = {
  Navigation: [
    { id: "home", label: "Home", path: "M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" },
    { id: "menu", label: "Menu", path: "M4 6h16M4 12h16M4 18h16" },
    { id: "arrow-left", label: "Back", path: "M19 12H5M12 19l-7-7 7-7" },
    { id: "arrow-right", label: "Forward", path: "M5 12h14M12 5l7 7-7 7" },
    { id: "chevron-down", label: "Chevron down", path: "M6 9l6 6 6-6" },
    { id: "external-link", label: "External link", path: "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14L21 3" },
  ],
  Actions: [
    { id: "plus", label: "Add", path: "M12 5v14M5 12h14" },
    { id: "minus", label: "Remove", path: "M5 12h14" },
    { id: "edit", label: "Edit", path: "M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" },
    { id: "trash", label: "Delete", path: "M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" },
    { id: "copy", label: "Copy", path: "M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2M15 2H9a1 1 0 0 0-1 1v2h8V3a1 1 0 0 0-1-1z" },
    { id: "search", label: "Search", path: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.35-4.35" },
    { id: "filter", label: "Filter", path: "M22 3H2l8 9.46V19l4 2v-8.54L22 3z" },
    { id: "download", label: "Download", path: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" },
    { id: "upload", label: "Upload", path: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12" },
    { id: "share", label: "Share", path: "M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8M16 6l-4-4-4 4M12 2v13" },
  ],
  Media: [
    { id: "image", label: "Image", path: "M19 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2zM8.5 10a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3zM21 15l-5-5L5 21" },
    { id: "video", label: "Video", path: "M23 7l-7 5 7 5V7zM14 5H3a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2z" },
    { id: "play", label: "Play", path: "M5 3l14 9-14 9V3z" },
    { id: "pause", label: "Pause", path: "M6 4h4v16H6V4zM14 4h4v16h-4V4z" },
    { id: "volume", label: "Volume", path: "M11 5L6 9H2v6h4l5 4V5zM19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07" },
  ],
  Communication: [
    { id: "mail", label: "Mail", path: "M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2zM22 6l-10 7L2 6" },
    { id: "message", label: "Message", path: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" },
    { id: "bell", label: "Notifications", path: "M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9M13.73 21a2 2 0 0 1-3.46 0" },
    { id: "phone", label: "Phone", path: "M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z" },
  ],
  User: [
    { id: "user", label: "User", path: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z" },
    { id: "users", label: "Users", path: "M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" },
    { id: "settings", label: "Settings", path: "M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z" },
    { id: "lock", label: "Lock", path: "M19 11H5a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7a2 2 0 0 0-2-2zM7 11V7a5 5 0 0 1 10 0v4" },
  ],
  Files: [
    { id: "file", label: "File", path: "M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9zM13 2v7h7" },
    { id: "folder", label: "Folder", path: "M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" },
    { id: "save", label: "Save", path: "M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2zM17 21v-8H7v8M7 3v5h8" },
  ],
  Status: [
    { id: "check", label: "Check", path: "M20 6L9 17l-5-5" },
    { id: "x", label: "Close", path: "M18 6L6 18M6 6l12 12" },
    { id: "alert", label: "Alert", path: "M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0zM12 9v4M12 17h.01" },
    { id: "info", label: "Info", path: "M12 16v-4M12 8h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0z" },
    { id: "star", label: "Star", path: "M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" },
    { id: "heart", label: "Heart", path: "M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z" },
  ],
  Layout: [
    { id: "grid", label: "Grid", path: "M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z" },
    { id: "columns", label: "Columns", path: "M12 3h7a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-7V3zM5 3h7v18H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z" },
    { id: "maximize", label: "Maximize", path: "M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3" },
    { id: "more-h", label: "More horizontal", path: "M12 12h.01M19 12h.01M5 12h.01" },
    { id: "more-v", label: "More vertical", path: "M12 12h.01M12 19h.01M12 5h.01" },
  ],
};

/** Named menu/toolbar layouts — used by “Import from layout” dropdown. */
export const MENU_LAYOUTS = {
  "File menu (desktop)": [
    { kind: "text", label: "File", w: 48, h: 28 },
    { kind: "text", label: "Edit", w: 48, h: 28 },
    { kind: "text", label: "View", w: 52, h: 28 },
    { kind: "text", label: "Help", w: 52, h: 28 },
    { kind: "icon", iconId: "save", w: 32, h: 32 },
    { kind: "icon", iconId: "folder", w: 32, h: 32 },
    { kind: "icon", iconId: "download", w: 32, h: 32 },
  ],
  "Mobile bottom nav": [
    { kind: "icon", iconId: "home", label: "Home", w: 64, h: 56 },
    { kind: "icon", iconId: "search", label: "Search", w: 64, h: 56 },
    { kind: "icon", iconId: "plus", label: "Create", w: 64, h: 56 },
    { kind: "icon", iconId: "bell", label: "Alerts", w: 64, h: 56 },
    { kind: "icon", iconId: "user", label: "Profile", w: 64, h: 56 },
  ],
  "Toolbar (editor)": [
    { kind: "icon", iconId: "edit", w: 36, h: 36 },
    { kind: "icon", iconId: "copy", w: 36, h: 36 },
    { kind: "icon", iconId: "trash", w: 36, h: 36 },
    { kind: "linebreak", w: 8, h: 36 },
    { kind: "icon", iconId: "arrow-left", w: 36, h: 36 },
    { kind: "icon", iconId: "arrow-right", w: 36, h: 36 },
    { kind: "linebreak", w: 8, h: 36 },
    { kind: "icon", iconId: "share", w: 36, h: 36 },
    { kind: "icon", iconId: "settings", w: 36, h: 36 },
  ],
  "Sidebar (settings)": [
    { kind: "panel", label: "Account", w: 200, h: 40, color: "#f3f4f6" },
    { kind: "panel", label: "Notifications", w: 200, h: 40, color: "#f3f4f6" },
    { kind: "linebreak", w: 200, h: 12 },
    { kind: "panel", label: "Privacy", w: 200, h: 40, color: "#f3f4f6" },
    { kind: "panel", label: "Billing", w: 200, h: 40, color: "#f3f4f6" },
    { kind: "icon", iconId: "lock", w: 32, h: 32 },
    { kind: "textfield", label: "Search settings…", w: 200, h: 36 },
  ],
  "Dashboard header": [
    { kind: "icon", iconId: "menu", w: 40, h: 40 },
    { kind: "text", label: "Dashboard", w: 120, h: 32 },
    { kind: "textfield", label: "Search…", w: 220, h: 36 },
    { kind: "icon", iconId: "bell", w: 40, h: 40 },
    { kind: "icon", iconId: "user", w: 40, h: 40 },
  ],
  "Card actions row": [
    { kind: "icon", iconId: "heart", w: 32, h: 32 },
    { kind: "icon", iconId: "message", w: 32, h: 32 },
    { kind: "icon", iconId: "share", w: 32, h: 32 },
    { kind: "icon", iconId: "star", w: 32, h: 32 },
  ],
};

export function iconById(id) {
  for (const items of Object.values(ICON_CATEGORIES)) {
    const found = items.find((i) => i.id === id);
    if (found) return found;
  }
  return { id, label: id, path: "M12 2v20M2 12h20" };
}

export function allIconsFlat() {
  const out = [];
  for (const [cat, items] of Object.entries(ICON_CATEGORIES)) {
    for (const item of items) out.push({ ...item, category: cat });
  }
  return out;
}
