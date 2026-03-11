Ich habe dir den **Security-Sprint im gleichen Stil wie deine bestehenden Sprint-Docs aufgebaut**, sodass dein CustomGPT daraus **direkt Codex-Prompts erzeugen kann** (klare Story-Blöcke, Tasks, Akzeptanzkriterien, Changed Files).
Die Struktur orientiert sich bewusst an deinen bisherigen Sprints (z. B. Sprint A/B).  

---

# 🔐 Security Sprint 1 – Hardening & Supply-Chain Sicherheit

**Ziel des Sprints**

Behebung aller im Security-Audit identifizierten Risiken ohne funktionale Änderungen der App.

Der Sprint fokussiert auf:

* Update-Channel Trust
* Binary Execution Sicherheit
* Supply-Chain Stabilität
* Privacy-Safe Diagnostics

⚠️ **Keine Refactors.**
⚠️ **Keine neuen Features.**
Nur **Security Hardening**.

---

# Story S1 — Update-URL Allowlist & HTTPS Enforcement

## Ziel

Verhindern, dass manipulierte Update-Manifeste Benutzer auf fremde Webseiten umleiten.

Aktuell wird `release_url` aus dem Update-JSON ungeprüft geöffnet.

Risiko:

```
webbrowser.open(release_url)
```

Ein kompromittiertes Manifest könnte z. B.:

```
file://
javascript:
http://malicious.site
```

öffnen.

---

## Tasks

### 1️⃣ URL Parser einführen

Beim Öffnen der Release-URL:

* `urllib.parse.urlparse()` verwenden

Validieren:

```
scheme == https
```

---

### 2️⃣ Host Allowlist

Nur folgende Hosts erlauben:

```
github.com
www.github.com
```

---

### 3️⃣ Path Allowlist

Pfad muss beginnen mit:

```
/daenu71/IWAS/releases
```

---

### 4️⃣ Fallback Verhalten

Wenn URL ungültig:

```
show_error("Invalid update URL")
```

und **nicht öffnen**.

---

### 5️⃣ Optional Logging

Loggen:

```
Rejected update URL: ...
```

---

## Akzeptanzkriterien

* Nur HTTPS erlaubt
* Nur GitHub-Release URLs erlaubt
* Keine Öffnung bei ungültiger URL
* UI zeigt Fehlermeldung

---

## Geänderte Dateien

```
src/ui/app.py
```

---

## Deliverables

**Title**

```
Security: Update URL validation + GitHub allowlist
```

**Summary**

* Release URL wird validiert
* HTTPS Pflicht
* GitHub Host + Path Allowlist
* Schutz vor manipulierten Update-Manifests

---

# Story S2 — FFmpeg Binary Trust (PATH Hijack Schutz)

## Ziel

Verhindern, dass ein manipuliertes `ffmpeg.exe` aus dem System-PATH gestartet wird.

Der aktuelle Resolver:

```
which("ffmpeg")
```

kann eine fremde Binary laden.

---

## Sicherheitsstrategie

### EXE-Modus

Wenn iWAS als **packaged App läuft**

→ **nur bundled FFmpeg verwenden**

Pfad:

```
_internal/tools/ffmpeg/
```

Wenn Binary fehlt:

```
raise RuntimeError("Bundled ffmpeg missing")
```

---

### Source-Mode (Developer)

Nur im Source-Mode darf PATH-Fallback erlaubt sein.

Erkennung z. B.:

```
if running_from_source:
```

---

## Tasks

### 1️⃣ Packaged Mode Detection

Implementieren:

```
sys.frozen
```

(PyInstaller Standard)

---

### 2️⃣ Resolver Logik ändern

Pseudo-Logik:

```
if packaged:
    return bundled_ffmpeg
else:
    fallback PATH search
```

---

### 3️⃣ Logging

Beim Start loggen:

```
FFmpeg source: bundled
```

oder

```
FFmpeg source: PATH
```

---

## Akzeptanzkriterien

* EXE startet **niemals PATH-ffmpeg**
* Dev-Mode funktioniert weiterhin
* Fehlende bundled binary → klarer Fehler

---

## Geänderte Dateien

```
src/core/ffmpeg_tools.py
src/core/ffmpeg_plan.py
```

---

## Deliverables

**Title**

```
Security: FFmpeg PATH hijack protection
```

**Summary**

* EXE nutzt nur bundled ffmpeg
* PATH fallback nur im Dev-Mode
* Schutz vor manipulierten ffmpeg binaries

---

# Story S3 — Dependency Locking (Supply-Chain Hardening)

## Ziel

Verhindern, dass Builds unkontrolliert neue Package-Versionen laden.

Aktuell:

```
pip install -r requirements.txt
```

ohne Versionen.

---

## Lösung

Pinned Dependencies.

---

## Tasks

### 1️⃣ Versions fixieren

Beispiel:

```
numpy==1.26.4
pandas==2.2.1
opencv-python==4.9.0
irsdk==1.4.0
```

---

### 2️⃣ Optional Hash Lock

Optional:

```
pip-tools
```

oder

```
requirements-lock.txt
```

---

### 3️⃣ Build Script anpassen

`build_onefolder.ps1`

ändern zu:

```
pip install -r requirements.txt
```

mit gepinnten Versionen.

---

### 4️⃣ Dokumentation

In README ergänzen:

```
Dependency policy: pinned versions
```

---

## Akzeptanzkriterien

* Alle Dependencies gepinnt
* Build reproduzierbar
* Keine Floating Versions

---

## Geänderte Dateien

```
requirements.txt
packaging/build_onefolder.ps1
```

---

## Deliverables

**Title**

```
Security: Dependency pinning for reproducible builds
```

**Summary**

* requirements.txt mit festen Versionen
* Supply-Chain Risiko reduziert
* reproduzierbare Builds

---

# Story S4 — Privacy-Safe Diagnostics Bundle

## Ziel

Support-Bundles dürfen keine sensiblen Daten enthalten.

Aktuell enthalten Logs:

* absolute Pfade
* Username
* ffmpeg commands

---

## Tasks

### 1️⃣ Path Redaction

Vor Bundle-Export:

Ersetzen:

```
C:\Users\Daniel\...
```

→

```
C:\Users\<redacted>\
```

---

### 2️⃣ URL Token Scrubbing

Entfernen:

```
token=
apikey=
auth=
```

---

### 3️⃣ ffmpeg command masking

Beispiel:

```
-input C:\Users\Daniel\video.mp4
```

→

```
-input <user_path>\video.mp4
```

---

### 4️⃣ Safe Diagnostics Mode

Option im UI:

```
Export Safe Diagnostics
```

nutzt Redaction.

---

## Akzeptanzkriterien

Diagnostics enthalten keine:

* Usernamen
* absoluten Pfade
* Tokens

---

## Geänderte Dateien

```
src/core/diagnostics.py
src/main.py
src/core/ffmpeg_plan.py
```

---

## Deliverables

**Title**

```
Security: Privacy-safe diagnostics bundle
```

**Summary**

* Pfade anonymisiert
* Tokens entfernt
* sichere Support-Bundles

---

# Definition of Done – Security Sprint 1

Der Sprint ist abgeschlossen wenn:

* Update-URL Injection ausgeschlossen
* FFmpeg PATH Hijack verhindert
* Dependencies gepinnt
* Diagnostics anonymisiert

---

# Security Level nach Sprint

Vorher:

```
Medium risk
```

Nach Sprint:

```
Low risk
```

---

# Empfehlung (Projektarchitektur)

Ich würde zusätzlich **Story S5 vorbereiten**, aber **nicht zwingend im selben Sprint**:

### Story S5 – Update Manifest Signatur

Ziel:

```
version.json
```

wird signiert (SHA256 + Public Key).

Damit kann niemand dein Update-Manifest manipulieren.

Das wäre **Enterprise-Level Sicherheit**.

