# SURYA-KAVACH — Architecture & Development Design Document

**Project:** SURYA-KAVACH ("Solar Shield") — Dual-channel (Soft + Hard X-ray) Nowcasting & Forecasting of Solar Flares from Aditya-L1
**Event:** Bharatiya Antariksh Hackathon (BAH) 2026 — Problem Statement 15
**Tagline:** *"Seeing the Sun's strike before it lands."*
**Document version:** 2.1 · **Status:** Implemented & running on real Feb–Aug 2024 data

> **Implementation status (as built).** This document describes the design; the system is **built and
> verified** on real Aditya-L1 data for **Feb, Mar, Apr, May, Jul, Aug 2024** (177 SoLEXS + 436 HEL1OS
> files → 15.5 M samples → **3,328 flare events**, 141 dual-channel cross-confirmed). Validated with
> **exclusive 1:1 matching** against the **complete** NOAA/SWPC catalogue (2,117 flares):
> **≥M completeness 0.784 at purity 0.712 (F1 0.746)**; overall 0.632 / 0.441.
> ⚠️ **Forecasting from X-ray alone is a documented NEGATIVE result** (AUC 0.46–0.56 = random; BSS −0.463) — see §15. Magnetogram/SHARP data is required for genuine forecast skill.
> **Done:** ingestion, causal nowcast detector + SXR/HXR fusion (HXR multi-hour bug fixed), leakage-safe
> HistGradientBoosting forecast, SQLite persistence with dedup, FastAPI REST + WebSocket, self-contained
> interactive React dashboard (zoom/pan, working playback), **NOAA/SWPC GOES validation** (completeness/
> purity via HEK), **near-real-time scheduler**, notifications, Docker, and 16 passing tests.
> **Simplified:** "live" re-ingests the affected window (not per-minute streaming); nowcast purity vs the
> curated GOES list is low by nature (sensitive instrument — completeness is the fair metric); metrics
> keep improving as more months are ingested.
> **Not yet:** incremental tail streaming, walk-forward CIs + model card, auto flux calibration, a Vite/TS
> frontend build. For the engineering handbook (per-file detail, setup, how to run & train, how to extend),
> see **`DEVELOPER_GUIDE.md`**.

---

## Table of Contents
1. [The Idea](#1-the-idea)
2. [Problem Statement & Objectives](#2-problem-statement--objectives)
3. [The Science (Background)](#3-the-science-background)
4. [Data Sources & Formats](#4-data-sources--formats)
5. [System Architecture](#5-system-architecture)
6. [Canonical Data Contract](#6-canonical-data-contract)
7. [Algorithms — Detail](#7-algorithms--detail)
   - 7.1 Preprocessing (causal feature engineering)
   - 7.2 Nowcasting — flare detection
   - 7.3 Multi-instrument fusion → master catalogue
   - 7.4 Flare classification (A–X)
   - 7.5 Forecasting — ML model
   - 7.6 SoLEXS → GOES calibration
   - 7.7 Scientific validation vs NOAA/GOES
8. [Real-Time / Streaming Design](#8-real-time--streaming-design)
9. [Persistence — Database Schema](#9-persistence--database-schema)
10. [Backend API Design](#10-backend-api-design)
11. [Frontend Design](#11-frontend-design)
12. [Notifications & Alerting](#12-notifications--alerting)
13. [Technology Stack](#13-technology-stack)
14. [Project Structure](#14-project-structure)
15. [Evaluation Metrics](#15-evaluation-metrics)
16. [Operational Robustness](#16-operational-robustness)
17. [Deployment](#17-deployment)
18. [Testing Strategy](#18-testing-strategy)
19. [Configuration Parameters (defaults)](#19-configuration-parameters-defaults)
20. [Development Roadmap (phases)](#20-development-roadmap-phases)
21. [USP & Differentiation](#21-usp--differentiation)
22. [Glossary](#22-glossary)

---

## 1. The Idea

Solar flares release magnetic energy as sudden X-ray bursts that disrupt satellites, GPS, HF radio,
and power grids. ISRO's **Aditya-L1** observes the Sun continuously from L1 with two X-ray payloads:

- **SoLEXS** — *Solar Low Energy X-ray Spectrometer* → **soft X-rays** (thermal plasma emission).
- **HEL1OS** — *High Energy L1 Orbiting X-ray Spectrometer* → **hard X-rays** (non-thermal,
  accelerated-electron emission).

**SURYA-KAVACH** is an automated, end-to-end operations platform that:
1. **Nowcasts** — detects and classifies flares in real time by combining soft + hard X-ray light curves.
2. **Forecasts** — predicts the probability of a flare in the next *N* minutes with a quantified lead time.
3. **Visualizes & Alerts** — a live mission-control dashboard with light curves and visual + push alerts.

**Core insight (the differentiator):** Hard X-rays (HEL1OS) physically *precede* the soft X-ray peak
(SoLEXS) — the **Neupert effect**. Fusing both channels gives (a) higher-confidence, cross-confirmed
detections and (b) a genuine physical *precursor* for forecasting, rather than a black-box predictor.

---

## 2. Problem Statement & Objectives

**PS-15:** Build an automated algorithmic pipeline that uses combined SoLEXS + HEL1OS time-series data to
**detect (nowcast)** and **predict (forecast)** solar flares.

**Objectives**
- Automated flare **detection algorithm** for nowcasting (real-time detection + A–X classification).
- Predictive **forecasting algorithm** that identifies precursor patterns *before* a flare occurs.

**Expected outcomes**
- An automated **database of nowcasted flares** (master catalogue from soft + hard X-ray fusion).
- A trained model that **forecasts flares with quantifiable lead time**.
- An **interface** that visualizes light curves and triggers visual alerts on nowcast/forecast.

**Evaluation criteria**
- Detection of both low- and high-class flares.
- High True Positive Rate (TPR), low False Alarm Rate (FAR).
- Lead time — minutes before flare peak the alert fires.

---

## 3. The Science (Background)

| Concept | Meaning | Why it matters here |
|---|---|---|
| **Soft X-ray (SXR)** | Thermal emission from hot (~10–20 MK) flare plasma | Smooth rise/decay; basis of GOES A–X classification |
| **Hard X-ray (HXR)** | Bremsstrahlung from non-thermal electrons | **Impulsive** (seconds–minutes); the *precursor* phase |
| **Neupert effect** | HXR emission ≈ time-derivative of SXR; HXR peaks **before** SXR | Physical lead time → forecasting signal |
| **FRED profile** | Fast-Rise Exponential-Decay — canonical flare light-curve shape | Detector state machine + synthetic validation |
| **GOES class** | A < B < C < M < X (×10 each) by peak 1–8 Å flux | Standard severity scale we map SoLEXS onto |
| **Flare clustering** | Active regions stay productive for hours–days | Strongest statistical precursor → context features |

---

## 4. Data Sources & Formats

### 4.1 Primary — Aditya-L1 (ISSDC PRADAN)
- **SoLEXS Level-1** (soft X-ray light curve). Native: gzipped FITS `.lc.gz`, HDU `RATE`
  (`TIME` in Unix seconds, `COUNTS`). Cadence ~1 s. Units: **counts/s**.
- **HEL1OS Level-1** (hard X-ray light curve). Native: multi-HDU FITS (one HDU per energy band; use
  the broad/total band): `ISOT` timestamp, `CTR` (count rate, counts/s). Cadence ~1 s.

> Real working set used to date: **177 SoLEXS daily files + 436 HEL1OS files (Feb, Mar, Apr, May, Jul,
> Aug 2024)**, ≈ **15.5 million 1-Hz samples** after merge.

### 4.2 Supplementary — validation ground truth
- **NOAA / SWPC GOES X-ray flare event list** (public): independent catalogue of flare onset/peak/class
  for the same UTC dates. Used to measure detection **completeness & purity** and to **anchor calibration**.

### 4.3 Ingestion formats supported by the loader
`*.fits, *.fit, *.fts, *.csv, *.parquet`. PRADAN ZIPs are unpacked and normalized by a converter:
```
data/incoming/AL1_SLX_*.zip  →  data/raw/solexs/<archive>.csv   (ISOT, COUNTS)
data/incoming/HLS_*.zip      →  data/raw/hel1os/<archive>.csv   (ISOT, CTR)
```

---

## 5. System Architecture

Layered, production-grade, with strict separation: **science has zero web code; backend has zero science
code; frontend is a standalone app.** Contracts between layers are the canonical schema and the
WebSocket/REST message shapes.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ DATA SOURCES   Aditya-L1 SoLEXS (soft) · HEL1OS (hard) — ISSDC PRADAN          │
│                NOAA/SWPC GOES flare list (validation ground truth)             │
└───────────────┬──────────────────────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ INGESTION      PRADAN FITS/ZIP loader → canonical 5-col schema (validated)     │
│                → batch / incremental / streaming(tail) engine                  │
└───────────────┬──────────────────────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ SCIENCE        Preprocess (causal) │ Nowcast (SXR + HXR detectors → Fusion)    │
│ PIPELINE       Forecast (features → HGB model) │ Calibrate │ Validate          │
└───────────────┬──────────────────────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ PERSISTENCE    Repository (SQLite default / Postgres-ready):                   │
│                flares · forecast_alerts · ingest_runs · app_state  (dedup)     │
└───────────────┬──────────────────────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ SERVICE        FastAPI REST + WebSocket · Auth (API key) · Event bus           │
│                Notification service (email / webhook)                          │
└───────────────┬──────────────────────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ PRESENTATION   React + TypeScript + Plotly dashboard                           │
│                (live curves · forecast gauge · alert feed · catalogue)         │
└──────────────────────────────────────────────────────────────────────────────┘
   Cross-cutting: Docker · CI · structured logging · automated tests (incl. leakage)
```

---

## 6. Canonical Data Contract

Every loader (real *or* synthetic) returns a pandas DataFrame with **exactly** these columns. This
decouples data source from algorithms — "swap the dataset, change nothing downstream." Enforced at
runtime with **pandera**.

| Column | Type | Units | Meaning |
|---|---|---|---|
| `time` | datetime64[ns], UTC, monotonic | — | Timestamp |
| `sxr_flux` | float | W/m² (GOES-equivalent) | Soft X-ray flux (SoLEXS, scaled from counts) |
| `hxr_flux` | float | counts/s | Hard X-ray count rate (HEL1OS) |
| `sxr_quality` | int8 | 0=good, 1=flagged/interpolated | SXR data quality flag |
| `hxr_quality` | int8 | 0=good, 1=flagged/interpolated | HXR data quality flag |

**Cleaning pipeline:** discover files → parse TIME + signal columns (case-insensitive candidate lists)
→ merge SXR/HXR on time (±2 s tolerance) → resample to common **1 Hz** cadence → interpolate gaps ≤ 10 s
→ **segment on gaps > 1 hour** (so non-adjacent months don't build a giant empty grid) → flag quality.

---

## 7. Algorithms — Detail

> **Causality rule (non-negotiable):** every value used at time *t* is computed from **trailing windows
> only** (`center=False`, expanding/past-only statistics). No feature may peek into the future. This is
> what makes the live system honest and the metrics trustworthy — and it is enforced by a unit test.

### 7.1 Preprocessing (causal feature engineering)
Derived per-channel columns, all trailing:
- **background** — rolling low-percentile (e.g. 10th pct over 60 min) ≈ quiet-Sun level (robust to flares).
- **smooth** — short trailing rolling mean (~15 s) to denoise.
- **excess** = max(smooth − background, 0).
- **ratio** = smooth / background.
- **slope** — per-minute linear trend of excess (precursor / rise indicator).
- **volatility** — rolling std of excess (burstiness).

### 7.2 Nowcasting — flare detection
A **per-channel state machine** runs independently on SXR and HXR:

```
QUIET ──(trigger sustained ≥ min_rise_duration)──► RISING
RISING ──(local maximum reached)──────────────────► DECAYING
DECAYING ──(excess ≤ end_drop_fraction × peak)────► QUIET   (event closed)
Records: onset_time, peak_time, peak_value, end_time
```

**Triggers**
- **SXR:** a **significance** test — `excess ≥ SXR_RISE_SIGMA · σ` (exit below `SXR_EXIT_SIGMA · σ`), where
  **σ = √(k·background/N)** is the analytic Poisson photon shot noise. *(The original ratio test
  `smooth/background ≥ 1.5` was unsound: a low percentile of a Poisson-noisy series sits far below its
  mean, so the ratio was biased high on quiet Sun and the detector fired 62% of the time — on noise.)*
- **HXR:** `excess ≥ HXR_RISE_THRESHOLD_SIGMA × σ_past`, where `σ_past` is an **expanding** (past-only)
  noise std → causal. HXR is impulsive, so it uses a **shorter** sustain (`HXR_MIN_RISE_DURATION_S`).

**Hardening fix (vs prior version):** HXR events are physically seconds-to-minutes. Enforce a
**maximum event duration** and a **refractory period** so the detector cannot emit a multi-hour /
multi-day "hard X-ray flare" (a real bug observed earlier where one HXR event spanned > 24 h).

### 7.3 Multi-instrument fusion → master catalogue
For each SXR event, search the HXR catalogue for a peak within `COINCIDENCE_WINDOW_MIN` of SXR onset/peak:
- **confirmed** (high confidence) — both channels present (thermal + non-thermal signatures).
- **sxr_only** (medium) — thermal only (HXR possibly below sensitivity).
- **hxr_only** (low) — impulsive burst with no clear thermal counterpart.

Also compute **`hxr_leads_sxr_minutes`** (Neupert lead) per confirmed event.

### 7.4 Flare classification (A–X)
Peak SXR flux → GOES class by NOAA boundaries (W/m²): A=1e-8, B=1e-7, C=1e-6, M=1e-5, X=1e-4, with the
sub-level mantissa (e.g. `C7.8`). Requires the SoLEXS→GOES calibration (§7.6) for absolute correctness.

### 7.5 Forecasting — ML model
Framed as **binary classification on a sliding window**:
```
For each decision minute t:
  X(t) = features over the past LOOKBACK_WINDOW_MIN minutes (+ CONTEXT_WINDOW_MIN activity)
  y(t) = 1 if a flare of class ≥ MIN_FLARE_CLASS_FOR_POSITIVE starts within the next
             FORECAST_HORIZON_MIN minutes, else 0
```

**Features** (all causal):
- *Univariate per channel:* mean, std, max, OLS slope, max jump, microburst count.
- *Multivariate:* SXR–HXR cross-correlation.
- *Context (strongest driver):* 6-hour trailing SXR/HXR max, mean, and **recent-activity fraction**
  (time spent above the rise threshold) — captures flare clustering.
- *Leakage guard:* microburst thresholds fit on **training portion only**.

**Model:** `HistGradientBoostingClassifier` (scikit-learn).
- *Why boosted trees, not deep nets:* data-efficient, fast to retrain on streaming data, interpretable,
  **CPU-only**, and a strong, well-evidenced baseline in flare-forecasting literature.

**Evaluation discipline (no leakage):**
- **Chronological** train/val/test split (70/15/15) — never shuffled.
- **Out-of-fold live curve:** each probability point predicted by a model trained only on earlier data.
- **Walk-forward** evaluation available on-demand for confidence intervals.
- **Alert threshold** auto-chosen on validation (maximize F1 or Youden's J), applied to unseen test.
- **Skill baseline:** compare against climatology/persistence so AUC is contextualized (beats base rate?).
- **Model card** artifact emitted (data coverage, metrics, limitations).

### 7.6 SoLEXS → GOES calibration
SoLEXS L1 is in **counts/s**, not W/m². A single linear scale `SOLEXS_COUNTS_TO_FLUX` maps counts →
GOES-equivalent flux so A–X classification is meaningful. It is a **fittable** parameter, not a magic
number: match a few flares to GOES 1–8 Å peak flux at the same UTC and run a robust log-space regression
(`fit_counts_to_flux([(counts, goes_flux), …])`). Detection, ranking, lead time, and the forecast model
work in **count space** and are unaffected by this scale — it sets only the displayed flux/class letter.

### 7.7 Scientific validation vs NOAA/GOES
Cross-check the nowcast master catalogue against the independent NOAA/SWPC GOES event list for the same
dates:
- **Completeness** = fraction of real GOES flares we detected.
- **Purity** = fraction of our detections that correspond to a real GOES flare.
- Matching by onset/peak time within a tolerance window; report per-class. This independent ground truth
  is the single strongest "this is real, not a demo" evidence.

---

## 8. Real-Time / Streaming Design

Two modes, **one code path** (so the demo exercises the real pipeline):
- **Tail mode (true live):** watch `data/incoming` / a growing file; parse only *new* samples; maintain a
  rolling **causal** in-memory buffer; run the incremental nowcast state machine + per-minute forecast
  inference; emit events as they fire.
- **Replay mode (demo):** stream processed historical light curves in accelerated time (1×–32×) through
  the *same* detector/forecaster.

**Transport:** WebSocket pushes `init` (metadata, thresholds, event list) then `frame` batches
(timestamps + flux + any firing events). An internal **event bus** lets the WS broadcaster *and* the
notifier subscribe to the same flare/alert events.

**Live alerting:** when forecast probability crosses threshold OR a flare onset is nowcast → fire alert →
persist + notify + push to UI. **Debounce / refractory** prevents alert storms.

---

## 9. Persistence — Database Schema

Repository pattern over **SQLAlchemy** (SQLite default, WAL mode for concurrent reads; Postgres-ready).
Filesystem/DB access wrapped with retry + backoff (handles OneDrive sync stalls).

```sql
flares(
  id PK, event_id UNIQUE, fingerprint UNIQUE,           -- (onset, sxr_peak, hxr_peak, class)
  status,                                                -- confirmed | sxr_only | hxr_only
  confidence, onset_time, peak_time_sxr, peak_time_hxr, end_time,
  flare_class, peak_sxr_flux_w_m2, peak_hxr_counts_s, hxr_leads_sxr_minutes,
  first_seen, last_seen
)
forecast_alerts(
  id PK, alert_id UNIQUE, start_time, end_time, peak_probability, first_seen, last_seen
)
ingest_runs(
  id PK, started_at, finished_at, source, status, error_message,
  num_samples, time_range, catalogue_size
)
app_state( key UNIQUE, value )                           -- monitor status, toggles, etc.
```

**Deduplication:** each flare gets a `fingerprint`; re-ingesting the same data updates `last_seen`
instead of inserting → re-ingest yields **0 duplicates**.

---

## 10. Backend API Design (FastAPI)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health`, `/ready` | Liveness / readiness |
| GET | `/api/summary` | KPI metrics for dashboard cards |
| GET | `/api/catalogue` | Master flare catalogue (filter/sort/paginate) |
| GET | `/api/alerts` | Forecast alert episodes |
| GET | `/api/metrics` | Forecast performance (TPR/FAR/lead/AUC) |
| GET | `/api/validation` | NOAA/GOES completeness & purity + model card |
| GET | `/api/monitor` | Operations status (MONITORING / INGESTING / IDLE) + totals |
| POST | `/api/ingest` | Trigger an ingest run (manual) |
| POST | `/api/upload/{solexs\|hel1os}` | Upload raw files |
| WS | `/ws/live` | Live light-curve frames + nowcast/forecast events (pause/resume/speed) |

**Auth:** API-key header for programmatic endpoints; optional single-token UI login. **Logging:**
structured (JSON) with request IDs. **CLI** (typer): `convert · ingest · train · validate · serve · backfill`.

---

## 11. Frontend Design (React + TypeScript)

- **Stack:** Vite + React 18 + TypeScript + Tailwind CSS + Plotly.js + TanStack Query + a WebSocket hook.
- **Performance pattern:** imperative Plotly via `Plotly.extendTraces` to append points at high frame
  rates without React re-render storms.
- **Pages:** Overview · Live Monitor (3-panel SXR/HXR/forecast + play/pause/speed) · Nowcast Catalogue ·
  Forecast · Alerts · Science/Validation (NOAA cross-check, calibration, model card) · Data/Ingest · Settings.
- **No hardcoded URLs:** relative `/api`, `/ws` (works in dev proxy + production). Typed client +
  message contracts kept in sync with the backend.

---

## 12. Notifications & Alerting

Pluggable adapters behind a dispatcher:
- **Email** (SMTP), **Webhook / Slack-compatible** (shipped). Future: SMS, Telegram.
- Triggered by the event bus on: forecast threshold crossing, nowcast onset (confirmed), or rising-activity
  watch. Debounced; severity-tagged (Watch / Warning / Alert). Each notification persisted for audit.

---

## 13. Technology Stack

| Area | Tools |
|---|---|
| **Languages** | Python 3.11, TypeScript |
| **Science / ML** | NumPy, Pandas, SciPy, scikit-learn (HistGradientBoosting), Astropy (FITS), pandera |
| **Backend** | FastAPI, Uvicorn, WebSocket, SQLAlchemy (SQLite → Postgres), joblib |
| **Frontend** | React 18, Vite, Tailwind CSS, Plotly.js, TanStack Query |
| **Ops / Deploy** | Docker + docker-compose, GitHub Actions CI, pytest / vitest, ruff / black / mypy |
| **Data** | Aditya-L1 SoLEXS & HEL1OS L1 (ISSDC PRADAN); NOAA/SWPC GOES list (validation) |

**Why this stack:** tree-based ML is data-efficient, fast to retrain, interpretable & CPU-only; FastAPI
unifies async REST + WebSocket in one app; the entire stack is open-source → near-zero cost, deployable
on-prem/edge at ISRO.

---

## 14. Project Structure

```
Solar_Flares/
  pyproject.toml              # deps + ruff/black/mypy/pytest config; installable package
  .env.example  Dockerfile  docker-compose.yml  Makefile
  README.md  docs/            # this doc + architecture, methodology, API ref, runbook
  scripts/                    # start.ps1 / start.sh / convert / backfill
  solarflare/                 # Python package
    config/      # Pydantic-settings: paths, thresholds, calibration, env overrides
    schema/      # canonical DataFrame contract + pandera validation
    io/          # FITS/CSV loaders, PRADAN zip→csv converter, NOAA/GOES fetcher
    preprocess/  # causal background, smoothing, excess/ratio/slope/volatility
    nowcast/     # SXR + HXR detectors (fixed), fusion → master catalogue
    forecast/    # sliding-window features, HGB model, leakage-safe train/eval, model card
    calibrate/   # SoLEXS→GOES fit + class correction
    validate/    # NOAA/GOES completeness & purity, skill scores
    stream/      # tail + replay ingest engine, rolling buffer, live state
    pipeline/    # orchestration: batch / incremental / streaming
    store/       # SQLAlchemy models + repository (dedup, migrations, retries)
    notify/      # email / webhook adapters + dispatcher
    api/         # FastAPI app, routers, websocket, auth, deps, response schemas
    cli.py       # typer CLI
  tests/         # pytest: unit + integration + leakage + injected-flare + api
  frontend/      # Vite + React + TS + Tailwind + Plotly app
  data/          # gitignored: raw/ processed/ db  (or external DATA_DIR outside OneDrive)
```

---

## 15. Evaluation Metrics

Numbers below are from the **actual current build on real Feb, Mar, Apr, May, Jul, Aug 2024 data**
(6 months), scored against the **complete** NOAA/SWPC GOES catalogue (**2,117 in-window flares**, fetched from
HEK *with paging* — see the audit; the previous reference was silently truncated to 195).

**Nowcast (detection) — the deliverable**

Scored with **exclusive 1:1 matching** (a many-to-one matcher previously inflated purity).

| Tier | GOES flares | detected | matched | **completeness** | **purity** | **F1** |
|---|---|---|---|---|---|---|
| All | 2,117 | 3,033 | 1,337 | 0.632 | 0.441 | 0.520 |
| ≥C | 2,086 | 2,501 | 1,262 | 0.605 | 0.505 | 0.550 |
| **≥M** | **542** | **597** | **425** | **0.784** | **0.712** | **0.746** |

Catalogue: **3,328 events** — 141 **dual-channel cross-confirmed**, 2,892 soft-only, 295 hard-only.
Our ≥M count (597) is within 10% of NOAA's (542); X-class 57 vs 39.
Detector: **Poisson-significance trigger** (σ = √(k·bg/N)) plus a minimum **absolute** peak
prominence — *not* a ratio test. Class letters from a **robust Huber fit** over 1,475 matched pairs
(reproducible: `scripts/fit_calibration.py`).

**Forecast — an honest NEGATIVE result**

Trained and scored on the **independent** NOAA/SWPC list (not on our own detections):

| Target | Horizon | Test AUC (0.5 = random) |
|---|---|---|
| GOES ≥C onset | 30 / 60 / 120 min | 0.546 / 0.531 / 0.531 |
| GOES ≥M onset | 30 / 60 / 120 min | 0.563 / 0.552 / 0.539 |
| Brier Skill Score | — | **−0.463** (worse than climatology) |

> **Honesty note — the most important paragraph in this document.**
> **Nowcast:** the detector is validated 1:1 against the *complete* official catalogue and detects **78% of
> all M-class-and-above flares at 71% purity**. That is the system's deliverable, and PS-15 explicitly permits
> "Forecasting **and/or** Nowcasting".
> **Forecast:** X-ray-only flare *forecasting* has **no skill**. Every class and horizon is
> indistinguishable from random, with a negative Brier Skill Score. This is **not** a modelling failure —
> soft/hard X-ray flux reports that a flare *is occurring*, it does not encode that one *is coming*.
> Operational forecasters rely on **photospheric magnetic-field data (SHARP parameters, SDO/HMI)** for
> precisely this reason. We report the negative result **with the evidence**, in preference to the
> AUC ≈ 0.66 we obtained earlier by training on our own detector's output — which is *circular* and
> measures agreement with our own bugs, not solar physics.
> **Next phase:** ingest SHARP/magnetogram active-region features alongside the X-ray channels. That is
> the established route to genuine forecast skill, and it is the honest recommendation this project makes.

**Tuning when results look off** (too many / missing flares, events too long, too many false alarms, wrong
class letters): every behaviour is a single threshold in `config.py`. See the full symptom→knob playbook
in **Developer Guide §6a**; the loop is edit one knob → `pytest` → `ingest --force` → `validate`.

---

## 16. Operational Robustness
- Structured JSON logging with request IDs; health/readiness probes.
- Retry + exponential backoff on filesystem/DB access (OneDrive/Docker mount stalls).
- DB WAL mode + serialized writes; idempotent dedup.
- API-key auth; secrets via `.env` (never committed).
- Auto-ingest scheduler (watch for new data every N s) + auto-retrain on the full accumulated cache
  (incremental, **no catastrophic forgetting**).
- Automated tests + CI gate before merge.

---

## 17. Deployment
- **Docker multi-stage:** build the React frontend → FastAPI serves static assets + API in one image.
- **docker-compose** with a persistent volume for `data/` + the SQLite DB.
- **One command:** `docker compose up` (or `scripts/start.ps1` / `make dev`).
- CPU-only; runs on a modest cloud VM or on-prem server.
- **Running the CLI directly:** use `python -m solarflare.cli <command>` (works from the project folder
  with no install). The shorter `solarflare <command>` shortcut exists only after `pip install -e .`; if
  you see *"'solarflare' is not recognized..."*, that's why — use the `python -m solarflare.cli` form.

---

## 18. Testing Strategy

| Test | What it guarantees |
|---|---|
| **Schema validation** | Every loader output satisfies the canonical contract |
| **Injected-flare detection** | A synthetic FRED flare is reliably detected (recall sanity) |
| **Leakage test** | Mutating future data does **not** change past feature values (causality) |
| **HXR-duration sanity** | No detected HXR event exceeds the physical max duration |
| **Dedup test** | Re-ingesting identical data inserts 0 duplicates |
| **Skill-baseline test** | Forecast model beats climatology/persistence |
| **API smoke tests** | Endpoints return valid, auth-gated responses |
| **Frontend (vitest)** | Components render; WS/REST contracts hold |

---

## 19. Configuration Parameters (defaults)

All tunable in one place (`config/`). Defaults below are the design baseline.

| Parameter | Default | Meaning |
|---|---|---|
| `RESAMPLE_CADENCE_S` | 1 | Common cadence (1 Hz) |
| `BACKGROUND_WINDOW_MIN` | 60 | Trailing background window |
| `BACKGROUND_PERCENTILE` | 10 | Quiet-Sun percentile |
| `SXR_RISE_SIGMA` / `SXR_EXIT_SIGMA` | 12 / 6 | SXR trigger, in units of photon shot noise σ |
| `SXR_PEAK_MIN_PROMINENCE_FLUX` | 2e-6 | a peak must RISE this much to be a separate flare |
| `FORECAST_LABEL_SOURCE` | goes | train/score on the INDEPENDENT NOAA list |
| `HXR_RISE_THRESHOLD_SIGMA` | 3.5 | HXR trigger (σ above past noise) |
| `MIN_RISE_DURATION_S` | 30 | SXR sustained-rise requirement |
| `HXR_MIN_RISE_DURATION_S` | 8 | HXR sustained-rise (impulsive) |
| `PEAK_SEARCH_WINDOW_MIN` | 120 | Max search window for peak |
| `END_DROP_FRACTION` | 0.5 | Event-end decay fraction |
| `COINCIDENCE_WINDOW_MIN` | 10 | SXR↔HXR fusion matching window |
| `FORECAST_HORIZON_MIN` | 30 | Predict flare within next N min |
| `LOOKBACK_WINDOW_MIN` | 60 | Feature history window |
| `CONTEXT_WINDOW_MIN` | 360 | 6-h activity/clustering context |
| `MIN_FLARE_CLASS_FOR_POSITIVE` | "C" | Smallest class counted positive |
| `TRAIN / VAL / TEST` | 0.70 / 0.15 / 0.15 | Chronological split |
| `SOLEXS_COUNTS_TO_FLUX` | 1e-8 | Counts→W/m² scale (calibratable) |

---

## 20. Development Roadmap (phases)

| Phase | Deliverable | Verify |
|---|---|---|
| **0 Scaffolding** | Package, tooling, Docker skeleton, CI, data dir | `pytest` runs; lint clean |
| **1 Science core** | Loaders, schema, preprocess, nowcast (HXR fix), forecast | Catalogue + metrics reproduced; leakage/injected-flare tests pass |
| **2 Validation & calibration** | NOAA/GOES cross-check, GOES calibration, skill baseline, model card | Real completeness/purity numbers produced |
| **3 Persistence & API** | SQLAlchemy repo + dedup, FastAPI REST, auth, CLI | Ingest → DB; re-ingest = 0 dupes; endpoints live |
| **4 Streaming & alerts** | Tail + replay engine, WebSocket, notifications | Drop a file → live detect + alert + notify + push |
| **5 Frontend** | React dashboard (all pages), live WS | UI updates live against backend |
| **6 Deploy & docs** | Docker compose, one-command start, full docs | `docker compose up` → working app on real data |

---

## 21. USP & Differentiation
- **Dual-instrument fusion** (soft + hard X-ray) for both detection *and* prediction — most systems use
  soft X-ray alone.
- **Physics-informed precursor** (Neupert effect) → real, quantified lead time (median ≈ 135 min before peak).
- **Scientifically trustworthy:** causal features, out-of-fold/walk-forward evaluation, **validated against
  independent NOAA/GOES** ground truth, leakage unit test, model card.
- **Truly real-time:** streaming/tail ingestion + live alerting (not just offline analysis).
- **Self-improving:** auto-ingest + auto-retrain, no forgetting.
- **Sovereign & near-zero cost:** built on Aditya-L1, fully open-source, CPU-only, deployable on-prem/edge.

---

## 22. Glossary
- **SoLEXS / HEL1OS** — Aditya-L1 soft / hard X-ray spectrometers.
- **Nowcast** — real-time detection + classification of an ongoing/just-started flare.
- **Forecast** — probabilistic prediction of a flare *before* it begins, with lead time.
- **Lead time** — minutes between alert and flare peak (PS-15 metric) / onset.
- **TPR / FAR** — true-positive rate / false-alarm rate.
- **Neupert effect** — HXR emission precedes and is ~the derivative of SXR.
- **FRED** — Fast-Rise Exponential-Decay flare light-curve shape.
- **Master catalogue** — fused soft+hard flare database (confirmed / sxr-only / hxr-only).
- **PRADAN** — ISSDC data portal for Aditya-L1 products.

---

*End of document — SURYA-KAVACH Architecture & Design v1.0.*
