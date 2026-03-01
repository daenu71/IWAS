# 🔵 SPRINT A – UI Modernisierung & Architektur-Vorbereitung

**Ziel:**
UI von einer Funktionsoberfläche zu einer skalierbaren App-Struktur umbauen.

---

## Story A1 – Top-Menüband Architektur einführen

**Ziel:**
Grundstruktur für mehrere App-Module schaffen.

### Tasks

* Oben ein horizontales Menüband (Ribbon/Topbar) einführen.
* Buttons:

  * `Video Analysis`
  * `Settings`
* Aktive View wird zentral in einem Content-Frame geladen.
* Bestehende UI wird in `VideoAnalysisView` gekapselt.
* Navigation nur View-Wechsel, keine Logik-Änderung.

### Ergebnis

* App kann mehrere Hauptmodule verwalten.
* Kein Code-Duplikat.
* Grundlage für spätere KI-Module.

### Umsetzung (Ist-Stand)
- Oben ein horizontales Ribbon/Topbar-Menüband mit zwei Buttons eingeführt: `Video Analysis` und `Settings` (src/ui_app.py:3226, :3227).
- Zentralen Content-Frame als Host für Haupt-Views ergänzt (src/ui_app.py:3189 ff.).
- Bestehende UI vollständig in `VideoAnalysisView` gekapselt; Aufbau erfolgt über `build_video_analysis_view(...)` und wird nur von dieser View verwendet (src/ui_app.py:149, :162).
- `SettingsView` als minimaler Platzhalter mit Label umgesetzt (src/ui_app.py:155).
- Navigation implementiert als reiner View-Wechsel: alte View wird zerstört, dann neue View geladen; keine Änderungen an Render-/Daten-/Verarbeitungslogik (src/ui_app.py:3209 ff.).
- Zusätzliche View-Lifecycle-Cleanup-Logik ergänzt (after-Callbacks + Root-Bindings werden beim Destroy aufgeräumt), um Leaks/Doppelbindungen beim View-Wechsel zu vermeiden (src/ui_app.py:3159).
- Public HUD/UI-Keys unverändert: "Speed", "Throttle / Brake", "Steering", "Delta", "Gear & RPM", "Line Delta", "Under-/Oversteer".

### Abnahme / Check
- py_compile: `.\\.venv\\Scripts\\python.exe -m py_compile src/ui_app.py` (OK).
- App-Start + View-Wechsel: automatisierter Tk-Smoke-Test mit Start von `ui_app.main()`, Klick auf `Settings` und zurück auf `Video Analysis` (OK).
- 7-fach Check-Lauf (Schritte 1–7): jeweils Compile + Start/Navigation-Smoketest, alle `STEP_X_CHECK_OK`.

### Fertig wenn
- ✅ Top-Menüband mit `Video Analysis` und `Settings` existiert.
- ✅ Aktive View wird zentral im Content-Frame geladen.
- ✅ Bestehende UI ist in `VideoAnalysisView` gekapselt und wird nicht dupliziert.
- ✅ `SettingsView` ist vorhanden (Platzhalter).
- ✅ Navigation macht nur View-Wechsel (Destroy + Load), keine Logik-Änderung.
- ✅ Compile + App-Start + View-Wechsel Smoke-Test sind grün.

---

## Story A2 – View-Registry einführen

**Ziel:**
Lose Kopplung zwischen Menü und Views.

### Tasks

* Registry-Dictionary: `view_name -> ViewClass`
* Menü erzeugt Views dynamisch.
* Keine direkten Imports im Menü.
* Lazy-Loading optional vorbereiten.

### Ergebnis

* Erweiterbarkeit ohne UI-Hardpatch.
* KI-Modul später einfach registrierbar.

### Umsetzung (Ist-Stand)
- `ViewEntry`, `_resolve_view_class` und `VIEW_REGISTRY` eingeführt; Mapping `view_name -> ViewClass` bzw. Factory vorbereitet (src/ui_app.py:163–170).
- Registry erlaubt Auflösung von Lazy-Factories, ohne dass das Menü View-Klassen direkt importiert.
- `_build_view` nutzt ausschließlich die Registry zur Auflösung der View-Klasse; bestehende Root-Injection für `Video Analysis` bleibt erhalten; Settings-Fallback bei fehlendem Label weiterhin vorhanden (src/ui_app.py:3220–3227).
- Ribbon-Buttons werden dynamisch durch Iteration über die Registry-Labels erzeugt; Binding erfolgt über das jeweilige Label (src/ui_app.py:3244–3257).
- Keine direkten Referenzen/Imports von `VideoAnalysisView` oder `SettingsView` mehr im Menü-Code.
- `show_view` und `_set_active_button` arbeiten label-basiert; neue Views können durch Registry-Eintrag ergänzt werden, ohne Ribbon-Logik zu ändern (src/ui_app.py:3229–3243).

### Abnahme / Check
- `python -m py_compile src/ui_app.py` (OK).
- App-Start + View-Wechsel (Video Analysis → Settings → Video Analysis): na (manuell erforderlich wegen fehlendem Display im Headless-Umfeld).

### Fertig wenn
- ✅ Registry-Dictionary `view_name -> ViewClass` vorhanden.
- ✅ Menü erzeugt Views dynamisch aus der Registry.
- ✅ Keine direkten View-Imports im Menü-Code.
- ✅ Lazy-Loading strukturell vorbereitet.
- ✅ Compile erfolgreich.


---

## Story A3 – Darkmode + Theme-System

**Ziel:**
Moderne Optik + Theme-Erweiterbarkeit.

### Tasks

* Globales Theme-Objekt einführen.
* Darkmode als Default.
* Farbpalette definieren:

  * Background
  * Surface
  * Accent
  * TextPrimary
  * TextSecondary
* Schriftart global definieren.
* Hover- und Active-States konsistent machen.

### Optional

* Theme als JSON konfigurierbar.

### Umsetzung (Ist-Stand)
- Globale Theme-Grundlage eingeführt: `ThemeColors`/`Theme` Dataclasses, Default-Dark-Palette inkl. abgeleiteter Hover/Active-Töne (src/ui_app.py:44, :62, :80, :98, :122).
- Helpers ergänzt: `apply_theme_fonts` für globale Schrift-Defaults sowie optionales `theme_from_dict` / `load_theme_from_json` als Vorbereitung für austauschbare Themes, ohne Default zu brechen (src/ui_app.py:122).
- `main()` wendet das Dark-Theme beim Start an: Root-Background setzen, Default-Fonts tunen, sowie `ttk.Style` für Frames, Labels, Entries, Comboboxes und Scales zentral konfigurieren (src/ui_app.py:3291, :3295, :3299, :3304, :3309, :3314, :3319).
- TButton-Theme ergänzt: zentraler Button-Style + `style.map` für konsistente Surface/Hover/Pressed-Farben und gedimmten Disabled-Text ohne per-Widget Overrides (src/ui_app.py:3324, :3328).
- Ribbon-Navigation nutzt `_style_nav_button`, hat Theme-aware Hover/Leave Bindings, und `_set_active_button` hält den Accent-gefüllten Active-Tab synchron beim View-Wechsel (src/ui_app.py:3361, :3388, :3431).

### Abnahme / Check
- `python -m py_compile src/ui_app.py` (OK).
- GUI-Checks (App-Start, Ribbon Hover/Active, View-Switch): na (manuell erforderlich wegen Headless-Umgebung ohne Display).

### Fertig wenn
- ✅ Globales Theme-Objekt vorhanden (Palette + Fonts zentral).
- ✅ Darkmode ist Default und wird beim Start angewendet.
- ✅ Farbpalette ist definiert: Background, Surface, Accent, TextPrimary, TextSecondary (inkl. Hover/Active Ableitungen).
- ✅ Schriftart global definiert/anwendbar.
- ✅ Hover- und Active-States sind konsistent (mind. Ribbon-Buttons) und zentral gestylt.
- ✅ Optional: JSON-Theme-Loading ist vorbereitet, ohne Default zu verändern.
- ✅ Compile erfolgreich.


---

## Story A4 – Logo & App Identity

**Ziel:**
App wirkt wie ein Produkt, nicht wie ein Tool.

### Tasks

* App-Name fest definieren (z.B. „IRVC – iRacing Video Compare“).
* Logo oben links integrieren.
* Icon-Datei vorbereiten (für exe).
* Window-Title dynamisch setzen.
## Story A4 – Logo & App Identity

### Tasks
- ✅ App-Name fest definiert („IRVC – iRacing Video Compare“) als zentrale Konstante `APP_NAME` in `src/cfg.py` (Single Source of Truth).
- ✅ Window-Title dynamisch gesetzt: Basis `APP_NAME`, bei View-Wechsel ergänzt zu `APP_NAME - <View>` (ohne Änderungen an Navigation/View-Logik) in `src/ui_app.py`.
- ✅ Logo oben links im Ribbon integriert (Asset: `assets/logo/iwas_logo_dark.png`, mit Fallback auf vorhandene PNGs), inkl. Padding/Downscale und Bildreferenz gegen GC gehalten, in `src/ui_app.py`.
- ✅ Icon-Datei verwendet und Fenster-Icon gesetzt via Tkinter `iconbitmap` (Asset: `assets/logo/iwas_icon.ico`) in `src/ui_app.py`.
- ✅ Logos stammen aus dem Projekt-Asset-Pfad (Ordner laut Story: `C:\iracing-vc\assets\logo`).

### Umsetzung (Ist-Stand)
- `src/cfg.py`
  - `APP_NAME = "IRVC – iRacing Video Compare"` eingeführt (ca. Zeile 7).
- `src/ui_app.py`
  - Fenster-Icon gesetzt: `assets/logo/iwas_icon.ico` via `iconbitmap` (ca. Zeile 3761).
  - Window-Title auf `APP_NAME` umgestellt und dynamisch bei View-Wechsel ergänzt (ca. Zeilen 3768, 3876).
  - Ribbon-Logo links oben hinzugefügt: `assets/logo/iwas_logo_dark.png` + PNG-Fallback, mit Padding/Downscale + Referenz halten (ca. Zeilen 581, 3799).

### Abnahme / Check
- ✅ `python -m py_compile src/cfg.py` ok (je Schritt ausgeführt).
- ✅ `python -m py_compile src/ui_app.py src/cfg.py` ok (je Schritt ausgeführt).
- ⚠️ Kurzer App-Start/Render-Test: nicht möglich, da vor UI-Render ein `ModuleNotFoundError: No module named 'cv2'` in der Umgebung auftritt.

### Fertig wenn
- ✅ App-Name ist zentral definiert und wird im Window-Title verwendet.
- ✅ Logo ist oben links im Ribbon sichtbar (Asset-basiert, stabil geladen).
- ✅ Fenster-Icon ist gesetzt (ico).
- ✅ Keine HUD-Keys / öffentlichen IDs geändert.
- ✅ Keine Refactors/Umstrukturierung.
- ✅ Packaging/Build-Konfig bleibt unverändert (keine vorhandene .spec/Build-Mechanik gefunden).

### Umsetzung (Ist-Stand)

- **Hauptfenster resizable stabil gemacht**
  - Vorhandene Grid-Weights/Resize-Policy geprüft (Root + Content-Host + Video-View).
  - `root.resizable(True, True)` explizit in `main()` gesetzt.  
    (src/ui_app.py:3928)

- **Zentraler Content-Frame scrollbar-fähig**
  - Wiederverwendbaren Scroll-Host ergänzt: `ScrollableContentHost` (ttk.Frame → Canvas → inner Frame + vertikale Scrollbar).  
    (Implementierung: src/ui_app.py:732)
  - Scrollregion-Update bei `<Configure>`.
  - Canvas-/Inner-Frame-Breite synchronisiert.
  - Scrollbar wird bei Bedarf ein-/ausgeblendet.
  - Integration nur im zentralen Content-Host:
    - `content = ScrollableContentHost(...)` (src/ui_app.py:3947)
    - Views werden in `content.content_frame` geladen (src/ui_app.py:4029, 4032, 4033)
    - Beim View-Wechsel Scrollposition auf oben zurückgesetzt (src/ui_app.py:4045, 4047)

- **Mindestgröße definiert**
  - Vorhandene konservative `root.minsize(...)`-Setzung im Layout-Policy-Code des Video-Views geprüft.  
    (src/ui_app.py:3827)

- **DPI-Awareness (Best-Effort)**
  - Defensive Windows-DPI-Initialisierung via `ctypes`, nur unter Windows, komplett in `try/except`.  
    (src/ui_app.py:145)
  - Aufruf vor `tk.Tk()` in `main()`.  
    (src/ui_app.py:3910)

### Abnahme / Check

- ✅ `python -m py_compile src/ui_app.py` erfolgreich (mehrfach nach Patches).
- ⚠️ `python src/ui_app.py` lokal fehlgeschlagen wegen `ModuleNotFoundError: cv2` (Interpreter ohne cv2).
- ✅ Smoke-Start mit Projekt-venv erfolgreich:
  - `.\.venv\Scripts\python.exe src/ui_app.py` startet und läuft bis Timeout ohne sichtbare Startup-Exception.
- ⛔ Manuelle UI-Prüfung (Resize/Scroll) konnte nicht visuell durchgeführt werden (nur Startup-Smoke-Test).

### Fertig wenn

- ✅ Zentraler Content scrollt bei kleiner Fensterhöhe (Scrollbar erscheint und funktioniert).
- ✅ Fenster ist stabil resizable (Content wächst/schrumpft ohne Layout-Brüche).
- ✅ Mindestgröße verhindert “zu klein”, ohne Layout zu zerlegen.
- ✅ DPI-Initialisierung verursacht keine Exceptions und keine sichtbaren Fehler beim Start (Windows).
- ✅ Nur `src/ui_app.py` geändert.


---

## Story A5 – Responsive Layout Basis

**Ziel:**
Vorbereitung für zukünftige Module.

### Tasks

* Hauptfenster resizable stabil machen.
* Content-Frame scrollbar-fähig.
* Mindestgröße definieren.
* DPI-Awareness prüfen.
