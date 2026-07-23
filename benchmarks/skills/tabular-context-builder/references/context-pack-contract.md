# Context pack contract

Input fields:

- `title`: pack title.
- `tables`: one or more `{name, columns, rows}` objects.
- `columns`: unique ordered string names.
- `rows`: objects containing only declared columns.

Output layout:

```text
pack/
  navigation.md
  manifest.json
  tables/<safe-name>.csv
  tables/<safe-name>.md
```

`manifest.json` records the source table name, ordered columns, exact row count, CSV path, preview
path, and SHA-256 of the complete CSV. Markdown includes at most 10 data rows. CSV is authoritative.
