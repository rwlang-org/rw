# 69. UDP

Issue: #167 (the UDP part; IPv6 and Unix sockets came with spec 50, TLS is
still open)

## API

UDP uses the entry points from spec 50, with a `udp://` address:

| | |
|---|---|
| `dial("udp://host:port")` | a connected UDP socket: `write` sends one datagram to that peer, `read(h, max)` receives one datagram from it |
| `listen("udp://host:port")` | a bound UDP socket (`udp://:port` binds every IPv4 interface; port 0 picks one) |
| `recv_from(h, max) -> tuple[Bytes, string]` | the next datagram and its sender as `"udp://ip:port"` |
| `send_to(h, data, addr) -> int` | send one datagram to `addr` (`udp://…`); bytes sent, or -1 |

```python
srv = listen("udp://127.0.0.1:0")
data, sender = recv_from(srv, 2048)
send_to(srv, data, sender)
```

## Semantics

- One call moves one datagram:
  - A datagram longer than `max` is truncated (the rest is discarded by the
    kernel).
  - An empty datagram is valid: `recv_from` returns empty data with a
    sender, and `write` / `send_to` of empty bytes send one.
  - For the other socket kinds, an empty `write` still sends nothing.
- `recv_from` on error or timeout returns empty data and an empty sender,
  with `last_error` set (spec 51). The read timeout (spec 53) applies to
  `recv_from`, the write timeout to `send_to`.
- `send_to` resolves the address (on the aio pool, like `dial`) and uses the
  first result whose family matches the socket's. A non-`udp://` address is
  `EINVAL`.
- `accept` on a UDP socket fails (`EOPNOTSUPP`).
- `local_addr` / `peer_addr` print `udp://` for datagram sockets, so their
  results can be passed back to `dial` and `send_to`.

## Runtime

`parse_addr` records the socket type (`SOCK_STREAM` for `tcp://`,
`SOCK_DGRAM` for `udp://`) and `resolve` passes it to `getaddrinfo`.
- `bind_listen` only binds a datagram socket, without `listen`, and makes it
  nonblocking.
- `recv_from` / `send_to` loop on `EAGAIN` through `rw_net_block`: a fiber
  parks, the main thread polls.
- `rw_recv_from` fills an `rw_datagram { data, addr }`, whose layout is the
  `tuple[Bytes, string]` struct.

## Tests

- `examples/udp_echo.rw` (expected output):
  - a connected client against a `recv_from` / `send_to` echo fiber,
    including an empty datagram
  - `send_to` to a tcp:// address, a `recv_from` timeout, and `accept`
- `tests/test_e2e_udp.py`:
  - an rw client against Python UDP servers on IPv4 and IPv6
  - an rw server answering ten Python clients over five rounds, echoing
    each sender's address and datagram size
