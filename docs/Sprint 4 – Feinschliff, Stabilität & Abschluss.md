# Sprint 4 – Feinschliff, Stabilität & Abschluss

## Sprint-Ziel

Die App ist **stabil**, **verständlich**, **reproduzierbar** und **bereit für echte Nutzung**.
Keine neuen Kernfunktionen, sondern **Absicherung und Bedienkomfort**.

Am Ende von Sprint 4:

> Die App kann zuverlässig genutzt werden, ohne Spezialwissen.

---

## Story 1 – UI-Feinschliff & Bedienbarkeit

**Ziel**
Die App fühlt sich „fertig“ an.

**Tasks**

* klare Gruppierung:

  * Dateien
  * Vorschau
  * HUDs
  * Export
* verständliche Beschriftungen
* Tooltips für:

  * Startpunkt
  * HUD-Bereich
  * Synchronisation

**Fertig wenn**

* Ein neuer Nutzer versteht die App ohne Erklärung
* Keine kryptischen Bezeichnungen

---

## Story 2 – Profil- & Preset-System erweitern

**Ziel**
Wiederkehrende Setups schnell nutzbar machen.

**Tasks**

* Profile:

  * speichern
  * laden
  * überschreiben
* Presets:

  * z. B. „YouTube 21:9“
  * „Analyse 32:9“
* Presets ändern nur:

  * Auflösung
  * Seitenverhältnis
  * HUD-Breite

**Fertig wenn**

* Ein Klick reicht für Standard-Setups
* Profile sind versionsstabil

---

## Story 3 – Validierung & klare Fehlermeldungen

**Ziel**
Fehler früh erkennen und verständlich anzeigen.

**Checks**

* falsche Anzahl Videos
* falsche CSV-Zuordnung
* fehlende LapDistPct
* unplausible Sync
* inkompatible Auflösung

**Regeln**

* Kein Absturz
* Klare Meldung:

  * was fehlt
  * was zu tun ist

**Fertig wenn**

* Jeder Fehler zu einer verständlichen Meldung führt

---

## Story 4 – Performance-Feinschliff & Robustheit

**Ziel**
Stabiles Verhalten auf verschiedenen Systemen.

**Tasks**

* saubere Auswahl:

  * GPU neu
  * GPU alt
  * CPU
* klare Anzeige:

  * welcher Pfad genutzt wird
* Timeout / Abbruch bei Hängern

**Fertig wenn**

* Rendering läuft zuverlässig
* Nutzer weiß, was gerade passiert

---

## Story 5 – Vorschau vs. Final Render trennen

**Ziel**
Schnelle Vorschau, sauberes Endvideo.

**Regeln**

* Vorschau:

  * reduzierte Auflösung
  * schneller Render
* Final:

  * volle Qualität
  * stabile Einstellungen

**Fertig wenn**

* Vorschau reagiert schnell
* Final-Video entspricht Erwartungen

---

## Story 6 – Logging & Debug-Modus

**Ziel**
Probleme nachvollziehen können.

**Tasks**

* Normalmodus:

  * kurze Logs
* Debugmodus:

  * CSV-Werte
  * Sync-Infos
  * HUD-Daten
* Logs klar strukturiert

**Fertig wenn**

* Fehler nachvollziehbar sind
* Logs nicht „zumüllen“

---

## Story 7 – Projektabschluss & Dokumentation

**Ziel**
Projekt ist wartbar und verständlich.

**Inhalte**

* README:

  * Projektidee
  * Workflow
* Modulübersicht (aktualisiert)
* bekannte Grenzen
* bewusste Nicht-Ziele

**Fertig wenn**

* Projekt auch in 6 Monaten noch verständlich ist

---

## Sprint-4-Abschlusskriterien

Sprint 4 ist abgeschlossen, wenn:

* App stabil läuft
* Fehler klar kommuniziert werden
* Profile & Presets funktionieren
* Performance nachvollziehbar ist
* Dokumentation vollständig ist

---

## Projektstatus nach Sprint 4

* **Sprint 1** – App & Bedienung ✅
* **Sprint 2** – Video & Sync ✅
* **Sprint 3** – HUD-System ✅
* **Sprint 4** – Feinschliff & Abschluss ✅

Danach ist das Projekt **funktional abgeschlossen**
und kann gezielt erweitert werden (z. B. neue HUDs, Exporte, Analyse-Features).

---

## Troubleshooting - BSOD 0x3B (cldflt.sys)

Wenn ein Blue Screen mit `SYSTEM_SERVICE_EXCEPTION (0x3B)` und `cldflt.sys` auftritt:

1. OneDrive-Pause-Test
   - OneDrive kurz pausieren und denselben iWAS-Workflow erneut testen.
   - Wenn stabil: Storage/Output in einen lokalen Ordner ausserhalb OneDrive legen.

2. Systemdateien pruefen (Admin-CMD/Powershell)
   - `sfc /scannow`
   - `DISM /Online /Cleanup-Image /RestoreHealth`

3. Crash-Artefakte sichern
   - Minidumps: `C:\Windows\Minidump\`
   - Voller Dump: `C:\Windows\MEMORY.DMP`
   - Neueste `.dmp` manuell zum Support-Bundle/Issue anhaengen (nicht automatisch kopieren).

