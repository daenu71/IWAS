# Story 1 — Settings: iRacing Telemetry Ordner konfigurieren

**Title:**
Settings: konfigurierbaren iRacing-Telemetry-Ordner für `.ibt`-Suche hinzufügen

**Changed files:**

* `src/ui/app.py`
* `src/core/config/*` oder die aktuelle Settings-/Config-Datei
* ggf. `src/ui/settings_*` falls bereits ausgelagert
* keine Coaching-Analyse-Logik in diesem Schritt

**Short summary:**
Erweitere die Settings um einen konfigurierbaren Standardpfad für den iRacing-Telemetry-Ordner. Default soll auf den üblichen Windows-iRacing-Telemetry-Pfad zeigen. Der Wert wird persistent gespeichert und zentral verfügbar gemacht. In diesem Schritt nur Settings + Laden/Speichern + Validierung, noch keine `.ibt`-Verarbeitung.

## Ziel

Ein zentraler Setting-Wert für den iRacing-Telemetry-Ordner, den spätere Coaching-/Track-Geometrie-Tasks nutzen können.

## Anforderungen

* Im Top-Menü `Settings` ein neues Feld für den iRacing-Telemetry-Ordner hinzufügen
* Default-Wert:

  * `%USERPROFILE%\Documents\iRacing\telemetry`
* Wert persistent speichern
* Beim Laden der App wiederherstellen
* Pfad zentral über Config zugänglich machen
* Kleine Validierung:

  * leer erlaubt
  * nicht existierender Pfad darf gespeichert werden, aber mit klarer UI-Hinweis-/Logmeldung
* Keine automatische Scan-Logik in diesem Schritt

## Akzeptanzkriterien

* Settings-Seite zeigt den neuen Pfad
* Wert bleibt nach Neustart erhalten
* Andere Module können den Wert zentral lesen
* Keine Änderung an Recorder, Analyzer oder Trackmap

## Erwartete Ausgabe im .dm Format
**Titel:** Sinvoller Titel der Änderung
**Zusammenfassung:** Kurze Beschreibung was gefunden und/oder geändert wurde
**Geänderte Dateien:**

---

# Story 2 — Track-Geometrie aus `.ibt` erzeugen und speichern

**Title:**
Offline-Import: Track-Geometrie aus iRacing `.ibt` für bekannte Strecke erzeugen und unter `track_geometries` speichern

**Changed files:**

* `src/core/coaching/track_geometry_*` oder bestehendes Track-Geometrie-Modul
* neuer Import-/Scanner-Service unter `src/core/coaching/`
* ggf. `src/core/irsdk/` nur falls dort bereits `.ibt`-Leselogik sinnvoll liegt
* keine UI-Fallback-Logik in diesem Schritt

**Short summary:**
Füge einen Offline-Pfad hinzu, der aus einer vorhandenen iRacing-`.ibt`-Datei die Streckengeometrie einer Strecke extrahiert und als persistente Track-Geometrie unter `C:\iWAS\data\coaching\track_geometries` speichert. Fokus dieses Schritts: Datenquelle `.ibt`, Streckenidentifikation, Extraktion, Persistenz. Noch keine automatische Einbindung in die UI.

## Ziel

Eine gespeicherte, wiederverwendbare Streckengeometrie pro Strecke aus `.ibt` aufbauen.

## Anforderungen

* Einen Service implementieren, der den konfigurierten iRacing-Telemetry-Ordner scannt
* `.ibt`-Dateien einer gewünschten Strecke finden
* Streckenidentifikation robust über Trackname / TrackKey aus den `.ibt`-Metadaten
* Aus `.ibt` die Track-Geometrie ableiten
* Ergebnis speichern unter:

  * `C:\iWAS\data\coaching\track_geometries\<track_key>\track_road_geometry.json`
* Falls für dieselbe Strecke bereits eine echte gespeicherte Geometrie vorhanden ist:

  * nicht blind überschreiben
  * nur sauber loggen und überspringen
* Falls nur Fallback-Geometrie vorhanden ist:

  * Status/Quelle im gespeicherten Datensatz klar kennzeichnen
* Noch keine automatische Trigger-Logik nach Session-Ende

## Wichtig

* Keine Änderung an Live-Recorder-Logik
* Keine UI-Änderung
* Keine Vermischung von “echte aus `.ibt` erzeugte Geometrie” und “dead_reckoning fallback”
* Quelle im JSON klar markieren, z. B.:

  * `source_type = ibt`
  * `source_type = fallback`

## Akzeptanzkriterien

* Für eine vorhandene `.ibt` einer Strecke kann eine Track-Geometrie erzeugt und gespeichert werden
* Track-Key/Ordnerstruktur ist stabil
* Quelle ist im gespeicherten Datensatz klar erkennbar
* Bestehende echte Geometrie wird nicht still überschrieben

## Erwartete Ausgabe im .dm Format
**Titel:** Sinvoller Titel der Änderung
**Zusammenfassung:** Kurze Beschreibung was gefunden und/oder geändert wurde
**Geänderte Dateien:**

---

# Story 3 — Nach jeder Session prüfen, ob neue `.ibt` für die Strecke vorliegt

**Title:**
Coaching-Analyse: nach Session-Aufzeichnung neue `.ibt` für die Strecke prüfen und fehlende Track-Geometrie automatisch erzeugen

**Changed files:**

* `src/core/coaching/lap_analyzer.py`
* `src/core/coaching/indexer.py`
* neuer `.ibt`-Scan-/Import-Service aus Story 2
* ggf. kleiner Hook im Recorder-/Run-Abschluss-Modul

**Short summary:**
Erweitere den Coaching-Analysepfad so, dass nach Abschluss einer Session-Aufzeichnung geprüft wird, ob für die betroffene Strecke bereits eine echte gespeicherte Track-Geometrie existiert. Falls nicht, soll im konfigurierten iRacing-Telemetry-Ordner nach passender `.ibt` gesucht und daraus die Track-Geometrie erzeugt werden. Noch keine größere UI-Umstellung; nur Trigger + Persistenzpfad.

## Ziel

Nach jeder Session automatisch versuchen, für die aktuelle Strecke eine echte gespeicherte Track-Geometrie aufzubauen.

## Anforderungen

* Trigger nach abgeschlossener Session/Run-Analyse
* Prüfen:

  1. gibt es für die Strecke bereits eine echte Track-Geometrie?
  2. wenn nein: konfigurierten `.ibt`-Ordner scannen
  3. wenn passende `.ibt` vorhanden: Geometrie erzeugen und speichern
  4. wenn nicht vorhanden: nur loggen, kein Fehler
* Zusätzlich:

  * Wenn eine Strecke aktuell nur Fallback-Geometrie besitzt, bei jeder Analyse erneut zuerst prüfen, ob inzwischen eine passende `.ibt` verfügbar ist
* Scan klein halten:

  * keine unnötigen Vollscans pro Frame
  * nur pro Analyse-/Session-Ende
* Logs klar trennen:

  * `track_geometry source=ibt created`
  * `track_geometry source=fallback retained`
  * `track_geometry ibt_not_found`

## Nicht Teil dieses Tasks

* keine Änderung, wie die UI die Trackmap rendert
* keine neue Button-/Dialog-Logik
* kein Hintergrund-Daemon mit ständigem Polling

## Akzeptanzkriterien

* Nach Session-Ende wird genau einmal geprüft
* Fehlende echte Geometrie kann aus `.ibt` nachgezogen werden
* Vorhandene Fallback-Geometrie bleibt bestehen, bis echte Geometrie erzeugt wurde
* Kein Crash, wenn der `.ibt`-Ordner leer oder ungültig ist

## Erwartete Ausgabe im .dm Format
**Titel:** Sinvoller Titel der Änderung
**Zusammenfassung:** Kurze Beschreibung was gefunden und/oder geändert wurde
**Geänderte Dateien:**

---

# Story 4 — Trackmap-Priorität: gespeicherte Streckengeometrie vor Fallback verwenden

**Title:**
Trackmap-Quelle priorisieren: gespeicherte Streckengeometrie aus `track_geometries` vor Dead-Reckoning-Fallback verwenden

**Changed files:**

* `src/ui/viewmodels/lap_view_model.py`
* `src/ui/coaching_detail.py`
* ggf. `src/ui/track_geometry.py`
* ggf. kleines zentrales Track-Geometry-Resolver-Modul

**Short summary:**
Stelle die Trackmap-Quellwahl sauber um: Wenn für eine Strecke eine gespeicherte Track-Geometrie unter `track_geometries` vorhanden ist, soll diese für die Darstellung priorisiert verwendet werden. Wenn keine echte gespeicherte Geometrie vorhanden ist, bleibt die bisherige Fallback-Methode aktiv. Hat eine Strecke nur Fallback-Geometrie, soll bei Analyse weiterhin zuerst geprüft werden, ob inzwischen `.ibt`-basierte echte Geometrie verfügbar ist.

## Ziel

Die sichtbare Streckenkarte soll bevorzugt aus der stabilen gespeicherten Streckengeometrie kommen und nur im Notfall aus der bisherigen Fallback-Methode.

## Anforderungen

* Trackmap-Resolver mit klarer Priorität:

  1. echte gespeicherte Track-Geometrie aus `track_geometries`
  2. gespeicherte Fallback-Geometrie, falls so ein Zustand existiert
  3. bisherige Dead-Reckoning-Fallback-Methode
* Quelle im Debug/Log klar ausweisen:

  * `trackmap_source=track_geometries_ibt`
  * `trackmap_source=track_geometries_fallback`
  * `trackmap_source=dead_reckoning_live_fallback`
* Wenn keine echte Geometrie vorhanden ist:

  * keine leere UI
  * bisherige Methode weiter verwenden
* Wenn Strecke gar nicht in `track_geometries` existiert:

  * bei vorheriger Analyse bereits `.ibt`-Check versucht
  * UI nutzt bis dahin Fallback
* Kein stilles Mischen verschiedener Geometriequellen in derselben Karte

## Akzeptanzkriterien

* Vorhandene echte Track-Geometrie wird sichtbar priorisiert
* Fehlende Strecke fällt sauber auf bisherige Methode zurück
* Logs zeigen eindeutig, welche Quelle verwendet wurde
* Keine Regression in bestehender Coaching-Ansicht

## Erwartete Ausgabe im .dm Format
**Titel:** Sinvoller Titel der Änderung
**Zusammenfassung:** Kurze Beschreibung was gefunden und/oder geändert wurde
**Geänderte Dateien:**

---

## Empfohlene Reihenfolge

Genau so umsetzen:

1. **Story 1**
2. **Story 2**
3. **Story 3**
4. **Story 4**

Das ist die kleinste saubere Kette.

---

## Warum nicht alles in einem Prompt

Weil sonst diese Risiken entstehen:

* Settings fertig, aber noch kein Nutzer der Settings
* `.ibt`-Import halb eingebaut, aber UI nutzt ihn nicht sauber
* Fallback-/Echt-Geometrie vermischt
* Session-Ende-Trigger feuert zu früh oder mehrfach
* Trackmap-Quelle später nicht mehr klar debugbar

---

Wenn du willst, formuliere ich dir jetzt direkt **Story 1 als final kopierbaren Codex-Prompt**.
