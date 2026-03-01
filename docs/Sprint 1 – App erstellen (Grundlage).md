# Projekt: iRacing Video Compare – App

## Ziel des Projekts

Eine **Desktop-App**, mit der zwei Rennvideos **visuell sauber** verglichen werden können.
Fokus ist **Bedienbarkeit**, **Vorschau** und **reproduzierbare Einstellungen**.

Kein Relativ-Ghost.
Kein Auto-Tracking.
Kein Raten.

---

# Sprint 1 – App erstellen (Grundlage)

## Sprint-Ziel

Eine **benutzbare App**, mit der ein Vergleichsvideo **manuell korrekt vorbereitet und erzeugt** werden kann.

Am Ende von Sprint 1:

> Ein Nutzer kann zwei Videos laden, Einstellungen vornehmen, Vorschau prüfen, Profile speichern/laden und ein finales Video erzeugen.

---

## Story 1 – App-Grundgerüst

**Ziel**
Die App startet und zeigt eine klare Oberfläche.

**Inhalt**

* Desktop-App (Fenster)
* Titel: *iRacing Video Compare*
* Feste Hauptbereiche:

  * Dateibereich
  * Vorschau
  * Einstellungen
  * Aktionen

**Fertig wenn**

* App startet ohne Fehler
* Leeres UI ist sichtbar

---

## Story 2 – Dateien laden (Drag & Drop)

**Ziel**
Videos und CSVs können einfach geladen werden.

**Regeln**

* Drag & Drop
* Alternativ: Datei-Dialog
* Erwartet:

  * 2 Videos
  * optional CSVs (für spätere Sprints)

**Logik**

* Dateiname enthält Zeit (`MM.SS.mmm`)
* Kürzere Zeit = fast
* Längere Zeit = slow

**Fertig wenn**

* Beide Videos sichtbar sind
* Fast/Slow korrekt erkannt wird

---

## Story 3 – Videos vorbereiten (Schnitt)

### 3.1 Startpunkt setzen

**Ziel**
Startpunkt **framegenau** setzen.

**Funktionen**

* Einzelbild-Vorschau
* Play / Pause
* Frame vor / zurück
* „Start hier setzen“

**Fertig wenn**

* Startframe gespeichert ist
* Vorschau zeigt korrekten Start

---

### 3.2 Endpunkt setzen

**Ziel**
Ende automatisch aus Rundenzeit bestimmen.

**Regeln**

* Zeit aus Dateinamen
* Länge = Rundenzeit
* Ende = Start + Dauer

**Fertig wenn**

* Endframe automatisch gesetzt ist
* Nutzer kann Ende optional korrigieren

---

## Story 4 – Output-Format wählen

**Ziel**
Ausgabeformat kontrollieren.

**Optionen**

* Seitenverhältnis:

  * 32:9
  * 21:9
  * 16:9
* Auflösung:

  * Presets (z. B. 5120×1440, 3840×1080, 2560×1440)
* FPS:

  * vom Video übernehmen (Anzeige)

**Fertig wenn**

* Auswahl möglich
* Vorschau passt sich an

---

## Story 5 – HUD-Bereich definieren

**Ziel**
Platz für HUDs reservieren.

**Funktionen**

* HUD-Bereich in der Mitte
* Breite einstellbar (px)
* Live-Vorschau

**Fertig wenn**

* HUD-Bereich sichtbar ist
* Videos links/rechts korrekt angepasst werden

---

## Story 6 – HUD-Platzhalter & Layout

**Ziel**
HUDs visuell platzieren, ohne echte Daten.

**Funktionen**

* Auswahl von HUD-Typen (Platzhalter):

  * Speed
  * Throttle / Brake
  * Steering
  * Delta
  * Gear & RPM
  * Line Delta
  * Under-/Oversteer
* Platzhalter als Boxen
* HUD-Boxen:

  * verschiebbar
  * Breite/Höhe änderbar
  * bleiben im HUD-Bereich

**Fertig wenn**

* HUD-Boxen frei positionierbar sind
* Positionen gespeichert werden

---

## Story 7 – Video-Vorschau mit PNG-Frames

**Ziel**
Exakte visuelle Ausrichtung.

**Funktionen**

* Erster Frame als PNG

  * links (slow)
  * rechts (fast)
* PNG:

  * zoomen
  * verschieben
  * „PNG auf Rahmenhöhe“ (fit)
* App merkt sich:

  * Zoom
  * Position
  * Fit-Status

**Fertig wenn**

* PNG-Vorschau korrekt angezeigt wird
* Einstellungen reproduzierbar sind

---

## Story 8 – Profil speichern & laden

**Ziel**
Alle Einstellungen sichern und wiederherstellen.

**Gespeichert wird**

* Video-Dateien
* Start-/Endframe
* Output-Format
* HUD-Breite
* HUD-Positionen / Größen
* PNG-Zoom & Offset (slow + fast) inkl. Fit

**Format**

* Profildatei (z. B. `.json`)

**Fertig wenn**

* Profil speichern
* Profil laden
* Vorschau stellt alles wieder her

---

## Story 9 – Video generieren

**Ziel**
Finales Vergleichsvideo erzeugen.

**Regeln**

* Nutzt aktuelle Einstellungen aus der App
* Fortschrittsanzeige
* Abbruch möglich
* Start erfolgt über `main.py` (Orchestrierung)

**Fertig wenn**

* Video wird erzeugt
* Fortschritt sichtbar
* Abbruch stoppt den Render-Prozess zuverlässig
* Übergabedaten sind im Log nachvollziehbar

---

# Übergabe an main.py (Sprint 1)

## Prinzip

Beim Klick auf **„Video erzeugen“**:

1. Die App schreibt eine Datei:
   `config/ui_last_run.json`

2. Danach startet die App:
   `python src/main.py --ui-json config/ui_last_run.json`

3. `main.py` schreibt die wichtigsten Werte ins Log (Sprint 1 = Kontrolle).

**Wichtig:**
Viele Werte werden in Sprint 1 nur übergeben und geloggt.
Sie werden erst in Sprint 2/3 wirklich fürs Rendering genutzt.

---

## Datei: config/ui_last_run.json

### Video-Dateien

* `slow_video`
  Pfad zum Slow-Video (längere Zeit im Dateinamen).

* `fast_video`
  Pfad zum Fast-Video (kürzere Zeit im Dateinamen).

* `slow_csv`, `fast_csv`
  Optional. Pfade zu CSVs, wenn vorhanden und zuordenbar.
  In Sprint 1 noch nicht genutzt.

* `out_video`
  Zielpfad der Ausgabedatei (MP4), die erzeugt wird.

---

### Output-Format

* `output.aspect`
  Seitenverhältnis, z. B. `32:9`.

* `output.preset`
  Ausgewähltes Preset, z. B. `5120x1440`.
  Das ist die Zielauflösung.

* `output.quality`
  Qualitätsmodus, z. B. `Original`.
  Bedeutung wird in Sprint 2 konkret umgesetzt.

* `output.hud_width_px`
  HUD-Fensterbreite in Pixeln (mittlerer Streifen).

---

### HUD (Platzhalter-Layout)

* `hud_enabled`
  Liste/Map, welche HUDs aktiv sind (an/aus).

* `hud_boxes`
  Position und Größe je HUD-Box, als Werte:

  * `x`, `y` = Position im HUD-Bereich
  * `w`, `h` = Größe der Box

Das ist die Grundlage, um in Sprint 2/3 echte HUDs dort zu rendern.

---

### Video-Placement (PNG-Ausrichtung als Render-Grundlage)

* `png_view_state.L`
  Aktueller Zustand für die linke Seite (Slow):

  * `zoom`
  * `off_x`
  * `off_y`
  * `fit_to_height`

* `png_view_state.R`
  Aktueller Zustand für die rechte Seite (Fast), gleiche Felder.

Diese Werte beschreiben, wie das Video im Rahmen liegen soll.

---

### Zusatzdaten (für Profile / spätere Nutzung)

* `png_view_key`
  Schlüssel, der beschreibt, zu welchem Output-Setup die PNG-Ausrichtung gehört.

* `hud_layout_data`, `png_view_data`
  Gesamtspeicher für mehrere Layouts/Keys.
  In Sprint 1 wichtig für Profile.
  In Sprint 2/3 optional für erweitertes Verhalten.

---

# Sprint-1-Abschlusskriterien

Sprint 1 ist abgeschlossen, wenn:

* App stabil läuft
* Zwei Videos sauber verglichen werden können (PNG-Ausrichtung)
* Einstellungen sind reproduzierbar (Profil)
* Video-Render startet über `main.py`
* Abbruch funktioniert
* Übergabedaten sind im Log nachvollziehbar

---

## Wichtig (bewusst nicht in Sprint 1)

* Keine CSV-Synchronisation
* Keine echten HUD-Daten
* Keine automatische Ausrichtung
* Keine Performance-Optimierung
* Kein Auto-Tracking

---

