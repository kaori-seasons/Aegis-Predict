"""Connector registry: ``kind`` -> implementation."""

from __future__ import annotations

from typing import Any

from ..core.errors import InvalidInput
from .spi import SourceConnector, WritebackConnector

_REGISTRY: dict[str, Any] = {}
_BUILTINS_LOADED = False


def register(connector_cls: type) -> type:
    _REGISTRY[connector_cls.kind] = connector_cls
    return connector_cls


def _load_builtin() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    from .connectors.file_csv import FileCsvConnector
    from .connectors.generic_rest import GenericRestConnector
    from .connectors.amazon_sp import AmazonSpConnector
    from .connectors.tiktok_shop import TikTokShopConnector

    _REGISTRY.setdefault(FileCsvConnector.kind, FileCsvConnector)
    _REGISTRY.setdefault(GenericRestConnector.kind, GenericRestConnector)
    _REGISTRY.setdefault(AmazonSpConnector.kind, AmazonSpConnector)
    _REGISTRY.setdefault(TikTokShopConnector.kind, TikTokShopConnector)
    from .connectors.mock_platforms import MockAmazonConnector, MockTikTokConnector
    _REGISTRY.setdefault(MockAmazonConnector.kind, MockAmazonConnector)
    _REGISTRY.setdefault(MockTikTokConnector.kind, MockTikTokConnector)
    _BUILTINS_LOADED = True


def get_connector(kind: str) -> SourceConnector:
    _load_builtin()
    try:
        return _REGISTRY[kind]()
    except KeyError as exc:
        raise InvalidInput(f"unknown connector kind '{kind}'", details={"available": sorted(_REGISTRY)}) from exc


def available_kinds() -> list[str]:
    _load_builtin()
    return sorted(_REGISTRY)


def supports_writeback(connector: Any, config: dict[str, Any]) -> bool:
    return isinstance(connector, WritebackConnector) and bool(connector.writeback_commands(config))
