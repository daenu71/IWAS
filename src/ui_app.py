"""Compatibility wrapper for launching the UI app module."""

from ui.app import *


def _run_ui_with_recorder_service() -> None:
    """Run ui with recorder service."""
    from core import persistence
    from core.irsdk.recorder_service import RecorderService
    import ui.app as ui_app

    recorder_service = RecorderService()

    def _current_irsdk_sample_hz() -> int:
        """Implement current irsdk sample hz logic."""
        try:
            settings = persistence.load_coaching_recording_settings()
            return int(settings.get("irsdk_sample_hz", 120))
        except Exception:
            return 120

    try:
        ui_app.irsdk_recorder_service = recorder_service
        ui_app.start_irsdk_recorder_service = lambda: recorder_service.start(_current_irsdk_sample_hz())
        ui_app.stop_irsdk_recorder_service = recorder_service.stop
    except Exception:
        pass

    try:
        ui_app.main()
    finally:
        recorder_service.stop()


if __name__ == "__main__":
    _run_ui_with_recorder_service()
