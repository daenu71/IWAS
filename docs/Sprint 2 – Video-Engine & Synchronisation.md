# Sprint 2 – Video-Engine & Synchronisation **(aktualisiert)**

## Sprint-Ziel

Das Vergleichsvideo wird **technisch korrekt**, **synchron** und **zuverlässig** erzeugt.

Sprint 2 nutzt **ausschließlich Daten**, die in Sprint 1 über die UI an `main.py` übergeben werden.

Am Ende von Sprint 2:

> Zwei Videos sind **streckengenau synchronisiert** und werden **in der richtigen Geometrie** gerendert.

---

## Grundprinzip (wichtig)

* Die UI entscheidet **alles**.
* `main.py` **wertet nur aus**.
* Sprint 2 **interpretiert keine HUDs**.
* Der HUD-Bereich ist **nur eine leere Fläche mit fixer Breite**.

---

## Verwendete Übergabedaten aus Sprint 1

Sprint 2 nutzt **nur diese Werte** aus `ui_last_run.json`:

### Video-Dateien

* `slow_video`
  Linkes Video

* `fast_video`
  Rechtes Video

* `slow_csv`, `fast_csv`
  CSV-Dateien für die Synchronisation

* `out_video`
  Zielpfad der Ausgabedatei

---

### Output-Geometrie

* `output.aspect`
  Seitenverhältnis des Zielvideos
  z. B. `32:9`

* `output.preset`
  Zielauflösung
  z. B. `5120x1440`

* `output.quality`
  Qualitätsmodus (technisch, nicht visuell)

---

### HUD-Bereich (Sprint-2-relevant)

* `output.hud_width_px`

**Bedeutung in Sprint 2**

Der mittlere Bereich wird:

* **leer gelassen**
* **nicht gerendert**
* **nicht interpretiert**

Er trennt nur **Slow links** und **Fast rechts**.

---

### Video-Ausrichtung (entscheidend)

* `png_view_state.L`
* `png_view_state.R`

Jeweils:

* `zoom`
* `off_x`
* `off_y`
* `fit_to_height`

**Bedeutung in Sprint 2**

Diese Werte definieren, wie jedes Video:

* skaliert wird
* verschoben wird
* in den linken / rechten Zielbereich passt

Sprint 2 übernimmt diese Werte **1:1** für Cropping und Scaling im Render.

---

## Story 1 – Render-Grundlage festlegen

**Ziel**
Stabile technische Basis.

**Entscheidungen**

* ffmpeg als Render-Engine
* Trennung von:

  * Decode
  * Filter
  * Encode

**Fertig wenn**

* ffmpeg-Pipeline definiert ist
* Keine UI-Abhängigkeit besteht

---

## Story 2 – Output-Geometrie aufbauen

**Ziel**
Zielvideo korrekt aufteilen.

**Ablauf**

1. Zielauflösung aus `output.preset`
2. Zielbreite = Gesamtbreite
3. Zielhöhe = Zielhöhe
4. Mittlerer Bereich = `output.hud_width_px`
5. Restliche Breite:

   * links = Slow-Video
   * rechts = Fast-Video

**Fertig wenn**

* Ziel-Canvas korrekt berechnet ist
* HUD-Bereich als leerer Spacer existiert

---

## Story 3 – Video-Placement anwenden

**Ziel**
Videos liegen exakt so wie in der Vorschau.

**Ablauf je Seite (Slow / Fast)**

* Eingabevideo laden
* `zoom` anwenden
* `off_x`, `off_y` anwenden
* optional `fit_to_height`
* Ergebnis in linken / rechten Zielbereich rendern

**Quelle der Wahrheit**

* ausschließlich `png_view_state.L / R`

**Fertig wenn**

* Renderbild visuell zur Vorschau passt

---

## Story 4 – CSV-basierte Synchronisation

**Ziel**
Beide Videos zeigen denselben Streckenpunkt.

**Regeln**

* Synchronisation **nur über CSV**
* Schlüssel: `LapDistPct`
* Kein Zeit-Sync als Primärregel
* Kein Frame-Raten-Abgleich

**Ablauf**

1. `slow_csv` laden
2. `fast_csv` laden
3. gemeinsamen Bereich bestimmen
4. Mapping Slow → Fast erzeugen

**Fertig wenn**

* Kurven optisch zusammenpassen
* Kein Drift sichtbar ist

---

# Story 5 – Performance-Strategie (verbindlich)

## Ziel

Render ist **schnell**, **stabil** und läuft **auf allen Systemen**.

## Reihenfolge im Encode (verbindlich)

1. **NVENC** (NVIDIA)
2. **QSV** (Intel)
3. **AMF** (AMD)
4. **CPU-Fallback** (`libx264`)

**Wichtig**

* Breite Videos sind erlaubt.
* Keine GPU vorhanden? Dann läuft es trotzdem (CPU).
* Kein Abbruch nur wegen fehlender GPU.

## Logging (verbindlich)

* ffmpeg-Ausgabe ist **live** sichtbar.
* Wenn ein GPU-Encoder fehlschlägt:

  * **Tail** der letzten Zeilen wird angezeigt
  * dann wird der nächste Encoder probiert

## Windows-Stabilität (verbindlich)

* Sehr lange `-filter_complex` Strings können auf Windows fehlschlagen.
* Deshalb wird der Filter bei Bedarf **als Datei** an ffmpeg übergeben (`-filter_complex_script`).

**Fertig wenn**

* Render läuft auf Systemen **mit und ohne GPU**
* GPU-Fail führt **nicht** zum Abbruch
* CPU-Fallback funktioniert immer

---

# Story 6 – Stream-Sync ohne PNG (Sprint-2-Kern)

## Ziel

Synchronisiertes Vergleichsvideo wird in **einem** ffmpeg-Run erzeugt.

## Verbindliche Regeln

* **kein PNG**
* **kein `fast_sync.mp4`**
* **ein ffmpeg-Run**
* Output wird auf den **gemeinsamen Bereich** gekürzt:

  * kein Freeze
  * kein Schwarzbild
  * nur sauberes Kürzen

## Wie Sync passiert

* Slow ist die Zeitbasis.
* Fast wird im Filtergraph in **Segmente** aufgeteilt.
* Jedes Segment wird zeitlich leicht gestreckt/gestaucht, damit es zu Slow passt.
* Segment-Schritt wird über `k_frames` gesteuert.

## Genauigkeit vs Geschwindigkeit

* kleineres `k_frames`:

  * besser in Kurven
  * mehr Segmente
  * etwas langsamer

* größeres `k_frames`:

  * schneller
  * kann in Kurven etwas ungenauer werden

## Optional: dynamische Segmente (falls aktiv)

Segmente können dynamisch gewählt werden:

* kurze Segmente, wenn “kritisch”

  * starke Änderung im Mapping (Kurven / starke Abweichung)
  * großer Speed-Unterschied (falls Speed in CSV vorhanden)

* lange Segmente, wenn “ruhig”

**Wichtig:** Das ist optional. Standard kann fix bleiben.

## Debug-Datei (für Sprint 3)

`output/debug/sync_cache.json` wird geschrieben und bleibt erhalten.

Enthält z. B.:

* `fps`
* `frame_count`
* `cut_i0`, `cut_i1`
* `slow_frame_to_lapdist`
* `slow_frame_to_fast_frame`
* `slow_frame_to_fast_time_s`

Damit kann Sprint 3 später HUDs bauen, ohne alles neu zu berechnen.

## Fertig wenn (Akzeptanzkriterien)

* kein PNG-Ordner wird genutzt oder benötigt
* kein `fast_sync.mp4` wird erstellt
* ein ffmpeg-Run erzeugt direkt das Endvideo
* Sync ist optisch driftfrei
* Output wird auf gemeinsamen Bereich gekürzt
* GPU-Reihenfolge bleibt: NVENC → QSV → AMF → CPU
* Logs sind live sichtbar + Tail bei Fail

---

## Sprint-2-Abschlusskriterien

Sprint 2 ist abgeschlossen, wenn:

* `png_view_state` korrekt im Render genutzt wird
* HUD-Bereich nur als Leerraum existiert
* CSV-Sync funktioniert (LapDistPct)
* Stream-Sync läuft ohne PNG und ohne fast_sync.mp4
* Performance stabil ist (GPU-Fallback + CPU-Fallback)
* Render reproduzierbar ist

---

## Bewusst **nicht** in Sprint 2

* ❌ HUD-Boxen
* ❌ HUD-Daten-Visualisierung
* ❌ HUD-Design
* ❌ UI-Logik
* ❌ Auto-Ausrichtung

Diese kommen **ausschließlich in Sprint 3**.

---
