# Projektstruktur – Modulübersicht

Diese Übersicht beschreibt die zentralen Module des Projekts, deren Zweck, Abhängigkeiten und Aufgaben.

---

## 1. `src/main.py`

**Zweck:**
CLI-Einstiegspunkt.

**Abhängigkeiten:**

* `cfg.py`
* `log.py`
* `render_split.py`
* `csv_g61.py`
* `resample_lapdist.py`
* `sync_map.py`
* Stdlib (`argparse`, `json`, `re`, `pathlib`)

**Macht:**

* Liest `--ui-json`
* Lädt Konfiguration
* Wählt Sync-Renderpfad
* Startet den Renderprozess

---

## 2. `src/render_split.py`

**Zweck:**
Zentrale Orchestrierung für Sync, HUD-Signale und Rendering.

**Abhängigkeiten:**

* `encoders.py`
* `ffmpeg_plan.py`
* `huds/*.py`
* `csv_g61.py` (im Under/Oversteer-Builder)
* Stdlib (`math`, `json`, `os`, `subprocess`, `pathlib`)

**Macht:**

* Baut Zeit-/Sync-Mapping
* Berechnet HUD-Frames (inkl. Under-/Oversteer-Proxy)
* Rendert HUD-PNGs
* Startet `ffmpeg`

---

## 3. `src/csv_g61.py`

**Zweck:**
Einlesen und Typisierung von Garage61-CSV.

**Abhängigkeiten:**

* Stdlib (`csv`, `dataclasses`, `pathlib`)

**Macht:**

* Liefert `RunData`
* Spaltenzugriff (`get_float_col`)
* Spaltenprüfung (`has_col`)

---

## 4. `src/encoders.py`

**Zweck:**
Encoder-Erkennung und Fallback-Strategie.

**Abhängigkeiten:**

* `ffmpeg_plan.py` (`EncodeSpec`)
* Stdlib (`subprocess`, `dataclasses`)

**Macht:**

* Liest verfügbare `ffmpeg`-Encoder
* Baut Encode-Spezifikationen
* Wählt fallback-sicher

---

## 5. `src/ffmpeg_plan.py`

**Zweck:**
Bau und Ausführung des `ffmpeg`-Aufrufs.

**Abhängigkeiten:**

* Stdlib (`os`, `math`, `subprocess`, `pathlib`, `dataclasses`)

**Macht:**

* Erzeugt Decode/Filter/Encode-Plan
* Behandelt lange Filter (`-filter_complex_script`)
* Führt `ffmpeg` aus

---

## 6. `src/cfg.py`

**Zweck:**
Konfiguration laden.

**Abhängigkeiten:**

* Stdlib (`configparser`, `pathlib`, `dataclasses`)

**Macht:**

* Liest `config/defaults.ini`
* Liefert strukturiertes `Cfg`

---

## 7. `src/log.py`

**Zweck:**
Dateibasiertes Logging.

**Abhängigkeiten:**

* Stdlib (`datetime`, `pathlib`, `dataclasses`)

**Macht:**

* Erzeugt timestamped Logdateien
* Schreibt Key/Value-Logs
* Schreibt Textlogs

---

## 8. `src/resample_lapdist.py`

**Zweck:**
LapDist-Resampling-Helfer.

**Abhängigkeiten:**

* Stdlib (`math`, `dataclasses`, `typing`)

**Macht:**

* Erzeugt LapDist-Grid
* Lineare Resamples für Kanäle

---

## 9. `src/sync_map.py`

**Zweck:**
LapDist-basiertes Frame-Mapping.

**Abhängigkeiten:**

* Stdlib (`math`, `dataclasses`)

**Macht:**

* Mappt Slow-Frames auf Fast-Samples via nearest LapDist

---

## 10. `src/huds/common.py`

**Zweck:**
Gemeinsame HUD-Konstanten.

**Abhängigkeiten:**

* Keine externen

**Macht:**

* Definiert Farben
* Definiert HUD-Gruppen (`SCROLL_HUD_NAMES`, `TABLE_HUD_NAMES`)

---

## 11. `src/huds/speed.py`

**Zweck:**
Speed-HUD rendern.

**Abhängigkeiten:**

* Optional `PIL.ImageFont`
* Sonst Fallback

**Macht:**

* Zeichnet Speed/Min für slow/fast

---

## 12. `src/huds/gear_rpm.py`

**Zweck:**
Gear & RPM-HUD rendern.

**Abhängigkeiten:**

* Optional `PIL.ImageFont`

**Macht:**

* Zeichnet Gang/RPM für slow/fast

---

## 13. `src/huds/throttle_brake.py`

**Zweck:**
Throttle/Brake(+ABS)-HUD rendern.

**Abhängigkeiten:**

* Optional `PIL.ImageFont`
* Stdlib `os`

**Macht:**

* Zeichnet zeitbasierte Kurven
* Zeichnet Marker für Inputs

---

## 14. `src/huds/steering.py`

**Zweck:**
Steering-HUD rendern.

**Abhängigkeiten:**

* Stdlib `math`, `os`

**Macht:**

* Zeichnet Lenk-Kurven im Scrollfenster
* Implementiert Headroom-Logik

---

## 15. `src/huds/delta.py`

**Zweck:**
Time-Delta-HUD rendern.

**Abhängigkeiten:**

* Optional `PIL.ImageFont`

**Macht:**

* Zeichnet Delta-Kurve
* Zeichnet Delta-Anzeige im Zeitfenster

---

## 16. `src/huds/line_delta.py`

**Zweck:**
Line-Delta-HUD rendern.

**Abhängigkeiten:**

* Stdlib `math`
* Optional `PIL.ImageFont`

**Macht:**

* Zeichnet Linie um Null-Basis
* Marker im zentrierten Scrollfenster

---

## 17. `src/huds/under_oversteer.py`

**Zweck:**
Under-/Oversteer-HUD rendern.

**Abhängigkeiten:**

* Stdlib `math`
* Optional `PIL.ImageFont`

**Macht:**

* Zeichnet Baseline (`0`)
* Slow = Rot
* Fast = Blau
* Basis: `ctx.signals.under_oversteer_*`

---

## Laufzeit-Abhängigkeiten

Zur Laufzeit werden folgende externe Komponenten benötigt:

### Externe Tools

* `ffmpeg`
* `ffprobe`

### Python-Bibliotheken

* `Pillow (PIL)`

  * Für HUD-PNG-Text und Font-Rendering
  * Ohne PIL: HUD-Rendering wird übersprungen oder auf Fallback reduziert

