# Windows EXE Build (One-folder)

## Build Commands

Repo `.venv` (recommended):

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install pyinstaller
.venv\Scripts\python.exe -m PyInstaller packaging\iracing_vc_onefolder.spec --clean --noconfirm --distpath dist --workpath build\pyinstaller
```

Generic `python`:

```powershell
python -m pip install -r requirements.txt
python -m pip install pyinstaller
python -m PyInstaller packaging\iracing_vc_onefolder.spec --clean --noconfirm --distpath dist --workpath build\pyinstaller
```

Optional runner:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_onefolder.ps1
```

## Clean Windows Test Checklist

Prerequisite for render test: `ffmpeg` and `ffprobe` available in `PATH`.

1. Copy the complete folder `dist\iracing-vc\` to the test machine (do not copy only the `.exe`).
2. Start `dist\iracing-vc\iracing-vc.exe` with double-click.
3. Verify minimal app startup:
   - Main window opens.
   - Top logo/icon is visible (asset bundle check).
4. Verify minimal render path:
   - Select one fast and one slow video plus matching CSVs.
   - Start a short render.
   - Check that an output video is created in the app output folder.
5. If render fails immediately, first verify `ffmpeg`/`ffprobe` in `PATH` on the clean machine.
