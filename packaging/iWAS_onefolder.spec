from pathlib import Path
import os
import shutil


SPEC_DIR = Path(globals().get("SPECPATH") or Path.cwd()).resolve()
PROJECT_ROOT = SPEC_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"


def _collect_ffmpeg_binaries() -> list[tuple[str, str]]:
    raw_bundle = str(os.environ.get("IWAS_BUNDLE_FFMPEG", "1")).strip().lower()
    if raw_bundle in ("0", "false", "no", "off"):
        print("[iWAS build] FFmpeg bundling disabled via IWAS_BUNDLE_FFMPEG")
        return []

    candidates: list[Path] = []

    for env_key in ("IWAS_FFMPEG_BIN_DIR", "FFMPEG_BIN_DIR"):
        raw = str(os.environ.get(env_key, "")).strip()
        if raw:
            candidates.append(Path(raw))

    # Common local folders on this project setup.
    candidates.append(Path(r"C:\ffmpeg-shared\bin"))
    candidates.append(Path(r"C:\ffmpeg\bin"))

    for tool in ("ffmpeg", "ffprobe"):
        try:
            hit = shutil.which(tool)
        except Exception:
            hit = None
        if hit:
            try:
                candidates.append(Path(hit).resolve().parent)
            except Exception:
                pass

    chosen: Path | None = None
    seen: set[str] = set()
    for c in candidates:
        try:
            cp = c.resolve()
        except Exception:
            cp = c
        key = str(cp).lower()
        if key in seen:
            continue
        seen.add(key)
        if (cp / "ffmpeg.exe").exists() and (cp / "ffprobe.exe").exists():
            chosen = cp
            break

    if chosen is None:
        raise FileNotFoundError(
            "Could not locate ffmpeg/ffprobe for bundling. "
            "Set IWAS_FFMPEG_BIN_DIR to a folder containing ffmpeg.exe and ffprobe.exe."
        )

    out: list[tuple[str, str]] = [
        (str(chosen / "ffmpeg.exe"), "tools/ffmpeg"),
        (str(chosen / "ffprobe.exe"), "tools/ffmpeg"),
    ]
    for dll in sorted(chosen.glob("*.dll")):
        out.append((str(dll), "tools/ffmpeg"))

    print(f"[iWAS build] Bundling FFmpeg tools from: {chosen}")
    return out


FFMPEG_BINARIES = _collect_ffmpeg_binaries()


a = Analysis(
    [str(SRC_DIR / "app_entry.py")],
    pathex=[str(SRC_DIR)],
    binaries=FFMPEG_BINARIES,
    datas=[
        (str(PROJECT_ROOT / "assets"), "assets"),
        (str(PROJECT_ROOT / "config"), "config"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="iWAS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(PROJECT_ROOT / "assets" / "logo" / "iwas_icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="iWAS",
)
