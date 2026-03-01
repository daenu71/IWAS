"""HUD renderer registry helpers."""

# hud_registry.py
# Zentrale Registry: HUD-Name (UI-Key) -> Legacy-Render-Funktion
# Adapter-Phase: verweist bewusst auf bestehende Funktionen in render_split.py

from typing import Callable, Dict


HudRenderer = Callable[..., None]


def build_hud_registry(legacy_renderers: Dict[str, HudRenderer]) -> Dict[str, HudRenderer]:
    """
    Erwartet ein Dict mit Legacy-Renderer-Funktionen aus render_split.py
    und gibt es unverändert zurück.

    Trennungspunkt für spätere echte HUD-Module.
    """
    return dict(legacy_renderers)