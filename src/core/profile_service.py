from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from core.models import AppModel, HudLayoutState, PngViewState, Profile


def build_profile_dict(
    *,
    videos: list[Path],
    csvs: list[Path],
    startframes_by_name: dict[str, int],
    endframes_by_name: dict[str, int],
    profile_model_from_ui_state: Callable[[list[str], list[str], dict[str, int], dict[str, int]], Profile],
    set_app_model: Callable[[AppModel], None],
) -> dict[str, Any]:
    vnames: list[str] = []
    cnames: list[str] = []

    for p in videos[:2]:
        try:
            vnames.append(p.name)
        except Exception:
            pass

    for p in csvs[:2]:
        try:
            cnames.append(p.name)
        except Exception:
            pass

    starts: dict[str, int] = {}
    ends: dict[str, int] = {}

    for n in vnames:
        try:
            starts[n] = int(startframes_by_name.get(n, 0))
        except Exception:
            starts[n] = 0
        try:
            ends[n] = int(endframes_by_name.get(n, 0))
        except Exception:
            ends[n] = 0

    profile = profile_model_from_ui_state(vnames, cnames, starts, ends)
    set_app_model(
        AppModel(
            output=profile.output,
            hud_layout=HudLayoutState(hud_layout_data=profile.hud_layout_data),
            png_view=PngViewState(png_view_data=profile.png_view_data),
        )
    )
    return profile.to_dict()


def apply_profile_dict(
    profile: dict[str, Any],
    *,
    set_out_aspect: Callable[[str], None],
    set_out_quality: Callable[[str], None],
    set_out_preset: Callable[[str], None],
    set_hud_width_px: Callable[[int], None],
    get_out_aspect: Callable[[], str],
    get_out_quality: Callable[[], str],
    get_out_preset: Callable[[], str],
    get_hud_width_px: Callable[[], int],
    save_output_format: Callable[[dict[str, str]], None],
    set_hud_layout_data: Callable[[dict[str, Any]], None],
    save_hud_layout: Callable[[dict[str, Any]], None],
    set_png_view_data: Callable[[dict[str, Any]], None],
    save_png_view: Callable[[dict[str, Any]], None],
    sync_app_model_from_ui_state: Callable[[], None],
    startframes_by_name: dict[str, int],
    endframes_by_name: dict[str, int],
    save_startframes: Callable[[dict[str, int]], None],
    save_endframes: Callable[[dict[str, int]], None],
    input_video_dir: Path,
    input_csv_dir: Path,
    set_videos: Callable[[list[Path]], None],
    set_csvs: Callable[[list[Path]], None],
    reset_last_scan_sig: Callable[[], None],
    close_preview_video: Callable[[], None],
    refresh_display: Callable[[], None],
    get_preview_mode: Callable[[], str],
    png_load_state_for_current: Callable[[], None],
    render_png_preview: Callable[..., None],
    refresh_layout_preview: Callable[[], None],
) -> None:
    if not isinstance(profile, dict):
        return

    out = profile.get("output")
    if isinstance(out, dict):
        a = str(out.get("aspect") or "").strip()
        p = str(out.get("preset") or "").strip()
        q = str(out.get("quality") or "").strip()
        h = str(out.get("hud_width_px") or "").strip()

        if a:
            try:
                set_out_aspect(a)
            except Exception:
                pass
        if q:
            try:
                set_out_quality(q)
            except Exception:
                pass
        if p:
            try:
                set_out_preset(p)
            except Exception:
                pass
        if h != "":
            try:
                set_hud_width_px(max(0, int(float(h))))
            except Exception:
                pass

        try:
            save_output_format(
                {
                    "aspect": get_out_aspect(),
                    "preset": get_out_preset(),
                    "quality": get_out_quality(),
                    "hud_width_px": str(get_hud_width_px()),
                }
            )
        except Exception:
            pass

    hl = profile.get("hud_layout_data")
    if isinstance(hl, dict):
        set_hud_layout_data(hl)
        try:
            save_hud_layout(hl)
        except Exception:
            pass

    pv = profile.get("png_view_data")
    if isinstance(pv, dict):
        set_png_view_data(pv)
        try:
            save_png_view(pv)
        except Exception:
            pass

    sync_app_model_from_ui_state()

    sf = profile.get("startframes")
    if isinstance(sf, dict):
        for k, v in sf.items():
            try:
                startframes_by_name[str(k)] = int(v)
            except Exception:
                pass
        try:
            save_startframes(startframes_by_name)
        except Exception:
            pass

    ef = profile.get("endframes")
    if isinstance(ef, dict):
        for k, v in ef.items():
            try:
                endframes_by_name[str(k)] = int(v)
            except Exception:
                pass
        try:
            save_endframes(endframes_by_name)
        except Exception:
            pass

    vlist = profile.get("videos")
    clist = profile.get("csvs")

    new_videos: list[Path] = []
    new_csvs: list[Path] = []

    if isinstance(vlist, list):
        for n in vlist[:2]:
            try:
                p = input_video_dir / str(n)
                if p.exists():
                    new_videos.append(p)
            except Exception:
                pass

    if isinstance(clist, list):
        for n in clist[:2]:
            try:
                p = input_csv_dir / str(n)
                if p.exists():
                    new_csvs.append(p)
            except Exception:
                pass

    set_videos(new_videos[:2])
    set_csvs(new_csvs[:2])

    reset_last_scan_sig()
    try:
        close_preview_video()
    except Exception:
        pass
    try:
        refresh_display()
    except Exception:
        pass
    try:
        if get_preview_mode() == "png":
            png_load_state_for_current()
            render_png_preview(force_reload=True)
        else:
            refresh_layout_preview()
    except Exception:
        pass
