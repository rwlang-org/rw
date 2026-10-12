# 62. `url` module; decoded requests, forms, and cookies in `web.rw`

## Motivation

`examples/web.rw` handed the raw text to handlers. A query value of `caf%C3%A9`
or a path parameter with `%20` arrived encoded, HTML form posts could not be
read, and there were no cookies. These are the remaining basics a web
framework needs before sessions or authentication can be written on top.

## `str_from_byte`

`str_from_byte(b: int) -> string` returns a one-byte string holding the low 8
bits of `b`. `chr` (spec 58) encodes a code point as UTF-8, so it cannot
produce a lone byte ≥ 0x80. Percent-decoding needs that to rebuild UTF-8
byte by byte.

## `url` module (`stdlib/url.rw`)

| Function | Result |
|---|---|
| `unescape(s, plus = true)` | `%XX` decoded. With `plus`, `+` is a space (the query/form convention); pass `false` for path segments. A `%` not followed by two hex digits is kept literally |
| `escape(s)` | everything except the RFC 3986 unreserved set (`A-Z a-z 0-9 - . _ ~`) as `%XX` |
| `parse_query(q) -> List[url.Param]` | `key=value` pairs split on `&`, decoded and in order. A piece without `=` has value `""`; empty pieces are skipped |
| `get(params, key) -> Option[string]` | the first value for `key` |

## `web.rw`

- `query(req, name)` decodes the value (`url.parse_query`).
- Path segments are percent-decoded after splitting the path, both when
  comparing literal segments and for captured `:name` parameters. `%2F`
  cannot create a new segment.
- `form(req, name)` reads `application/x-www-form-urlencoded` bodies and
  returns `""` for any other content type.
- `cookie(req, name)` reads the `Cookie` header.
- `set_cookie(res, name, value, attrs = "Path=/; HttpOnly; SameSite=Lax")`
  adds a `Set-Cookie` header.
- `examples/web_app.rw` gains `POST /login` (a form that sets a cookie) and
  `GET /whoami` (reads it back).

## Tests

- `examples/url_basic.rw` (expected output).
- `tests/test_e2e_web.py`:
  - decoded path parameters and query values
  - a form post with UTF-8 sets a cookie that a second request reads back
  - `form` ignores a `text/plain` body
