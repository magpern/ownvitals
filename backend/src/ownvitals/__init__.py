"""ownvitals backend: web API and worker."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ownvitals")
except PackageNotFoundError:  # pragma: no cover - only without an installed package
    __version__ = "0+unknown"
