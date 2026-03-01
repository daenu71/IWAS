"""Runtime module for core/coaching/parquet_writer.py."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from pathlib import Path
import time
from typing import Any


class ParquetRunWriter:
    """Container and behavior for Parquet Run Writer."""
    def __init__(
        self,
        run_path: str | Path,
        *,
        recorded_channels: Sequence[str],
        dtype_policy: Mapping[str, str] | None = None,
        dtype_decisions: Mapping[str, str] | None = None,
        chunk_seconds: float = 1.0,
        sample_hz: float | int = 120,
    ) -> None:
        """Implement init logic."""
        self.run_path = Path(run_path)
        self.recorded_channels = [str(name) for name in recorded_channels]
        self.dtype_decisions = dict(dtype_policy or dtype_decisions or {})
        self.chunk_seconds = float(chunk_seconds) if chunk_seconds else 1.0
        try:
            hz = float(sample_hz)
        except Exception:
            hz = 0.0
        chunk_rows = int(round(self.chunk_seconds * hz)) if hz > 0 else 1
        self.chunk_rows = max(1, chunk_rows)

        self._buffer: list[dict[str, Any]] = []
        self._writer: Any | None = None
        self._schema: Any | None = None
        self._pa: Any | None = None
        self._pq: Any | None = None
        self._closed = False
        self._last_flush_summary: dict[str, Any] | None = None

    def append(self, sample: Mapping[str, Any], now_ts: float | None = None) -> bool:
        """Implement append logic."""
        raw = sample.get("raw")
        raw_dict = raw if isinstance(raw, Mapping) else {}
        row: dict[str, Any] = {}
        ts = sample.get("timestamp_wall")
        mono_ts = sample.get("timestamp_monotonic")
        if ts is None:
            ts = time.time()
        if mono_ts is None:
            mono_ts = now_ts if now_ts is not None else time.monotonic()
        row["ts"] = ts
        row["monotonic_ts"] = mono_ts
        for name in self.recorded_channels:
            row[name] = raw_dict.get(name)
        return self.append_row(row)

    def append_row(self, row: Mapping[str, Any]) -> bool:
        """Implement append row logic."""
        if self._closed:
            raise RuntimeError("ParquetRunWriter is closed")
        self._buffer.append(dict(row))
        if len(self._buffer) < self.chunk_rows:
            return False
        self.flush()
        return True

    def flush(self) -> None:
        """Implement flush logic."""
        if self._closed or not self._buffer:
            return
        started = time.perf_counter()
        rows_in_chunk = len(self._buffer)
        self._ensure_backend()
        if self._schema is None:
            self._schema = self._build_schema(self._buffer)
        if self._writer is None:
            self.run_path.parent.mkdir(parents=True, exist_ok=True)
            self._writer = self._pq.ParquetWriter(self.run_path, self._schema)

        try:
            arrays = []
            for field in self._schema:
                values = [self._coerce_value(row.get(field.name), field.type) for row in self._buffer]
                arrays.append(self._pa.array(values, type=field.type))
            table = self._pa.Table.from_arrays(arrays, schema=self._schema)
            table_nbytes = None
            try:
                table_nbytes = int(getattr(table, "nbytes"))
            except Exception:
                table_nbytes = None
            self._writer.write_table(table)
            self._buffer.clear()
            file_size_bytes = None
            try:
                file_size_bytes = int(self.run_path.stat().st_size)
            except Exception:
                file_size_bytes = None
            self._last_flush_summary = {
                "ok": True,
                "path": str(self.run_path),
                "rows": int(rows_in_chunk),
                "duration_ms": float((time.perf_counter() - started) * 1000.0),
                "chunk_rows_target": int(self.chunk_rows),
                "chunk_seconds": float(self.chunk_seconds),
                "table_nbytes": table_nbytes,
                "file_size_bytes": file_size_bytes,
                "ts_wall": float(time.time()),
            }
        except Exception as exc:
            self._last_flush_summary = {
                "ok": False,
                "path": str(self.run_path),
                "rows": int(rows_in_chunk),
                "duration_ms": float((time.perf_counter() - started) * 1000.0),
                "chunk_rows_target": int(self.chunk_rows),
                "chunk_seconds": float(self.chunk_seconds),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "ts_wall": float(time.time()),
            }
            raise

    def close(self, final: bool = True) -> None:
        """Close."""
        if self._closed:
            return
        if final:
            self.flush()
        writer = self._writer
        self._writer = None
        self._closed = True
        if writer is not None:
            writer.close()

    def consume_last_flush_summary(self) -> dict[str, Any] | None:
        """Implement consume last flush summary logic."""
        summary = self._last_flush_summary
        self._last_flush_summary = None
        return dict(summary) if isinstance(summary, dict) else None

    def _ensure_backend(self) -> None:
        """Implement ensure backend logic."""
        if self._pa is not None and self._pq is not None:
            return
        try:
            import pyarrow as pa  # type: ignore
            import pyarrow.parquet as pq  # type: ignore
        except Exception as exc:
            raise RuntimeError("pyarrow is required for ParquetRunWriter") from exc
        self._pa = pa
        self._pq = pq

    def _build_schema(self, rows: Sequence[Mapping[str, Any]]) -> Any:
        """Build and return schema."""
        self._ensure_backend()
        fields = [
            self._pa.field("ts", self._pa.float64(), nullable=True),
            self._pa.field("monotonic_ts", self._pa.float64(), nullable=True),
        ]
        for name in self.recorded_channels:
            fields.append(self._pa.field(name, self._resolve_arrow_type(name, rows), nullable=True))
        return self._pa.schema(fields)

    def _resolve_arrow_type(self, name: str, rows: Sequence[Mapping[str, Any]]) -> Any:
        """Resolve arrow type."""
        self._ensure_backend()
        decision = str(self.dtype_decisions.get(name, "") or "").strip().lower()
        if "[" in decision and "]" in decision:
            # IRSDK array values are not guaranteed scalar here; store as string best-effort.
            return self._pa.string()
        if decision in {"float32", "float"}:
            return self._pa.float32()
        if decision in {"float64", "double"}:
            return self._pa.float64()
        if decision in {"int8"}:
            return self._pa.int8()
        if decision in {"uint8"}:
            return self._pa.uint8()
        if decision in {"int16"}:
            return self._pa.int16()
        if decision in {"uint16"}:
            return self._pa.uint16()
        if decision in {"int32", "int"}:
            return self._pa.int32()
        if decision in {"uint32", "bitfield"}:
            return self._pa.uint32()
        if decision in {"int64"}:
            return self._pa.int64()
        if decision in {"uint64"}:
            return self._pa.uint64()
        if decision in {"bool"}:
            return self._pa.bool_()
        if decision in {"char", "string", "str"}:
            return self._pa.string()

        for row in rows:
            value = row.get(name)
            if value is None:
                continue
            if isinstance(value, bool):
                return self._pa.bool_()
            if isinstance(value, int) and not isinstance(value, bool):
                return self._pa.int64()
            if isinstance(value, float):
                return self._pa.float32()
            return self._pa.string()
        return self._pa.float32()

    def _coerce_value(self, value: Any, arrow_type: Any) -> Any:
        """Coerce value."""
        if value is None:
            return None
        type_id = getattr(arrow_type, "id", None)

        if self._pa.types.is_boolean(arrow_type):
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                text = value.strip().lower()
                if text in {"1", "true", "yes", "y", "on"}:
                    return True
                if text in {"0", "false", "no", "n", "off"}:
                    return False
            return None

        if self._pa.types.is_floating(arrow_type):
            try:
                result = float(value)
            except Exception:
                return None
            if not math.isfinite(result):
                return None
            return result

        if self._pa.types.is_integer(arrow_type):
            if isinstance(value, bool):
                return int(value)
            try:
                if isinstance(value, str):
                    text = value.strip()
                    if not text:
                        return None
                    return int(text, 10)
                return int(value)
            except Exception:
                return None

        if self._pa.types.is_string(arrow_type):
            if isinstance(value, (list, tuple)):
                return ",".join("" if item is None else str(item) for item in value)
            return str(value)

        # Fallback for unsupported arrow types (should be rare with current schema mapping).
        try:
            return value if type_id is not None else str(value)
        except Exception:
            return None
