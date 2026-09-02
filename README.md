# Eagle Browse

Keyboard-first browser for an [Eagle.cool](https://eagle.cool) library on Omarchy/Linux.

Browse **smart folders** and regular folders, search tags/names, and **copy the absolute path** of an image so you can paste it into upload dialogs or other tools.

Smart folders are evaluated from Eagle’s `metadata.json` rules (nested filters inherit parents — e.g. `Eunbi` → `images`). Create and edit them in the sidebar.

## Install

Eagle Browse is distributed as an Arch package. Releases are built from Git
tags matching `pkgver` in `PKGBUILD`.

```bash
git clone https://github.com/progressions/eagle-browse.git
cd eagle-browse
makepkg -si
```

The package installs `/usr/bin/eagle-browse`, `eagle-api`, and
`eagle-inbox-watch`, plus the desktop entry, sounds, and user systemd units.
Pacman installs the required Python, GTK, libadwaita, GStreamer, and FFmpeg
dependencies. Optional integrations are listed by `pacman -Qi eagle-browse`.

Installed commands do not run Git or update themselves. Package upgrades own
application updates.

### Migrating from source launchers

After package installation, remove old user symlinks and the copied desktop
entry if they exist:

```bash
unlink ~/.local/bin/eagle-browse
unlink ~/.local/bin/eagle-api
unlink ~/.local/bin/eagle-inbox-watch
unlink ~/.local/bin/phone-browse   # obsolete; phone browse left this package
rm ~/.local/share/applications/eagle-browse.desktop
```

This does not remove the Eagle library, inbox, configuration, state, indexes,
or backups. The packaged desktop entry calls `/usr/bin/eagle-browse` directly.

### Services

The package installs user-unit templates. Machine overrides can be placed in
`~/.config/eagle-browse/environment`, for example:

```bash
EAGLE_LIBRARY=/path/to/Eunbi.library
EAGLE_INBOX=/path/to/intake
```

Enable the inbox watcher only on the machine that should import into the
library (one machine only).

```bash
systemctl --user daemon-reload
systemctl --user enable --now eagle-inbox-watch.service
# If an older package left phone browse installed:
#   systemctl --user disable --now eagle-phone-browse.service
```

### Upgrade, downgrade, and uninstall

Build a newer checked-out tag with `makepkg -si` to upgrade. Downgrade with a
previous package from `/var/cache/pacman/pkg` using `sudo pacman -U`. Uninstall
with `sudo pacman -Rns eagle-browse`. Pacman removes program files and unit
templates only; user data is preserved.

Maintainer release procedure:

1. Update `pkgver` in `PKGBUILD` and the project version in `pyproject.toml`;
   reset `pkgrel` to 1.
2. Run the tests, installed-wheel smoke tests, and a real-library launch.
3. Commit, tag the approved commit as `v<version>`, and push both.
4. Run `makepkg -si` from the tagged checkout.

## Run

The window chrome opens immediately. The library scan (tens of thousands of items) runs in the background; the grid says **Loading library…** until it finishes.

```bash
eagle-browse
```

Or point at another library:

```bash
EAGLE_LIBRARY=/path/to/Something.library eagle-browse
# or
eagle-browse /path/to/Something.library
```

Default library: `~/Dropbox/ISAAC/GENNIE/Eunbi.library`

### Develop while the package is installed

Daily use stays on `/usr/bin/eagle-browse` (Super+Shift+I, desktop entry,
systemd services). To run the git checkout instead:

```bash
cd ~/tech/eagle-browse
./eagle-browse --no-update
```

That launcher execs `app.py` from the checkout directory, so it does not use
the installed `site-packages` copy. Leave `~/.local/bin/eagle-browse` unset so
it does not shadow the package. Full notes: `GENNIE/Ops/isaac-archrepo.md`.

### Source-checkout auto-update

The development shell launchers in a source checkout check `origin` on start
(`git fetch`, 8s timeout). If the remote branch is strictly ahead and the
tracked working tree is clean, they `git pull --ff-only` and re-exec so the new
code runs. Offline, timed out, dirty tree, or diverged history → keep the
current checkout and start normally (no hang).

| Opt-out | How |
|---------|-----|
| One shot | `eagle-browse --no-update` (flag is stripped before Python sees it) |
| Env | `EAGLE_BROWSE_NO_UPDATE=1` |
| Debug | `EAGLE_BROWSE_UPDATE_VERBOSE=1` prints fetch timeouts / skip reasons |

Local uncommitted changes always block the pull (it will print a notice if the
remote is ahead). Untracked files are ignored for that check.

Packaged commands never invoke this source updater.

## Hotkeys

| Key | Action |
|-----|--------|
| `?` | Show the **keyboard command reference** |
| `v` | **Go to folder**: fuzzy-search special views, smart folders, and library folder paths (`uncat` → Intake; `eun im` → `Eunbi / images`) |
| `i` | Open **Intake**, the new assets with no category |
| `←` / `→` or `h` / `l` | Previous / next image |
| `↑` / `↓` or `k` / `j` | Image above / below (one row) |
| `gg` / `Shift+G` | Jump to the first / last asset in the full filtered view |
| `Enter` / `o` | Open larger: **images and videos → inline viewer**, **audio → mpv** |
| `1`–`5` | Set **star rating** (selection; also click stars in the right inspector) |
| `0` | Clear rating |
| `x` | **Crop** focused image (also the crop icon on the viewer toolbar) |
| Viewer **Upscale** button | Queue a still (Z-Image 2× on Jack) or video (SeedVR2 on Eric) through PromptForge. Tags the source `upscaling`. Toasts if PromptForge is down (no lying tag). No hotkey. |
| `n` | Open **Notes** for the focused or selected asset(s) |
| `F2` / `Shift+N` | **Rename** focused file (media + matching thumbnail; Eagle id unchanged) |
| `e` | Open the focused file in Files |
| `Shift+E` | Add the focused video/audio to the current clip-editor project |
| `Ctrl+Shift+E` | New clip-editor project with the focused video/audio |
| `Ctrl+G` | **Open the set** containing the focused grouped asset |
| `gs` | **Group** selected items into a set |
| `Alt+←` / `Alt+→` | **Back** / **Forward** through views (also header buttons) |
| `gr` | **Remove** selection from its set |
| `p` (video playing) | **Save frame** at the current playhead; ffmpeg writes a new untagged still |
| `i` / `o` (video playing) | Mark **in** / **out** at the playhead |
| `x` (video playing) | **Cut** the marked range to a new untagged H.264/AAC clip |
| `Ctrl+A` | **Select all** assets in the current grid view |
| `Delete` / `Backspace` | **Soft-delete** selection (Eagle trash — files stay on disk) |
| `Ctrl+Z` | **Undo** last delete batch (restore items) |

Grid cursor keys stay within the asset grid. Use `b` for the sidebar, `/` for
Search, and the filter and inspector controls' direct shortcuts or the mouse.

### Crop

Opens a modal editor for the focused **image** (hotkey **`x`**, or the **crop icon** in the top header bar):

- Enter **width × height** (source pixels) — the overlay updates to that size
- **Aspect presets:** Free · Orig · 1:1 · **3:4** · 4:3 · **9:16** · 16:9 · 2:3 · 3:2  
  Locked ratios keep the aspect while you drag corners/edges
- **Drag** the crop rectangle to move it; drag **corners/edges** to resize
- Arrow keys (or `h`/`j`/`k`/`l`) nudge 1px; **Shift+arrows** = 10px

Footer actions:

| Button | Keys | Result |
|--------|------|--------|
| **Cancel** | `Esc` | Close without writing |
| **Save as** | `Shift+Enter` | New library item with the crop — **no tags, no folders** (same as a fresh import) |
| **Save** | `Enter` | Overwrite the original media in place (backup under `backup/eagle-browse-writes/`), update `width`/`height`/`size`, regenerate thumbnail |

**Audio:** `x` on an audio file opens a start–end crop. Drag the handles (or type
seconds) to keep a segment. **Play selection** previews it. Same footer:
Cancel / Save as / Save. ffmpeg does the cut.

### Inspector (right sidebar)

Shows the focused asset (or **common** values when multi-selected):

- Thumbnail preview  
- **Rating** — stars + Clear on one row (`1`–`5` / `0`)  
- **Tags / Folders** — pill chips (shared tags plain; partial multi-select marked `±`); pencil icon or click chips to edit  
- **Notes** — card with truncated Eagle annotation; click, pencil, or `n` to view/edit (Ctrl+Enter / Ctrl+S saves)
- Path (single selection, dim line at bottom)
| `t` | **Tags** picker (recent + autocomplete; Enter toggles and clears the filter; Esc closes) |
| `f` | **Folders / categories** picker (same UX as tags) |
| `A` (sidebar) | **Folder auto-tags** for the selected folder (or right-click the folder) |
| `e` (sidebar on a smart folder) | **Edit** that smart folder’s rules |
| `Delete` (sidebar on a smart folder) | **Delete** that smart folder (confirm) |
| Drag a smart folder / `Shift+↑↓` | **Reorder** it (drop on the header to move to the top) |
| `m` | **Filter by type** (or use the **Type** button on the filter bar) |
| `Esc` | Clear marks → clear view filters → clear search |
| `b` | Focus **sidebar** |

Left nav includes **Untagged** and **Intake** virtual views. Intake contains assets with no category.

### Folder auto-tags (Eagle-compatible)

Same as Eagle’s **Auto tagging** on a folder: each folder stores a `tags` list in library `metadata.json`. When you add an item to that folder (via **`f`**), those tags — plus auto-tags from ancestor folders — are applied to the item.

- **Edit:** select a folder in the left nav → **`A`**, or **right-click** the folder name  
- **Badge:** folders with auto-tags show a 🏷 marker; tooltip lists the tags  
- **Storage:** same field Eagle uses (`folder.tags` in `metadata.json`), so desktop Eagle and Eagle Browse stay in sync  

### Collapsible sidebars

- **◀ Nav** / **Inspector ▶** buttons on the filter bar collapse the left nav and right inspector.
- Blue grid highlight only shows while the **grid has keyboard/mouse focus** (not when typing in search or using the sidebars). Multi-select checkmarks still show.

### View filter bar (top of grid)

Buttons: **Tags · Folders · Type · Stars · Size · Duration · Clear filters**

| Filter | Include | Exclude |
|--------|---------|---------|
| Tags / Folders / Type | **Enter** (✓) | **Shift+Enter** or **right-click** (✗) |
| Stars | 1–5 with **=** / **≥** / **≤** (unrated counts as 0) | — |
| Size | min/max width & height (px) | — |
| Duration | min/max seconds (video/audio) | — |

Active filters show as chips under the buttons; click a chip to remove it.

### Multi-select

| Action | Behavior |
|--------|----------|
| **Click** | Select only that asset (sets range anchor) |
| **Shift+click** | Select range from anchor to clicked item |
| **Ctrl+click** | Add/remove one item without clearing others |
| **Shift+arrows** | Extend range selection as you move |
| **Arrows** (after multi-select) | Move focus **without** clearing checkboxes |
| **Ctrl+arrows** | Same — move focus, keep selection |
| **Space** | Toggle focused item in/out of the selection |
| **Esc** | Collapse multi-select back to the focused item |
| **Click** (no modifiers) | Select only that item (clears multi-select) |

**Non-contiguous with keyboard:** Shift+arrows for a range → plain arrows to another asset (selection stays) → **Space** to add it.

Selection applies to: **copy Eagle id** (`y`), **copy path** (`Y` / Shift+Y or `c`), **tags** (`t`), **folders** (`f`), **ratings** (`1`–`5`), **stage** (`s`).

| `+` / `-` | Larger / smaller thumbnails |

### Library writes (tags, ratings, crop, smart folders)

Eagle Browse can **write** item metadata, crop media, and edit smart folders:

- Uses a lock file `.eagle-browse.write.lock`
- Atomic JSON writes + backups under `backup/eagle-browse-writes/`
- **Crop** also backs up the media file before overwrite and rewrites the thumbnail
- Prefer **one writer** (don’t edit `metadata.json` from two machines at once)

**Smart folders:** **+** on the Smart folders header creates one. Right-click a folder for Edit rules / New child / Delete. Groups are “all / any / none are true”; rules are rating (`=` / `≥` / `≤`), tags (all or any present), and categories (all or any present). See [docs/SMART_FOLDERS.md](docs/SMART_FOLDERS.md).

### Inbox import (consume new media)

Inbox path comes from a TOML config, not from code:

| File | Role |
|------|------|
| `<vault>/eagle-browse.toml` | Shared (Dropbox). `inbox = "intake"` |
| `~/.config/eagle-browse/config.toml` | Optional per-machine overlay |
| `$EAGLE_BROWSE_CONFIG` | Explicit file |
| `$EAGLE_INBOX` | Overrides the inbox path |

See `config.toml.example`. Relative paths are resolved from the file that set them.

**Only one process should auto-consume the inbox** — the headless
`eagle-inbox-watch` on a single machine (see below). The GUI does **not**
poll or import on open. Opening Eagle Browse on Ginger, Jack, and Eric at
once is safe; none of them will race the intake folder.

| Action | How |
|--------|-----|
| **Auto** | `eagle-inbox-watch` only (one host; user systemd unit) |
| **Manual (GUI)** | No dedicated shortcut; leave imports to the watcher in normal use |
| Override path | `EAGLE_INBOX=/path/to/folder` |

Import steps:

1. Unpack any complete ``.zip`` in the inbox: media files are flattened into the
   inbox root, then the zip and the extract folder are deleted. Incomplete
   downloads wait. Subfolders inside the zip are not kept — Eagle only scans
   the inbox root.
2. Content-hash (MD5) check against the library — exact duplicates: watcher uses `--dup` policy; GUI opens a review dialog
3. Copies new items into `Eunbi.library/images/<ID>.info/`
4. Writes Eagle `metadata.json` + thumbnail (ffmpeg for video)
5. Leaves new items untagged / uncategorized (unless folder auto-tags apply when you file them)
6. Deletes the inbox file after import or “use existing”

### Duplicate import review

When an inbox file is byte-identical to something already in the library:

| Action | Behavior |
|--------|----------|
| **Use existing** | Keep the library item; set its imported-at (`modificationTime`) to now; delete inbox file |
| **Import as new** | Create another library item with a full copy |
| **Skip** | Leave the inbox file alone |
| **Apply to all N** | Repeat the chosen action for every remaining duplicate in this batch |

Requires `ffmpeg` / `ffprobe` for video (and ImageMagick `convert` as thumb fallback).

### Agent API (`eagle-api`)

JSON CLI + Python API for search and writes (tags, folders, ratings, smart folders). See [docs/API.md](docs/API.md).

```bash
eagle-api search --smart-folder "Eunbi/images" --limit 10
eagle-api search --tag eunbi --rating-min 3 --type video
eagle-api tag add <id> sofie
eagle-api folder add <id> Eunbi
eagle-api rate <id> 4
eagle-api crop <id> --aspect 9:16 --mode new
eagle-api crop <id> --width 1080 --height 1440 --mode overwrite
eagle-api smart-folder show "Eunbi/images"
eagle-api smart-folder create --name "Sofie videos 3+" --tag sofie --type video --rating-min 3
eagle-api smart-folder update "Sofie videos 3+" --rating-min 4
eagle-api smart-folder delete "Sofie videos 3+"
eagle-api smart-folder move "Sofie videos 3+" --after Sofie
```

### Headless inbox watcher (no UI)

Runs at login and imports inbox files without opening Eagle Browse:

```bash
systemctl --user daemon-reload
systemctl --user enable --now eagle-inbox-watch.service

# Status / logs
systemctl --user status eagle-inbox-watch
journalctl --user -u eagle-inbox-watch -f
# also: ~/.local/state/eagle-browse/inbox-watch.log

# One-shot test
eagle-inbox-watch --once -v
```

| Flag | Meaning |
|------|---------|
| `--dup=reuse` | Exact MD5 match → bump imported-at, delete inbox file (**default**) |
| `--dup=skip` | Leave duplicates in the inbox |
| `--dup=queue` | Move dups to `inbox/.dup-queue/` for later review |
| `--dup=new` | Always create a new library item |
| `--once` | Single scan then exit |
| `--no-notify` / `--no-sound` | Quiet mode |

The watcher and the GUI share the same write lock for metadata writes. That
does **not** make multi-consumer intake safe — do not run two watchers, and do
not rely on the GUI for auto-import.
| `y` | **Copy Eagle id(s)** (newline-separated if multi-selected) — agent-CLI safe |
| `Y` / Shift+Y / `c` | **Copy path(s)** (absolute filesystem path) |
| `Ctrl+Y` | Copy selection as `file://` URIs |
| `s` | **Stage** marked files → outbox folder (copy; source library files are unchanged) and open the folder |
| `Esc` | Clear marks (then clear search) |

### Multi-file handoff (Lightroom / other apps)

1. Browse smart folder, **`Space`** to mark several images  
2. **`y`** — paste Eagle ids for an agent, **`Y`** / **`c`** — paste paths into a tool, **or** **`s`** — copy files to staging and open the folder  
3. Default stage dir: `~/Dropbox/ISAAC/GENNIE/Eunbi/outbox`  
   Override: `EAGLE_STAGE_DIR=/path/to/folder eagle-browse`  
4. On another machine, import that Dropbox folder into Lightroom (or watch it)

### Website upload / Omarchy file dialog

Browsers open the system file picker (GTK portal). It does **not** auto-jump to a copied path — you paste into the location bar:

1. In Eagle Browse: select image → **`Y`** or **`c`** (copy absolute path)  
2. In the file dialog: **`Ctrl+L`** (open location / path bar)  
3. **`Ctrl+V`** paste the path → **Enter**  
   - Paste the **full file path** (what `Y` / `c` copies), not only the folder  

**Alternative:** **`e`** in Eagle Browse opens **Files (Nautilus)** with that image selected — then drag it into the upload dialog if the site accepts drag-and-drop.

### Image viewer (`imv`) keys

| Key | Action |
|-----|--------|
| **`q`** or **`Esc`** | Close |
| `+` / `-` | Zoom in / out |
| `a` | Actual size (100%) |
| `r` | Reset view |
| `f` | Fullscreen |

### Video viewer (center pane)

| Key | Action |
|-----|--------|
| **`Enter`** / **`o`** (grid) / double-click | Play in the center pane (`o` while playing marks **out**) |
| **`Space`** | Play / pause |
| **`p`** or camera button | Save the current frame as a new still |
| Viewer toolbar | Mark **in** at the playhead (written immediately) |
| **`o`** | Mark **out** at the playhead (Esc still closes the viewer) |
| **`x`** or scissors button | Cut `[in, out]` via ffmpeg to a new untagged H.264/AAC clip (Buffer-safe) |
| **`Esc`** | Close the viewer |
| `←` / `→` | Previous / next image or video |

Audio still opens in **mpv**.

| Key | Action |
|-----|--------|
| **`Space`** | Play / pause |
| **`q`** or **`Esc`** | Close |
| `←` / `→` | Seek ~5s |
| `↑` / `↓` | Volume |
| `f` | Fullscreen |
| `m` | Mute |
| `9` / `0` | Volume down / up |
| `/` or `Ctrl+F` | Focus search |
| `f` | Focus sidebar (smart folders + folders) |
| `Enter` (in sidebar) | Toggle expand / collapse smart folder or **Folders** section |
| `→` / `←` (in sidebar) | Expand / collapse smart folder or **Folders** section |
| click ▶ / ▼ | Expand / collapse |

The **Folders** heading starts **collapsed** (same idea as top-level smart folders).
| `d` | Toggle “include subfolders” (regular folders only) |

Smart folders start **collapsed** at the top level. Expand only the category you’re working in.
| `gg` / `Shift+G` | Jump to the first / last asset in the full filtered view |
| `gs` | **Group** selected items into a set (`set:` tag) |
| `gr` | **Remove** selection from its set |
| `r` | Reload library from disk |
| `Esc` | Clear search / leave search |
| Super+W | Close window (Hyprland; `q` does not quit) |

## Safety

- Metadata writes (tags, ratings, folders, smart folders, crop) use a lock file and backups under `backup/eagle-browse-writes/`.
- One writer at a time. The official Eagle desktop app is retired; do not edit `metadata.json` from two machines at once.

## Omarchy launcher (optional)

Add a desktop entry or Hyprland bind if you want Super+key access:

```bash
# example Hyprland bind (edit ~/.config/hypr/bindings.conf yourself)
bind = SUPER SHIFT, E, exec, /usr/bin/eagle-browse
```

## Phone browse

LAN phone browse is **no longer part of this package**. Use the separate phone
app. Older installs may still have `/usr/bin/phone-browse` and
`eagle-phone-browse.service` until you upgrade past the release that removed
them; disable that unit if it is still running.
