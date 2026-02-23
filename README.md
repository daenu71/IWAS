# iWAS

iWAS is a desktop tool for creating synchronized iRacing lap comparison videos with telemetry-based HUD overlays.

It renders a split-screen video (slow lap vs fast lap), matches telemetry CSV files, syncs both laps by lap distance, and overlays HUD widgets such as speed, throttle/brake, steering, delta, and more.

Status: `0.1.0 (beta)`

## Features

- Split-screen lap comparison rendering
- Telemetry-based sync (lap-distance mapping)
- Automatic CSV matching (including Garage 61 style filenames)
- Configurable output presets / aspect ratios
- Configurable HUD layout and HUD box placement
- HUD overlays:
  - `Speed`
  - `Throttle / Brake`
  - `Steering`
  - `Delta`
  - `Gear & RPM`
  - `Line Delta`
  - `Under-/Oversteer`
- FFmpeg encoder fallback (GPU encoders if available, CPU fallback)

## Requirements

- Windows (primary target for the packaged app)
<<<<<<< HEAD
- `ffmpeg` and `ffprobe` available in `PATH`
=======
>>>>>>> cc1ffa4 (gepatcht, dass die dist-Version ffmpeg und ffprobe (inkl. benötigter FFmpeg-DLLs) mitliefert.)
- Matching lap videos (`.mp4`) and telemetry CSVs (`.csv`)

Python source run additionally requires:

- Python 3.11+
- `pip`
<<<<<<< HEAD
=======
- `ffmpeg` and `ffprobe` available in `PATH`
>>>>>>> cc1ffa4 (gepatcht, dass die dist-Version ffmpeg und ffprobe (inkl. benötigter FFmpeg-DLLs) mitliefert.)

## Quick Start (Windows EXE)

1. Download the release and extract it.
2. Copy the whole `dist\\iWAS\\` folder (not only `iWAS.exe`).
<<<<<<< HEAD
3. Ensure `ffmpeg` and `ffprobe` are available in `PATH`.
4. Start `iWAS.exe`.
5. Select:
   - slow lap video
   - fast lap video
   - CSVs (optional if auto-matching finds them)
6. Start rendering.
=======
3. Start `iWAS.exe`.
4. Select:
   - slow lap video
   - fast lap video
   - CSVs (optional if auto-matching finds them)
5. Start rendering.
>>>>>>> cc1ffa4 (gepatcht, dass die dist-Version ffmpeg und ffprobe (inkl. benötigter FFmpeg-DLLs) mitliefert.)

## Run From Source

```powershell
python -m pip install -r requirements.txt
python src\app_entry.py
```

Notes:

- `src/app_entry.py` starts the GUI by default.
- Internal render mode is triggered by the UI using `--ui-json`.

## Build Windows EXE (PyInstaller)

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install pyinstaller
.venv\Scripts\python.exe -m PyInstaller packaging\iWAS_onefolder.spec --clean --noconfirm --distpath dist --workpath build\pyinstaller
```

Alternative helper:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_onefolder.ps1
```

## Input / Output

Default project folders (typical usage):

<<<<<<< HEAD
- `input/video` for source videos. (Make a screen video of a slow and fast lap using an iRacing replay.)
- `input/csv` for telemetry CSVs. (Download the csv file from Garage 61 for the same laps. Rename the videos to match the csv files.)
=======
- `input/video` for source videos
- `input/csv` for telemetry CSVs
>>>>>>> cc1ffa4 (gepatcht, dass die dist-Version ffmpeg und ffprobe (inkl. benötigter FFmpeg-DLLs) mitliefert.)
- `output/video` for rendered comparison videos
- `output/debug` for sync/debug artifacts

The app can also work with files selected from arbitrary folders.

<<<<<<< HEAD
### Render fails immediately

- Check `ffmpeg` and `ffprobe` are in `PATH`.
- Verify video and CSV filenames belong to the same laps.
- Check the generated log file for the first error lines.

=======
## Troubleshooting

### HUD boxes visible but HUD content missing

- Update to the latest `0.1.0 beta` build (this was fixed in the packaged EXE workflow).

### Render fails immediately

- For source runs: check `ffmpeg` and `ffprobe` are in `PATH`.
- For packaged builds: ensure the full `dist\\iWAS\\` folder was copied (including `_internal\\tools\\ffmpeg`).
- Verify video and CSV filenames belong to the same laps.
- Check the generated log file for the first error lines.

### Console window appears during render

- Recent beta builds hide the FFmpeg console window on Windows.
- If you still see one, test with the latest build.

>>>>>>> cc1ffa4 (gepatcht, dass die dist-Version ffmpeg und ffprobe (inkl. benötigter FFmpeg-DLLs) mitliefert.)
## Project Structure (high level)

- `src/ui/` GUI
- `src/features/render_split.py` render orchestration + HUD pipeline
- `src/core/` sync, ffmpeg planning, encoders, services
- `packaging/` PyInstaller spec and build scripts
- `config/` app defaults and saved UI config templates

## Beta Notes

This is a beta release. Edge cases may still exist depending on:

- video codecs / frame rates
- CSV format variations
- FFmpeg installation differences
- unusual HUD layouts or presets

Bug reports with a short log excerpt are very helpful.
