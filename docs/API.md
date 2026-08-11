# Eagle Browse API & CLI

Query and update the Eagle library without the GTK app:

| Interface | Use |
|-----------|-----|
| **`eagle-api`** CLI | Humans (readable tables) and agents (`--json`) |
| **`EagleAPI`** Python | Import from `api.py` |

## Setup

```bash
ln -sfn ~/Work/tech/eagle-browse/eagle-api ~/.local/bin/eagle-api
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

# Tags / categories / rating
eagle-api tag add MXXXXXXXXXXXX sofie,raw
eagle-api tag remove MXXXXXXXXXXXX raw
eagle-api folder add MXXXXXXXXXXXX Eunbi
eagle-api folder remove MXXXXXXXXXXXX Eunbi
eagle-api rate MXXXXXXXXXXXX 4
eagle-api rate MXXXXXXXXXXXX,MYYYYYYYYYYYY 0   # clear

# Crop (images only)
eagle-api crop MXXXXXXXXXXXX --aspect 9:16 --mode new          # new untagged item
eagle-api crop MXXXXXXXXXXXX --aspect 3:4 --mode overwrite     # replace original
eagle-api crop MXXXXXXXXXXXX --width 1080 --height 1440 --anchor top
eagle-api crop MXXXXXXXXXXXX --x 100 --y 50 --width 800 --height 1200 --mode new
eagle-api crop MXXXXXXXXXXXX --aspect 9:16 --mode new --json   # agent output

# Catalog
eagle-api tags
eagle-api folders
eagle-api smart-folder list
eagle-api smart-folder show "Eunbi/images"

# Create smart folder: Sofie videos with 3+ stars
eagle-api smart-folder create \
  --name "Sofie videos 3+" \
  --parent Sofie \
  --tag sofie \
  --type video \
  --rating-min 3
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

# Crop
api.crop(item_id, aspect="9:16", mode="new")          # new item, no tags/folders
api.crop(item_id, aspect="3:4", mode="overwrite")     # replace original
api.crop(item_id, width=1080, height=1440, anchor="top")
api.crop(item_id, x=100, y=50, width=800, height=1200, mode="new")

# Create smart folder under Sofie
api.create_smart_folder(
    "Sofie videos 3+",
    parent="Sofie",
    tags=["sofie"],
    media_type="video",
    rating_min=3,
)
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
- Smart folder create updates `metadata.json` (backed up under `backup/eagle-browse-writes/`). Reload the GUI with **`r`** to see new smart folders.
- `rating_min` uses method `gte` in our evaluator (works in Eagle Browse; desktop Eagle may only show equal/unequal in its UI).
- Soft-deleted items are excluded from search by default.
