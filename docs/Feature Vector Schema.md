# Feature Vector Schema v1.0

## 0) Grundprinzip

* **Ein Vector pro Segment** (Standard: “CornerSegment” = Entry→Apex→Exit).
* Segmentierung ist **deterministisch** und **streckenbasiert** (LapDistPct).
* Jede Feature-Gruppe hat:

  * **Name**
  * **Typ/Einheit**
  * **Quelle**: CSV / IRSDK
  * **Aggregation**: min/max/mean/std, oder Event-basiert
  * **Definition**: wie berechnet

---

## 1) Entities / Granularität

### A) `CornerSegment` (Haupteinheit v1.0)

Ein Segment hat folgende Fixpunkte (streckenbasiert):

* `entry_start` (Beginn Brems-/Lift-Phase oder definierter Lookback)
* `brake_start` (Brake > Schwelle)
* `turn_in` (Steering über Schwelle oder Krümmungsbeginn)
* `apex` (Minimum Speed oder max curvature window)
* `throttle_onset` (Throttle > Schwelle nach Apex)
* `exit_end` (z.B. +X% LapDistPct nach Apex oder nächster BrakeStart)

> Die genauen Schwellen sind Konfig (INI), aber das Schema bleibt stabil.

### B) `LapSummary` (optional v1.0, aber sehr nützlich)

Pro Runde aggregierte Features für Konstanz/Progress.

---

## 2) Feature Gruppen (v1.0)

### Gruppe G0 — IDs & Kontext (immer)

| Feature        |                Typ | Quelle    | Definition                                |
| -------------- | -----------------: | --------- | ----------------------------------------- |
| `track_id`     |             string | meta      | Strecke                                   |
| `car_id`       |             string | meta      | Auto                                      |
| `driver_id`    |             string | meta      | Fahrer                                    |
| `lap_id`       |         string/int | meta      | Runde                                     |
| `segment_id`   |                int | derived   | fortlaufend pro Lap                       |
| `segment_type` |               enum | derived   | hairpin/sweeper/compound/… (aus Krümmung) |
| `dist_entry`   | float (LapDistPct) | CSV/IRSDK | Segment Entry                             |
| `dist_apex`    |              float | CSV/IRSDK | Apex                                      |
| `dist_exit`    |              float | CSV/IRSDK | Segment Exit                              |

---

### Gruppe G1 — Geometrie / Streckenform (Core)

| Feature          |                Typ | Quelle                  | Definition                                                             |
| ---------------- | -----------------: | ----------------------- | ---------------------------------------------------------------------- |
| `curv_mean`      |        float (1/m) | CSV (X/Y) / IRSDK (X/Y) | mittlere Krümmung im Segment                                           |
| `curv_max`       |        float (1/m) | CSV/IRSDK               | max Krümmung                                                           |
| `radius_min`     |          float (m) | CSV/IRSDK               | 1/curv_max (geclamped)                                                 |
| `curv_trend`     |              float | CSV/IRSDK               | increasing vs decreasing radius (z.B. slope der Krümmung über Distanz) |
| `compound_peaks` |                int | CSV/IRSDK               | Anzahl lokaler Krümmungsmaxima                                         |
| `segment_length` | float (LapDistPct) | CSV/IRSDK               | exit-entry                                                             |
| `heading_change` |        float (deg) | CSV/IRSDK               | integrierte Richtungsänderung (aus XY)                                 |

**IRSDK-Add-Ons (3D):**

| Feature             |         Typ | Quelle           | Definition                                     |
| ------------------- | ----------: | ---------------- | ---------------------------------------------- |
| `elev_delta`        |   float (m) | IRSDK            | Z(exit)-Z(entry)                               |
| `slope_mean`        | float (m/m) | IRSDK            | dZ/dS Mittel                                   |
| `crest_index`       |       float | IRSDK            | Pattern: +slope→0→−slope (Kuppe)               |
| `compression_index` |       float | IRSDK            | Pattern: −slope→0→+slope                       |
| `banking_proxy`     |       float | IRSDK (optional) | falls Roll/track banking verfügbar; sonst “na” |

---

### Gruppe G2 — Speed Profil (Core)

| Feature           |          Typ | Quelle    | Definition                          |
| ----------------- | -----------: | --------- | ----------------------------------- |
| `speed_entry`     | float (km/h) | CSV/IRSDK | Speed am entry                      |
| `speed_min`       |        float | CSV/IRSDK | Minimum Speed im Segment            |
| `speed_exit`      |        float | CSV/IRSDK | Speed am exit                       |
| `speed_drop`      | float (km/h) | derived   | entry - min                         |
| `speed_recovery`  | float (km/h) | derived   | exit - min                          |
| `time_in_segment` |    float (s) | derived   | aus Distanz+Speed (oder IRSDK time) |

---

### Gruppe G3 — Brake Features (Core)

| Feature               |                  Typ | Quelle    | Definition                  |       |     |
| --------------------- | -------------------: | --------- | --------------------------- | ----- | --- |
| `brake_start_dist`    |                float | CSV/IRSDK | erste Stelle Brake > thresh |       |     |
| `brake_peak`          |            float (%) | CSV/IRSDK | max Brake im Segment        |       |     |
| `brake_mean`          |            float (%) | CSV/IRSDK | Mittelwert                  |       |     |
| `brake_duration_dist` |   float (LapDistPct) | CSV/IRSDK | Distanz mit Brake > thresh  |       |     |
| `brake_release_rate`  | float (%/LapDistPct) | CSV/IRSDK | slope im Release-Fenster    |       |     |
| `brake_overlap_steer` |            float (%) | CSV/IRSDK | Anteil Distanz: Brake>th &  | Steer | >th |

**IRSDK-Add-Ons (Qualität):**

| Feature              |          Typ | Quelle | Definition                                     |
| -------------------- | -----------: | ------ | ---------------------------------------------- |
| `abs_activity_ratio` |  float (0–1) | IRSDK  | Anteil Zeit/Distanz ABS aktiv                  |
| `long_decel_peak`    | float (m/s²) | IRSDK  | min LongAccel                                  |
| `decel_efficiency`   |        float | IRSDK  | LongDecel / Brake (bei Peak)                   |
| `vert_load_at_brake` |        float | IRSDK  | VertAccel oder susp travel proxy bei BrakePeak |

---

### Gruppe G4 — Throttle Features (Core)

| Feature                    |                  Typ | Quelle    | Definition                                  |
| -------------------------- | -------------------: | --------- | ------------------------------------------- |
| `throttle_lift_start_dist` |                float | CSV/IRSDK | erste Stelle Throttle < thresh (falls Lift) |
| `throttle_onset_dist`      |                float | CSV/IRSDK | erste Stelle nach Apex Throttle > thresh    |
| `throttle_onset_delay`     |   float (LapDistPct) | derived   | throttle_onset - apex                       |
| `throttle_mean_exit`       |            float (%) | CSV/IRSDK | Mittelwert im Exit-Fenster                  |
| `throttle_ramp_rate`       | float (%/LapDistPct) | CSV/IRSDK | slope onset→x%                              |

**IRSDK-Add-On:**

| Feature             |         Typ | Quelle | Definition                        |
| ------------------- | ----------: | ------ | --------------------------------- |
| `tc_activity_ratio` | float (0–1) | IRSDK  | Anteil TC aktiv (falls verfügbar) |

---

### Gruppe G5 — Steering / Control (Core)

| Feature             |              Typ | Quelle         | Definition                                    |       |   |
| ------------------- | ---------------: | -------------- | --------------------------------------------- | ----- | - |
| `steer_peak_abs`    | float (norm/deg) | CSV/IRSDK      | max                                           | steer |   |
| `steer_mean_abs`    |            float | CSV/IRSDK      | mean                                          | steer |   |
| `steer_var`         |            float | CSV/IRSDK      | Varianz (Stabilität)                          |       |   |
| `steer_corrections` |              int | CSV/IRSDK      | Anzahl Sign-/Slope-Wechsel > thresh           |       |   |
| `steer_efficiency`  |            float | CSV* / IRSDK** | CSV: speed_min / steer_peak, IRSDK: latAccel/ | steer |   |

---

### Gruppe G6 — Rotation / Yaw (IRSDK-only in v1.0, sonst “na”)

| Feature                    |           Typ | Quelle | Definition                                              |         |   |
| -------------------------- | ------------: | ------ | ------------------------------------------------------- | ------- | - |
| `yaw_rate_peak`            | float (deg/s) | IRSDK  | max                                                     | YawRate |   |
| `yaw_rate_var`             |         float | IRSDK  | Varianz (Snap/Instabilität)                             |         |   |
| `yaw_vs_ideal_mean`        |         float | IRSDK  | mean(YawRate - Speed/Radius)                            |         |   |
| `oversteer_index_peak`     |         float | IRSDK  | max(YawRate - Speed/Radius)                             |         |   |
| `rotation_from_load_score` |         float | IRSDK  | YawRate steigt während BrakeRelease bei geringem ΔSteer |         |   |

---

### Gruppe G7 — Forces / Grip Envelope (IRSDK-only, sonst “na”)

| Feature           |          Typ | Quelle | Definition                        |          |   |
| ----------------- | -----------: | ------ | --------------------------------- | -------- | - |
| `lat_accel_peak`  | float (m/s²) | IRSDK  | max                               | LatAccel |   |
| `combo_g_peak`    |        float | IRSDK  | max sqrt(lat²+long²)              |          |   |
| `grip_usage_peak` |  float (0–1) | IRSDK  | combo_g_peak / maxObservedSession |          |   |
| `grip_usage_mean` |  float (0–1) | IRSDK  | mean combo_g / maxObs             |          |   |
| `vert_accel_min`  | float (m/s²) | IRSDK  | min VertAccel (Kuppe)             |          |   |
| `vert_accel_max`  | float (m/s²) | IRSDK  | max VertAccel (Kompression)       |          |   |

---

### Gruppe G8 — Gear / RPM Events (Core+)

| Feature               |   Typ | Quelle    | Definition                   |
| --------------------- | ----: | --------- | ---------------------------- |
| `gear_min`            |   int | CSV/IRSDK | min gear im Segment          |
| `downshift_count`     |   int | CSV/IRSDK | Anzahl Downshifts            |
| `rpm_peak`            | float | CSV/IRSDK | max RPM                      |
| `downshift_near_apex` |   int | CSV/IRSDK | Downshifts im Window um Apex |

**IRSDK-Add-On:**

| Feature                      |   Typ | Quelle | Definition                              |
| ---------------------------- | ----: | ------ | --------------------------------------- |
| `gearshift_yaw_spike_score`  | float | IRSDK  | Korrelation GearChange ↔ YawRate spikes |
| `gearshift_long_spike_score` | float | IRSDK  | Korrelation ↔ LongAccel spikes          |

---

### Gruppe G9 — Delta / Vergleich (wenn Vergleichsrunde vorhanden)

Diese Features existieren nur, wenn ein Referenzlauf vorhanden ist (Fast vs Slow oder BestLap vs Current).

| Feature                      |                Typ | Quelle  | Definition                     |
| ---------------------------- | -----------------: | ------- | ------------------------------ |
| `delta_time_segment`         |          float (s) | derived | Zeitdifferenz Segment          |
| `delta_speed_min`            |       float (km/h) | derived | speed_min(this)-speed_min(ref) |
| `delta_brake_start`          | float (LapDistPct) | derived | brake_start_dist(this)-ref     |
| `delta_throttle_onset`       | float (LapDistPct) | derived | throttle_onset_dist(this)-ref  |
| `delta_grip_usage_peak`      |              float | IRSDK   | grip_usage_peak(this)-ref      |
| `delta_oversteer_index_peak` |              float | IRSDK   | oversteer_index_peak(this)-ref |

---

## 3) LapSummary v1.0 (Konstanz/Progress)

| Feature                |       Typ | Quelle        | Definition                                                             |
| ---------------------- | --------: | ------------- | ---------------------------------------------------------------------- |
| `lap_time`             | float (s) | derived/IRSDK | Rundenzeit                                                             |
| `consistency_score`    |     float | derived       | gewichtete Varianz (brake_start, apex_speed, throttle_onset) über Laps |
| `lap_grip_mean`        |     float | IRSDK         | mean grip_usage_mean über Segmente                                     |
| `lap_oversteer_events` |       int | IRSDK         | Count oversteer_index_peak > thresh                                    |
| `lap_abs_ratio`        |     float | IRSDK         | mean abs_activity_ratio über Segmente                                  |

---

## 4) Pflichtfelder vs Optional (damit v1.0 robust bleibt)

**Pflicht (CSV-Core):**

* G0, G1(2D), G2, G3, G4, G5, G8
* (G9 nur wenn Vergleich)

**Optional (IRSDK-Erweiterung):**

* G1(3D), G3(ABS+Effizienz), G6, G7, G8(Add-Ons), G9(IRSDK-Deltas)

---

## 5) Kurzbewertung v1.0 (warum das gut “v1” ist)

* **Transparenz**: jede Kennzahl ist deterministisch erklärbar.
* **Skalierbarkeit**: du kannst pro Segment hunderte Laps vergleichen.
* **LLM-tauglich**: der LLM bekommt *keine* Rohdaten, sondern strukturierte Evidenz.
* **IRSDK-Upgrade**: erweitert Feature-Set, ohne Schema-Bruch.

---

## 6) JSON-Schema

{
  "schema_id": "irvc_feature_vector_v1_0",
  "schema_version": "1.0",
  "entity": "CornerSegment",
  "segment_axis": "LapDistPct",
  "principles": {
    "deterministic": true,
    "csv_is_truth_for_v1": true,
    "compare_over_distance_not_time": true,
    "lapdist_unwrap_before_alignment": true
  },
  "notes": {
    "thresholds": "All thresholds are config-driven (INI). The schema stays stable.",
    "missing_values": "If a feature is not available (e.g., IRSDK-only while using CSV), output null."
  },
  "enums": {
    "segment_type": [
      "unknown",
      "hairpin",
      "sweeper",
      "increasing_radius",
      "decreasing_radius",
      "compound",
      "chicane",
      "kink",
      "straight"
    ]
  },
  "fields": [
    {
      "group": "G0_ids_context",
      "required": true,
      "fields": [
        {
          "name": "track_id",
          "type": "string",
          "unit": null,
          "source": ["meta"],
          "definition": "Track identifier (e.g., 'Sachsenring')."
        },
        {
          "name": "car_id",
          "type": "string",
          "unit": null,
          "source": ["meta"],
          "definition": "Car identifier (e.g., 'McLaren 720S GT3 EVO')."
        },
        {
          "name": "driver_id",
          "type": "string",
          "unit": null,
          "source": ["meta"],
          "definition": "Driver identifier."
        },
        {
          "name": "lap_id",
          "type": "string",
          "unit": null,
          "source": ["meta"],
          "definition": "Lap identifier (string or int as string)."
        },
        {
          "name": "segment_id",
          "type": "integer",
          "unit": null,
          "source": ["derived"],
          "definition": "Monotonic segment index within lap."
        },
        {
          "name": "segment_type",
          "type": "enum",
          "enum": "segment_type",
          "unit": null,
          "source": ["derived"],
          "definition": "Geometric classification derived from curvature profile."
        },
        {
          "name": "dist_entry",
          "type": "number",
          "unit": "LapDistPct",
          "source": ["csv", "irsdk", "derived"],
          "definition": "Segment entry point (LapDistPct)."
        },
        {
          "name": "dist_apex",
          "type": "number",
          "unit": "LapDistPct",
          "source": ["csv", "irsdk", "derived"],
          "definition": "Apex point (typically at speed minimum or curvature max window)."
        },
        {
          "name": "dist_exit",
          "type": "number",
          "unit": "LapDistPct",
          "source": ["csv", "irsdk", "derived"],
          "definition": "Segment exit point."
        }
      ]
    },
    {
      "group": "G1_geometry_2d_core",
      "required": true,
      "fields": [
        {
          "name": "curv_mean",
          "type": "number",
          "unit": "1/m",
          "source": ["csv_xy", "irsdk_xy", "derived"],
          "definition": "Mean curvature over the segment computed from XY path derivatives."
        },
        {
          "name": "curv_max",
          "type": "number",
          "unit": "1/m",
          "source": ["csv_xy", "irsdk_xy", "derived"],
          "definition": "Max curvature over the segment."
        },
        {
          "name": "radius_min",
          "type": "number",
          "unit": "m",
          "source": ["derived"],
          "definition": "Minimum radius proxy: 1 / clamp(curv_max)."
        },
        {
          "name": "curv_trend",
          "type": "number",
          "unit": "unitless",
          "source": ["derived"],
          "definition": "Curvature trend slope vs distance (positive ~ tightening, negative ~ opening)."
        },
        {
          "name": "compound_peaks",
          "type": "integer",
          "unit": null,
          "source": ["derived"],
          "definition": "Count of local maxima in curvature profile within the segment."
        },
        {
          "name": "segment_length",
          "type": "number",
          "unit": "LapDistPct",
          "source": ["derived"],
          "definition": "dist_exit - dist_entry (in LapDistPct)."
        },
        {
          "name": "heading_change",
          "type": "number",
          "unit": "deg",
          "source": ["derived"],
          "definition": "Integrated heading change across the segment from XY heading."
        }
      ]
    },
    {
      "group": "G1_geometry_3d_irsdk",
      "required": false,
      "fields": [
        {
          "name": "elev_delta",
          "type": "number",
          "unit": "m",
          "source": ["irsdk_z", "derived"],
          "definition": "Z(exit) - Z(entry)."
        },
        {
          "name": "slope_mean",
          "type": "number",
          "unit": "m/m",
          "source": ["irsdk_z", "derived"],
          "definition": "Mean slope proxy over segment: dZ/dS."
        },
        {
          "name": "crest_index",
          "type": "number",
          "unit": "unitless",
          "source": ["irsdk_z", "derived"],
          "definition": "Crest pattern strength (positive slope -> 0 -> negative slope)."
        },
        {
          "name": "compression_index",
          "type": "number",
          "unit": "unitless",
          "source": ["irsdk_z", "derived"],
          "definition": "Compression pattern strength (negative slope -> 0 -> positive slope)."
        },
        {
          "name": "banking_proxy",
          "type": "number",
          "unit": "deg",
          "source": ["irsdk_optional", "derived"],
          "definition": "Optional banking proxy if roll/banking signal exists; else null."
        }
      ]
    },
    {
      "group": "G2_speed_profile",
      "required": true,
      "fields": [
        {
          "name": "speed_entry",
          "type": "number",
          "unit": "km/h",
          "source": ["csv", "irsdk"],
          "definition": "Speed at dist_entry."
        },
        {
          "name": "speed_min",
          "type": "number",
          "unit": "km/h",
          "source": ["csv", "irsdk"],
          "definition": "Minimum speed in segment."
        },
        {
          "name": "speed_exit",
          "type": "number",
          "unit": "km/h",
          "source": ["csv", "irsdk"],
          "definition": "Speed at dist_exit."
        },
        {
          "name": "speed_drop",
          "type": "number",
          "unit": "km/h",
          "source": ["derived"],
          "definition": "speed_entry - speed_min."
        },
        {
          "name": "speed_recovery",
          "type": "number",
          "unit": "km/h",
          "source": ["derived"],
          "definition": "speed_exit - speed_min."
        },
        {
          "name": "time_in_segment",
          "type": "number",
          "unit": "s",
          "source": ["derived", "irsdk_time"],
          "definition": "Estimated segment time (from distance & speed), or IRSDK time if available."
        }
      ]
    },
    {
      "group": "G3_brake_core",
      "required": true,
      "fields": [
        {
          "name": "brake_start_dist",
          "type": "number",
          "unit": "LapDistPct",
          "source": ["csv", "irsdk", "derived"],
          "definition": "First point where brake > threshold within segment window."
        },
        {
          "name": "brake_peak",
          "type": "number",
          "unit": "%",
          "source": ["csv", "irsdk"],
          "definition": "Max brake value in segment."
        },
        {
          "name": "brake_mean",
          "type": "number",
          "unit": "%",
          "source": ["csv", "irsdk"],
          "definition": "Mean brake value across segment."
        },
        {
          "name": "brake_duration_dist",
          "type": "number",
          "unit": "LapDistPct",
          "source": ["derived"],
          "definition": "Distance span where brake > threshold."
        },
        {
          "name": "brake_release_rate",
          "type": "number",
          "unit": "%/LapDistPct",
          "source": ["derived"],
          "definition": "Release slope in a defined release window around turn-in/apex."
        },
        {
          "name": "brake_overlap_steer",
          "type": "number",
          "unit": "0-1",
          "source": ["derived"],
          "definition": "Ratio of segment distance where brake>th AND |steer|>th."
        }
      ]
    },
    {
      "group": "G3_brake_irsdk_quality",
      "required": false,
      "fields": [
        {
          "name": "abs_activity_ratio",
          "type": "number",
          "unit": "0-1",
          "source": ["irsdk"],
          "definition": "Fraction of segment where ABS is active."
        },
        {
          "name": "long_decel_peak",
          "type": "number",
          "unit": "m/s^2",
          "source": ["irsdk"],
          "definition": "Most negative longitudinal acceleration in segment."
        },
        {
          "name": "decel_efficiency",
          "type": "number",
          "unit": "unitless",
          "source": ["derived_irsdk"],
          "definition": "Decel per brake proxy at peak (e.g., |LongAccel| / BrakePeak)."
        },
        {
          "name": "vert_load_at_brake",
          "type": "number",
          "unit": "m/s^2",
          "source": ["irsdk"],
          "definition": "Vertical acceleration (or proxy) sampled at brake peak."
        }
      ]
    },
    {
      "group": "G4_throttle_core",
      "required": true,
      "fields": [
        {
          "name": "throttle_lift_start_dist",
          "type": "number",
          "unit": "LapDistPct",
          "source": ["csv", "irsdk", "derived"],
          "definition": "First point where throttle < threshold (if lift exists) in segment."
        },
        {
          "name": "throttle_onset_dist",
          "type": "number",
          "unit": "LapDistPct",
          "source": ["csv", "irsdk", "derived"],
          "definition": "First point after apex where throttle > threshold."
        },
        {
          "name": "throttle_onset_delay",
          "type": "number",
          "unit": "LapDistPct",
          "source": ["derived"],
          "definition": "throttle_onset_dist - dist_apex."
        },
        {
          "name": "throttle_mean_exit",
          "type": "number",
          "unit": "%",
          "source": ["csv", "irsdk"],
          "definition": "Mean throttle in exit window."
        },
        {
          "name": "throttle_ramp_rate",
          "type": "number",
          "unit": "%/LapDistPct",
          "source": ["derived"],
          "definition": "Throttle slope from onset to a defined post-onset window."
        }
      ]
    },
    {
      "group": "G4_throttle_irsdk",
      "required": false,
      "fields": [
        {
          "name": "tc_activity_ratio",
          "type": "number",
          "unit": "0-1",
          "source": ["irsdk_optional"],
          "definition": "Fraction of segment where TC is active (if available)."
        }
      ]
    },
    {
      "group": "G5_steering_control",
      "required": true,
      "fields": [
        {
          "name": "steer_peak_abs",
          "type": "number",
          "unit": "deg_or_norm",
          "source": ["csv", "irsdk"],
          "definition": "Max absolute steering input in segment."
        },
        {
          "name": "steer_mean_abs",
          "type": "number",
          "unit": "deg_or_norm",
          "source": ["csv", "irsdk"],
          "definition": "Mean absolute steering input in segment."
        },
        {
          "name": "steer_var",
          "type": "number",
          "unit": "deg_or_norm^2",
          "source": ["derived"],
          "definition": "Variance of steering (stability proxy)."
        },
        {
          "name": "steer_corrections",
          "type": "integer",
          "unit": null,
          "source": ["derived"],
          "definition": "Count of meaningful steering corrections (thresholded slope/sign changes)."
        },
        {
          "name": "steer_efficiency",
          "type": "number",
          "unit": "unitless",
          "source": ["derived_csv_or_irsdk"],
          "definition": "CSV proxy: speed_min/steer_peak_abs; IRSDK preferred: latAccel/|steer|."
        }
      ]
    },
    {
      "group": "G6_rotation_yaw_irsdk",
      "required": false,
      "fields": [
        {
          "name": "yaw_rate_peak",
          "type": "number",
          "unit": "deg/s",
          "source": ["irsdk"],
          "definition": "Max absolute yaw rate in segment."
        },
        {
          "name": "yaw_rate_var",
          "type": "number",
          "unit": "(deg/s)^2",
          "source": ["irsdk"],
          "definition": "Yaw rate variance (snap/instability proxy)."
        },
        {
          "name": "yaw_vs_ideal_mean",
          "type": "number",
          "unit": "deg/s",
          "source": ["derived_irsdk"],
          "definition": "Mean(YawRate - Speed/Radius) over segment."
        },
        {
          "name": "oversteer_index_peak",
          "type": "number",
          "unit": "deg/s",
          "source": ["derived_irsdk"],
          "definition": "Peak(YawRate - Speed/Radius) in segment."
        },
        {
          "name": "rotation_from_load_score",
          "type": "number",
          "unit": "unitless",
          "source": ["derived_irsdk"],
          "definition": "Score for 'rotation begins before steering': yaw rises during brake release with low steering change."
        }
      ]
    },
    {
      "group": "G7_forces_grip_irsdk",
      "required": false,
      "fields": [
        {
          "name": "lat_accel_peak",
          "type": "number",
          "unit": "m/s^2",
          "source": ["irsdk"],
          "definition": "Peak lateral acceleration."
        },
        {
          "name": "combo_g_peak",
          "type": "number",
          "unit": "m/s^2",
          "source": ["derived_irsdk"],
          "definition": "Peak combined accel: sqrt(lat^2 + long^2)."
        },
        {
          "name": "grip_usage_peak",
          "type": "number",
          "unit": "0-1",
          "source": ["derived_irsdk_session"],
          "definition": "combo_g_peak / maxObservedSessionComboG."
        },
        {
          "name": "grip_usage_mean",
          "type": "number",
          "unit": "0-1",
          "source": ["derived_irsdk_session"],
          "definition": "Mean combined accel normalized by session max."
        },
        {
          "name": "vert_accel_min",
          "type": "number",
          "unit": "m/s^2",
          "source": ["irsdk"],
          "definition": "Min vertical acceleration (unload/crest proxy)."
        },
        {
          "name": "vert_accel_max",
          "type": "number",
          "unit": "m/s^2",
          "source": ["irsdk"],
          "definition": "Max vertical acceleration (compression proxy)."
        }
      ]
    },
    {
      "group": "G8_gear_rpm",
      "required": true,
      "fields": [
        {
          "name": "gear_min",
          "type": "integer",
          "unit": null,
          "source": ["csv", "irsdk"],
          "definition": "Minimum gear in segment."
        },
        {
          "name": "downshift_count",
          "type": "integer",
          "unit": null,
          "source": ["derived"],
          "definition": "Count of downshifts in segment."
        },
        {
          "name": "rpm_peak",
          "type": "number",
          "unit": "rpm",
          "source": ["csv", "irsdk"],
          "definition": "Peak RPM in segment."
        },
        {
          "name": "downshift_near_apex",
          "type": "integer",
          "unit": null,
          "source": ["derived"],
          "definition": "Downshifts in a defined window around apex."
        }
      ]
    },
    {
      "group": "G8_gear_rpm_irsdk_addons",
      "required": false,
      "fields": [
        {
          "name": "gearshift_yaw_spike_score",
          "type": "number",
          "unit": "unitless",
          "source": ["derived_irsdk"],
          "definition": "Correlation/score of gear changes aligning with yaw spikes."
        },
        {
          "name": "gearshift_long_spike_score",
          "type": "number",
          "unit": "unitless",
          "source": ["derived_irsdk"],
          "definition": "Correlation/score of gear changes aligning with longitudinal accel spikes."
        }
      ]
    },
    {
      "group": "G9_compare_deltas",
      "required": false,
      "fields": [
        {
          "name": "delta_time_segment",
          "type": "number",
          "unit": "s",
          "source": ["derived"],
          "definition": "Segment time difference vs reference segment."
        },
        {
          "name": "delta_speed_min",
          "type": "number",
          "unit": "km/h",
          "source": ["derived"],
          "definition": "speed_min(this) - speed_min(ref)."
        },
        {
          "name": "delta_brake_start",
          "type": "number",
          "unit": "LapDistPct",
          "source": ["derived"],
          "definition": "brake_start_dist(this) - brake_start_dist(ref)."
        },
        {
          "name": "delta_throttle_onset",
          "type": "number",
          "unit": "LapDistPct",
          "source": ["derived"],
          "definition": "throttle_onset_dist(this) - throttle_onset_dist(ref)."
        },
        {
          "name": "delta_grip_usage_peak",
          "type": "number",
          "unit": "unitless",
          "source": ["derived_irsdk"],
          "definition": "grip_usage_peak(this) - grip_usage_peak(ref)."
        },
        {
          "name": "delta_oversteer_index_peak",
          "type": "number",
          "unit": "deg/s",
          "source": ["derived_irsdk"],
          "definition": "oversteer_index_peak(this) - oversteer_index_peak(ref)."
        }
      ]
    }
  ],
  "required_groups": [
    "G0_ids_context",
    "G1_geometry_2d_core",
    "G2_speed_profile",
    "G3_brake_core",
    "G4_throttle_core",
    "G5_steering_control",
    "G8_gear_rpm"
  ],
  "optional_groups": [
    "G1_geometry_3d_irsdk",
    "G3_brake_irsdk_quality",
    "G4_throttle_irsdk",
    "G6_rotation_yaw_irsdk",
    "G7_forces_grip_irsdk",
    "G8_gear_rpm_irsdk_addons",
    "G9_compare_deltas"
  ],
  "value_conventions": {
    "boolean": "Use true/false only.",
    "numbers": "Use IEEE float; round only for UI; store full precision in analysis artifacts.",
    "nullability": "IRSDK-only fields MUST be null when IRSDK data is not present."
  }
}


---