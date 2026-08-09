# Eagle Browse

Keyboard-first, **read-only** browser for an [Eagle.cool](https://eagle.cool) library on Omarchy/Linux.

Browse **smart folders** and regular folders, search tags/names, and **copy the absolute path** of an image so you can paste it into upload dialogs or other tools.

Smart folders are evaluated read-only from Eagle’s `metadata.json` rules (nested filters inherit parents — e.g. `Eunbi` → `images`).

## Requirements

- Python 3
- GTK 4 + libadwaita (`python-gobject`, `libadwaita`)
- Wayland clipboard helper: `wl-copy` (optional but recommended)
- Image preview: `imv` (optional; falls back to `xdg-open`)

On Omarchy these are typically already present.

## Run

```bash
# From source
~/Work/tech/eagle-browse/eagle-browse

# Or if installed on PATH (symlink in ~/.local/bin)
eagle-browse
```

Or point at another library:

```bash
EAGLE_LIBRARY=/path/to/Something.library eagle-browse
# or
eagle-browse /path/to/Something.library
```

Default library: `~/Dropbox/ISAAC/GENNIE/Eunbi.library`

### Omarchy install

```bash
# Launcher on PATH
ln -sfn ~/Work/tech/eagle-browse/eagle-browse ~/.local/bin/eagle-browse

# Walker / app menu
cp ~/Work/tech/eagle-browse/eagle-browse.desktop ~/.local/share/applications/
# (or use the installed copy under ~/.local/share/applications/eagle-browse.desktop)

# Hotkey: Super+Shift+I  (set in ~/.config/hypr/bindings.conf)
# bindd = SUPER SHIFT, I, Eagle Browse, exec, omarchy-launch-or-focus cool.eagle.Browse "uwsm-app -- $HOME/.local/bin/eagle-browse"
```

## Hotkeys

| Key | Action |
|-----|--------|
| `←` / `→` or `h` / `l` | Previous / next image |
| `←` on leftmost column | Focus sidebar on the **current** smart folder / folder |
| `↑` / `↓` or `k` / `j` | Image above / below (one row) |
| `Enter` / `o` | Open larger: **images → imv**, **video/audio → mpv** |
| `1`–`5` | Set **star rating** (marked items, or focused) |
| `0` | Clear rating |
| `t` | **Tags** picker (recent + autocomplete; Enter toggles; Esc closes) |
| `f` | **Folders / categories** picker (same UX as tags) |
| `m` | **Filter by type** (or use the **Type** button on the filter bar) |
| `Esc` | Clear marks → clear view filters → clear search |
| `b` | Focus **sidebar** |

### View filter bar (top of grid)

Buttons: **Tags · Folders · Type · Size · Duration · Clear filters**

| Filter | Include | Exclude |
|--------|---------|---------|
| Tags / Folders / Type | **Enter** (✓) | **Shift+Enter** or **right-click** (✗) |
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
| **Ctrl+arrows** | Move focus **without** clearing the selection |
| **Space** | Toggle focused item in/out of the selection |
| **Esc** | Collapse multi-select back to the focused item |

**Non-contiguous with keyboard only:** select a range (Shift+arrows) → **Ctrl+arrows** to another asset → **Space** to add it.

Selection applies to: **copy paths** (`y`/`Y`), **tags** (`t`), **folders** (`f`), **ratings** (`1`–`5`), **stage** (`s`).

| `+` / `-` | Larger / smaller thumbnails |

### Library writes (tags & ratings)

Eagle Browse can **write** item metadata into the library (tags, stars):

- Uses a lock file `.eagle-browse.write.lock`
- Atomic JSON writes + backups under `backup/eagle-browse-writes/`
- Prefer **one writer** (don’t run official Eagle edits at the same time)

Smart folder **rules** are not edited in-app yet — see [docs/SMART_FOLDERS.md](docs/SMART_FOLDERS.md) for agent/JSON editing.

### Inbox import (consume new media)

Default inbox:

`~/Dropbox/ISAAC/GENNIE/Eunbi/PICS/Eunbi`

| Action | How |
|--------|-----|
| **Auto** | App polls every ~3s; when a file’s size is stable, it imports |
| **Manual** | Press **`i`** to import everything currently in the inbox |
| Override path | `EAGLE_INBOX=/path/to/folder eagle-browse` |

Import:

1. Copies into `Eunbi.library/images/<ID>.info/`
2. Writes Eagle `metadata.json` + thumbnail (ffmpeg for video)
3. Tags with `eunbi`, folder id for top-level **Eunbi**
4. Moves original to `inbox/.imported/`

Requires `ffmpeg` / `ffprobe` for video (and ImageMagick `convert` as thumb fallback).
| `Y` | **Copy all marked paths** (newline-separated; if none marked, copies focused) |
| `Ctrl+Y` | Copy marked as `file://` URIs |
| `y` / `c` | Copy **one** focused path |
| `s` | **Stage** marked files → outbox folder (copy; library stays read-only) |
| `Esc` | Clear marks (then clear search) |

### Multi-file handoff (Lightroom / other apps)

1. Browse smart folder, **`Space`** to mark several images  
2. **`Y`** — paste paths into a tool, **or** **`s`** — copy files to staging  
3. Default stage dir: `~/Dropbox/ISAAC/GENNIE/Eunbi/outbox`  
   Override: `EAGLE_STAGE_DIR=/path/to/folder eagle-browse`  
4. On another machine, import that Dropbox folder into Lightroom (or watch it)

### Website upload / Omarchy file dialog

Browsers open the system file picker (GTK portal). It does **not** auto-jump to a copied path — you paste into the location bar:

1. In Eagle Browse: select image → **`y`** (copy absolute path)  
2. In the file dialog: **`Ctrl+L`** (open location / path bar)  
3. **`Ctrl+V`** paste the path → **Enter**  
   - Paste the **full file path** (what `y` copies), not only the folder  

**Alternative:** **`e`** in Eagle Browse opens **Files (Nautilus)** with that image selected — then drag it into the upload dialog if the site accepts drag-and-drop.

### Image viewer (`imv`) keys

| Key | Action |
|-----|--------|
| **`q`** or **`Esc`** | Close |
| `+` / `-` | Zoom in / out |
| `a` | Actual size (100%) |
| `r` | Reset view |
| `f` | Fullscreen |

### Video / audio player (`mpv`) keys

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
| `a` | All items |
| `d` | Toggle “include subfolders” (regular folders only) |

Smart folders start **collapsed** at the top level. Expand only the category you’re working in.
| `g` / `G` | First / last item |
| `r` | Reload library from disk |
| `Esc` | Clear search / leave search |
| `q` | Quit |

## Safety

- **Read-only** — never writes into the Eagle library or Dropbox folder.
- Safe to use while the same library is synced; still best practice is one Eagle writer at a time on Mac/Windows.

## Omarchy launcher (optional)

Add a desktop entry or Hyprland bind if you want Super+key access:

```bash
# example Hyprland bind (edit ~/.config/hypr/bindings.conf yourself)
bind = SUPER SHIFT, E, exec, /home/isaac/Work/tech/eagle-browse/eagle-browse
```
