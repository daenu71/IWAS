param(
    [string]$PythonExe = ".venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$FfmpegBin = Join-Path $RepoRoot "third_party\ffmpeg\lgpl_shared\bin"
$FfmpegExe = Join-Path $FfmpegBin "ffmpeg.exe"
$FfprobeExe = Join-Path $FfmpegBin "ffprobe.exe"

if (-not (Test-Path -LiteralPath $FfmpegExe)) {
    throw "Missing required FFmpeg binary: $FfmpegExe. Download and install the LGPL shared zip into third_party\ffmpeg\lgpl_shared\bin."
}
if (-not (Test-Path -LiteralPath $FfprobeExe)) {
    throw "Missing required FFprobe binary: $FfprobeExe. Download and install the LGPL shared zip into third_party\ffmpeg\lgpl_shared\bin."
}

try {
    $verLines = & $FfmpegExe -version 2>&1
} catch {
    throw "Failed to execute FFmpeg for license check ($FfmpegExe): $($_.Exception.Message)"
}

$ver = ($verLines | Out-String).Trim()
if ([string]::IsNullOrWhiteSpace($ver)) {
    throw "FFmpeg version check returned no output: $FfmpegExe"
}
if ($ver -match "--enable-gpl") {
    throw "GPL FFmpeg detected. Use LGPL zip: https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl-shared.zip"
}

$BuildDir = Join-Path $RepoRoot "build"
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
Set-Content -Path (Join-Path $BuildDir "ffmpeg_version.txt") -Value $ver -Encoding UTF8

Push-Location $RepoRoot
try {
    if (-not (Test-Path $PythonExe)) {
        $PythonExe = "python"
    }

    & $PythonExe -m pip install -r requirements.txt
    & $PythonExe -m pip install pyinstaller
    & $PythonExe -m PyInstaller packaging\iWAS_onefolder.spec --clean --noconfirm --distpath dist --workpath build\pyinstaller
} finally {
    Pop-Location
}
