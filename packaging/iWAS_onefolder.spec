from pathlib import Path


SPEC_DIR = Path(globals().get("SPECPATH") or Path.cwd()).resolve()
PROJECT_ROOT = SPEC_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"


a = Analysis(
    [str(SRC_DIR / "app_entry.py")],
    pathex=[str(SRC_DIR)],
    binaries=[],
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
