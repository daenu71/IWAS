"""HUD layout activation and filtering helpers."""

# hud_layout.py
from typing import Dict, List, Tuple


HudBox = Tuple[int, int, int, int]


class HudLayoutItem:
    """Container and behavior for Hud Layout Item."""
    def __init__(self, name: str, box: HudBox):
        """Implement init logic."""
        self.name = name
        self.box = box


def build_active_hud_layout(
    hud_enabled: Dict[str, bool],
    hud_boxes: Dict[str, HudBox],
) -> List[HudLayoutItem]:
    """
    Liefert eine deterministische Liste aktiver HUDs mit ihren Boxen.
    Reihenfolge folgt der Reihenfolge in hud_boxes.
    """

    items: List[HudLayoutItem] = []

    for name, box in hud_boxes.items():
        if not hud_enabled.get(name, False):
            continue

        items.append(HudLayoutItem(name=name, box=box))

    return items
