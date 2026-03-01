# HUD API Vertrag – Render-Split-konform (Ist-Stand)

> **Status:** verbindlich (Ist-Code)
> **Quelle:** `render_split.py` → `_render_hud_scroll_frames_png(...)`
> **Ziel:** Beim Modularisieren keine Interpretationsfehler / kein Umbenennen nötig.

---

## 1. Zweck

Dieses Dokument beschreibt den **aktuellen Ist-Vertrag**, den die HUD-Logik heute schon nutzt:

* **Parameter-/Feldnamen sind exakt wie im Code**
* **Bedeutung ist exakt wie im Code**
* HUDs dürfen **keine eigene Sync-Logik** bauen
* HUDs dürfen **nicht annehmen**, dass optionale Arrays immer vorhanden sind

---

## 2. Wo der Vertrag heute “lebt”

Der Vertrag ist heute die **Funktionssignatur** von:

* `_render_hud_scroll_frames_png(...)`

Alle HUD-Daten kommen dort als Parameter rein und werden pro Frame verarbeitet.

---

## 3. Zentrale Zeitbasis / Indizes

### 3.1 Zeitbasis

* **Slow ist die Zeitbasis**
* Slow-Zeit: `t_slow = i / fps`
* Fast-Zeit: `t_fast = slow_frame_to_fast_time_s[i]` (wenn vorhanden)

### 3.2 Frame-Bereich

* `cut_i0` = erster Slow-Frame (inkl.)
* `cut_i1` = letzter Slow-Frame (exkl.)
* Anzahl Frames: `frames = cut_i1 - cut_i0`

### 3.3 Sync-Index

Wenn Sync aktiv ist:

* `fi = slow_to_fast_frame[i]`

➡️ Ein HUD darf **nicht** selbst “passenden Fast-Frame suchen”.
Das Mapping ist **verbindlich** über `slow_to_fast_frame`.

---

## 4. Geometrie / Boxen (Layout)

### 4.1 OutputGeometry

* `geom` ist ein `OutputGeometry` Objekt.
* Wichtig für HUD-Rendering:

  * `geom.hud` (HUD-Spaltenbreite)
  * `geom.H` (Output-Höhe)

### 4.2 HUD-Aktivierung & Boxen

* `hud_enabled`: bestimmt, welche HUDs aktiv sind (Keys müssen exakt passen)
* `hud_boxes`: enthält Layout-Boxen für HUDs

Die Box-Auswahl passiert **zentral** (nicht im HUD-Modul).
Ein ausgelagertes HUD bekommt später nur noch “seine” Box.

---

## 5. Streckenachse (LapDistPct)

### 5.1 Pflicht für Scroll-HUDs

* `slow_frame_to_lapdist: list[float]`
* Zugriff pro Frame: `slow_frame_to_lapdist[i]`

Eigenschaften (Ist-Logik):

* bereits “unwrapped” (entrollt)
* monoton steigend
* kann > 1.0 sein

---

## 6. Fenster um den Marker (Scroll-HUDs)

Es gibt **zwei Ebenen**:

### 6.1 Default-Fenster (Parameter)

* `before_s: float`
* `after_s: float`

Das ist das Standard-Fenster (Sekunden links/rechts).

### 6.2 `hud_windows` (Story 4.2 Status)

* `hud_windows: Any | None`
* Mögliche Struktur (Kompatibilität), aber per-HUD-Overrides sind inaktiv:

  * `hud_windows[hud_key]["before_s"]`
  * `hud_windows[hud_key]["after_s"]`

Die effektiven Scroll-Fenster kommen global aus `before_s` / `after_s`.

➡️ Wichtig: **Nur der Orchestrator** entscheidet die finalen Werte.
Das HUD liest nur “seine” finalen Werte.

---

## 7. Signal-Arrays (Frame-basiert)

Alle Signale sind **Frame-Arrays**.
Sie sind in `_render_hud_scroll_frames_png(...)` als **optionale** Parameter definiert.

### 7.1 Speed (Story “Speed”)

Parameter (optional):

* `slow_speed_frames: list[float] | None`
* `fast_speed_frames: list[float] | None`
* `slow_min_speed_frames: list[float] | None`
* `fast_min_speed_frames: list[float] | None`

Einheiten / Update:

* `hud_speed_units: str` (z. B. `"kmh"` / `"mph"`)
* `hud_speed_update_hz: int`

Zugriff pro Frame:

* Slow: `slow_speed_frames[i]`
* Fast: `fast_speed_frames[fi]` (wenn Sync + Array vorhanden)
* Min-Speed analog

### 7.2 Gear & RPM (Story “Gear & RPM”)

Parameter (optional):

* `slow_gear_frames: list[int] | None`
* `fast_gear_frames: list[int] | None`
* `slow_rpm_frames: list[float] | None`
* `fast_rpm_frames: list[float] | None`

Update:

* `hud_gear_rpm_update_hz: int`

Zugriff pro Frame:

* Slow: `slow_gear_frames[i]`, `slow_rpm_frames[i]`
* Fast: `fast_gear_frames[fi]`, `fast_rpm_frames[fi]`

### 7.3 Steering (Story “Steering”)

Parameter (optional):

* `slow_steer_frames: list[float] | None`
* `fast_steer_frames: list[float] | None`

Kurven-Dichte (Glättung/Ausdünnung):

* `hud_curve_points_default: int`
* `hud_curve_points_overrides: Any | None`
  (Ist-Form: dict pro HUD-Key)

### 7.4 Throttle / Brake / ABS (Story “Throttle / Brake”)

Parameter (optional):

* `slow_throttle_frames: list[float] | None`
* `fast_throttle_frames: list[float] | None`
* `slow_brake_frames: list[float] | None`
* `fast_brake_frames: list[float] | None`
* `slow_abs_frames: list[int] | None`
* `fast_abs_frames: list[int] | None`

INI-Optionen (`config/defaults.ini`):

* `hud_pedals_sample_mode`
* `hud_pedals_abs_debounce_ms`
* `max_brake_delay_distance`
  * Einheit: LapDistPct-Delta (track-basiert)
* `max_brake_delay_pressure`
  * Einheit: Prozent `0..100`
* Max.-Brake Event-Reset/Rearm ist strikt nur bei exaktem `Brake == 0` (kein Epsilon)

Globale HUD-Text-Lesbarkeit (alle HUD-Labels/Werte):

* `hud_text_shadow_enable` (`0/1`)
* `hud_text_shadow_offset_px` (Pixel)
* `hud_text_shadow_alpha` (`0..255`)
* `hud_text_brighten_enable` (`0/1`)
* `hud_text_debug_force_visible` (`0/1`, Diagnosemodus)
* Standardwerte: `1`, `1`, `160`, `1`, `0`

Zugriff pro Frame:

* Slow: `slow_throttle_frames[i]`, `slow_brake_frames[i]`, `slow_abs_frames[i]`
* Fast: `fast_throttle_frames[fi]`, `fast_brake_frames[fi]`, `fast_abs_frames[fi]`

### 7.5 Delta (Story “Delta”)

Delta basiert **nur** auf:

* `fps`
* `slow_frame_to_fast_time_s`

Parameter:

* `slow_frame_to_fast_time_s: list[float] | None`

Ist-Definition (pro Slow-Frame):

* `delta_s = (i / fps) - slow_frame_to_fast_time_s[i]`

Kurven-Dichte wie bei Steering:

* `hud_curve_points_default`
* `hud_curve_points_overrides`

---

## 8. HUD Keys (Namen sind “API”)

Diese Strings sind verbindlich (wie in `hud_enabled` / `hud_boxes`):

* `"Speed"`
* `"Throttle / Brake"`
* `"Steering"`
* `"Delta"`
* `"Gear & RPM"`
* `"Line Delta"`
* `"Under-/Oversteer"`

➡️ Die Keys dürfen beim Umbau **nicht geändert** werden.

---

## 9. Status: Welche HUDs sind im Ist-Code schon “voll verkabelt”

**Im Parametervertrag von `_render_hud_scroll_frames_png(...)` sind heute echte Daten vorgesehen für:**

* Speed (inkl. Min-Speed, Units, Update-Hz)
* Gear & RPM (Update-Hz)
* Steering (Kurvenpunkte)
* Throttle / Brake / ABS
* Delta (Fast-Time, Kurvenpunkte)

**Für diese beiden Keys gibt es im Ist-Parametervertrag aktuell keine eigenen Signal-Arrays:**

* `"Line Delta"`
* `"Under-/Oversteer"`

➡️ Bedeutet praktisch: Die Keys existieren im UI/Enable-Set, aber der “Datenvertrag” dafür ist in `render_split.py` noch nicht als Parameter vollständig definiert. (Beim Modularisieren: entweder später ergänzen oder aktuell als “noch ohne Daten” behandeln.)

---

## 10. Robustheitsregeln (Ist-Philosophie)

Ein HUD muss robust bleiben, auch wenn Felder fehlen:

* Wenn ein Array `None` ist oder zu kurz: HUD zeichnet **nichts** oder zeigt **„na“**
* Wenn Sync fehlt (`slow_to_fast_frame` oder `slow_frame_to_fast_time_s` fehlt):

  * Orchestrator entscheidet den Fallback
  * HUD baut keinen eigenen Ersatz-Mapping

---

## 11. Debug / Logging

* `log_file: Path | None` ist der zentrale “Log-Sink”.
* HUD-Code darf nur über die vorhandenen Logging-Helfer loggen (keine eigenen Log-Dateien).

---

## 12. Mini-Checkliste für die Modularisierung

Wenn ein HUD als Modul ausgelagert wird, muss es **ohne Umbenennung** weiter funktionieren, indem es genau diese Werte bekommt:

* Basis: `fps`, `cut_i0`, `cut_i1`, `slow_frame_to_lapdist`, `geom`, `hud_enabled`, `hud_boxes`
* Sync: `slow_to_fast_frame`, `slow_frame_to_fast_time_s` (falls nötig)
* HUD-spezifische Arrays: wie oben in Abschnitt 7
* Fenster: `before_s`, `after_s` (global für Scroll-HUDs)
* Settings: `hud_speed_units`, `hud_speed_update_hz`, `hud_gear_rpm_update_hz`, `hud_curve_points_default`, `hud_curve_points_overrides`


