from pathlib import Path
import os


SPEC_DIR = Path(globals().get("SPECPATH") or Path.cwd()).resolve()
PROJECT_ROOT = SPEC_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
project_root = str(PROJECT_ROOT)
ffmpeg_src = os.path.join(project_root, "third_party", "ffmpeg", "lgpl_shared", "bin")


def _collect_ffmpeg_binaries() -> list[tuple[str, str]]:
    src = Path(ffmpeg_src)
    if not (src / "ffmpeg.exe").exists():
        raise FileNotFoundError(
            f"Missing FFmpeg binary for bundling: {src / 'ffmpeg.exe'} "
            "Install the LGPL shared build in third_party\\ffmpeg\\lgpl_shared\\bin."
        )
    if not (src / "ffprobe.exe").exists():
        raise FileNotFoundError(
            f"Missing FFprobe binary for bundling: {src / 'ffprobe.exe'} "
            "Install the LGPL shared build in third_party\\ffmpeg\\lgpl_shared\\bin."
        )

    out: list[tuple[str, str]] = [
        # PyInstaller one-folder places binaries under dist/iWAS/_internal/...
        (str(src / "ffmpeg.exe"), "tools/ffmpeg"),
        (str(src / "ffprobe.exe"), "tools/ffmpeg"),
    ]
    for dll in sorted(src.glob("*.dll")):
        out.append((str(dll), "tools/ffmpeg"))

    print(f"[iWAS build] Bundling FFmpeg tools from authoritative source: {src}")
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
