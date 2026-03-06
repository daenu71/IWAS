"""Unit tests for privacy-safe diagnostics export."""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.diagnostics import export_diagnostics_bundle, format_masked_ffmpeg_command  # noqa: E402


def test_format_masked_ffmpeg_command_masks_paths_and_tokens() -> None:
    args = [
        "C:\\ffmpeg\\bin\\ffmpeg.exe",
        "-i",
        "C:\\Users\\Daniel\\Videos\\lap.mp4",
        "-metadata",
        "source=https://example.test/render?token=abc123&apikey=secret",
        "C:\\iWAS\\output\\video\\out.mp4",
    ]

    masked = format_masked_ffmpeg_command(args)

    assert "Daniel" not in masked
    assert "abc123" not in masked
    assert "secret" not in masked
    assert "<user_path>\\Videos\\lap.mp4" in masked
    assert "token=<redacted>" in masked
    assert "apikey=<redacted>" in masked
    assert "<abs_path>\\video\\out.mp4" in masked


def test_export_diagnostics_bundle_scrubs_sensitive_content(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    logs_dir = project_root / "_logs"
    config_dir = project_root / "config"
    storage_dir = tmp_path / "coaching_store"
    session_dir = storage_dir / "session_001"
    bundle_path = tmp_path / "safe_diagnostics.zip"

    logs_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    session_dir.mkdir(parents=True, exist_ok=True)

    log_path = logs_dir / "render.txt"
    log_path.write_text(
        "\n".join(
            [
                "ui_slow_video=C:\\Users\\Daniel\\Videos\\lap.mp4",
                "[FFMPEG-CMD] ffmpeg -i \"C:\\Users\\Daniel\\Videos\\lap.mp4\" -metadata \"source=https://example.test/render?token=abc123&auth=topsecret\" \"C:\\iWAS\\output\\video\\out.mp4\"",
                "csv_search_dirs=C:\\Users\\Daniel\\Documents;C:\\iWAS\\input\\csv",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (config_dir / "user.ini").write_text(
        "\n".join(
            [
                "token = abc123",
                "api_key = secret-value",
                "output_dir = C:\\Users\\Daniel\\Videos",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (session_dir / "session_meta.json").write_text(
        json.dumps(
            {
                "video_path": "C:\\Users\\Daniel\\Videos\\lap.mp4",
                "upload_url": "https://example.test/upload?apikey=secret&auth=token123",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (session_dir / "run_0001_meta.json").write_text(
        json.dumps(
            {
                "output_path": "C:\\iWAS\\output\\video\\out.mp4",
                "token": "xyz",
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    export_diagnostics_bundle(
        bundle_path,
        project_root=project_root,
        coaching_storage_dir=storage_dir,
        output_video_dir="C:\\Users\\Daniel\\Videos",
        redact_sensitive=True,
    )

    with zipfile.ZipFile(bundle_path, "r") as zf:
        names = set(zf.namelist())
        assert "logs/render.txt" in names
        assert "config/user.ini" in names
        assert "coaching/session_001/session_meta.json" in names
        assert "coaching/session_001/run_0001_meta.json" in names
        assert "diagnostics_manifest.json" in names

        log_text = zf.read("logs/render.txt").decode("utf-8")
        config_text = zf.read("config/user.ini").decode("utf-8")
        session_text = zf.read("coaching/session_001/session_meta.json").decode("utf-8")
        run_meta_text = zf.read("coaching/session_001/run_0001_meta.json").decode("utf-8")
        manifest_text = zf.read("diagnostics_manifest.json").decode("utf-8")
        dump_text = zf.read("windows_dump_paths.txt").decode("utf-8")

    for text in (log_text, config_text, session_text, run_meta_text, manifest_text):
        assert "Daniel" not in text
        assert "abc123" not in text
        assert "secret-value" not in text
        assert "token123" not in text
        assert "C:\\Users\\Daniel" not in text
        assert "C:\\iWAS" not in text

    assert "<user_path>\\Videos\\lap.mp4" in log_text
    assert "token=<redacted>" in log_text
    assert "auth=<redacted>" in log_text
    assert "<user_path>\\Documents;<abs_path>\\input\\csv" in log_text
    assert "token = <redacted>" in config_text
    assert "api_key = <redacted>" in config_text
    assert "<user_path>\\Videos" in config_text
    assert "<redacted>" in session_text
    assert "<abs_path>\\video\\out.mp4" in run_meta_text
    assert "<windows_minidump_path>" in dump_text
    assert "<windows_memory_dump_path>" in dump_text
