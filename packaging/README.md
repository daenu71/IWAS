# iWAS Windows EXE Build (One-folder)

## Build Commands

Repo `.venv` (recommended):

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install pyinstaller
.venv\Scripts\python.exe -m PyInstaller packaging\iWAS_onefolder.spec --clean --noconfirm --distpath dist --workpath build\pyinstaller
```

Generic `python`:

```powershell
python -m pip install -r requirements.txt
python -m pip install pyinstaller
python -m PyInstaller packaging\iWAS_onefolder.spec --clean --noconfirm --distpath dist --workpath build\pyinstaller
```

Optional runner:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_onefolder.ps1
```

## Clean Windows Test Checklist

Prerequisite for render test:

- Copy the complete `dist\iWAS\` folder (contains bundled `ffmpeg`/`ffprobe` and FFmpeg DLLs).
- Do not copy only `iWAS.exe`.

1. Copy the complete folder `dist\iWAS\` to the test machine.
2. Start `dist\iWAS\iWAS.exe` with double-click.
3. Verify minimal app startup:
   - Main window opens.
   - Top logo/icon is visible (asset bundle check).
4. Verify minimal render path:
   - Select one fast and one slow video plus matching CSVs.
   - Start a short render.
   - Check that an output video is created in the app output folder.
5. If render fails immediately, verify `_internal\tools\ffmpeg\` exists in the copied folder.
