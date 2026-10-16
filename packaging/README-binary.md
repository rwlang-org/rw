# rw standalone binary

This archive holds `rwc` with its own Python, the prebuilt C runtime, and the
standard library. Python is not needed.

## Requirements

- clang (or cc): rwc compiles programs to LLVM IR and links them with clang
- the Boehm GC library and headers: `brew install bdw-gc` (macOS),
  `sudo apt-get install libgc-dev` (Debian/Ubuntu)

## Install

```sh
tar xzf rw-<version>-<platform>.tar.gz
export PATH="$PWD/rw-<version>-<platform>:$PATH"
rwc run hello.rw
```

Keep `rwc` next to its `_internal/` directory; it finds the runtime and the
standard library there.

On macOS, a browser download is quarantined; clear it with
`xattr -dr com.apple.quarantine rw-<version>-macos-arm64`.

Verify a download with the `.sha256` file next to it:
`shasum -a 256 -c rw-<version>-<platform>.tar.gz.sha256`.
