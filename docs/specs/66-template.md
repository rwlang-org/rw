# 66. `template`: Mustache-style templates

## Module

`stdlib/template.rw` renders a logic-less template against a `json.Value`
(spec 58), a subset of Mustache:

| Tag | Meaning |
|---|---|
| `{{name}}` | the value as text, HTML-escaped (`& < > " '`) |
| `{{{name}}}`, `{{& name}}` | the value as text, not escaped |
| `{{a.b.c}}` | dotted lookup |
| `{{.}}` | the current item (inside a section) |
| `{{#name}}…{{/name}}` | section: once per element of an array, or once with the value as the new context if it is truthy |
| `{{^name}}…{{/name}}` | inverted section: once if the value is missing or falsy |
| `{{! comment }}` | nothing |

- **Lookup**: the first segment of a name is resolved against the context
  stack, innermost first, so a section can still use outer values; the
  remaining segments walk down from there. A missing name renders as `""`
  and is falsy.
- **Text form**: strings as is, `null` as `""`, numbers and booleans as
  text, arrays and objects as compact JSON.
- **Falsy**: missing, `null`, `false`, `""`, and `[]`.

## API

```python
match template.compile(src):        # Result[template.Template, string]
    case Ok(t):
        html = t.render(data)       # data: json.Value
    case Err(msg):
        ...                         # "unclosed section 'items' at line 3, column 5"

template.render(src, data)          # compile + render: Result[string, string]
template.escape_html(s)
```

`compile` parses once into a tree of `template.Node` records, so a template
used for many requests can be compiled once. Errors give the line and column
of the offending tag:
- an unclosed section or tag
- a closing tag that does not match its section, or one with no section
- an empty `{{}}`

Not supported: partials (`{{> x}}`), lambdas, delimiter changes, and the
spec's standalone-line whitespace trimming.

## Tests

- `examples/template_basic.rw` (expected output):
  - escaping
  - raw tags
  - array and object sections
  - inverted sections
  - dotted and outer-context lookups
  - comments
  - each compile error
- `tests/test_e2e_web.py`: `/page/:name` escapes a decoded path parameter and
  renders a tag list or its inverted fallback.
