/* Eagle phone browse — local LAN client */
(() => {
  "use strict";

  const PAGE = 60;
  const VIDEO_EXT = new Set(["mp4", "mov", "webm", "mkv", "m4v", "avi", "wmv", "flv"]);
  const AUDIO_EXT = new Set([
    "mp3", "wav", "flac", "aac", "m4a", "ogg", "wma", "aiff", "aif",
  ]);
  const IMAGE_EXT = new Set([
    "png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff", "avif",
    "heic", "heif", "svg", "ico",
  ]);

  const state = {
    catalog: null,
    character: null, // 'eunbi' | 'sofie' | null
    folderId: null,
    folderDescendants: null, // Set
    smartFolderId: null,
    smartConditions: null, // inherited condition groups
    smartFolderName: null,
    specialView: null, // null | "untagged" | "uncategorized"
    tag: null,
    search: "",
    filtered: [],
    shown: 0,
    smartById: {}, // id -> node with path name
    lbIndex: -1,
  };

  const $ = (id) => document.getElementById(id);
  const el = {
    status: $("status"),
    grid: $("grid"),
    chars: $("chars"),
    drawerChars: $("drawer-chars"),
    folderTree: $("folder-tree"),
    smartTree: $("smart-tree"),
    viewList: $("view-list"),
    tagList: $("tag-list"),
    pageTitle: $("page-title"),
    smartPicker: $("smart-picker"),
    smartPickerList: $("smart-picker-list"),
    smartPickerViews: $("smart-picker-views"),
    smartFilter: $("smart-filter"),
    drawer: $("drawer"),
    scrim: $("scrim"),
    searchBar: $("search-bar"),
    search: $("search"),
    activeFilters: $("active-filters"),
    lightbox: $("lightbox"),
    lbStage: $("lb-stage"),
    lbMeta: $("lb-meta"),
    sentinel: $("sentinel"),
  };

  function setStatus(text) {
    el.status.textContent = text;
  }

  function openDrawer(open) {
    el.drawer.classList.toggle("open", open);
    el.drawer.setAttribute("aria-hidden", open ? "false" : "true");
    el.scrim.classList.toggle("hidden", !open);
  }

  function folderMap(nodes, parentId = null, acc = {}) {
    for (const n of nodes || []) {
      acc[n.id] = { ...n, parentId };
      folderMap(n.children, n.id, acc);
    }
    return acc;
  }

  function descendants(folderId, fmap) {
    const ids = new Set([folderId]);
    const walk = (id) => {
      for (const [fid, f] of Object.entries(fmap)) {
        if (f.parentId === id) {
          ids.add(fid);
          walk(fid);
        }
      }
    };
    walk(folderId);
    return ids;
  }

  function isVideo(ext) {
    return VIDEO_EXT.has(String(ext || "").toLowerCase());
  }

  function asStrList(value) {
    if (value == null) return [];
    if (Array.isArray(value)) return value.map(String);
    return [String(value)];
  }

  function typeMatches(ext, value) {
    const v = String(value || "").toLowerCase().replace(/^\./, "");
    const e = String(ext || "").toLowerCase().replace(/^\./, "");
    if (v === "video") return VIDEO_EXT.has(e);
    if (v === "audio") return AUDIO_EXT.has(e);
    if (v === "image" || v === "img" || v === "photo") return IMAGE_EXT.has(e);
    return e === v;
  }

  /** Same semantics as library.py _eval_rule / eval_smart_conditions */
  function evalRule(item, propertyName, method, value) {
    method = String(method || "").toLowerCase();
    const prop = String(propertyName || "").toLowerCase();
    const tags = new Set((item.tags || []).map(String));
    const folders = new Set((item.folders || []).map(String));
    const ext = String(item.ext || "").toLowerCase();
    const name = String(item.name || "").toLowerCase();

    if (prop === "tags") {
      const vals = new Set(asStrList(value));
      if (method === "intersection" || method === "union") {
        for (const v of vals) if (tags.has(v)) return true;
        return false;
      }
      if (method === "identity") {
        for (const v of vals) if (tags.has(v)) return false;
        return true;
      }
      if (method === "equal") {
        if (tags.size !== vals.size) return false;
        for (const v of vals) if (!tags.has(v)) return false;
        return true;
      }
      if (method === "unequal") {
        if (tags.size !== vals.size) return true;
        for (const v of vals) if (!tags.has(v)) return true;
        return false;
      }
      return false;
    }

    if (prop === "folders") {
      const vals = new Set(asStrList(value));
      if (method === "intersection" || method === "union") {
        for (const v of vals) if (folders.has(v)) return true;
        return false;
      }
      if (method === "identity") {
        for (const v of vals) if (folders.has(v)) return false;
        return true;
      }
      if (method === "equal") {
        if (folders.size !== vals.size) return false;
        for (const v of vals) if (!folders.has(v)) return false;
        return true;
      }
      if (method === "unequal") {
        if (folders.size !== vals.size) return true;
        for (const v of vals) if (!folders.has(v)) return true;
        return false;
      }
      return false;
    }

    if (prop === "type") {
      const ok = typeMatches(ext, value);
      if (method === "equal") return ok;
      if (method === "unequal") return !ok;
      if (method === "intersection" || method === "union") return ok;
      if (method === "identity") return !ok;
      return false;
    }

    if (prop === "name") {
      const v = String(value || "").toLowerCase();
      if (method === "contain") return name.includes(v);
      if (method === "uncontain") return !name.includes(v);
      if (method === "equal") return name === v;
      if (method === "unequal") return name !== v;
      return false;
    }

    if (prop === "rating") {
      const star = item.star == null ? 0 : Number(item.star) || 0;
      const target = parseInt(value, 10);
      if (Number.isNaN(target)) return false;
      if (method === "equal") return star === target;
      if (method === "unequal") return star !== target;
      if (method === "gt" || method === "greater") return star > target;
      if (method === "lt" || method === "less") return star < target;
      if (method === "gte" || method === "ge") return star >= target;
      if (method === "lte" || method === "le") return star <= target;
      return false;
    }

    if (prop === "annotation") {
      const text = String(item.annotation || "").toLowerCase();
      const v = String(value || "").toLowerCase();
      if (method === "contain") return text.includes(v);
      if (method === "uncontain") return !text.includes(v);
      if (method === "equal") return text === v;
      if (method === "unequal") return text !== v;
      return false;
    }

    return false;
  }

  function evalGroup(item, group) {
    const rules = group.rules || [];
    const match = String(group.match || "AND").toUpperCase();
    const boolean = String(group.boolean || "TRUE").toUpperCase();
    let ok = true;
    if (rules.length) {
      const results = rules.map((r) =>
        evalRule(item, r.property, r.method, r.value)
      );
      ok = match === "OR" ? results.some(Boolean) : results.every(Boolean);
    }
    if (boolean === "FALSE") ok = !ok;
    return ok;
  }

  function evalSmartConditions(item, conditions) {
    if (!conditions || !conditions.length) return true;
    return conditions.every((g) => evalGroup(item, g));
  }

  function indexSmartFolders(nodes, prefix = []) {
    for (const n of nodes || []) {
      const path = prefix.concat(n.name);
      state.smartById[n.id] = {
        id: n.id,
        name: n.name,
        path: path.join(" / "),
        inherited: n.inherited || n.conditions || [],
        children: n.children || [],
      };
      indexSmartFolders(n.children, path);
    }
  }

  function selectSmartFolder(id) {
    if (!id) {
      state.smartFolderId = null;
      state.smartConditions = null;
      state.smartFolderName = null;
      return;
    }
    const node = state.smartById[id];
    if (!node) return;
    state.smartFolderId = id;
    state.smartConditions = node.inherited;
    state.smartFolderName = node.path;
    // Smart folder is the primary Eagle-like view; drop competing scope filters
    state.specialView = null;
    state.character = null;
    state.folderId = null;
    state.folderDescendants = null;
  }

  function selectSpecialView(view) {
    state.specialView = view || null;
    if (view) {
      selectSmartFolder(null);
      state.folderId = null;
      state.folderDescendants = null;
    }
  }

  function scopeTitle() {
    if (state.smartFolderName) return state.smartFolderName;
    if (state.specialView === "untagged") return "Untagged";
    if (state.specialView === "uncategorized") return "Uncategorized";
    return "Eagle";
  }

  function applyFilters() {
    if (!state.catalog) return;
    const q = state.search.trim().toLowerCase();
    const tokens = q ? q.split(/\s+/).filter(Boolean) : [];
    const charFolder = state.character
      ? (state.catalog.characters || {})[state.character]
      : null;
    const charTag = state.character;

    const out = [];
    for (const item of state.catalog.items) {
      if (state.specialView === "untagged") {
        if ((item.tags || []).length) continue;
      } else if (state.specialView === "uncategorized") {
        if ((item.folders || []).length) continue;
      }

      if (state.smartConditions) {
        if (!evalSmartConditions(item, state.smartConditions)) continue;
      }

      if (state.folderId && state.folderDescendants) {
        let hit = false;
        for (const f of item.folders || []) {
          if (state.folderDescendants.has(f)) {
            hit = true;
            break;
          }
        }
        if (!hit) continue;
      }

      if (state.character) {
        const inFolder =
          charFolder && (item.folders || []).includes(charFolder);
        const hasTag = (item.tags || []).some(
          (t) => String(t).toLowerCase() === charTag
        );
        if (!inFolder && !hasTag) continue;
      }

      if (state.tag) {
        const want = state.tag.toLowerCase();
        if (!(item.tags || []).some((t) => String(t).toLowerCase() === want)) {
          continue;
        }
      }

      if (tokens.length) {
        const hay = [item.id, item.name, item.ext, ...(item.tags || [])]
          .join(" ")
          .toLowerCase();
        if (!tokens.every((t) => hay.includes(t))) continue;
      }

      out.push(item);
    }

    state.filtered = out;
    state.shown = 0;
    el.grid.innerHTML = "";
    renderFilterChips();
    highlightNav();
    if (el.pageTitle) el.pageTitle.textContent = scopeTitle();
    appendPage();
    setStatus(
      `${out.length.toLocaleString()} items` +
        (state.catalog.built_at ? ` · index ${state.catalog.built_at}` : "")
    );
  }

  function renderFilterChips() {
    const bits = [];
    if (state.specialView === "untagged") {
      bits.push(chipHtml("Untagged", "special"));
    }
    if (state.specialView === "uncategorized") {
      bits.push(chipHtml("Uncategorized", "special"));
    }
    if (state.smartFolderId) {
      bits.push(
        chipHtml(`◈ ${state.smartFolderName || "smart"}`, "smart")
      );
    }
    if (state.character) {
      bits.push(chipHtml(state.character, "character"));
    }
    if (state.folderId) {
      const fmap = folderMap(state.catalog.folders);
      const name = (fmap[state.folderId] || {}).name || state.folderId;
      bits.push(chipHtml(`folder: ${name}`, "folder"));
    }
    if (state.tag) {
      bits.push(chipHtml(`#${state.tag}`, "tag"));
    }
    if (state.search.trim()) {
      bits.push(chipHtml(`“${state.search.trim()}”`, "search"));
    }
    el.activeFilters.innerHTML = bits.join("");
    el.activeFilters.querySelectorAll("[data-clear]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const kind = btn.getAttribute("data-clear");
        if (kind === "smart") selectSmartFolder(null);
        if (kind === "special") selectSpecialView(null);
        if (kind === "character") state.character = null;
        if (kind === "folder") {
          state.folderId = null;
          state.folderDescendants = null;
        }
        if (kind === "tag") state.tag = null;
        if (kind === "search") {
          state.search = "";
          el.search.value = "";
        }
        syncCharChips();
        applyFilters();
      });
    });
  }

  function chipHtml(label, clearKey) {
    return `<button type="button" class="chip on" data-clear="${clearKey}">${escapeHtml(
      label
    )} ×</button>`;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function appendPage() {
    const slice = state.filtered.slice(state.shown, state.shown + PAGE);
    if (state.shown === 0 && slice.length === 0) {
      el.grid.innerHTML = `<div class="empty">No items match these filters.</div>`;
      return;
    }
    const frag = document.createDocumentFragment();
    for (const item of slice) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "cell";
      btn.dataset.id = item.id;
      const img = document.createElement("img");
      img.loading = "lazy";
      img.decoding = "async";
      img.alt = item.name || "";
      img.src = `/thumb/${encodeURIComponent(item.id)}`;
      btn.appendChild(img);
      if (isVideo(item.ext)) {
        const b = document.createElement("span");
        b.className = "badge";
        b.textContent = item.ext || "video";
        btn.appendChild(b);
      }
      if (item.star) {
        const s = document.createElement("span");
        s.className = "star";
        s.textContent = "★".repeat(Math.min(5, item.star));
        btn.appendChild(s);
      }
      btn.addEventListener("click", () => openLightbox(item));
      frag.appendChild(btn);
    }
    el.grid.appendChild(frag);
    state.shown += slice.length;
  }

  function lightboxIndexOf(item) {
    if (!item) return -1;
    return state.filtered.findIndex((it) => it.id === item.id);
  }

  function openLightbox(item) {
    const idx = lightboxIndexOf(item);
    state.lbIndex = idx >= 0 ? idx : 0;
    const show = state.filtered[state.lbIndex] || item;
    renderLightbox(show);
    el.lightbox.classList.remove("hidden");
    if (!history.state || !history.state.lb) history.pushState({ lb: true }, "");
  }

  function renderLightbox(item) {
    el.lbStage.innerHTML = "";
    el.lbStage.style.transform = "";
    const url = `/media/${encodeURIComponent(item.id)}`;
    if (isVideo(item.ext)) {
      const v = document.createElement("video");
      v.src = url;
      v.controls = true;
      v.playsInline = true;
      v.autoplay = true;
      el.lbStage.appendChild(v);
    } else {
      const img = document.createElement("img");
      img.src = url;
      img.alt = item.name || "";
      el.lbStage.appendChild(img);
    }
    const tags = (item.tags || []).join(", ");
    const pos =
      state.lbIndex >= 0
        ? `${state.lbIndex + 1} / ${state.filtered.length}`
        : "";
    el.lbMeta.innerHTML = `
      <div class="name">${escapeHtml(item.name)}.${escapeHtml(item.ext || "")}</div>
      <div>${item.w || "?"}×${item.h || "?"} · ${formatBytes(item.size || 0)}${
        pos ? ` · ${pos}` : ""
      }</div>
      <div class="tags">${escapeHtml(tags || "(no tags)")}</div>
    `;
  }

  function stepLightbox(delta) {
    if (el.lightbox.classList.contains("hidden")) return;
    const next = state.lbIndex + delta;
    if (next < 0) {
      dismissLightbox();
      return;
    }
    if (next >= state.filtered.length) return;
    state.lbIndex = next;
    renderLightbox(state.filtered[next]);
  }

  function dismissLightbox() {
    closeLightbox();
    if (history.state && history.state.lb) history.back();
  }

  function closeLightbox() {
    el.lightbox.classList.add("hidden");
    el.lbStage.innerHTML = "";
    el.lbStage.style.transform = "";
    state.lbIndex = -1;
  }

  function formatBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  }

  function syncCharChips() {
    document.querySelectorAll("[data-character]").forEach((btn) => {
      const c = btn.getAttribute("data-character");
      const on = c === "" ? !state.character : state.character === c;
      btn.classList.toggle("on", on);
    });
  }

  function highlightNav() {
    document.querySelectorAll("[data-special]").forEach((btn) => {
      const v = btn.getAttribute("data-special") || "";
      const on = v
        ? state.specialView === v
        : !state.specialView && !state.smartFolderId;
      btn.classList.toggle("on", on);
    });
    if (el.smartTree) {
      el.smartTree.querySelectorAll(".folder-item").forEach((btn) => {
        btn.classList.toggle(
          "on",
          btn.dataset.smartId === state.smartFolderId
        );
      });
    }
    if (el.smartPickerList) {
      el.smartPickerList.querySelectorAll(".folder-item").forEach((btn) => {
        btn.classList.toggle(
          "on",
          btn.dataset.smartId === state.smartFolderId
        );
      });
    }
    if (el.folderTree) {
      el.folderTree.querySelectorAll(".folder-item").forEach((btn) => {
        btn.classList.toggle("on", btn.dataset.folderId === state.folderId);
      });
    }
  }

  function flattenSmart(nodes, depth, acc) {
    for (const n of nodes || []) {
      acc.push({ node: n, depth });
      flattenSmart(n.children, depth + 1, acc);
    }
    return acc;
  }

  function chooseSmart(id) {
    selectSmartFolder(id);
    closeSmartPicker();
    openDrawer(false);
    syncCharChips();
    applyFilters();
  }

  function chooseSpecial(view) {
    if (view) selectSpecialView(view);
    else {
      selectSpecialView(null);
      selectSmartFolder(null);
    }
    closeSmartPicker();
    openDrawer(false);
    syncCharChips();
    applyFilters();
  }

  function openSmartPicker() {
    if (!el.smartPicker) return;
    openDrawer(false);
    renderSmartPicker();
    el.smartPicker.classList.remove("hidden");
    if (!history.state || !history.state.sf) history.pushState({ sf: true }, "");
    if (el.smartFilter) {
      el.smartFilter.value = "";
      el.smartFilter.focus();
    }
  }

  function closeSmartPicker() {
    if (!el.smartPicker) return;
    el.smartPicker.classList.add("hidden");
  }

  function renderSmartPicker() {
    if (!el.smartPickerViews || !el.smartPickerList) return;
    el.smartPickerViews.innerHTML = "";
    const views = [
      { id: "", label: "All" },
      { id: "untagged", label: "Untagged" },
      { id: "uncategorized", label: "Uncategorized" },
    ];
    for (const v of views) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chip";
      btn.dataset.special = v.id;
      btn.textContent = v.label;
      btn.addEventListener("click", () => chooseSpecial(v.id || null));
      el.smartPickerViews.appendChild(btn);
    }

    const q = (el.smartFilter && el.smartFilter.value.trim().toLowerCase()) || "";
    el.smartPickerList.innerHTML = "";
    const rows = flattenSmart(state.catalog.smart_folders || [], 0, []);
    let shown = 0;
    for (const { node, depth } of rows) {
      const path = (state.smartById[node.id] || {}).path || node.name;
      if (q && !path.toLowerCase().includes(q) && !String(node.name).toLowerCase().includes(q)) {
        continue;
      }
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "folder-item picker-row";
      if (depth === 0) btn.classList.add("smart-root");
      if ((node.children || []).length) btn.classList.add("has-kids");
      btn.dataset.smartId = node.id;
      const name = document.createElement("span");
      name.className = "picker-name";
      name.textContent = node.name;
      btn.appendChild(name);
      if (depth > 0) {
        const sub = document.createElement("span");
        sub.className = "picker-path";
        sub.textContent = path;
        btn.appendChild(sub);
      }
      btn.style.paddingLeft = `${12 + depth * 16}px`;
      btn.addEventListener("click", () => chooseSmart(node.id));
      el.smartPickerList.appendChild(btn);
      shown += 1;
    }
    if (!shown) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = q ? "No folders match." : "No smart folders in the index.";
      el.smartPickerList.appendChild(empty);
    }
    highlightNav();
  }

  function renderChrome() {
    state.smartById = {};
    indexSmartFolders(state.catalog.smart_folders || []);

    const chars = state.catalog.characters || {};
    const order = ["eunbi", "sofie"];
    const make = (parent) => {
      parent.innerHTML = "";
      const all = document.createElement("button");
      all.type = "button";
      all.className = "chip";
      all.dataset.character = "";
      all.textContent = "All";
      all.addEventListener("click", () => {
        state.character = null;
        // keep smart folder if set
        syncCharChips();
        applyFilters();
      });
      parent.appendChild(all);
      for (const key of order) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "chip";
        btn.dataset.character = key;
        btn.textContent = key[0].toUpperCase() + key.slice(1);
        if (!chars[key]) {
          btn.title = "Folder id not in index; filtering by tag only";
        }
        btn.addEventListener("click", () => {
          state.character = key;
          // character shortcut exits smart-folder mode
          selectSmartFolder(null);
          syncCharChips();
          applyFilters();
        });
        parent.appendChild(btn);
      }
    };
    make(el.chars);
    make(el.drawerChars);
    syncCharChips();

    // Virtual views (same as desktop sidebar)
    if (el.viewList) {
      el.viewList.innerHTML = "";
      const addView = (id, label) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "folder-item";
        btn.dataset.special = id;
        btn.textContent = label;
        btn.addEventListener("click", () => chooseSpecial(id || null));
        el.viewList.appendChild(btn);
      };
      addView("", "All");
      addView("untagged", "Untagged");
      addView("uncategorized", "Uncategorized");
    }

    // Smart folder tree
    el.smartTree.innerHTML = "";
    const clearSf = document.createElement("button");
    clearSf.type = "button";
    clearSf.className = "folder-item";
    clearSf.textContent = "— none —";
    clearSf.addEventListener("click", () => {
      selectSmartFolder(null);
      openDrawer(false);
      syncCharChips();
      applyFilters();
    });
    el.smartTree.appendChild(clearSf);

    const addSmart = (node, depth) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "folder-item";
      if (depth === 0) btn.classList.add("smart-root");
      if ((node.children || []).length) btn.classList.add("has-kids");
      btn.style.paddingLeft = `${8 + depth * 14}px`;
      btn.dataset.smartId = node.id;
      btn.textContent = node.name;
      btn.addEventListener("click", () => {
        selectSmartFolder(node.id);
        openDrawer(false);
        syncCharChips();
        applyFilters();
      });
      el.smartTree.appendChild(btn);
      for (const c of node.children || []) addSmart(c, depth + 1);
    };
    for (const n of state.catalog.smart_folders || []) addSmart(n, 0);

    // Folder tree
    el.folderTree.innerHTML = "";
    const addFolder = (node, depth) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "folder-item";
      btn.style.paddingLeft = `${8 + depth * 14}px`;
      btn.dataset.folderId = node.id;
      btn.textContent = node.name;
      btn.addEventListener("click", () => {
        state.folderId = node.id;
        const fmap = folderMap(state.catalog.folders);
        state.folderDescendants = descendants(node.id, fmap);
        selectSmartFolder(null);
        selectSpecialView(null);
        openDrawer(false);
        applyFilters();
      });
      el.folderTree.appendChild(btn);
      for (const c of node.children || []) addFolder(c, depth + 1);
    };
    for (const n of state.catalog.folders || []) addFolder(n, 0);

    // Top tags
    el.tagList.innerHTML = "";
    const tags = (state.catalog.tags || []).slice(0, 40);
    for (const t of tags) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chip";
      const count = (state.catalog.tag_counts || {})[t];
      btn.textContent = count != null ? `${t} (${count})` : t;
      btn.addEventListener("click", () => {
        state.tag = t;
        openDrawer(false);
        applyFilters();
      });
      el.tagList.appendChild(btn);
    }
  }

  async function loadCatalog() {
    setStatus("Loading catalog…");
    const res = await fetch("/api/catalog");
    if (!res.ok) throw new Error(`catalog ${res.status}`);
    state.catalog = await res.json();
    renderChrome();
    // Prefer smart folder "Eunbi" if present; else character chip
    const eunbiSmart = Object.values(state.smartById).find(
      (n) => n.name === "Eunbi" && n.path === "Eunbi"
    );
    if (eunbiSmart) {
      selectSmartFolder(eunbiSmart.id);
    } else if ((state.catalog.characters || {}).eunbi) {
      state.character = "eunbi";
    }
    syncCharChips();
    applyFilters();
  }

  const io = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        if (e.isIntersecting && state.shown < state.filtered.length) {
          appendPage();
        }
      }
    },
    { rootMargin: "400px" }
  );
  io.observe(el.sentinel);

  $("btn-nav").addEventListener("click", () => openDrawer(true));
  $("btn-close-nav").addEventListener("click", () => openDrawer(false));
  el.scrim.addEventListener("click", () => openDrawer(false));
  $("btn-smart").addEventListener("click", () => openSmartPicker());
  $("btn-close-smart").addEventListener("click", () => {
    closeSmartPicker();
    if (history.state && history.state.sf) history.back();
  });
  const btnSmartAll = $("btn-smart-all");
  if (btnSmartAll) btnSmartAll.addEventListener("click", () => openSmartPicker());
  if (el.smartFilter) {
    el.smartFilter.addEventListener("input", () => renderSmartPicker());
  }
  $("btn-search").addEventListener("click", () => {
    el.searchBar.classList.toggle("hidden");
    if (!el.searchBar.classList.contains("hidden")) el.search.focus();
  });
  $("btn-clear-search").addEventListener("click", () => {
    el.search.value = "";
    state.search = "";
    applyFilters();
  });
  let searchTimer = null;
  el.search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.search = el.search.value;
      applyFilters();
    }, 200);
  });
  $("lb-close").addEventListener("click", () => dismissLightbox());

  // Swipe: left = next; right = previous (first image closes back to the grid).
  {
    const SWIPE_MIN = 56;
    let sx = 0;
    let sy = 0;
    let tracking = false;
    let locked = "";

    const onStart = (x, y) => {
      sx = x;
      sy = y;
      tracking = true;
      locked = "";
      el.lbStage.style.transition = "none";
    };

    const onMove = (x, y, ev) => {
      if (!tracking) return;
      const dx = x - sx;
      const dy = y - sy;
      if (!locked) {
        if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return;
        locked = Math.abs(dx) > Math.abs(dy) * 1.15 ? "h" : "v";
      }
      if (locked === "h") {
        ev.preventDefault();
        el.lbStage.style.transform = `translateX(${dx}px)`;
      }
    };

    const onEnd = (x, y) => {
      if (!tracking) return;
      tracking = false;
      const dx = x - sx;
      const dy = y - sy;
      el.lbStage.style.transition = "";
      el.lbStage.style.transform = "";
      if (locked !== "h") return;
      if (Math.abs(dx) < SWIPE_MIN || Math.abs(dx) < Math.abs(dy)) return;
      if (dx < 0) stepLightbox(1);
      else stepLightbox(-1);
    };

    el.lightbox.addEventListener(
      "touchstart",
      (e) => {
        if (e.touches.length !== 1) return;
        onStart(e.touches[0].clientX, e.touches[0].clientY);
      },
      { passive: true }
    );
    el.lightbox.addEventListener(
      "touchmove",
      (e) => {
        if (!tracking || e.touches.length !== 1) return;
        onMove(e.touches[0].clientX, e.touches[0].clientY, e);
      },
      { passive: false }
    );
    el.lightbox.addEventListener("touchend", (e) => {
      const t = e.changedTouches[0];
      if (t) onEnd(t.clientX, t.clientY);
      else {
        tracking = false;
        el.lbStage.style.transform = "";
      }
    });
    el.lightbox.addEventListener("touchcancel", () => {
      tracking = false;
      el.lbStage.style.transition = "";
      el.lbStage.style.transform = "";
    });
  }

  window.addEventListener("keydown", (e) => {
    if (el.lightbox.classList.contains("hidden")) return;
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      stepLightbox(-1);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      stepLightbox(1);
    } else if (e.key === "Escape") {
      e.preventDefault();
      dismissLightbox();
    }
  });
  window.addEventListener("popstate", () => {
    if (!el.lightbox.classList.contains("hidden")) closeLightbox();
    if (el.smartPicker && !el.smartPicker.classList.contains("hidden")) {
      closeSmartPicker();
    }
  });
  $("btn-reload").addEventListener("click", async () => {
    setStatus("Reloading index…");
    await fetch("/api/reload");
    await loadCatalog();
    openDrawer(false);
  });
  $("btn-rebuild").addEventListener("click", async () => {
    setStatus("Rebuilding index (slow)…");
    openDrawer(false);
    const res = await fetch("/api/rebuild");
    if (!res.ok) {
      setStatus("Rebuild failed");
      return;
    }
    await loadCatalog();
  });

  let updatesSince = 0;
  async function pollUpdates() {
    if (!state.catalog) return;
    try {
      const res = await fetch(`/api/updates?since=${updatesSince}`);
      if (!res.ok) return;
      const data = await res.json();
      if (typeof data.ts === "number") updatesSince = data.ts;
      const incoming = Array.isArray(data.items) ? data.items : [];
      if (!incoming.length) return;
      const have = new Set((state.catalog.items || []).map((it) => it.id));
      const fresh = incoming.filter((it) => it && it.id && !have.has(it.id));
      if (!fresh.length) return;
      state.catalog.items.unshift(...fresh.reverse());
      state.catalog.item_count = (state.catalog.item_count || 0) + fresh.length;
      applyFilters();
    } catch (_err) {
      /* LAN blip — try again next tick */
    }
  }

  loadCatalog()
    .then(async () => {
      try {
        const res = await fetch("/api/updates?since=0");
        if (res.ok) {
          const data = await res.json();
          if (typeof data.ts === "number") updatesSince = data.ts;
        }
      } catch (_err) {
        updatesSince = Date.now() / 1000;
      }
      setInterval(pollUpdates, 1000);
    })
    .catch((err) => {
    console.error(err);
    setStatus("Failed to load catalog — is the server running?");
    el.grid.innerHTML = `<div class="empty">Could not load /api/catalog.<br>${escapeHtml(
      String(err)
    )}</div>`;
  });
})();
