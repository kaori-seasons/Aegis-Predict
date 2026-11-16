"""``file_csv``: CSV / XLSX exports dropped into a tenant inbox directory.

The universal fallback (plan 5.11): most sellers can export Excel from their ERP. Each stream reads
files matching a glob, column headers use canonical field names (or are renamed via
``column_map``). Files are processed in name order and remembered by content hash, so a file that
is re-uploaded unchanged is skipped and a corrected file with the same name is re-read.
"""

from __future__ import annotations

import csv
import hashlib
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from ...core.config import Settings, get_settings
from ..errors import ContractError, TransientError
from ..spi import Checkpoint, ConnCtx, HealthResult, RawItem, SyncWindow
from ..streams import STREAMS, FieldError, parse_datetime

MAX_REMEMBERED_FILES = 5000
SUPPORTED_SUFFIXES = (".csv", ".xlsx")


def _resolve_directory(directory: str, settings: Settings) -> Path:
    root = Path(settings.file_inbox_root).expanduser().resolve()
    candidate = Path(directory).expanduser()
    path = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"directory must be inside the inbox root {root}")
    return path


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class FileCsvConnector:
    kind: ClassVar[str] = "file_csv"
    version: ClassVar[str] = "1.0"

    # -- configuration -----------------------------------------------------------------------
    def validate_config(self, config: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
        settings = settings or get_settings()
        directory = str(config.get("directory") or "").strip()
        if not directory:
            raise ValueError("directory is required")
        _resolve_directory(directory, settings)
        streams_cfg = config.get("streams") or {}
        if not isinstance(streams_cfg, dict):
            raise ValueError("streams must be an object keyed by stream name")
        unknown = sorted(set(streams_cfg) - set(STREAMS))
        if unknown:
            raise ValueError(f"unknown streams: {', '.join(unknown)}")
        column_map = config.get("column_map") or {}
        if not isinstance(column_map, dict) or any(not isinstance(value, dict) for value in column_map.values()):
            raise ValueError("column_map must map stream -> {canonical_field: header}")
        delimiter = str(config.get("delimiter") or ",")
        if len(delimiter) != 1:
            raise ValueError("delimiter must be a single character")
        naive_timezone = str(config.get("naive_timezone") or "UTC")
        parse_datetime("2000-01-01T00:00:00", naive_timezone)  # validates the zone name
        return {
            "directory": directory,
            "source_system": (str(config["source_system"]).strip() or None) if config.get("source_system") else None,
            "streams": {
                name: {"pattern": str((spec or {}).get("pattern") or f"{name}*")} for name, spec in streams_cfg.items()
            },
            "column_map": column_map,
            "delimiter": delimiter,
            "encoding": str(config.get("encoding") or "utf-8-sig"),
            "naive_timezone": naive_timezone,
        }

    def streams(self, config: dict[str, Any]) -> list[str]:
        configured = config.get("streams") or {}
        return sorted(configured) if configured else sorted(STREAMS)

    # -- runtime -----------------------------------------------------------------------------
    def _directory(self, ctx: ConnCtx) -> Path:
        directory = _resolve_directory(ctx.config["directory"], ctx.settings)
        tenant_root = (ctx.settings.file_inbox_root / str(ctx.tenant_id)).resolve()
        if not directory.is_relative_to(tenant_root):
            raise ContractError("file connection directory must be inside this tenant's inbox")
        return directory

    def _files(self, ctx: ConnCtx, stream: str) -> list[Path]:
        directory = self._directory(ctx)
        if not directory.is_dir():
            raise TransientError(f"inbox directory {directory} is not available")
        pattern = ((ctx.config.get("streams") or {}).get(stream) or {}).get("pattern") or f"{stream}*"
        return sorted(
            path
            for path in directory.glob(pattern)
            if path.is_file() and path.resolve().is_relative_to(directory.resolve())
            and path.suffix.lower() in SUPPORTED_SUFFIXES and not path.name.startswith((".", "~$"))
        )

    def check(self, ctx: ConnCtx) -> HealthResult:
        directory = self._directory(ctx)
        if not directory.is_dir():
            return HealthResult(False, f"directory {directory} does not exist")
        if not os.access(directory, os.R_OK):
            return HealthResult(False, f"directory {directory} is not readable")
        counts = {stream: len(self._files(ctx, stream)) for stream in self.streams(ctx.config)}
        return HealthResult(True, "ok", {"files": {key: value for key, value in counts.items() if value}})

    def count(self, ctx: ConnCtx, stream: str, window: SyncWindow) -> int | None:
        return None

    def read(
        self,
        ctx: ConnCtx,
        stream: str,
        cursor: dict[str, Any] | None,
        window: SyncWindow | None,
    ) -> Iterator[RawItem | Checkpoint]:
        processed: dict[str, str] = dict((cursor or {}).get("processed") or {})
        template = STREAMS[stream]
        column_map = (ctx.config.get("column_map") or {}).get(stream) or {}
        for path in self._files(ctx, stream):
            digest = _file_digest(path)
            if processed.get(path.name) == digest:
                continue
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            rows = self._rows(ctx, path)
            header = next(rows, None)
            if header is None:
                processed[path.name] = digest
                yield Checkpoint(stream, {"processed": _trim(processed)})
                continue
            positions = self._positions(stream, path, header, column_map)
            for line_number, values in rows:
                if all(value in (None, "") for value in values):
                    continue
                payload = {
                    field: values[index] if index < len(values) else None for field, index in positions.items()
                }
                payload = {key: (value.strip() if isinstance(value, str) else value) for key, value in payload.items()}
                try:
                    external_id = template.external_id(payload)
                except FieldError:
                    external_id = f"{path.name}:{line_number}"
                yield RawItem(
                    stream=stream,
                    external_id=external_id,
                    payload=payload,
                    source_updated_at=self._updated_at(payload, ctx, mtime),
                    source_ref=f"{path.name}:{line_number}",
                )
            processed.pop(path.name, None)
            processed[path.name] = digest
            yield Checkpoint(stream, {"processed": _trim(processed)})

    # -- helpers -----------------------------------------------------------------------------
    @staticmethod
    def _positions(stream: str, path: Path, header: tuple[int, list[Any]], column_map: dict[str, str]) -> dict[str, int]:
        _, names = header
        normalised = {str(name).strip().lower(): index for index, name in enumerate(names) if name is not None}
        positions: dict[str, int] = {}
        for field in STREAMS[stream].field_names:
            column = str(column_map.get(field, field)).strip().lower()
            if column in normalised:
                positions[field] = normalised[column]
        missing = [field for field in STREAMS[stream].required_fields if field not in positions]
        if missing:
            raise ContractError(f"{path.name}: missing required columns for stream {stream}: {', '.join(missing)}")
        return positions

    @staticmethod
    def _updated_at(payload: dict[str, Any], ctx: ConnCtx, fallback: datetime) -> datetime:
        value = payload.get("updated_at")
        if value not in (None, ""):
            try:
                return parse_datetime(value, ctx.config.get("naive_timezone") or "UTC")
            except (ValueError, TypeError):
                pass
        return fallback

    def _rows(self, ctx: ConnCtx, path: Path) -> Iterator[tuple[int, list[Any]]]:
        if path.suffix.lower() == ".xlsx":
            yield from self._xlsx_rows(path)
            return
        encoding = ctx.config.get("encoding") or "utf-8-sig"
        delimiter = ctx.config.get("delimiter") or ","
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.reader(handle, delimiter=delimiter)
                for index, row in enumerate(reader, start=1):
                    yield index, row
        except UnicodeDecodeError as exc:
            raise ContractError(f"{path.name}: cannot decode file as {encoding}: {exc}") from exc

    @staticmethod
    def _xlsx_rows(path: Path) -> Iterator[tuple[int, list[Any]]]:
        from openpyxl import load_workbook

        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            sheet = workbook.worksheets[0]
            for index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                yield index, list(row)
        finally:
            workbook.close()


def _trim(processed: dict[str, str]) -> dict[str, str]:
    if len(processed) <= MAX_REMEMBERED_FILES:
        return dict(processed)
    items = list(processed.items())[-MAX_REMEMBERED_FILES:]
    return dict(items)
