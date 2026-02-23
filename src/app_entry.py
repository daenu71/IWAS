from __future__ import annotations

import sys


def main() -> None:
    # Single PyInstaller entry: render mode when UI passes --ui-json, otherwise GUI.
    if any(arg == "--ui-json" for arg in sys.argv[1:]):
        from main import main as render_main

        render_main()
        return

    from ui.app import main as ui_main

    ui_main()


if __name__ == "__main__":
    main()
