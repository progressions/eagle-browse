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
    tag: null,
    search: "",
    filtered: [],
    shown: 0,
    smartById: {}, // id -> node with path name
  };

  const $ = (id) => document.getElementById(id);
  const el = {
    status: $("status"),
    grid: $("grid"),
    chars: $("chars"),
    drawerChars: $("drawer-chars"),
    folderTree: $("folder-tree"),
    smartTree: $("smart-tree"),
    tagList: $("tag-list"),
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
    state.character = null;
    state.folderId = null;
    state.folderDescendants = null;
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
        const hay = [item.name, item.ext, ...(item.tags || [])]
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
    appendPage();
    setStatus(
      `${out.length.toLocaleString()} items` +
        (state.catalog.built_at ? ` · index ${state.catalog.built_at}` : "")
    );
  }

  function renderFilterChips() {
    const bits = [];
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

  function openLightbox(item) {
    el.lbStage.innerHTML = "";
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
    el.lbMeta.innerHTML = `
      <div class="name">${escapeHtml(item.name)}.${escapeHtml(item.ext || "")}</div>
      <div>${item.w || "?"}×${item.h || "?"} · ${formatBytes(item.size || 0)}</div>
      <div class="tags">${escapeHtml(tags || "(no tags)")}</div>
    `;
    el.lightbox.classList.remove("hidden");
    history.pushState({ lb: true }, "");
  }

  function closeLightbox() {
    el.lightbox.classList.add("hidden");
    el.lbStage.innerHTML = "";
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
    if (el.smartTree) {
      el.smartTree.querySelectorAll(".folder-item").forEach((btn) => {
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
  $("lb-close").addEventListener("click", () => {
    closeLightbox();
    if (history.state && history.state.lb) history.back();
  });
  window.addEventListener("popstate", () => {
    if (!el.lightbox.classList.contains("hidden")) closeLightbox();
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
