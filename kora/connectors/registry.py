from __future__ import annotations

from ..models import Source
from .base import ActionConnector, SourceConnector


def _build(src: Source):
    if src.connector == "filesystem":
        from .filesystem import FilesystemConnector
        return FilesystemConnector(src.config["root"], src.system, src.config.get("site"))
    raise ValueError(f"unknown connector type {src.connector!r}")


def get_connector(src: Source) -> SourceConnector:
    """Read-only view used by ingestion."""
    return _build(src)


def get_action_connector(src: Source) -> ActionConnector:
    """Privileged view used only by the deletion executor (separate credentials in production)."""
    if not src.config.get("allow_actions", True):
        raise PermissionError(f"source {src.name} does not permit KORA actions")
    return _build(src)
