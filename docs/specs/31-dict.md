# rw `Dict[K, V]`

## Context

The last missing basic container (#8). Counting words, caching results,
looking up HTTP headers or route parameters by name — all currently need a
`List` of records and a linear search.

```rw
def main() -> int:
    counts: Dict[string, int] = dict_new()
    for w in str_split("a b a c a", " "):
        if dict_has(counts, w):
            counts[w] += 1
        else:
            counts[w] = 1
    for w in counts:                       # insertion order: a b c
        print(w + " " + str(counts[w]))
    match dict_get(counts, "zzz"):
        case Some(n):
            print(n)
        case None:
            print("missing")
    return 0
```

## Goals

- `Dict[K, V]` with `K` in {`int`, `string`} and any non-void `V`
- **Reference semantics**, like records: a dict is a heap object, updated in
  place; assignment and argument passing share it
- `dict_new() -> Dict[K, V]`, typed from the expected type
- `d[k]` (aborts with a message if `k` is missing), `d[k] = v`, and
  `d[k] OP= e` (desugared like spec 27)
- `dict_get(d, k) -> Option[V]`, `dict_has(d, k) -> bool`,
  `dict_remove(d, k) -> bool` (true if it was present), `len(d)`
- `dict_keys(d) -> List[K]`, `dict_values(d) -> List[V]`, and `for k in d`,
  all in insertion order (a re-assigned key keeps its position; a removed
  and re-added key moves to the end), as in Python

## Non-Goals

- Dict literals (`{"a": 1}`): the lexer has no braces yet
- Keys other than `int` / `string` (a hashable protocol needs traits, #104)
- `==` / `print` on dicts; `in` operator; dict comprehensions
- Thread safety: concurrent updates from several fibers are not synchronized
  (#26)
- Freeing dicts (#67)

## Design

### Types and syntax

`types.DictType(key, value)`, compared structurally, passed as an `i8*`.
`Dict[K, V]` is recognised in type position (like `Callable`); `Dict` is
not a keyword. An index assignment parses as `IndexAssign(obj, index,
value)`; sema accepts it only for dicts and reports lists and strings as
immutable.

### Runtime

A compact, insertion-ordered hash table like CPython's dict:

- `entries`: an array of `{u64 hash, u64 live, key, value}` in insertion
  order (key: `int64_t` or `rw_str`; value: `val_size` bytes, padded to 8)
- `index`: an open-addressing table (power-of-two size, linear probing, load
  factor ≤ 2/3) of entry positions; `-1` is empty
- removal clears `live` and leaves the slot as a tombstone; the table is
  compacted when the entry array fills up
- hashes: splitmix64 for ints, FNV-1a for strings

```c
rw_dict *rw_dict_new   (int64_t key_kind, int64_t val_size);  /* 0 = int, 1 = string */
void     rw_dict_set   (rw_dict *d, const void *key, const void *val);
int8_t   rw_dict_get   (rw_dict *d, const void *key, void *val_out);
void     rw_dict_at    (rw_dict *d, const void *key, void *val_out);  /* aborts if missing */
int8_t   rw_dict_has   (rw_dict *d, const void *key);
int8_t   rw_dict_remove(rw_dict *d, const void *key);
int64_t  rw_dict_len   (rw_dict *d);
void     rw_dict_keys  (rw_list *out, rw_dict *d);
void     rw_dict_values(rw_list *out, rw_dict *d);
```

Keys and values cross the ABI by pointer to an entry-block stack slot.
`for k in d` iterates over `dict_keys(d)`, a snapshot, so updating the dict
inside the loop is safe.

## Changes per component

- `rwc/ast_nodes.py`, `rwc/parser.py`: `Dict[K, V]` types, `IndexAssign`, compound index assignment
- `rwc/types.py`, `rwc/sema.py`: `DictType`, builtins, indexing, `for k in d`
- `rwc/irgen.py`: lowering to the runtime calls
- `runtime/runtime.h`, `runtime/runtime.c`: the hash table
- `examples/dict_basic.rw` + `.expected`; tests

Layers touched: parser / sema / irgen / runtime (4).
