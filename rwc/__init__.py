"""rw compiler package."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("rw")
except PackageNotFoundError:  # not installed (e.g. running from a checkout)
    __version__ = "0.0.0+unknown"
