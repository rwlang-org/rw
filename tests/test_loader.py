from __future__ import annotations

import pytest

from rwc.diagnostics import CompileError
from rwc.loader import load_program


def _write(tmp_path, name: str, src: str) -> str:
    p = tmp_path / f"{name}.rw"
    p.write_text(src, encoding="utf-8")
    return str(p)


LIB = "def add(a: int, b: int) -> int:\n    return a + b\n"
ENTRY = "import lib\n\ndef main() -> int:\n    return lib.add(1, 2)\n"


def test_loads_entry_and_imported_module(tmp_path):
    _write(tmp_path, "lib", LIB)
    entry_path = _write(tmp_path, "main", ENTRY)
    prog = load_program(ENTRY, entry_path)
    assert prog.root_name == "main"
    assert "lib" in prog.modules
    assert any(fn.name == "add" for fn in prog.modules["lib"].functions)


def test_missing_module_errors(tmp_path):
    entry_path = _write(tmp_path, "main", ENTRY)  # no lib.rw written
    with pytest.raises(CompileError) as ei:
        load_program(ENTRY, entry_path)
    assert "cannot find module 'lib'" in ei.value.diagnostic.message


def test_imported_module_with_main_errors(tmp_path):
    _write(tmp_path, "lib", LIB + "\ndef main() -> int:\n    return 0\n")
    entry_path = _write(tmp_path, "main", ENTRY)
    with pytest.raises(CompileError) as ei:
        load_program(ENTRY, entry_path)
    assert "must not define 'main'" in ei.value.diagnostic.message


def test_transitive_import(tmp_path):
    _write(tmp_path, "base", "def base_v() -> int:\n    return 7\n")
    _write(tmp_path, "mid", "import base\n\ndef mid_v() -> int:\n    return base.base_v()\n")
    entry = "import mid\n\ndef main() -> int:\n    return mid.mid_v()\n"
    entry_path = _write(tmp_path, "main", entry)
    prog = load_program(entry, entry_path)
    assert set(prog.modules) == {"mid", "base"}


def test_cycle_detected(tmp_path):
    _write(tmp_path, "a", "import b\n\ndef a_v() -> int:\n    return b.b_v()\n")
    _write(tmp_path, "b", "import a\n\ndef b_v() -> int:\n    return a.a_v()\n")
    entry = "import a\n\ndef main() -> int:\n    return a.a_v()\n"
    entry_path = _write(tmp_path, "main", entry)
    with pytest.raises(CompileError) as ei:
        load_program(entry, entry_path)
    assert "import cycle" in ei.value.diagnostic.message


def test_duplicate_import_deduped(tmp_path):
    _write(tmp_path, "lib", LIB)
    _write(tmp_path, "other", "import lib\n\ndef other_v() -> int:\n    return lib.add(1, 1)\n")
    entry = (
        "import lib\n"
        "import other\n"
        "\n"
        "def main() -> int:\n"
        "    return lib.add(1, 2)\n"
    )
    entry_path = _write(tmp_path, "main", entry)
    prog = load_program(entry, entry_path)
    # `lib` is reachable via both entry and other, but loaded once.
    assert set(prog.modules) == {"lib", "other"}


def test_imported_main_error_points_at_imported_main(tmp_path):
    lib_src = LIB + "\ndef main() -> int:\n    return 0\n"
    lib_path = _write(tmp_path, "lib", lib_src)
    entry_path = _write(tmp_path, "main", ENTRY)
    with pytest.raises(CompileError) as ei:
        load_program(ENTRY, entry_path)
    diag = ei.value.diagnostic
    assert diag.file == lib_path
    assert diag.line == 4  # `def main` in lib.rw


def test_records_module_paths(tmp_path):
    lib_path = _write(tmp_path, "lib", LIB)
    entry_path = _write(tmp_path, "main", ENTRY)
    prog = load_program(ENTRY, entry_path)
    assert prog.paths["main"] == entry_path
    assert prog.paths["lib"] == lib_path


def test_stdlib_module_is_found(tmp_path):
    src = "import bufio\n\ndef main() -> int:\n    return 0\n"
    entry_path = _write(tmp_path, "main", src)
    prog = load_program(src, entry_path)
    assert prog.paths["bufio"].endswith("stdlib/bufio.rw")


def test_local_module_shadows_stdlib(tmp_path):
    _write(tmp_path, "bufio", LIB)
    src = "import bufio\n\ndef main() -> int:\n    return bufio.add(1, 2)\n"
    entry_path = _write(tmp_path, "main", src)
    prog = load_program(src, entry_path)
    assert prog.paths["bufio"] == str(tmp_path / "bufio.rw")


def test_rw_stdlib_env_overrides(tmp_path, monkeypatch):
    libdir = tmp_path / "mystd"
    libdir.mkdir()
    (libdir / "lib.rw").write_text(LIB, encoding="utf-8")
    monkeypatch.setenv("RW_STDLIB", str(libdir))
    entry_path = _write(tmp_path, "main", ENTRY)
    prog = load_program(ENTRY, entry_path)
    assert prog.paths["lib"] == str(libdir / "lib.rw")
