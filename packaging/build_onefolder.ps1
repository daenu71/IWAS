param(
    [string]$PythonExe = ".venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $PythonExe)) {
    $PythonExe = "python"
}

& $PythonExe -m pip install -r requirements.txt
& $PythonExe -m pip install pyinstaller
& $PythonExe -m PyInstaller packaging\iWAS_onefolder.spec --clean --noconfirm --distpath dist --workpath build\pyinstaller
