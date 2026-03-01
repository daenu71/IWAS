# 🟢 SPRINT B – Weitergabe, EXE & Updatefähigkeit

**Ziel:**
App professionalisieren, verteilbar machen.

---

## Story B1 – Projektstruktur trennen

**Ziel:**
Saubere Architektur für Packaging.

### Tasks

* src/
* assets/
* config/
* build/
* dist/
* Trennung von UI, Core, Features.

---

## Story B2 – Konfigurationssystem stabilisieren

**Ziel:**
User Settings update-sicher machen.

### Tasks

* defaults.ini bleibt unverändert.
* user_settings.ini getrennt.
* Migrationsmechanismus (Version Key).
* Backward Compatibility prüfen.

---

## Story B3 – Build Pipeline für EXE

**Ziel:**
Erste distributable Version erzeugen.

### Tasks

* PyInstaller Setup.
* Icon integrieren.
* One-folder Build (nicht one-file für Updates).
* Test auf Clean Windows.

### Umsetzung (Ist-Stand)
- PyInstaller One-folder Build-Pipeline umgesetzt.
- Neuer, klarer Entry-Point für PyInstaller ergänzt: `src/app_entry.py`
  - Startet GUI normal.
  - Render-Modus bei `--ui-json`.
- Frozen-kompatiblen Render-Subprozess ergänzt, damit das EXE-Rendering nicht an `src/main.py` scheitert.
- Resource-Helper ergänzt: `src/core/resources.py` mit `get_resource_path()`
  - Nur bundle-kritische Pfade abgesichert.
- PyInstaller Spec erstellt: `packaging/iracing_vc_onefolder.spec`
  - `assets/` und `config/` als `datas` eingebunden.
  - Icon integriert: `assets/logo/iwas_icon.ico`.
- Build-Runner ergänzt: `packaging/build_onefolder.ps1`.
- Build-Doku + Clean-Windows-Checklist ergänzt: `packaging/README.md`.
- Build-Artefakte werden erzeugt in `build/pyinstaller/...` und `dist/iracing-vc/...`.

### Geänderte / neue Dateien
- Neu: `src/app_entry.py`
- Neu: `src/core/resources.py`
- Neu: `packaging/iracing_vc_onefolder.spec`
- Neu: `packaging/build_onefolder.ps1`
- Neu: `packaging/README.md`
- Geändert: `src/core/render_service.py`
- Geändert: `src/ui/controller.py`
- Geändert: `src/ui/app.py`
- Geändert: `src/core/persistence.py`
- Geändert: `src/main.py`

### Abnahme / Check
- `py_compile`: ok
- Dev-Run (Start-Smoke): ok mit Repo-`.venv\Scripts\python.exe`
- PyInstaller Build (One-folder): ok
- Start aus `dist/iracing-vc/iracing-vc.exe`: ok
- Hinweis aus Zwischenchecks:
  - Dev-Start mit System-Python: fail (fehlendes `cv2`)
  - Erster PyInstaller-Build: fail wegen `__file__` in spec → auf `SPECPATH` korrigiert → danach ok

### Fertig wenn
- ✅ One-folder Windows-EXE Build per PyInstaller ist reproduzierbar über `.spec` und/oder `build_onefolder.ps1`.
- ✅ Icon ist in der EXE integriert.
- ✅ `assets/` und `config/` sind im Bundle enthalten und werden zur Laufzeit korrekt gefunden.
- ✅ Start aus `dist/iracing-vc/` funktioniert.
- ✅ Clean-Windows-Testcheckliste ist dokumentiert.
- ✅ Keine HUD-Key-Umbenennungen; `defaults.ini` inhaltlich unverändert.


---


## Story B4 – Update-Strategie definieren (GitHub Releases + version.json)

**Ziel:**
Die App soll Updates erkennen können, ohne Auto-Update-Chaos.
Für v1 (Beta) gibt es **nur einen Update-Check + Link** (kein Auto-Downloader).

---

## Verbindliche Entscheidung (v1)

**Wir nutzen GitHub als Update-Quelle:**

* GitHub **Repo wird public**
* `version.json` liegt im Repo (main branch)
* App lädt `version.json` über GitHub RAW-URL
* Wenn eine neuere Version verfügbar ist:

  * UI zeigt Hinweis „Neue Version verfügbar“
  * Button öffnet GitHub Release-Seite (latest)

---

## Fixe Parameter / URLs (müssen so verwendet werden)

**Repo (public):**

* `daenu71/IWAS`

**RAW URL für version.json:**

* `https://raw.githubusercontent.com/daenu71/IWAS/main/version.json`

**Release URL (latest):**

* `https://github.com/daenu71/IWAS/releases/latest`

**Start-Version (Beta):**

* `0.1.0`

**Tag-Format:**

* `v0.1.0`

---

## Schritt-für-Schritt Vorgehen (muss 1:1 umgesetzt werden)

### Schritt 1 – Repo auf Public stellen

1. GitHub Repo öffnen: `daenu71/IWAS`
2. **Settings**
3. Runterscrollen zu **Danger Zone**
4. **Change repository visibility**
5. Auf **Public** stellen und bestätigen

---

### Schritt 2 – version.json im Repo anlegen

1. Im Repo Root (neben `src/` und `config/`) eine neue Datei anlegen:

   * `version.json`
2. Dateiinhalt exakt so setzen (Beta 0.1.0):

```json
{
  "version": "0.1.0",
  "release_url": "https://github.com/daenu71/IWAS/releases/latest",
  "notes": "Beta release"
}
```

3. Commit Message z. B.:

* `Add version.json for update checks`

---

### Schritt 3 – Erstes GitHub Release erstellen (0.1.0)

1. Repo öffnen → rechts bei **Releases** → **Create a new release**
2. **Tag** erstellen:

   * Tag: `v0.1.0`
   * Target: `main`
3. Release Title:

   * `IWAS 0.1.0 (Beta)`
4. Beschreibung:

   * `Beta release`
5. Build-Artefakt hochladen:

   * Die EXE oder ZIP aus deinem `dist/...` (One-Folder Build)
6. **Publish release**

---

### Schritt 4 – App: lokale Version als Konstante (Single Source of Truth)

1. In der App eine zentrale Versions-Konstante definieren:

   * `APP_VERSION = "0.1.0"`
2. Diese Konstante muss beim Update-Check verwendet werden.

---

### Schritt 5 – App: Update-Check implementieren (nur Check + Link)

**Ablauf:**

1. Beim Start (oder im Settings Screen per Button „Check for Updates“):

   * Lade JSON von:

     * `https://raw.githubusercontent.com/daenu71/IWAS/main/version.json`
2. Parse:

   * `version` (string)
   * `release_url` (string)
   * optional `notes` (string)
3. Vergleiche `online_version` vs `APP_VERSION`:

   * Wenn online_version > APP_VERSION:

     * UI Dialog anzeigen:

       * Titel: `Update available`
       * Text: `A new version {online_version} is available.`
       * optional notes anzeigen
       * Button: `Open download page`
       * Button öffnet `release_url` im Browser
   * Sonst:

     * optional Info: `You are up to date.`

**Wichtig:**

* Kein Auto-Download
* Kein Self-Replace
* Kein Restart
* Fehlerfälle abfangen:

  * Kein Internet / Timeout
  * JSON kaputt
  * Felder fehlen
    → dann nur eine verständliche Meldung, kein Crash

---

## Akzeptanzkriterien (muss erfüllt sein)

* Repo ist public
* `version.json` liegt im Repo Root und ist über RAW-URL abrufbar:

  * `https://raw.githubusercontent.com/daenu71/IWAS/main/version.json`
* Release `v0.1.0` existiert und ist unter `releases/latest` erreichbar
* App zeigt bei neuerer Version einen Hinweis und öffnet die Release-Seite
* App bleibt stabil bei fehlendem Internet / ungültiger version.json

---

## Out of Scope (v1)

* Auto-Downloader
* Auto-Installer
* Restart/Replace der EXE
* Delta-Patches

### Umsetzung (Ist-Stand)

- **Update-Quelle GitHub (fix)**
  - `version.json` wird über RAW-URL geladen:
    - `https://raw.githubusercontent.com/daenu71/IWAS/main/version.json`
  - Release-Link (latest) wird aus `release_url` genutzt (soll auf latest zeigen):
    - `https://github.com/daenu71/IWAS/releases/latest`

- **Repo-Schritte (Checklisten, kein Code)**
  - Repo auf Public stellen:
    - Repo öffnen → Settings → Danger Zone → Change repository visibility → Make public → bestätigen
  - Erstes Release erstellen:
    - Create a new release
    - Tag `v0.1.0` auf `main`
    - Title: `IWAS 0.1.0 (Beta)`
    - Description: `Beta release`
    - EXE/ZIP aus `dist/...` (One-folder Build) hochladen
    - Publish release

- **version.json angelegt**
  - Neu im Repo-Root: `version.json` (Beta 0.1.0) mit den geforderten Feldern/Werten

- **Single Source of Truth für lokale Version**
  - `APP_VERSION = "0.1.0"` zentral ergänzt in `src/core/cfg.py` (Zeile 7)
  - Update-Check nutzt diese Konstante

- **Manueller Update-Check im Settings-Screen**
  - Trigger: Button **“Check for Updates”** im Settings-Screen (`src/ui/app.py` ~ Zeile 1015)
  - Fetch-/Parse-Logik:
    - RAW-URL-Konstante in `src/ui/app.py` (Zeile 47)
    - Fetch-Logik ab `src/ui/app.py` (ab Zeile 68)
  - SemVer-Vergleich:
    - Kleiner Parser in `src/ui/app.py` (ab Zeile 52)
    - Verifiziert: `0.10.0 > 0.2.0` ergibt `True`
  - Verhalten bei neuer Version:
    - Dialog-Titel: `Update available`
    - Text: `A new version {online_version} is available.`
    - Optional: `notes`
    - Button: `Open download page` → öffnet `release_url` im Standardbrowser (`webbrowser.open`)
  - Verhalten ohne Update:
    - Dialog-Titel: `Update check`
    - Text: `You are up to date.`
  - **Wichtig:** kein Auto-Download, kein Self-Replace, kein Restart

- **Fehlerbehandlung (kein Crash)**
  - Kein Internet / Timeout:
    - Titel: `Update check failed`
    - Text: `Could not check for updates. Please check your internet connection and try again.`
  - Kaputtes/ungültiges JSON:
    - Titel: `Update check failed`
    - Text: `Update check failed: received invalid JSON.`
  - JSON ist kein Objekt:
    - Titel: `Update check failed`
    - Text: `Update check failed: update data must be a JSON object.`
  - Pflichtfeld `version` fehlt/leer:
    - Titel: `Update check failed`
    - Text: `Update check failed: field 'version' is missing or empty.`
  - Pflichtfeld `release_url` fehlt/leer:
    - Titel: `Update check failed`
    - Text: Meldung inkl. erwarteter URL `https://github.com/daenu71/IWAS/releases/latest`
  - `notes` vorhanden, aber kein String:
    - Titel: `Update check failed`
    - Text: `Update check failed: field 'notes' must be a string.`
  - Unerwarteter Fehler:
    - Titel: `Update check failed`
    - Text: `Update check failed due to an unexpected error.`

---

### Abnahme / Check

- Geänderte / neue Dateien:
  - Neu: `version.json`
  - Geändert: `src/core/cfg.py`
  - Geändert: `src/ui/app.py`

- Verifikation:
  - Nach Schritt 2:
    - py_compile: ok
    - kurzer UI-Test-Start: ok (startet, nach 3s beendet)
  - Nach Schritt 4:
    - py_compile: ok
    - kurzer UI-Test-Start: ok
  - Nach Schritt 5:
    - py_compile: ok
    - kurzer UI-Test-Start: ok
    - zusätzlicher SemVer-Check (0.10.0 > 0.2.0): ok (True)

- Bedienung:
  - App starten → Settings Tab öffnen → **Check for Updates** klicken

---

### Fertig wenn ✅

- ✅ `version.json` liegt im Repo-Root und ist über die RAW-URL abrufbar
- ✅ `APP_VERSION = "0.1.0"` ist zentral definiert und wird für den Vergleich genutzt
- ✅ Update-Check ist **manuell** über Settings-Button auslösbar
- ✅ Bei neuer Version wird ein Dialog angezeigt und der Download-Link öffnet die GitHub Release-Seite (latest)
- ✅ Kein Auto-Update (kein Download/Self-Replace/Restart)
- ✅ Fehlerfälle führen zu verständlichen Meldungen und **keinem Crash**
- ✅ py_compile und kurzer UI-Test-Start sind ok


---

## Story B5 – Logging & Crash Handling

**Ziel:**
Supportfähig machen.

### Tasks

* Logs im AppData-Ordner.
* Crash-Dialog.
* Version + OS + Python Version loggen.