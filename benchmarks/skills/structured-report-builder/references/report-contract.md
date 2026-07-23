# Report input and output contract

The input is a JSON object with:

- `title`: non-empty string.
- `subtitle`: optional string.
- `metrics`: list of `{label, value, unit}` objects. `value` is rendered as supplied.
- `sections`: list of objects containing `heading`, `summary`, and a `table`.
- `table.columns`: ordered list of unique column names.
- `table.rows`: list of objects whose keys are declared columns.
- `provenance.source`: human-readable source identifier.

The output must be UTF-8 HTML with one `h1`, semantic `main`, `section`, `table`, `thead`, and
`tbody` elements, an explicit provenance footer, and no remote resources. Missing required fields,
duplicate columns, or rows with undeclared keys are input errors and must stop rendering.
