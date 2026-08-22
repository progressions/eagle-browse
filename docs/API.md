# Eagle Browse API & CLI

Query and update the Eagle library without the GTK app:

| Interface | Use |
|-----------|-----|
| **`eagle-api`** CLI | Humans (readable tables) and agents (`--json`) |
| **`EagleAPI`** Python | Import from `api.py` |

## Setup

```bash
ln -sfn ~/tech/eagle-browse/eagle-api ~/.local/bin/eagle-api
export EAGLE_LIBRARY=~/Dropbox/ISAAC/GENNIE/Eunbi.library   # optional default
eagle-api --help
```

## CLI (human by default)

```bash
# Search (pretty)
eagle-api search --tag eunbi --rating-min 3 --limit 20
eagle-api search --smart-folder "Eunbi/images" --type video
eagle-api search --folder Eunbi --name mirror --ids-only

# Item
eagle-api get MXXXXXXXXXXXX

# Tags / categories / rating / notes
eagle-api tag add MXXXXXXXXXXXX sofie,raw
eagle-api tag remove MXXXXXXXXXXXX raw
eagle-api group MXXXXXXXXXXXX MYYYYYYYYYYYY   # join child into source's set
eagle-api folder add MXXXXXXXXXXXX Eunbi
eagle-api folder remove MXXXXXXXXXXXX Eunbi
eagle-api rate MXXXXXXXXXXXX 4
eagle-api rate MXXXXXXXXXXXX,MYYYYYYYYYYYY 0   # clear
eagle-api note MXXXXXXXXXXXX "use for fanvue PPV"
eagle-api note MXXXXXXXXXXXX --clear

# Crop (images only)
eagle-api crop MXXXXXXXXXXXX --aspect 9:16 --mode new          # new untagged item
eagle-api crop MXXXXXXXXXXXX --aspect 3:4 --mode overwrite     # replace original
eagle-api crop MXXXXXXXXXXXX --width 1080 --height 1440 --anchor top
eagle-api crop MXXXXXXXXXXXX --x 100 --y 50 --width 800 --height 1200 --mode new
eagle-api crop MXXXXXXXXXXXX --aspect 9:16 --mode new --json   # agent output

# Video in/out + trim (ffmpeg H.264/AAC; source unchanged)
eagle-api mark MXXXXXXXXXXXX                    # show stored marks
eagle-api mark MXXXXXXXXXXXX --in 3.2 --out 8.05
eagle-api mark MXXXXXXXXXXXX --in 3.2           # set in only
eagle-api mark MXXXXXXXXXXXX --clear
eagle-api trim MXXXXXXXXXXXX                    # use sidecar marks
eagle-api trim MXXXXXXXXXXXX --start 3.2 --end 8.05
eagle-api trim MXXXXXXXXXXXX --json

# Catalog
eagle-api tags
eagle-api folders
eagle-api smart-folder list
eagle-api smart-folder show "Eunbi/images"

# Create / update / delete
eagle-api smart-folder create \
  --name "Sofie videos 3+" \
  --parent Sofie \
  --tag sofie \
  --type video \
  --rating-min 3
eagle-api smart-folder update "Sofie videos 3+" --rating-min 4
eagle-api smart-folder delete "Sofie videos 3+"
eagle-api smart-folder delete "Sofie videos 3+" --force   # also remove children
eagle-api smart-folder move "Sofie videos 3+" --after Sofie
eagle-api smart-folder move "Sofie videos 3+" --first
```

### JSON mode (agents)

```bash
eagle-api search --tag eunbi --limit 5 --json
eagle-api search --tag eunbi --limit 5 --compact   # one-line JSON
eagle-api smart-folder show "Eunbi/images" --json
```

Exit codes: `0` ok · `2` logical error · `1` hard failure.

## Python

```python
from api import EagleAPI

api = EagleAPI()  # uses EAGLE_LIBRARY or default Dropbox path

# Look in Eunbi/images smart folder
r = api.search(smart_folder="Eunbi/images", limit=20)
for it in r["items"]:
    print(it["display_name"], it["path"], it["tags"])

# Mutate
api.add_tags(item_id, ["sofie"])
api.add_folders(item_id, ["Eunbi"])
api.set_rating(item_id, 4)
api.set_annotation(item_id, "use for fanvue PPV")
api.set_annotation(item_id, "")  # clear

# Crop
api.crop(item_id, aspect="9:16", mode="new")          # new item, no tags/folders
api.crop(item_id, aspect="3:4", mode="overwrite")     # replace original
api.crop(item_id, width=1080, height=1440, anchor="top")
api.crop(item_id, x=100, y=50, width=800, height=1200, mode="new")

# Create / update / delete smart folders
api.create_smart_folder(
    "Sofie videos 3+",
    parent="Sofie",
    tags=["sofie"],
    media_type="video",
    rating_min=3,
)
api.update_smart_folder("Sofie videos 3+", rating_min=4)
api.delete_smart_folder("Sofie videos 3+", force=True)
api.move_smart_folder("Sofie videos 3+", after="Sofie")
```

### Crop details

| Arg | Meaning |
|-----|---------|
| `mode` | `overwrite` (default) or `new` / `save-as` |
| `aspect` | `9:16`, `3:4`, `1:1`, `16:9`, `2:3`, `3:2`, `4:3`, `orig`, `free` |
| `width` / `height` | Crop size in source pixels (with aspect, one side can be omitted) |
| `x` / `y` | Top-left; omit to place with `anchor` |
| `anchor` | `center` (default), `top`, `bottom`, `left`, `right`, corners |

- **`overwrite`**: backs up media, rewrites file + thumbnail, keeps tags/folders.
- **`new`**: creates a new item with empty tags and folders (fresh import). Source unchanged.
- Response includes `rect` and full `item` dict (id, path, width, height, …).

## Notes

- Writes use the same lock as Eagle Browse / inbox-watch (`.eagle-browse.write.lock`).
- Smart folder create/update/delete rewrite `metadata.json` (backed up under `backup/eagle-browse-writes/`). The GUI editor reloads the tree; after a CLI change press **`r`**.
- `rating_min` uses method `gte`. Tags/folders “all present” uses method `subset`.
- `delete` refuses a folder that has children unless `force=True` (the GUI always confirms and then deletes the subtree).
- Soft-deleted items are excluded from search by default.
