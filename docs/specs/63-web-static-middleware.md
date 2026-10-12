# 63. `web.rw`: static files, middleware, HEAD

## Static files

`static(app, prefix, root)` adds a `Mount`. Trailing slashes on the prefix
are ignored. For a GET or HEAD whose path is `prefix` or starts with
`prefix/`:

1. The rest of the path is percent-decoded (spec 62) and stripped of leading
   slashes.
2. `path.join_under(root, rest)` (spec 59) maps it into the root. `None`
   (a `..` that escapes, raw or encoded) goes to the not-found handler, so
   nothing reveals which files exist outside the root.
3. A directory is replaced by its `index.html`. A missing file or a
   non-regular file goes to the not-found handler.
4. `Last-Modified` is the file's mtime as an IMF-fixdate. If the request's
   `If-Modified-Since` equals it exactly, the answer is `304` with no body.
   Clients echo the value back verbatim, so string equality is enough.
5. Otherwise the file is read whole with `fs.read_file`, and the content type
   is chosen from the extension: html, css, js / mjs, json, txt / md, svg,
   png, jpg / jpeg, gif, webp, ico, wasm, pdf, else
   `application/octet-stream`.

## Middleware

There are no closures yet (#16), so middleware is a list of plain function
values stored on the `App`:

- `before(app, hook)` with `hook: Callable[[Request], Option[Response]]`.
  Hooks run in registration order before static files and routes. The first
  `Some(res)` answers the request: authentication, rate limits, maintenance
  mode.
- `after(app, hook)` with `hook: Callable[[Request, Response], Response]`.
  Every response passes through all of them in order, including 404 / 405,
  static files, and responses from `before` hooks: common headers, CORS,
  compression later.

## HEAD

A `HEAD` request is dispatched as `GET`, with `req.method` restored
afterwards so `after` hooks and the access log see `HEAD`. `serialize`
writes the full headers, with `Content-Length` of the body that a GET would
return, and omits the body.

## `examples/web_app.rw`

- `/static` serves `examples/public` (`WEB_PUBLIC` overrides the directory).
- A `before` hook guards `/admin` with an `X-Token` header (403 otherwise).
- An `after` hook adds `X-Powered-By: rw`.

## Tests

`tests/test_e2e_web.py`:
- content types, directory index, Last-Modified / 304
- six traversal and missing-file paths are all 404 and never leak a file
  next to the root
- HEAD on a static file and on a route
- the token check, and the after hook on static responses
