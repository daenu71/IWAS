from pathlib import Path
import os
import shutil


SPEC_DIR = Path(globals().get("SPECPATH") or Path.cwd()).resolve()
PROJECT_ROOT = SPEC_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
project_root = str(PROJECT_ROOT)
ffmpeg_src = os.path.join(project_root, "third_party", "ffmpeg", "lgpl_shared", "bin")
ffmpeg_lic_src = os.path.join(project_root, "third_party", "ffmpeg", "lgpl_shared", "licenses")


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


def _collect_ffmpeg_licenses() -> list[tuple[str, str]]:
    src = Path(ffmpeg_lic_src)
    if not (src / "LICENSE.txt").exists():
        raise FileNotFoundError(
            f"Missing FFmpeg license file for bundling: {src / 'LICENSE.txt'} "
            "Install the LGPL shared license files in third_party\\ffmpeg\\lgpl_shared\\licenses."
        )

    out = [(str(path), "tools/ffmpeg/licenses") for path in sorted(src.iterdir()) if path.is_file()]
    print(f"[iWAS build] Bundling FFmpeg license files from authoritative source: {src}")
    return out


FFMPEG_LICENSES = _collect_ffmpeg_licenses()


a = Analysis(
    [str(SRC_DIR / "app_entry.py")],
    pathex=[str(SRC_DIR)],
    binaries=FFMPEG_BINARIES,
    datas=[
        (str(PROJECT_ROOT / "assets"), "assets"),
        (str(PROJECT_ROOT / "config"), "config"),
        (os.path.join(project_root, "THIRD_PARTY_LICENSES.md"), "."),
        *FFMPEG_LICENSES,
    ],
    hiddenimports=["irsdk"],
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

shutil.copy2(os.path.join(project_root, "THIRD_PARTY_LICENSES.md"), str(Path(coll.name) / "THIRD_PARTY_LICENSES.md"))
