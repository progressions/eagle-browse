/* Eagle phone browse — local LAN client */
(() => {
  "use strict";

  const PAGE = 60;
  const VIDEO_EXT = new Set(["mp4", "mov", "webm", "mkv", "m4v", "avi", "wmv", "flv"]);
  const state = {
    catalog: null,
    character: null, // 'eunbi' | 'sofie' | null
    folderId: null,
    folderDescendants: null, // Set
    tag: null,
    search: "",
    filtered: [],
    shown: 0,
  };

  const $ = (id) => document.getElementById(id);
  const el = {
    status: $("status"),
    grid: $("grid"),
    chars: $("chars"),
    drawerChars: $("drawer-chars"),
    folderTree: $("folder-tree"),
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

  function applyFilters() {
    if (!state.catalog) return;
    const q = state.search.trim().toLowerCase();
    const tokens = q ? q.split(/\s+/).filter(Boolean) : [];
    const charFolder = state.character
      ? (state.catalog.characters || {})[state.character]
      : null;
    const charTag = state.character; // eunbi / sofie tag match

    const out = [];
    for (const item of state.catalog.items) {
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
        // Folder OR tag so filed-but-untagged and tagged-but-unfiled both show
        if (!inFolder && !hasTag) continue;
      }

      if (state.tag) {
        const want = state.tag.toLowerCase();
        if (!(item.tags || []).some((t) => String(t).toLowerCase() === want)) {
          continue;
        }
      }

      if (tokens.length) {
        const hay = [
          item.name,
          item.ext,
          ...(item.tags || []),
        ]
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
    appendPage();
    setStatus(
      `${out.length.toLocaleString()} items` +
        (state.catalog.built_at ? ` · index ${state.catalog.built_at}` : "")
    );
  }

  function renderFilterChips() {
    const bits = [];
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
    const v = el.lbStage.querySelector("video");
    if (v) v.pause();
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

  function renderChrome() {
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
        syncCharChips();
        applyFilters();
      });
      parent.appendChild(all);
      for (const key of order) {
        if (!chars[key] && key !== "eunbi" && key !== "sofie") continue;
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "chip";
        btn.dataset.character = key;
        const label = key[0].toUpperCase() + key.slice(1);
        btn.textContent = label;
        if (!chars[key]) btn.title = "Folder id not in index; filtering by tag only";
        btn.addEventListener("click", () => {
          state.character = key;
          syncCharChips();
          applyFilters();
        });
        parent.appendChild(btn);
      }
    };
    make(el.chars);
    make(el.drawerChars);
    syncCharChips();

    // Folder tree
    el.folderTree.innerHTML = "";
    const addFolder = (node, depth) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "folder-item";
      btn.style.paddingLeft = `${8 + depth * 14}px`;
      btn.textContent = node.name;
      btn.addEventListener("click", () => {
        state.folderId = node.id;
        const fmap = folderMap(state.catalog.folders);
        state.folderDescendants = descendants(node.id, fmap);
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
    // Default to Eunbi if that character folder exists
    if ((state.catalog.characters || {}).eunbi) {
      state.character = "eunbi";
    }
    syncCharChips();
    applyFilters();
  }

  // Infinite scroll
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

  // Events
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

  loadCatalog().catch((err) => {
    console.error(err);
    setStatus("Failed to load catalog — is the server running?");
    el.grid.innerHTML = `<div class="empty">Could not load /api/catalog.<br>${escapeHtml(
      String(err)
    )}</div>`;
  });
})();
