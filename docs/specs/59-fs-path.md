# 59. `path` and `fs`: paths, directories, metadata

Issue: #43

## Modules

Both live in the standard library (spec 54). `path` is pure rw. `fs` wraps
seven new builtins.

### `path`: POSIX path strings

No filesystem access; `..` is resolved lexically and symlinks are not
followed.

| Function | Result |
|---|---|
| `is_abs(p)` | starts with `/` |
| `join(a, b)` | `a/b` with one separator; an absolute `b` replaces `a` (as in Python) |
| `split(p) -> tuple[string, string]` | `(dir, base)` split at the last `/`; `dir` is `/` for `/x` and `""` without a slash |
| `basename(p)`, `dirname(p)` | the two halves of `split` |
| `ext(p)` | extension of the last component with its dot (`.gz` for `x.tar.gz`); `""` for none or a dotfile |
| `normalize(p)` | drops `.` and empty components and resolves `..`. An absolute path stops at `/`; a relative one keeps the leading `..` it cannot resolve; `""` becomes `.` |
| `join_under(root, rel) -> Option[string]` | `rel` resolved under `root`, or `None` if `rel` is absolute or its normalized form starts with `..`. Use it to map an untrusted request path onto a directory |

### `fs`: files and directories

| Function | Result |
|---|---|
| `stat(p) -> Option[fs.Stat]` | `Stat(kind, size, mtime_ms)`; `kind` is `"file"`, `"dir"`, or `"other"`; follows symlinks |
| `exists(p)`, `is_file(p)`, `is_dir(p)` | `bool` |
| `read_dir(p) -> List[string]` | entry names sorted by byte order, without `.` / `..`; empty on error |
| `rename(src, dst)`, `remove(p)`, `mkdir(p)`, `mkdir_all(p)` | `bool`. `remove` deletes a file or an empty directory; `mkdir_all` creates missing parents and succeeds if `p` already is a directory |
| `read_file(p) -> Option[string]`, `write_file(p, data) -> bool` | whole-file helpers on top of `open` / `read` / `write_all` |

Every failure leaves `last_error()` set (spec 51), for example `ENOENT` or
`ENOTEMPTY`. Five more names were added to the `error_name` table for this:
ENOTEMPTY, EXDEV, ELOOP, EROFS, and EBUSY.

## Builtins

| Builtin | Result |
|---|---|
| `fs_kind(p) -> string` | `"file"` / `"dir"` / `"other"`, or `""` on error |
| `fs_size(p) -> int`, `fs_mtime_ms(p) -> int` | -1 on error |
| `fs_read_dir(p) -> List[string]` | sorted names |
| `fs_rename(a, b)`, `fs_remove(p)`, `fs_mkdir(p)` | 0, or -1 on error; `mkdir` uses mode 0755 |

`runtime/fs.c` runs each syscall through `rw_aio_call` (spec 50), so a fiber
parks while the pool thread waits on a slow or network filesystem. A path
containing a NUL byte is `EINVAL`.

## Tests

`examples/fs_path.rw` (expected output) covers:
- every `path` function, including `join_under` refusing `..` and absolute
  paths
- `mkdir_all`, `write_file` / `read_file`, sorted `read_dir`, `stat`,
  `rename`, `ENOTEMPTY` on a non-empty directory, removal, and `ENOENT`
