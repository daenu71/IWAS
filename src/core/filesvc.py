"""Runtime module for core/filesvc.py."""

import os
import shutil
from pathlib import Path
from typing import Callable, Sequence


FolderScanSignature = tuple[tuple[str, ...], tuple[str, ...]]
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi"}


def copy_to_dir(src: Path, dst_dir: Path) -> Path:
    """Implement copy to dir logic."""
    dst = dst_dir / src.name
    if not dst.exists():
        shutil.copy2(src, dst)
    return dst


def delete_file(path: Path) -> bool:
    """Implement delete file logic."""
    try:
        if path.exists():
            path.unlink()
        return True
    except Exception:
        return False


def open_folder(path: Path) -> None:
    """Open folder."""
    try:
        os.startfile(str(path.parent))
    except Exception:
        pass


def scan_folders_signature(input_video_dir: Path, input_csv_dir: Path) -> FolderScanSignature:
    """Scan folders signature."""
    vids = tuple(sorted([p.name for p in input_video_dir.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS]))
    cs = tuple(sorted([p.name for p in input_csv_dir.glob("*.csv") if p.is_file()]))
    return vids, cs


def sync_from_folders_if_needed(
    videos: list[Path],
    csvs: list[Path],
    last_scan_sig: FolderScanSignature | None,
    input_video_dir: Path,
    input_csv_dir: Path,
    refresh_display: Callable[[], None],
    force: bool = False,
) -> tuple[list[Path], list[Path], FolderScanSignature | None]:
    """Synchronize from folders if needed."""
    sig = scan_folders_signature(input_video_dir, input_csv_dir)
    if (not force) and (sig == last_scan_sig):
        return videos, csvs, last_scan_sig
    last_scan_sig = sig

    available_videos = [input_video_dir / n for n in sig[0]]
    available_csvs = [input_csv_dir / n for n in sig[1]]

    videos = [p for p in videos if p.exists()]
    csvs = [p for p in csvs if p.exists()]

    if len(videos) == 0 and len(available_videos) > 0:
        videos = available_videos[:2]
    else:
        videos = [p for p in videos if p in available_videos][:2]

    if len(csvs) == 0 and len(available_csvs) > 0:
        csvs = available_csvs[:2]
    else:
        csvs = [p for p in csvs if p in available_csvs][:2]

    refresh_display()
    return videos, csvs, last_scan_sig


def periodic_folder_watch(sync_callback: Callable[[], None], schedule_callback: Callable[[], None]) -> None:
    """Implement periodic folder watch logic."""
    sync_callback()
    schedule_callback()


def select_files(paths: Sequence[str], input_video_dir: Path, input_csv_dir: Path) -> tuple[str, list[Path], list[Path]]:
    """Select files."""
    if not paths:
        return "empty", [], []

    selected_videos: list[Path] = []
    selected_csvs: list[Path] = []

    for p in paths:
        pp = Path(p)
        suf = pp.suffix.lower()
        if suf in VIDEO_EXTS:
            selected_videos.append(pp)
        elif suf == ".csv":
            selected_csvs.append(pp)

    if len(selected_videos) > 2:
        return "too_many_videos", [], []

    if len(selected_videos) == 0 and len(selected_csvs) > 0:
        copied_csvs: list[Path] = []
        for c in selected_csvs[:2]:
            copied_csvs.append(copy_to_dir(c, input_csv_dir))
        return "csv_only", [], copied_csvs[:2]

    copied_videos: list[Path] = []
    for v in selected_videos:
        copied_videos.append(copy_to_dir(v, input_video_dir))

    copied_csvs: list[Path] = []
    for c in selected_csvs[:2]:
        copied_csvs.append(copy_to_dir(c, input_csv_dir))

    return "ok", copied_videos[:2], copied_csvs[:2]
