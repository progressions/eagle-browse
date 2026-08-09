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
cd ~/Work/tech/eagle-browse
./eagle-browse
```

Or point at another library:

```bash
EAGLE_LIBRARY=/path/to/Something.library ./eagle-browse
# or
./eagle-browse /path/to/Something.library
```

Default library: `~/Dropbox/ISAAC/GENNIE/Eunbi.library`

## Hotkeys

| Key | Action |
|-----|--------|
| `←` / `→` or `h` / `l` | Previous / next image |
| `↑` / `↓` or `k` / `j` | Image above / below (one row) |
| `y` / `Enter` / `c` | **Copy absolute path** to clipboard |
| `o` | Open in `imv` / system viewer |
| `/` or `Ctrl+F` | Focus search |
| `f` | Focus sidebar (smart folders + folders) |
| `a` | All items |
| `d` | Toggle “include subfolders” (regular folders only) |
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
