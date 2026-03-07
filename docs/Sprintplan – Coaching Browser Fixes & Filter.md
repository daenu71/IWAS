## Sprintplan – Coaching Browser Fixes & Filter

---

### Story B1 – Zeitformat auf 3 Nachkommastellen (Millisekunden)

**Scope:** Nur im Coaching Browser (Liste). Alle Zeitanzeigen im Tree (Lap-Zeiten, Run-Bestzeit, Event-Bestzeit) auf Format `1:24.453` umstellen.

**Betroffene Dateien:**
- `src/ui/coaching_browser.py` (Zeitformatierungs-Funktion + alle Aufrufstellen)

**Akzeptanz:** Zeiten im Browser zeigen Millisekunden. Rest der App unverändert.

---

### Story B2 – Lap 0 Farbe: Schnellste Lap nur unter gültigen Laps

**Scope:** Im Browser wird aktuell Lap 0 (Pit-out-Lap) lila eingefärbt, weil sie die kürzeste Zeit hat. Fix: Nur Laps mit Status `OK` dürfen als schnellste Lap gewertet werden.

**Betroffene Dateien:**
- `src/ui/coaching_browser.py` (Fastest-Lap-Berechnung im Indexer oder Tree-Builder)

**Akzeptanz:** Lap 0 (offtrack/pit) wird nicht mehr als schnellste Lap hervorgehoben. Korrekte Lap wird lila.

---

### Story B3 – Lap 0 Status: Pit (out) korrekt anzeigen

**Scope:** Erste Lap eines Runs (Lap 0 / Out-Lap) wird als `OK` angezeigt obwohl sie eine Pit-out-Lap ist. Status muss aus den Lap-Metadaten kommen.

**Betroffene Dateien:**
- `src/ui/coaching_browser.py` (Status-Feld Auflösung)
- ggf. `src/core/coaching/lap_segmenter.py` (prüfen ob `pit_out` im Lap-Meta gesetzt wird)

**Akzeptanz:** Erste Lap zeigt `Pit (out)`. Bestzeit-Berechnung (B2) ignoriert diese Lap korrekt.

---

### Story B4 – Type-Spalte: Eventtyp + Session-Detail

**Scope:** 
- Event-Ebene: Statt `event` → Umgebungstyp anzeigen (`Offline`, `Official` etc.)
- Run-Ebene: Statt `run` → Session-Typ anzeigen (`Practice`, `Qualify`, `Race`)

**Betroffene Dateien:**
- `src/ui/coaching_browser.py` (Tree-Node Aufbau, Type-Spalte)
- `src/core/coaching/indexer.py` (prüfen ob session_type + environment im Index verfügbar)

**Akzeptanz:** Type-Spalte zeigt auf Event-Ebene den Umgebungstyp, auf Run-Ebene den Session-Typ.

---

### Story B5 – Run-Sortierung: Ältester Run zuerst innerhalb Session

**Scope:** Innerhalb einer Session (Event-Node) werden Runs aktuell neuester zuerst angezeigt. Umkehren auf ältester zuerst. Track/Car/Event-Ebene bleibt neuester zuerst.

**Betroffene Dateien:**
- `src/ui/coaching_browser.py` (Sort-Logik beim Tree-Build)

**Akzeptanz:** Run 0001 erscheint oben, Run 0004 unten. Events bleiben neueste zuerst.

---

### Story B6 – Filter-Engine: Index-Aufbau + Datenmodell

**Scope:** Grundlage für den Filter. Beim Laden des Coaching-Index werden alle filterbaren Felder extrahiert und in einer flachen Struktur gecacht:
- Strecke, Auto, Fahrer, EventTyp, SessionTyp
- Umgebungsvariablen aus `session_conditions` (Temp, Luftdruck, Humidity, Wind, Skies, Weather)
- Lap-Status-Werte
- Datum/Zeitraum

Index wird beim Browser-Load und Refresh aufgebaut, nicht pro Filter-Aufruf.

**Betroffene Dateien:**
- `src/ui/coaching_browser.py` (Filter-Cache beim Index-Load)
- ggf. `src/core/coaching/indexer.py` (prüfen ob session_conditions im Index vorhanden)

**Akzeptanz:** `_filter_index` mit allen Werten verfügbar. Kein UI noch, nur Daten.

---

### Story B7 – Filter-UI: Modales Popup mit allen Filtergruppen

**Scope:** Filter-Button (Trichter-Icon) in der Browser-Toolbar öffnet modales Tkinter-Popup mit:
- Checkboxen für Strecke, Auto, Fahrer, EventTyp, SessionTyp, Lap-Status
- Schieberegler/Eingabefelder für Umgebungsvariablen (Bereich von/bis)
- Pro Filtergruppe ein AND/OR-Toggle
- Reset-Button, Apply-Button
- Aktive Filter werden als kleines Badge am Filter-Button angezeigt (Anzahl aktiver Filter)

**Betroffene Dateien:**
- `src/ui/coaching_browser.py` (FilterDialog-Klasse, Button, Badge)

**Akzeptanz:** Popup öffnet, alle Felder vorhanden, Apply schließt und übergibt Filter-State.

---

### Story B8 – Filter-Logik: Tree-Filterung mit AND/OR pro Gruppe

**Scope:** Filter-State aus B7 wird auf den geladenen Index angewendet. Logik:
- Innerhalb einer Gruppe: OR (z.B. Strecke A OR Strecke B)
- Zwischen Gruppen: AND (Strecke-Filter AND Auto-Filter)
- Leere Gruppe = kein Filter (zeigt alles)
- Gefilterte Nodes: Event-Node wird ausgeblendet wenn kein Run darin passt. Run-Node wird ausgeblendet wenn kein Lap darin dem Lap-Status-Filter entspricht.

**Betroffene Dateien:**
- `src/ui/coaching_browser.py` (Tree-Build mit Filter-State)

**Akzeptanz:** Filter schränkt sichtbare Nodes korrekt ein. Performance: Filter auf 1000 Laps unter 100ms.

---

### Reihenfolge / Abhängigkeiten

```
B1 → unabhängig
B2 → unabhängig  
B3 → nach B2 (Bestzeit-Logik nutzt korrekten Status)
B4 → unabhängig
B5 → unabhängig
B6 → nach B1–B5 (Index braucht korrekten Status aus B3)
B7 → nach B6
B8 → nach B7
```

Empfohlene Reihenfolge: **B1, B5, B4, B2, B3, B6, B7, B8**