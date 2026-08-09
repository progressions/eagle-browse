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

# Create smart folder under Sofie
api.create_smart_folder(
    "Sofie videos 3+",
    parent="Sofie",
    tags=["sofie"],
    media_type="video",
    rating_min=3,
)
```

## Notes

- Writes use the same lock as Eagle Browse / inbox-watch (`.eagle-browse.write.lock`).
- Smart folder create updates `metadata.json` (backed up under `backup/eagle-browse-writes/`). Reload the GUI with **`r`** to see new smart folders.
- `rating_min` uses method `gte` in our evaluator (works in Eagle Browse; desktop Eagle may only show equal/unequal in its UI).
- Soft-deleted items are excluded from search by default.
