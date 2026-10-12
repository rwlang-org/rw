# rw web framework

`examples/web.rw` is a small web application framework written in rw itself.
It turns the language features added for it — string search (spec 18), fiber
detach (spec 19), records (spec 20), function values (spec 21), and
`List[T]` (spec 22) — into an HTTP/1.1 server with routing.

```rw
import web
from web import App, Request, Response, text, param

def hello(req: Request) -> Response:
    return text(200, "hello, " + param(req, "name") + "\n")

def main() -> int:
    app: App = web.new_app()
    web.get(app, "/hello/:name", hello)
    return web.serve(app, 8080)
```

`import web` resolves to `web.rw` next to the entry file, so copy
`examples/web.rw` beside your application. `examples/web_app.rw` is a complete
example:

```sh
make -C runtime
uv run rwc run examples/web_app.rw     # then: curl -i http://127.0.0.1:8080/hello/rw
```

## Types

| Type | Fields |
|---|---|
| `Request` | `method`, `path`, `query` (the raw text after `?`), `headers: List[Header]`, `params: List[Header]` (path parameters), `body` |
| `Response` | `status`, `headers: List[Header]`, `body` |
| `Header` | `name`, `value` |
| `App` | `routes`, `not_found` handler, `log` flag |

A handler is any function of type `Callable[[Request], Response]`.

## Application and routing

| Function | Description |
|---|---|
| `new_app() -> App` | An app with no routes, the default 404 handler, and access logging on |
| `get(app, pattern, handler) -> App` | Register a `GET` route |
| `post(app, pattern, handler) -> App` | Register a `POST` route |
| `route(app, method, pattern, handler) -> App` | Register a route for any method |
| `set_not_found(app, handler) -> App` | Replace the 404 handler |
| `set_log(app, on) -> App` | Turn the one-line-per-request access log on or off |
| `before(app, hook) -> App` | Middleware run before routing: `hook(req) -> Option[Response]`; `Some(res)` answers the request and skips the rest |
| `after(app, hook) -> App` | Middleware run on every response, in order: `hook(req, res) -> Response` |
| `static(app, prefix, root) -> App` | Serve the files under directory `root` at URL `prefix` (GET and HEAD) |
| `serve(app, port) -> int` | Listen and serve forever; returns `1` if the port cannot be opened |

`App` is a record, so the registration functions update the app in place; the
return value is the same app, for convenience.

Patterns are matched segment by segment. A `:name` segment captures any
non-empty segment: `/users/:id/posts/:post` matches `/users/7/posts/42`.
Segments are percent-decoded after the path is split, so `%2F` never creates
a new segment.
Routes are tried in registration order and the first match wins. A path that
matches a route only under another method gets `405 Method Not Allowed`;
anything else goes to the not-found handler.

A request goes through the `before` hooks, then the static mounts, then the
routes. The `after` hooks see every response, including 404s and static
files. `HEAD` is routed like `GET`, and the response keeps its headers
(including `Content-Length`) but drops the body.

Static files: the rest of the path after the prefix is percent-decoded and
resolved with `path.join_under`, so `..` (raw or encoded) and absolute
paths can never reach outside `root`; they get the not-found handler, as do
missing files. A directory serves its `index.html`. The content type comes
from the extension. `Last-Modified` is sent, and a request whose
`If-Modified-Since` repeats it gets `304 Not Modified`. Files are read
whole into memory.

## Reading requests

| Function | Description |
|---|---|
| `param(req, name) -> string` | A path parameter, percent-decoded |
| `query(req, name) -> string` | The first value of a query-string parameter (`?q=rw&page=2`), decoded (`%XX` and `+`) |
| `form(req, name) -> string` | A field of an `application/x-www-form-urlencoded` body, decoded; `""` for other content types |
| `cookie(req, name) -> string` | A cookie from the `Cookie` header (the raw value) |
| `header(req, name) -> string` | A request header, case-insensitive |

All of them return `""` when the value is absent. `req.path`, `req.query`,
and `req.body` keep the raw text. The stdlib `url` module (spec 62) decodes
or encodes anything else.

## Building responses

| Function | Description |
|---|---|
| `text(status, body)` | `text/plain; charset=utf-8` |
| `html(status, body)` | `text/html; charset=utf-8` |
| `json(status, body)` | `application/json` (the body is passed through as is) |
| `response(status, content_type, body)` | Any content type |
| `redirect(location)` | `302 Found` with a `Location` header |
| `set_cookie(res, name, value, attrs = "Path=/; HttpOnly; SameSite=Lax")` | Add a `Set-Cookie` header; escape the value yourself (`url.escape`) if it may contain `;`, `,`, or spaces |
| `set_header(res, name, value) -> Response` | Add a response header |

The framework adds `Content-Length` and `Connection` (`keep-alive` or `close`) to every response.

## Protocol behavior and limits

- Persistent connections (spec 64): HTTP/1.1 connections stay open unless the
  client sends `Connection: close`; HTTP/1.0 only with
  `Connection: keep-alive`. Pipelined requests are answered in order. An idle
  connection is closed after `app.keepalive_ms` (default 5000; 0 means one
  request per connection), after 1000 requests, and after any error response
- Requests are read with the stdlib buffered reader (`bufio`, spec 54)
- The request head may be up to 64 KiB (`413` above that); the body is read
  according to `Content-Length` or `Transfer-Encoding: chunked` (spec 65), up
  to 1 MiB (`413` above that). Other transfer encodings get `501`
- A malformed request line gets `400 Bad Request`
- Each connection has a read and write timeout (`app.timeout_ms`, default
  10000; 0 disables it). A client that sends part of a request and then
  stalls gets `408 Request Timeout`; one that sends nothing is closed (spec 53)
- Responses are written completely even when `write` accepts only part of
  the buffer
- After the response the connection is half-closed and leftover request
  bytes are drained before closing, so a refused request still sees its
  status instead of a connection reset (spec 56)
- Each connection runs in its own fiber, which is freed when the connection
  is done (spec 19)
- SIGINT / SIGTERM shut the server down gracefully: it stops accepting,
  finishes the requests in progress, and `serve` returns 0 (spec 57)
- The access log goes to stdout through `log.Logger` (spec 60): one
  unbuffered line per request

## Not supported yet

TLS, chunked responses (streaming), compression, and templates. Strings and
lists allocated while handling a request are not freed (memory management,
#67).
