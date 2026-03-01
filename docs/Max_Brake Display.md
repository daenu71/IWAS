
# Add “Max. Brake” Event Metric + 4th Column per Side in Throttle/Brake HUD  
*(Hard Zero-Reset, Anti-Flicker via Lap Distance)*

---

## Goal

- Extend the `throttle_brake` HUD to show **4 columns per side** (slow + fast).
- Add a new metric **“Max. Brake”** as the last column.
- “Max. Brake” behaves like the existing **“Min. Speed”** concept:
  - It holds the last confirmed event value
  - It updates only when a new valid event completes
- **Strict constraint:** `Brake == 0` must be treated as exact zero (no epsilon threshold).

---

## Scope

- Throttle/Brake HUD module  
- Layout code responsible for building table/columns (both sides)  
- INI configuration (`config/defaults.ini`)  
- Documentation of new INI keys  

---

## Requirements

### 1) Layout

- The Throttle/Brake HUD must display:
  - **4 columns per side**
  - “Max. Brake” as the **last column label**
- Values displayed as:
  - Percent (0–100)
  - Same formatting style as existing HUD values

---

### 2) Max-Brake Event Semantics (Strict Zero)

#### Brake Phase Start
A brake phase starts when:
- `Brake > 0`
- The detector is **armed**

#### During Brake Phase
- Track peak value:

peak = max(peak, Brake)



#### Brake Phase End
- A phase ends when:


Brake == 0



- On phase end:


last_max_brake_percent = peak * 100


- Round consistently with existing HUD logic
- Store persistently (hold value until next valid event)

#### Display Logic
- The HUD displays `last_max_brake_percent`
- Value updates only when a new valid brake phase ends

#### Strict Rearming Rule
- A new brake phase can start **only after Brake has reached exactly 0 at least once**

---

### 3) Anti-Flicker Gating (Track-Based)

After `Brake == 0`:

#### Rearm Delay Window
- Enter a delay window defined by:


max_brake_delay_distance


- This is a **LapDistPct delta**
- During this window:
- Do NOT allow a new phase to start
- Even if `Brake > 0`

This prevents flickering from small brake taps.

#### Pressure Override (Chicanes)

During the delay window:

- If:


Brake >= max_brake_delay_pressure%


- Then:
- Allow a new phase immediately
- Override the delay restriction

---

### 4) Units & Conversions

| Key | Unit | Notes |
|------|------|------|
| `max_brake_delay_distance` | LapDistPct delta | Track-based, not meters |
| `max_brake_delay_pressure` | Percent (0–100) | Convert internally to Brake scale |

---

### 5) Defaults & Documentation

#### Add to `config/defaults.ini`
- `max_brake_delay_distance`
- `max_brake_delay_pressure`

#### Documentation must explicitly state:
- Strict `Brake == 0` requirement
- Units:
- Delay distance → LapDistPct delta
- Delay pressure → percent

---

### 6) Debugging & Validation

Add minimal debug logging (guarded by existing debug flags):

Log when:

- A max-brake phase starts (include LapDistPct)
- A phase ends and is committed (include peak %)
- Pressure override triggers

---

## Implementation Notes (Investigate First)

- Locate existing **Min Speed** event implementation  
- Reuse event-hold state pattern  
- Identify:
- Where `throttle_brake` HUD builds header/value rows
- Column width calculation logic
- Ensure:
- The 4th column does not break alignment
- No clipping occurs at common output presets

---

## Do NOT

- No unrelated refactors
- Do not modify other HUD logic
- Do not change color logic
- Do not introduce epsilon thresholds
- `Brake == 0` must remain exact

---

## Deliverables (Print After Change)

- A meaningful title for the change  
- List of changed files  
- Short summary of the change  
- Location where the INI keys are documented  


# Throttle/Brake HUD – Erweiterung auf 4 Spalten + persistente „Max. Brake“-Metrik

## 1. Ziel der Änderung

Erweiterung des **Throttle/Brake HUD** um:

- 4 Spalten pro Seite (Slow + Fast)
- Neue persistente Event-Metrik **„Max. Brake“**
- Striktes Zero-Reset-Verhalten (`Brake == 0`)
- Anti-Flicker-Logik basierend auf **LapDistPct**

---

## 2. Geänderte Dateien

### Core / Rendering
- `src/render_split.py`
- `src/main.py`
- `src/core/render_service.py`
- `src/core/models.py`

### Konfiguration
- `config/defaults.ini`

### Dokumentation
- `docs/Sprint 3 – HUD-Erstellung.md`
- `docs/HUD API Vertrag.md`

---

## 3. Funktionsumfang

### 3.1 Neue INI-Parameter (end-to-end integriert)

Zwei neue konfigurierbare Parameter:

- `max_brake_delay_distance`  
  → Verzögerungsdistanz in **LapDistPct** zur Wiederfreigabe (Anti-Flicker)

- `max_brake_delay_pressure`  
  → Druckschwelle in Prozent (0–100) als Override für die Wiederfreigabe

Beide Parameter sind vollständig von INI → Service → Render-Pipeline verdrahtet.

---

### 3.2 Max. Brake Event-Logik

Implementiert wurde ein persistenter Event-Detektor mit folgendem Verhalten:

- **Striktes Phasenende nur bei `Brake == 0`**
  - Kein Epsilon
  - Keine Toleranzschwelle

- **Phasenstart nur wenn:**
  - System „armed“
  - `Brake > 0`

- Während der Phase:
  - Peak-Bremsdruck wird verfolgt

- Bei exakt `Brake == 0`:
  - Peak wird **committed**
  - Wert bleibt persistent sichtbar
  - Wird erst durch nächsten validen Event überschrieben

- **LapDist-basierte Wiederfreigabe**
  - Verhindert Flackern bei kurzen Bremslösungen
  - Optionaler Pressure-Override

- Debug-Logs integriert
  - Aktiv unter bestehendem HUD-Debug-Flag
  - Loggt:
    - Phase Start
    - Commit
    - Override-Ereignisse

---

### 3.3 HUD Layout-Erweiterung

Das Throttle/Brake HUD wurde erweitert auf:

- **4 Spalten pro Seite**
- „Max. Brake“ als letzte Spaltenüberschrift
- Prozentformatierung für alle Werte im Stil:

```

000%

```

Layout ist symmetrisch für Slow- und Fast-Seite umgesetzt.

---

### 3.4 Fast-Side LapDist Sampling

Erweiterung um:

- LapDist-Sampling auf der Fast-Seite
- Nutzung für streckenbasierte Gating-Logik

---

## 4. Validierung

Erfolgreiche Syntaxprüfung:

```

python -m py_compile
src/render_split.py
src/main.py
src/core/render_service.py
src/core/models.py

```

Status: ✔ Compile ohne Fehler

---

## 5. Dokumentation der INI-Keys

Die neuen Parameter sind dokumentiert in:

### Standardwerte + Inline-Dokumentation
- `config/defaults.ini`

### HUD INI Options Abschnitt
- `docs/Sprint 3 – HUD-Erstellung.md`

### API-Vertrag (Throttle/Brake INI Optionen)
- `docs/HUD API Vertrag.md`

---

## 6. Technische Kerneigenschaften

- Deterministisches Event-Verhalten
- Kein heuristisches Glätten
- Kein Epsilon
- Streckenbasierte Entprellung
- Persistente Anzeige bis zum nächsten validen Event
- Vollständig INI-gesteuert

---

**Status:** Implementiert, integriert, validiert.
```
