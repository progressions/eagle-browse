# Eagle smart folders

Smart folders live in the library root file:

`Eunbi.library/metadata.json` → key **`smartFolders`**

Edit them in Eagle Browse (sidebar **+**, right-click a folder → **Edit rules**, or **`e`** with the sidebar focused on a smart folder). Drag a folder up or down to reorder it (`Shift+↑↓` does the same among siblings). Agents can also use `eagle-api smart-folder create|update|delete|move`.

Nested children **inherit** parent conditions (AND together). The editor shows inherited parent rules as read-only.

## In-app editor

A smart folder is a name, optional parent, and one or more **groups**. Groups are ANDed. Each group has a mode and a list of rules.

| Group mode | Stored |
|------------|--------|
| All are true | `match: AND`, `boolean: TRUE` |
| Any are true | `match: OR`, `boolean: TRUE` |
| None are true | `match: OR`, `boolean: FALSE` |

| Rule | Stored |
|------|--------|
| Rating `=` / `≥` / `≤` and 1–5 stars | `property: rating`, `method: equal\|gte\|lte` |
| Tags, any present | `property: tags`, `method: union` |
| Tags, all present | `property: tags`, `method: subset` |
| Categories, any present | `property: folders`, `method: intersection` |
| Categories, all present | `property: folders`, `method: subset` |
| Created on/after, on/before, on, last N days | `property: createTime`, `method: gte\|lte\|equal\|within` |
| Added on/after, on/before, on, last N days | `property: btime`, `method: gte\|lte\|equal\|within` |

Created uses the original file time (Eagle `mtime` / `createTime`). Added uses library add time (`btime`). Dates are local calendar days. `within` value is `[N]` days (1 = today), matching Eagle’s existing “today” folder.

Type, name, `identity` (exclude), and other existing Eagle methods stay in the folder when you save. The editor lists them as “kept as-is” and does not offer an editor for them in this pass.

Unrated items count as 0 stars.

## Shape

```json
{
  "id": "MOQFWQ44CDP2A",
  "name": "Eunbi",
  "conditions": [
    {
      "match": "AND",
      "boolean": "TRUE",
      "rules": [
        { "property": "tags", "method": "union", "value": ["eunbi"] },
        { "property": "folders", "method": "identity", "value": ["MO75UHMRQDTBP"] },
        { "property": "rating", "method": "unequal", "value": "1" }
      ]
    }
  ],
  "children": [ /* nested smart folders */ ]
}
```

## Rule semantics (as implemented in Eagle Browse)

| property | field on item | notes |
|----------|---------------|--------|
| `tags` | `tags` | string list |
| `folders` | `folders` | folder **ids** |
| `type` | `ext` | also `video` / `audio` / `image` |
| `name` | `name` | filename stem |
| `rating` | **`star`** | 1–5; missing star treated as 0 |
| `createTime` / `mtime` | **`created_time`** (Eagle file `mtime`) | local calendar day |
| `btime` / `importTime` | **`btime`** | when the item entered this library |

| method | meaning |
|--------|---------|
| `intersection` / `union` | has **any** of the listed values |
| `subset` / `all` | has **all** of the listed values (tags and folders) |
| `contain` | tags/folders: same as `subset`. `name`: substring |
| `identity` | has **none** of the listed values (exclude) |
| `equal` / `unequal` | exact match / not |
| `uncontain` | substring absent on `name` |
| `gte` / `lte` | rating at least / at most; dates on-or-after / on-or-before |
| `within` | date in the last N days (`value: [N]`; 1 = today) |

| group | meaning |
|-------|---------|
| `match`: `AND` / `OR` | combine rules inside the group |
| `boolean`: `TRUE` / `FALSE` | invert group if `FALSE` |
| multiple condition groups | all groups must pass (AND) |

## Safety

- Writes use the library lock and a backup under `backup/eagle-browse-writes/`.
- One writer at a time — do not edit `metadata.json` from two machines at once.
- After a CLI edit: press **`r`** in Eagle Browse, or the in-app editor reloads the tree itself.
