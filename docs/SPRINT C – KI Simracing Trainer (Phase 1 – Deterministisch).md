# 🟣 SPRINT C – KI Simracing Trainer (Phase 1 – Deterministisch)

Ziel:
LLM ist nur Interpret.
Kausalität bleibt in Python.

---

## Story C1 – Segment-Engine v1

**Ziel:**
Kurven automatisch segmentieren.

### Tasks

* Krümmungsberechnung aus XY.
* Segment-ID pro Lap.
* Segment-Metadaten erzeugen.

---

## Story C2 – Feature Vector Engine v1

**Ziel:**
Feature Vector Schema implementieren.

### Tasks

* CornerSegment Objekt.
* FeatureExtractor.
* JSON Export.

---

## Story C3 – Hypothesen-Engine v1

**Ziel:**
Deterministische Hypothesen prüfen.

Beispiele:

* BrakeStartDelta
* ThrottleOnsetDelta
* SpeedMinDelta

Ausgabe:

```
{
  hypothesis_id,
  evidence_metrics,
  confidence_score
}
```

---

## Story C4 – LLM Interpretationslayer

**Ziel:**
LLM bekommt nur strukturierte Daten.

Input:

* FeatureVector
* Hypothesenliste
* Knowledge Files (Almeida + GITGUD)

Output:

* Priorisierte Coaching-Hinweise
* Evidenz-Zitate

---

# 🟣 SPRINT D – KI Trainer Phase 2 (IRSDK Physics Layer)

Nur wenn IRSDK integriert.

---

## Story D1 – Yaw & Rotation Index

* OversteerIndex
* RotationFromLoadScore

---

## Story D2 – Grip Envelope Modell

* LimitIndex
* GripUsage %

---

## Story D3 – Vertical Dynamics

* CrestIndex
* CompressionIndex
* BrakeInstabilityScore

---

# 🟣 SPRINT E – KI Trainer Phase 3 (Advanced Intelligence)

---

## Story E1 – Similarity Engine

* Segment Feature Vectors in DB
* KNN Suche
* „Du fährst wie Profil X“

---

## Story E2 – Fahrerprofil Modell

* Aggressivität
* RotationStyle
* BrakeStability
* GripUsage

---

## Story E3 – Trainingsplan Generator

Input:

* Schwächenranking

Output:

* Übungsempfehlung
* Priorisierte Kurven

---

# 🔴 Strategische Reihenfolge

1️⃣ Sprint A – UI Modernisierung
2️⃣ Sprint B – EXE & Update
3️⃣ Sprint C – Segment + Feature Engine
4️⃣ Sprint C4 – LLM Layer
5️⃣ Sprint D – IRSDK Physics
6️⃣ Sprint E – Advanced Trainer

---

# 🎯 Wichtig

Du baust damit:

Phase 1 → Professionelle Analyse-App
Phase 2 → Deterministischer Trainer
Phase 3 → Physik-basierter Trainer
Phase 4 → Intelligentes Coaching-System