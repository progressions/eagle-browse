# Eagle smart folders (agent-editable)

Smart folders live in the library root file:

`Eunbi.library/metadata.json` → key **`smartFolders`**

Eagle Browse **evaluates** these rules read-only. Until an in-app editor exists, change rules by editing this JSON (or asking an agent), then press **`r`** in Eagle Browse to reload.

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

Nested children **inherit** parent conditions (AND together).

## Rule semantics (as implemented in Eagle Browse)

| property | field on item | notes |
|----------|---------------|--------|
| `tags` | `tags` | string list |
| `folders` | `folders` | folder **ids** |
| `type` | `ext` | also `video` / `audio` / `image` |
| `name` | `name` | filename stem |
| `rating` | **`star`** | 1–5; missing star treated as 0 |

| method | meaning |
|--------|---------|
| `intersection` / `union` | has **any** of the listed values |
| `identity` | has **none** of the listed values (exclude) |
| `equal` / `unequal` | exact match / not |
| `contain` / `uncontain` | substring on `name` |

| group | meaning |
|-------|---------|
| `match`: `AND` / `OR` | combine rules inside the group |
| `boolean`: `TRUE` / `FALSE` | invert group if `FALSE` |
| multiple condition groups | all groups must pass (AND) |

## Safety

- Back up `metadata.json` before large edits.
- Do not edit smart folders while Eagle desktop is also rewriting the library.
- After edit: reload Eagle Browse (`r`) or reopen the app.
