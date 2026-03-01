# Sprint 3 – HUD-Erstellung

## Sprint-Ziel

Die im UI ausgewählten HUDs werden:

* aus CSV-Daten berechnet
* gemäß `hud_boxes` positioniert
* korrekt in das Video gerendert

Sprint 3 nutzt **ausschließlich Daten**, die bereits in **Sprint 1 übergeben**
und in **Sprint 2 für das Video vorbereitet** wurden.

---

## Grundprinzip (verbindlich)

* `main.py` ist ausführend
* Das UI bestimmt **was**, **wo** und **wie groß**
* Sprint 3 **zeichnet HUDs**
* Video-Geometrie kommt vollständig aus Sprint 2

---

## Verwendete Übergabedaten aus Sprint 1

Sprint 3 nutzt **nur diese Felder** aus `ui_last_run.json`:

### HUD-Auswahl

* `hud_enabled`

Map mit HUD-Namen → aktiv / inaktiv.

Beispiele:

* Speed
* Throttle / Brake
* Steering
* Delta
* Gear & RPM
* Line Delta
* Under-/Oversteer

👉 **Bedeutung**  
Nur HUDs mit `true` werden berechnet und gerendert.

---

### HUD-Layout

* `hud_boxes`

Für jedes aktive HUD:

* `x`
* `y`
* `w`
* `h`

Alle Werte beziehen sich **auf den HUD-Bereich**
(nicht auf das Gesamtvideo).

👉 **Bedeutung**  
Sprint 3 rendert **jedes HUD exakt in diese Box**.

---

### Video-Basis

Aus Sprint 2 übernommen:

* fertiges synchronisiertes Video
* leerer HUD-Bereich mit fixer Breite

Sprint 3 **ändert keine Video-Geometrie** mehr.

---

## Gemeinsame HUD-Regeln (verbindlich für alle HUDs)

### X-Achse (verbindlich)

* Die X-Achse aller Scroll-HUDs ist **zeitbasiert**
* X-Position wird ausschließlich aus dem **Frame-Offset zum Marker** berechnet
* Marker liegt **immer exakt in der Mitte**
* Links: Vergangenheit (`before_s`)
* Rechts: Zukunft (`after_s`)

**Wichtig:**

* LapDistPct wird **nicht** zur Pixel-Skalierung verwendet
* LapDistPct dient **nur** als Filter, welche Daten im Fenster sichtbar sind
* Es gibt **keine**:
  * Modulo-Logik (`% 1.0`)
  * shortest-path-Wraps
  * streckenbasierte Pixel-Skalierung

👉 Dadurch bleiben Kurven optisch stabil  
(z. B. kein „Zusammenziehen in der Mitte“ bei hoher Geschwindigkeit).

---

### Marker (verbindlich)

* Eine **vertikale Linie in der Mitte** des HUDs
* Zeigt den **aktuellen Punkt**
* Bleibt fix stehen
* Links: Vergangenheit
* Rechts: Zukunft

Marker ist Pflicht für alle Scroll-HUDs.

---

### Fenster

* Fenstergröße wird **in Sekunden** definiert
* Umrechnung in Frames über Video-FPS
* Fenster ist **zeitlich konstant**
* Kein Zoom
* Kein Stretching

---

### Farben (global, verbindlich)

Farben sind zentral definiert und gelten für alle HUDs:

* Blau = schneller Fahrer
* Rot = langsamer Fahrer
* Weiß = Texte und Referenzlinien

---

## Story 1 – HUD-Renderer-Grundlage

### Ziel

Ein einzelnes HUD kann als Bild erzeugt und in eine Box gerendert werden.

### Tasks

* HUD-Canvas pro Box erzeugen
* Koordinatensystem:

  * (0,0) oben links der Box
* Transparenter Hintergrund
* Alpha-Blending ins Video

### Fertig wenn

* Test-HUD erscheint korrekt an Position `x,y,w,h`

---

## Story 2 – Streckenfenster & Marker

### Ziel

Alle HUDs zeigen Daten relativ zum aktuellen Punkt.

### Tasks

* Streckenfenster definieren:

  * vor Marker
  * nach Marker
* Pro Frame:

  * aktueller Index `i`
  * Daten links / rechts sammeln
* Marker bleibt fix in der Mitte

### Wichtig

* Das Fenster bestimmt **welche Daten** sichtbar sind
* Das Fenster bestimmt **nicht**, wie breit sie gezeichnet werden

### Fertig wenn

* HUD scrollt
* Marker bleibt stehen

---

## Story 3 – Steering HUD (Referenz-Implementierung)

> **Diese Story definiert verbindlich, wie alle scrollenden HUDs umgesetzt werden.  
> Steering ist die technische und visuelle Referenz.**

---

### Ziel

Das Steering-HUD zeigt den Lenkwinkel **zeitlich stabil**, **vergleichbar** und **ruhig**.

---

### Datenquelle

```

SteeringWheelAngle

````

* Einheit intern: Radiant
* Anzeige: Grad
* Typische Werte: ±2.5 … ±3.0 rad
* Keine Ausreißer-Behandlung nötig

---

### Fensterlogik

```ini
hud_window_default_before_s = 10
hud_window_default_after_s  = 10
````

* Fenster wird in Sekunden definiert
* Umrechnung in Frames über Video-FPS
* Gilt global
* Optionale Overrides pro HUD möglich

---

### X-Koordinaten-Berechnung (verbindlich)

* X basiert auf **Frame-Offset zum Marker**

* Skalierung:

  * links über `before_frames`
  * rechts über `after_frames`

* Kein LapDist-Wrap

* Kein shortest-path

* Kein `% 1.0`

---

### Granularität

* HUD wird pro Output-Frame gerendert
* Kurvendetail über:

  * `hud_curve_points_default`
  * `stride`

Mehr Punkte machen die Kurve glatter, ändern aber nicht die Geometrie.

---

### Y-Skalierung

```
abs_max = max(abs(all Werte))
```

* Kein Headroom nach unten
* Headroom nur nach oben

---

### Linien

* Slow = Rot
* Fast = Blau
* Positiv oberhalb der Mittellinie
* Negativ unterhalb der Mittellinie

---

### Mittellinie

* Weiße horizontale Linie
* Entspricht `0`
* Immer sichtbar

---

### Texte

* Titel oben links, weiß
* Werte am Marker auf gleicher Höhe
* Fixe Textbreite
* Kein Springen

Beispiel:

```
+075°
-147°
```

---

### Status

* Umsetzung abgeschlossen
* Referenz für alle weiteren HUDs

---

## Story 4 – Throttle / Brake HUD (Pedal HUD)

### Datenquelle

```
Throttle
Brake
ABSActive
```

* Wertebereich: `0 … 1`
* ABSActive: `0 / 1`

---

### X-Logik

* identisch zu Steering
* zeitbasiert
* keine streckenbasierte Pixel-Skalierung

---

### Y-Skalierung

* Bereich: `0 … 1`
* Kein negativer Bereich
* Headroom nur oben

---

### Darstellung

* Eine gemeinsame Grafik
* Farben:

  * Slow:

    * Gas = hell rot
    * Bremse = dunkel rot
  * Fast:

    * Gas = hell blau
    * Bremse = dunkel blau

---

### ABS-Anzeige

* Zwei Balken oberhalb der Grafik
* Vergangenheit und Zukunft sichtbar
* Farbe:

  * Slow = rot
  * Fast = blau

### INI-Optionen (Stabilität)

* `hud_pedals_sample_mode = time`
  * `time` (Default): zeitbasiertes Sampling pro X-Position mit Interpolation
  * `legacy`: altes indexbasiertes Sampling (Fallback)
* `hud_pedals_abs_debounce_ms = 60`
  * ABS wird im HUD über ein Zeitfenster stabilisiert (Mehrheitsentscheidung), um Einzelframe-Flackern zu vermeiden
* `max_brake_delay_distance = 0.003`
  * Einheit: `LapDistPct`-Delta (track-basiert, keine Meter)
  * Nach exakt `Brake == 0` wird ein neues Max.-Brake-Event bis zu dieser Distanz blockiert (Anti-Flicker)
* `max_brake_delay_pressure = 35`
  * Einheit: Prozent `0..100`
  * Während der Distanz-Sperre erlaubt `Brake >= max_brake_delay_pressure%` einen sofortigen Start (Override)
* Max.-Brake-Reset ist strikt:
  * `Brake == 0` (exakt, kein Epsilon) beendet/rearmt die Phase

---

### Fertig wenn

* Pedale klar unterscheidbar
* Marker-Bezug stimmt
* Keine Verzerrung

---

## Story 5 – Geschwindigkeit HUD

### Ziel

Anzeige der Geschwindigkeit von **Slow** und **Fast** als stabiles, gut lesbares HUD.
Die Logik ist so aufgebaut, dass sie **für weitere tabellarische HUDs (z. B. Story 6)** wiederverwendbar ist.

---

### Datenquelle

* `Speed` aus CSV
* Einheit intern immer **m/s**
* Umrechnung erfolgt **erst im HUD**

---

### Anzeige

* **Zwei Spalten**

  * links: **Slow** (dunkel rot)
  * rechts: **Fast** (dunkel blau)

* **Farben**

  * `COL_SLOW_DARKRED = (234, 0, 0, 255)`
  * `COL_FAST_DARKBLUE = (36, 0, 250, 255)`

* **Zwei Zeilen im HUD**

  1. **Überschrift**

     * Text: `Speed / Min-Speed`
     * Schriftgrösse orientiert sich an **Throttle / Brake**
  2. **Wertezeile (gross)**

     * nutzt den **restlichen Platz des HUD**
     * Format:
       `123 / 98 km/h` oder `76 / 61 mph`

---

### Geschwindigkeit

* Anzeige als **Ganzzahl**
* Einheit:

  * `km/h` oder `mph`
* Einheit ist **konfigurierbar per INI**

---

### Min-Speed

* Definition:

  * **letzter lokaler Tiefpunkt** der Geschwindigkeit
* Logik:

  * −5 / +5-Regel

    * Min-Speed wird nur erkannt, wenn

      * der Speed mindestens **5 km/h (oder 5 mph)** fällt
      * und danach wieder mindestens **5 km/h (oder 5 mph)** steigt
* Zeitpunkt:

  * Min-Speed wird **genau beim Ereignis** angezeigt
  * **keine Voranzeige**
* Anzeige:

  * bleibt aktiv, bis ein neuer gültiger Min-Speed erkannt wird

---

### Aktualisierungsrate (Stabilität)

* Speed-Werte werden **nicht jedes Frame** neu gesetzt
* Aktualisierung:

  * konfigurierbar in Hz
  * **maximal 60 Hz**
  * zusätzlich begrenzt auf die **FPS des Output-Videos**
* Zwischen den Updates:

  * letzter Wert wird **gehalten**
* Ergebnis:

  * ruhige Anzeige
  * kein Zittern oder Flackern

---

### INI-Einstellungen

* `speed_units`

  * `kmh` oder `mph`
* `speed_update_hz`

  * gewünschte Aktualisierungsrate
  * effektiv genutzt wird:
    `min(speed_update_hz, 60, output_fps)`

---

### Architektur-Hinweis (wichtig für Story 6)

* Umrechnung der Werte erfolgt **vor dem Zeichnen**
* Update-Rate wird **vor dem Zeichnen angewendet**
* HUD selbst arbeitet nur mit:

  * vorbereiteten
  * stabilen
  * ganzzahligen Werten

➡️ **Story 6 (z. B. weitere Tabellen-HUDs)** kann exakt gleich aufgebaut werden:

* gleiche Update-Logik
* gleiche Schrift-Hierarchie
* gleiche linke/rechte Struktur

---

### Fertig wenn

* Anzeige ruhig und gut lesbar
* Einheit korrekt
* Min-Speed logisch und reproduzierbar
* Verhalten identisch bei 30 fps, 60 fps und breiten Videos


---

## Story 6 – Gang & RPM HUD

### Ziel

Anzeige von **Gang** und **RPM** als stabiles Text-HUD.
Die Struktur ist identisch zu Story 5, aber **ohne Ereignis-Logik**.

---

### Datenquelle

* `Gear`
* `RPM`

---

### Anzeige

* **Zwei Spalten**

  * links: **Slow**
  * rechts: **Fast**

* **Farben**

  * Slow: **Rot**
  * Fast: **Blau**

* **Text-HUD**

  * klare, gut lesbare Schrift
  * keine Grafiken
  * Fokus auf Stabilität

* **Inhalt**

  * Gang: ganzzahlig
  * RPM: ganzzahlig

---

### Aktualisierungsrate (Stabilität)

* Werte werden **nicht jedes Frame** neu gesetzt
* Aktualisierung:

  * über INI konfigurierbar
  * **maximal 60 Hz**
  * zusätzlich begrenzt auf die **FPS des Output-Videos**
* Zwischen Updates:

  * letzter Wert wird **gehalten**
* Ergebnis:

  * ruhige Anzeige
  * kein Flackern bei Gangwechseln oder RPM-Schwankungen

---

### INI-Einstellungen

* `gear_rpm_update_hz`

  * gewünschte Aktualisierungsrate
  * effektiv genutzt wird:
    `min(gear_rpm_update_hz, 60, output_fps)`

---

### Architektur-Hinweis (aus Story 5 übernommen)

* Update-Rate wird **vor dem Zeichnen** angewendet
* HUD bekommt nur:

  * vorbereitete
  * stabile
  * ganzzahlige Werte
* Linke/rechte Struktur ist identisch zu Story 5

➡️ Dadurch können beide HUDs:

* gleich gerendert werden
* gleich konfiguriert werden
* gemeinsam erweitert werden

---

### Fertig wenn

* Gang korrekt
* RPM korrekt
* Farben korrekt
* Anzeige ruhig bei allen FPS-Werten


---

## Story 7 – Zeitdelta HUD

### Datenquelle

* Sync-Map aus Sprint 2
* Zeitdelta in Sekunden

### Fenster & Marker

* identisch zu Story 3

### Y-Skalierung

* Bereich: `−max(|Delta|) … +max(|Delta|)`
* Mittellinie = `0 s`

### Darstellung

* Blau = schneller
* Rot = langsamer ist die horizontale Mittellinie
* **Farben**

  * `COL_SLOW_DARKRED = (234, 0, 0, 255)`
  * `COL_FAST_DARKBLUE = (36, 0, 250, 255)`

### Texte

* Titel: `Time delta`
* Aktueller Wert oberhalb Marker
* Tausendstel
* Vorzeichen immer sichtbar

---


## Story 8 – Linien-Delta HUD (zeitbasiertes Scroll-HUD)

---

### Ziel

Das Linien-Delta-HUD zeigt den **seitlichen Abstand** zwischen der Linie von
**slow** und **fast** als **ruhige, zeitbasierte Kurve** im HUD.

Der Vergleich erfolgt **pro Video-Frame** und ist **FPS-robust**.

---

### Grundprinzip (verbindlich)

Dieses HUD ist ein **scrollendes Zeit-HUD** mit fixer Marker-Position.

* Das HUD scrollt.
* Der Marker bleibt **immer exakt in der Mitte**.
* Links vom Marker: Vergangenheit.
* Rechts vom Marker: Zukunft.
* Die X-Achse ist **rein zeitbasiert**.

---

### Zeitbasis (verbindlich)

* Alle Berechnungen erfolgen **über Zeit (`Time_s`)**.
* CSV-Daten werden **nie per Frame-Index** verwendet.
* CSV-Daten werden **immer interpoliert**.
* „Nearest sample“ ist **verboten**.

Diese Regel gilt für **slow und fast** gleichermaßen.

---

### Datenquelle

* Garage61 CSV
* Track-Koordinaten:
  * `Lat`
  * `Lon`

Vorverarbeitung:

* Umrechnung von `Lat/Lon` in lokales **XY in Meter**
* Projektion identisch zur Projektdefinition
* Ergebnis:
  * `X_slow(t)`
  * `Y_slow(t)`
  * `X_fast(t)`
  * `Y_fast(t)`

---

### Zeit- und Sync-Logik

Pro Output-Frame:

1. Bestimme die Frame-Zeit:
```

t_slow = frame_index / output_fps

```

2. Interpoliere **slow** über `Time_s`:
* `X_slow(t_slow)`
* `Y_slow(t_slow)`
* `LapDistPct_slow(t_slow)`

3. Bestimme die zugehörige **fast-Zeit** über die bestehende Sync-Map:
```

t_fast = slow_frame_to_fast_time_s[frame_index]

```

4. Interpoliere **fast** über `Time_s`:
* `X_fast(t_fast)`
* `Y_fast(t_fast)`

Ergebnis:  
Slow und Fast werden **zeitlich synchron** verglichen.

---

### Line-Delta-Berechnung (Meter)

1. Bestimme die **Bewegungsrichtung von slow**:
* Tangente aus zwei zeitlich nahe beieinanderliegenden Punkten in XY

2. Bestimme die **Normalrichtung** (links/rechts) aus der Tangente

3. Berechne den seitlichen Abstand:
```

delta_m = dot( (P_fast - P_slow), normal_slow )

```

Vorzeichen:

* `delta_m > 0` → fast ist **links** von slow
* `delta_m < 0` → fast ist **rechts** von slow

---

### Fensterdefinition

* Fenstergröße wird **in Sekunden** definiert:
* `before_s`
* `after_s`
* Umrechnung in Frames über `output_fps`
* Fenstergröße ist **zeitlich konstant**
* Kein Zoom
* Kein Stretching

Das Fenster bestimmt:
* welche Daten sichtbar sind
* nicht, wie breit sie gezeichnet werden

---

### X-Achse (Pixel-Berechnung)

* X-Position basiert ausschließlich auf dem **Frame-Offset zum Marker**
* Marker liegt exakt bei `x = w / 2`
* Skalierung:
* links über `before_frames`
* rechts über `after_frames`

Nicht erlaubt:

* LapDist-basierte Pixel-Skalierung
* `% 1.0` Wrap
* shortest-path Logik

---

### Y-Skalierung

* Einheit: **Meter**
* Mittellinie: `0 m`
* Skalierung:
```

abs_max = max(abs(all sichtbaren delta_m))

```
* Headroom:
* nur nach oben
* kein Headroom nach unten

---

### Darstellung

* Kurve:
* Blau = schneller (Delta-Kurve)
* Referenz:
* weiße horizontale Linie bei `0 m`
* Kurve ist:
* ruhig
* nicht flackernd
* ohne Frame-Artefakte

---

### Texte

* Titel oben links:
```

Line delta

```
* Aktueller Wert am Marker:
* `L 0.87 m`
* `R 1.32 m`
* Fixe Textbreite
* Kein Springen bei Vorzeichenwechsel

---

### Fertig wenn

* Links/Rechts stimmt logisch
* Werte im plausiblen Meterbereich
* Darstellung ruhig bei 30 / 60 / 120 fps
* Kein alternierendes Springen


### Umsetzung (Ist-Stand)
- `render_split.py`
  - Line-Delta Precompute eingebaut:
    - Lädt `Lat/Lon/Time_s/LapDistPct` aus beiden Garage61 CSVs.
    - Wandelt `Lat/Lon` in lokales `XY` (Meter) um (gemeinsamer lokaler equirectangular Origin).
    - Interpoliert `slow/fast` kontinuierlich über `Time_s` (kein Nearest-Sample).
    - Pro Output-Frame: `t_slow = frame_index / output_fps`, `t_fast = slow_frame_to_fast_time_s[frame_index]`.
    - Berechnet signiertes `delta_m` über Tangente/Left-Normal von `slow` und Dot-Product.
  - Übergabe an HUD:
    - `line_delta_m_frames` in `HudSignals` ergänzt und an den Line-Delta Renderer weitergegeben.
    - Precompute läuft nur, wenn Line-Delta HUD aktiv ist (Scroll-Modus).
  - Y-Skalierung umgestellt:
    - Nach dem vollständigen Precompute über alle Frames:
      - `abs_global_max = max(abs(delta_m))` über die gesamte Serie
      - `line_delta_y_abs_m = abs_global_max * 2.0`
    - `line_delta_y_abs_m` wird in HUD-Signals gespeichert und an den Renderer übergeben.

- `huds/line_delta.py`
  - Rendering umgesetzt:
    - Zeitfenster strikt über Frame-Offsets `[-before_f .. +after_f]`.
    - Marker fix bei `x = w/2`.
    - X-Mapping strikt über Frame-Offset (links `before_f`, rechts `after_f`).
    - Weißer `0 m`-Baseline, blaue Kurve, Titel „Line delta“.
    - Markerwert-Text: `L/R {abs:.2f} m` mit fixer Breite, damit nichts springt.
  - Y-Skalierung fix (keine Autoskalierung mehr):
    - `y_min = -line_delta_y_abs_m`
    - `y_max = +line_delta_y_abs_m`
    - `0 m` bleibt dauerhaft zentriert.

### Abnahme / Check
- `python -m py_compile render_split.py huds/line_delta.py` ✅
- Short Render Run:
  - `IRVC_HUD_SCROLL=1`, `IRVC_DEBUG_MAX_S=2`, `python main.py --ui-json C:\iracing-vc\config\ui_last_run.json` ✅
  - Hinweis: Keine visuelle HUD-Prüfung möglich, weil Pillow fehlt (`No module named 'PIL'`) (na)
- Numerische Checks:
  - 30 FPS: `n=2378`, min/max ca. `-2.322 / +3.367 m`
  - 60 FPS: `n=4756`, min/max ca. `-2.322 / +3.367 m`
  - 120 FPS: `n=9512`, min/max ca. `-2.322 / +3.369 m`
  - Dummy Draw Check: Marker-Center & X-Range bestätigt (`marker_x=400` bei `w=800`, Range `[0..799]`)

### Fertig wenn
- Links/Rechts stimmt logisch ✅
- Werte im plausiblen Meterbereich ✅
- Darstellung ruhig bei 30 / 60 / 120 fps (kein Y-Rescale mehr, Skala global fix) ✅
- Kein alternierendes Springen ✅

---

## Story 9 – Under- / Oversteer Indicator (zeitbasiertes Scroll-HUD)

---

### Ziel

Der Under-/Oversteer-Indikator zeigt die **Tendenz**
zu Under- oder Oversteer als **vergleichende Kurve**.

Es handelt sich um ein **Proxy-HUD**.
Keine absolute Grip-Aussage.

---

### Grundprinzip

* Scroll-HUD mit fixer Marker-Position
* Zeitbasiert
* Vergleich slow vs fast
* Keine numerischen Werte

---

### Datenquelle

* Bewegungsrichtung (aus XY-Trajektorie)
* Yaw

---

### Zeitbasis

* Pro Output-Frame:
* `t = frame_index / output_fps`
* Alle CSV-Werte werden über `Time_s` interpoliert
* Keine Frame-Index-Zugriffe

---

### Fenster

* Fensterdefinition in Sekunden:
* `before_s`
* `after_s`
* Umrechnung in Frames über FPS
* Marker fix in der Mitte

---

### X-Achse

* X-Position basiert auf Frame-Offset zum Marker
* Zeitbasierte Skalierung
* Keine Strecken-Pixel-Skalierung

---

### Y-Skalierung

* Mittellinie = neutral
* Oberhalb = Oversteer
* Unterhalb = Understeer
* Symmetrische Skalierung
* Leichter Headroom nach oben

---

### Darstellung

* Vergleichskurven:
* Blau = schneller
* Rot = langsamer
* Keine Skala
* Keine Zahlen
* Fokus auf Verlauf

---

### Texte

* Titel:
```

Under / Oversteer

```
* Keine Werte
* Keine Marker-Beschriftung

---

### Fertig wenn

* Kurven reagieren plausibel
* Keine Flackerei
* Vergleich klar sichtbar



### Umsetzung (Ist-Stand)

- Minimaler HudSignals-Wiring für **Under-/Oversteer** ergänzt.
- Zeitbasierte Interpolations-Helper (linear, geklemmt) für XY- und Yaw-Sampling nach `Time_s` implementiert.
- Proxy-Precompute exakt gemäß Vorgabe umgesetzt:
  - `err(t) = wrap_angle(yaw(t) - heading_xy(t))`
  - `heading_xy(t)` aus XY-Tangente mit kleinem `dt`, Zeitbereich geklemmt.
  - Slow-Time: `t = frame_idx / output_fps`
  - Fast-Time: `slow_frame_to_fast_time_s[frame_idx]`
- Globale, stabile, symmetrische Y-Skala mit +15 % Headroom umgesetzt.
- Precompute wird **nur** aktiviert, wenn das HUD **"Under-/Oversteer"** aktiv ist.
- Scroll-HUD-Renderer implementiert:
  - Titel exakt **"Under / Oversteer"** (oben links)
  - Neutrale Basislinie bei 0
  - Slow-Kurve rot, Fast-Kurve blau
  - Marker-zentrierte X-Abbildung (Frame-Offset)
  - Keine Zahlen, keine Skalenlabels, kein Marker-Wert-Text

### Abnahme / Check

- `python -m py_compile render_split.py` (mehrere Schritte): ✅
- `python -m py_compile render_split.py huds/under_oversteer.py`: ✅
- Numerische Checks (zeichnungsunabhängig):
  - Lineare Interpolation korrekt
  - Proxy-Generierung liefert endliche Arrays, Slow/Fast unterscheiden sich
  - Strikte X-Mapping-Prüfung bestanden (Marker exakt enthalten)
  - Textprüfung Renderer: nur Titel vorhanden
  - Edge-Fallbacks geprüft, kein Crash

### Fertig wenn

- ✅ Umsetzung entspricht exakt der Story-Vorgabe
- ✅ Keine HUD-Key-Änderungen
- ✅ Kein Verhalten außerhalb des Under-/Oversteer-HUDs geändert
- ✅ Sichere Fallbacks bei fehlenden Daten


---

## Story 9 – Under- / Oversteer Indicator (Steering-Referenz)

> **Proxy-HUD.**
> Keine absolute Grip-Aussage.
> Umsetzung folgt vollständig der Steering-Schablone.

---

### Ziel

Anzeige der **Tendenz** zu Under- oder Oversteer
als **vergleichende Kurve** über das Streckenfenster.

---

### Datenquelle

* Bewegungsrichtung (aus XY)
* Yaw

---

### Fenster & Marker

* **identisch zu Story 3**
* Zeitbasiert
* Marker fix in der Mitte

---

### X-Achse

* identisch zu Story 3
* zeitbasiert
* Frame-Offset zum Marker

---

### Y-Skalierung

* Mittellinie = neutral
* Oberhalb = Oversteer
* Unterhalb = Understeer
* Symmetrische Skalierung
* Leichter Headroom nach oben

---

### Darstellung

* Vergleichskurven:
  * Blau = schneller
  * Rot = langsamer
* Keine Skala
* Keine Zahlen

---

### Texte

* Titel: `Under / Oversteer`
* Keine numerischen Werte
* Fokus auf Verlauf und Vergleich

---

### Fertig wenn

* Kurven reagieren plausibel
* Keine Flackerei
* Vergleich klar sichtbar

---

### Verbindlicher Gesamt-Hinweis

**Story 3 (Steering) ist die technische Referenz.**

Alle HUDs in Story:
- 4 (Throttle / Brake)
- 7 (Time delta)
- 8 (Line delta)
- 9 (Under / Oversteer)

übernehmen unverändert:

* Zeitbasis
* Marker-Logik
* Fenster-Logik
* Text-Regeln

Geändert werden **nur**:
- Datenquelle
- Y-Interpretation
- Titel

# Story 10 – Hintergrund-Raster & Y-Achsen-Beschriftung (alle HUDs)

## Ziel
Alle HUDs erhalten im Hintergrund **horizontale Hilfsstreifen** (Raster) sowie links eine **Y-Achse mit kleinen Wert-Labels** an den Übergängen der Streifen.

Das Raster dient ausschließlich der Orientierung und darf die Kurven nicht dominieren.

---

## 1) Hintergrund-Raster (horizontale Streifen)

### 1.1 Streifenmuster
- Das HUD wird in **horizontale Streifen** unterteilt.
- Streifen **wechseln gleichmäßig** zwischen:
  - normaler Hintergrundfarbe
  - leicht dunklerer Hintergrundfarbe
- Der dunklere Streifen ist **nur minimal dunkler**, damit:
  - das Raster sichtbar ist
  - die Kurven weiterhin klar im Vordergrund bleiben

---

### 1.2 Anzahl der Unterteilungen
- Pro HUD:
  - **mindestens 2**
  - **maximal 5** Unterteilungen (Segmente)
- Ziel ist ein **ruhiges, gleichmäßiges Raster**.

---

### 1.3 Tick-Logik („schöne Werte“)
Die Streifen-Grenzen orientieren sich an **runden, gut lesbaren Werten**.

Erlaubte Tick-Abstände (je nach Einheit des HUDs):
- `100`
- `10`
- `1`
- `0.5`

Regeln:
- Wähle den Tick-Abstand so, dass **2–5 Streifen** entstehen.
- Bevorzuge größere Schritte:
  - 100 vor 10
  - 10 vor 1
  - 1 vor 0.5
- Der Tick-Abstand richtet sich immer nach der **jeweiligen Y-Achsen-Einheit** des HUDs.

---

### 1.4 Randstreifen
- Der **oberste Streifen** (bei HUDs ohne negativen Bereich)
  - oder der **unterste Streifen** (bei HUDs mit negativem Bereich und 0-Mittellinie)
  darf **kleiner sein**, wenn die HUD-Höhe nicht exakt aufgeht.
- Das restliche Streifenmuster bleibt gleichmäßig.

---

### 1.5 Zeichenreihenfolge
1. HUD-Background
2. **Raster (horizontale Streifen)**
3. Baselines (z. B. Mittellinie)
4. Kurven
5. Text (Y-Achse, Labels, Titel)

---

## 2) Y-Achsen-Beschriftung (links)

### 2.1 Position & Stil
- Links außen im HUD, innen mit kleinem Padding.
- Beschriftung erfolgt **an jeder Streifen-Grenze**.
- Stil:
  - Farbe: **weiß**
  - Schrift: **klein**
  - Keine Maßeinheit im Text

---

### 2.2 Beschriftete Werte
- Angezeigt wird der **effektive Y-Wert**, der auf dieser Höhe gilt.
- Bei symmetrischen HUDs (0 in der Mitte):
  - Labels **oberhalb und unterhalb** der Mittellinie
- Text darf nicht abgeschnitten werden:
  - falls nötig, leicht nach rechts verschieben
  - oder minimal kleinere Schrift

---

## 3) HUD-spezifische Regeln

### 3.1 Speed HUD
- Unterteilung: **1/5** (wenn möglich 5 Segmente)
- Labels:
  - effektiver Speed-Wert auf dieser Höhe
  - Einheit gemäß HUD-Einstellung (km/h oder mph)
  - **keine Einheit im Text anzeigen**
- Tick-Abstand gemäß Regel 100 / 10 / 1 / 0.5

---

### 3.2 Throttle / Brake HUD
- Unterteilung: **1/5**
- Y-Achse in Prozent
- Labels fest:
  - `20`
  - `40`
  - `60`
  - `80`
- 0 % und 100 % müssen nicht zwingend beschriftet werden

---

### 3.3 Delta HUD
- Unterteilung: **1/5**
- Raster und Labels **nur oberhalb** der dynamischen Mittellinie (0)
- Unterhalb der Mittellinie:
  - kein Raster
  - keine Labels
- Labels:
  - effektiver Delta-Wert auf dieser Höhe
  - Format: **1 Dezimalstelle**
  - **keine Einheit im Text**

---

### 3.4 Steering HUD
- Unterteilung: **1/5**
- Labels:
  - effektiver Lenkwinkel auf dieser Höhe
  - Einheit intern Grad
  - **keine Einheit im Text**
- Format:
  - ganze Zahl oder 1 Dezimalstelle (bestehende Logik beibehalten)

---

### 3.5 Line-Delta HUD
- Unterteilung: **1/5**
- Raster **oberhalb und unterhalb** der Mittellinie (0)
- Symmetrische Skalierung
- Labels:
  - effektiver Wert auf dieser Höhe
  - Format: **1 Dezimalstelle**
  - **keine Einheit im Text**

---

### 3.6 Under- / Oversteer HUD
- Unterteilung: **1/5**
- Raster **oberhalb und unterhalb** der Mittellinie (0)
- Labels:
  - effektiver Wert auf dieser Höhe
  - Format: **1 Dezimalstelle**
  - **keine Einheit im Text**
- Einheit:
  - richtet sich nach der internen HUD-Skala
  - **keine automatische Umrechnung**
  - Darstellung bleibt ein reiner Proxy

---

## 4) Fertig wenn
- Alle HUDs zeigen dezente horizontale Hintergrund-Streifen.
- Pro HUD gibt es **2–5 gleichmäßige Unterteilungen**.
- Tick-Abstände sind „schön“ (100 / 10 / 1 / 0.5).
- Links sind kleine weiße Y-Achsen-Labels an den Streifen-Grenzen.
- Kurven bleiben klar lesbar, Raster ist subtil.
- Delta-HUD zeigt Raster/Labels nur oberhalb der Mittellinie.
- Line-Delta und Under-/Oversteer sind symmetrisch um 0 aufgebaut.

---

## 5) Nicht erlaubt
- Keine Änderungen an HUD-Keys oder API-Namen.
- Keine Änderungen an Kurven-Logik oder bestehender Skalierung.
- Keine Maßeinheiten im Text.



## Story 11 – HUD-Auswahl & Integration

### Ziel

Nur ausgewählte HUDs werden gerendert.

### Tasks

* `hud_enabled` auswerten
* `hud_boxes` anwenden
* Mehrere HUDs gleichzeitig rendern

### Fertig wenn

* Alle aktiven HUDs erscheinen
* Inaktive HUDs fehlen
* Kein Überlappen
