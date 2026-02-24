# Third-Party Licenses

This project bundles third-party software in the Windows `dist` package.

## FFmpeg (bundled in `dist/iWAS/_internal/tools/ffmpeg`)

iWAS bundles `ffmpeg` and `ffprobe` binaries (plus required FFmpeg shared libraries) for video processing.

- Project: FFmpeg
- Website: https://ffmpeg.org/
- Source of Windows binaries:
  `https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl-shared.zip`
- Authoritative repo location for bundled binaries:
  `third_party\ffmpeg\lgpl_shared\bin\`
- License type (required for bundled Windows build): `LGPL` (not GPL)

Important:

- The applicable license terms for FFmpeg are determined by the specific FFmpeg binaries distributed with iWAS.
- The Windows one-folder build is configured to bundle FFmpeg only from `third_party\ffmpeg\lgpl_shared\bin\` (not from PATH).
- The build script performs a hard guard by running `ffmpeg.exe -version` and aborts if `--enable-gpl` is present.
- If you replace the bundled FFmpeg build, update this file accordingly and keep the LGPL source URL above.

Recommended release metadata to record (per build):

- FFmpeg version:
- Source of binaries (URL/vendor):
- License type of bundled build (LGPL/GPL): `LGPL`
- Build date:

Bundled license texts (when included in the vendor ZIP):

- `third_party\ffmpeg\lgpl_shared\licenses\LICENSE.txt`

For FFmpeg licensing/compliance guidance, see:

- https://ffmpeg.org/legal.html
