# SURYA-KAVACH — Developer Guide

> ⭐ **Read [`KNOWLEDGE_TRANSFER.md`](KNOWLEDGE_TRANSFER.md) first** — it explains the science, the
> methodology, every file, what's done and what's left. This guide is the setup/tuning reference.

Everything a team member needs to set up, run, ingest data, train the model, understand **every file**,
and extend the system **without help**. Read this top to bottom once; after that use it as a reference.

- **What it is:** an Aditya-L1 dual-channel (SoLEXS soft + HEL1OS hard X-ray) solar-flare **nowcasting +
  forecasting** platform with a live dashboard. Built for ISRO BAH 2026, Problem Statement 15.
- **Where it runs today:** real **Feb, Mar, Apr, May, Jul, Aug 2024** PRADAN data (177 SoLEXS + 436 HEL1OS
  files → 15.5 M samples → **3,328 flare events**, 141 dual-channel cross-confirmed), validated against
  the **complete** NOAA/SWPC GOES catalogue (2,117 flares) with **exclusive 1:1 matching**.
  **Nowcast ≥M: completeness 0.784, purity 0.712 (F1 0.746)**; overall 0.632 / 0.441.
- ⚠️ **Forecasting from X-ray alone has NO skill** — trained/scored on the independent GOES list, every
  target and horizon yields AUC 0.46–0.56 (random) and a **negative** Brier Skill Score. This is a
  documented **negative result**, not a bug: X-ray flux indicates a flare is *occurring*, not that one is
  *imminent*. Genuine forecast skill needs magnetogram/SHARP data — see the roadmap.
  **Read [`Final_Project_Audit/`](Final_Project_Audit/) before trusting any older number in this repo.**

---

## Table of contents
1. [Current status — done / partial / not yet](#1-current-status)
2. [Tools & technologies used](#2-tools--technologies)
3. [One-time setup](#3-one-time-setup)
4. [Getting & ingesting data (step by step)](#4-getting--ingesting-data)
5. [Running it](#5-running-it)
6. [Training / retraining the model](#6-training--retraining-the-model)
7. [Scientific validation (GOES)](#7-scientific-validation)
8. [Going live (the scheduler)](#8-going-live)
9. [Repository map — every folder & file in depth](#9-repository-map)
10. [End-to-end execution flow](#10-end-to-end-execution-flow)
11. [Data & output artifacts (what gets written where)](#11-data--output-artifacts)
12. [Configuration reference](#12-configuration-reference)
13. [API reference](#13-api-reference)
14. [Extending it — tasks you can pick up, and exactly which file to edit](#14-extending-it)
15. [Testing](#15-testing)
16. [Troubleshooting](#16-troubleshooting)
17. [Team / GitHub workflow](#17-team--github-workflow)

---

## 1. Current status

**Done & verified on real data**
- Ingestion: PRADAN ZIP → CSV converter, and CSV → canonical 1-Hz light curve (gap-aware, quality-flagged).
- Nowcast: **Poisson-significance** detector (SXR + HXR) — `excess >= 12*sigma` where sigma is the analytic
  photon shot noise — plus hysteresis, gap-breaking, peak-splitting, A–X classification, SXR/HXR **fusion**
  into a master catalogue, with the **HXR multi-hour-event bug fixed** (max-duration cap).
- Forecast: per-minute causal features + **HistGradientBoosting**, **leakage-safe** chronological
  evaluation (TPR / FAR / lead time), model saved to disk.
- Persistence: SQLite (Postgres-ready) with **fingerprint dedup** (re-ingest = 0 duplicates).
- API: FastAPI REST + WebSocket live replay + optional API-key auth.
- Dashboard: self-contained React + Plotly (vendored, no npm), live monitor + catalogue + alerts +
  **validation** panel.
- **Scientific validation:** completeness/purity vs the official **NOAA/SWPC GOES** list (via HEK).
- **Live scheduler:** auto-converts new zips in `data/incoming/` and auto-ingests.
- Notifications: email + webhook adapters. CLI, Docker, tests (16 passing).

**Partial / simplified (works, but a known simplification)**
- "Live" = **near-real-time** (scheduler re-ingests the affected window). Each ingest **re-computes the
  full window** rather than processing only new minutes — fine for months, slower for years.
- Nowcast **over-detects at C-class** (2,501 detected vs 2,086 real ≥C → purity 0.505). The **M-tier is
  strong** (completeness 0.784, purity 0.712, F1 0.746) and the ≥M count (597) is within 10% of NOAA's (542).
- ⚠️ **Forecasting from X-ray alone has NO skill.** Scored on the independent NOAA/SWPC list, every class
  and horizon gives AUC 0.46–0.56 (random) with a **negative** Brier Skill Score. Documented negative
  result — X-ray flux says a flare *is happening*, not that one *is coming*. Magnetogram/SHARP features
  are required. **Do not quote any pre-audit forecast number** (they came from circular self-labelling).

**Not built yet (good first contributions — see §14)**
- True incremental "tail" streaming (process only new minutes).
- Walk-forward confidence intervals + an auto-generated model card.
- SoLEXS→GOES flux calibration **anchoring** (the fitter exists; it isn't auto-run).
- A full Vite/TypeScript frontend build (current UI is no-build by design).

---

## 2. Tools & technologies

| Area | Tool | Why / where |
|---|---|---|
| Language | **Python 3.11** | whole backend + science |
| Arrays / data | NumPy, Pandas, PyArrow | light curves, features, Parquet |
| Science | SciPy | numerics |
| ML | **scikit-learn** (`HistGradientBoostingClassifier`) | forecaster (`forecast.py`) |
| FITS I/O | **Astropy** | reading SoLEXS/HEL1OS FITS (`io/pradan.py`) |
| Model I/O | joblib | save/load the model |
| API | **FastAPI** + **Uvicorn** | REST + WebSocket (`api/`) |
| DB | **SQLAlchemy 2.0** + SQLite | persistence (`store.py`), Postgres-ready |
| HTTP | httpx | GOES/HEK fetch (`validate.py`) |
| Frontend | **React 18 + Plotly.js** (via `htm`, vendored) | dashboard (`frontend/dist/`) |
| Deploy | Docker + docker-compose | one-image deploy |
| Tests | pytest | `tests/` |

All Python deps are listed in `pyproject.toml`. The frontend needs **no Node/npm** — React & Plotly are
pre-downloaded into `frontend/dist/vendor/`.

---

## 3. One-time setup

```powershell
# 1. clone
git clone <your-repo-url>
cd Solar_Flares

# 2. (recommended) a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows ;  source .venv/bin/activate on mac/linux

# 3. install the package + dependencies
pip install -e .
```

If `pip` is blocked by a corporate TLS proxy (cert error), use:
`pip install -e . --trusted-host pypi.org --trusted-host files.pythonhosted.org`.

**Data location:** by default everything lives in `./data`. Working under OneDrive can cause file-lock
stalls, so for heavy use set a path **outside OneDrive**:
`setx SOLARFLARE_DATA_DIR "C:\solarflare-data"` (reopen the shell afterwards).

Verify the install: `python -m pytest` → should report **16 passed**.

---

## 4. Getting & ingesting data

### 4a. Download from ISSDC PRADAN
1. Log in to **PRADAN** (https://pradan.issdc.gov.in) → Aditya-L1.
2. Download, for the **same dates**, both instruments:
   - **SoLEXS** Level-1: files named `AL1_SLX_L1_YYYYMMDD_v1.0.zip`
   - **HEL1OS** Level-1: files named `HLS_YYYYMMDD_*_lev1_*.zip`
   You need **both** soft and hard X-ray, overlapping in time, or the fusion step has nothing to combine.
3. Put the `.zip` files either in the **project root** or in **`data/incoming/`**.

> Tip: ingest **one contiguous window at a time** (e.g. a month). Mixing far-apart dates is handled
> (the loader segments on >1 h gaps) but a single contiguous window is cleanest.

### 4b. Convert ZIP → CSV
```powershell
python -m solarflare.cli convert --include 202404      # only April 2024 archives
# or: python -m solarflare.cli convert                 # everything found in the source folder
```
This unpacks each ZIP, reads the nested FITS, and writes tidy CSVs to
`data/raw/solexs/*.csv` (`ISOT,COUNTS`) and `data/raw/hel1os/*.csv` (`ISOT,CTR`).

### 4c. Ingest (build catalogue + train + persist)
```powershell
python -m solarflare.cli ingest --force
```
This runs the whole pipeline and writes results to `data/flare_ops.db` and `outputs/`.
Re-running on the **same** data inserts **0 duplicates** (fingerprint dedup).

### 4d. Or just drop files and let it auto-ingest
With the server running (`serve`), drop new `.zip` files into `data/incoming/` — the **scheduler**
converts + ingests them within `SOLARFLARE_CHECK_INTERVAL` seconds (default 60). See §8.

---

## 5. Running it

The whole app is **3 simple steps**: get data in → crunch it → show it. There are 3 matching commands.

```powershell
cd "C:\Users\...\Desktop\Solar_Flares"

# STEP 1 — turn the downloaded ISRO .zip files into clean tables the app can read
python -m solarflare.cli convert

# STEP 2 — crunch: detect flares, train the AI, save everything to the database
python -m solarflare.cli ingest --force

# STEP 3 — open the live dashboard at http://127.0.0.1:8000
python -m solarflare.cli serve
```

**What each command actually does, in plain words:**

| Command | In plain words |
|---|---|
| **`convert`** | Opens each PRADAN `.zip`, digs out the real instrument files inside, and writes simple `CSV` tables to `data/raw/`. (Run once per new batch of zips.) |
| **`ingest --force`** | The big one. Reads **all** the CSVs, stitches them into one second-by-second timeline, **finds & classifies flares** (nowcast), **trains the AI forecaster**, **checks results against the official GOES list**, and **saves** the catalogue + model + summary to the database and `outputs/`. |
| **`serve`** | Starts the website + the background watcher. Open the URL it prints. Leave it running. |
| **`status`** | Prints a quick text status (how many flares, last ingest). |
| **`validate`** | Re-checks the catalogue against the official GOES flare list (completeness / purity). |

**Shortcuts:**
```powershell
.\scripts\start.ps1            # Windows: does convert → ingest → serve automatically
./scripts/start.sh            # mac/linux: same
docker compose up --build      # Docker: one container with API + UI + auto-updater
```
Already ingested once? You can skip straight to `serve`.

> **⚠️ Two ways to run the commands — and a common error.**
> - **`python -m solarflare.cli <command>`** — always works when you're inside the project folder, even
>   without installing anything. **Use this if unsure.**
> - **`solarflare <command>`** — the short form, but it only exists **after** you run `pip install -e .`
>   (that step registers the `solarflare` shortcut on your PATH).
>
> If you run `solarflare convert ...` and get
> *"`solarflare` is not recognized as the name of a cmdlet, function, script file, or operable program"*,
> it just means the shortcut isn't installed. Fix it either way:
> 1. run `pip install -e .` once, **or**
> 2. just use `python -m solarflare.cli convert ...` instead.
>
> Both forms are identical in behaviour. Throughout this guide you can swap one for the other freely.

---

## 6. Training / retraining the model  (in plain language)

**What "training" means here:** teaching the AI what the **hours before a flare** look like, so next time
it can **warn you in advance**. It learns from your real data — the more flare-active months you feed it,
the smarter it gets. (We saw this directly: going from 1 month to 6 months lifted the warning rate **TPR
from ~10% to ~50%** and made the forecaster **beat persistence + climatology** — see the skill scores
printed by `ingest` and stored in the summary.)

**You never run a separate "train" command — training happens automatically inside `ingest`.** When you
run `ingest`, this is what the training part (`forecast.run_forecast`) does, step by step:

1. **Chop time into 1-minute steps.** Each minute is one "question" for the AI.
2. **Describe the recent past at each minute** (these are the "features" — all from the *past only*, never
   the future): how bright the soft & hard X-rays are, whether they're rising or falling, the hard-vs-soft
   relationship (the precursor), and how active the Sun has been over the last 6 hours.
3. **Write the answer key:** mark a minute "1" if a real **C-class-or-bigger flare actually started within
   the next 30 minutes**, else "0". (The answers come from the nowcast catalogue.)
4. **Split the timeline in order — not shuffled:** first **70%** to *learn*, next **15%** to *tune*, last
   **15%** to *test*. Keeping time order means the AI is always tested on a future it has never seen, so it
   can't cheat. (This is the "no leakage" guarantee.)
5. **Learn the patterns:** a gradient-boosted tree model (`HistGradientBoostingClassifier`) studies the
   "describe the past → did a flare follow?" pairs and learns the tell-tale build-up before flares.
6. **Pick the alarm level & grade itself** on the unseen test part — reporting **TPR** (how many real
   flares it warned about), **FAR** (how many alarms were false), and **lead time** (how early).
7. **Save the trained model** to `outputs/forecaster_model.joblib`.

**How to retrain / make it better — the simple recipe:**
```powershell
# 1. add more data (download more months of zips from PRADAN), then:
python -m solarflare.cli convert --include 202405     # e.g. add May 2024
# 2. retrain on EVERYTHING you now have:
python -m solarflare.cli ingest --force
```
Each `ingest` rebuilds the timeline from **all** CSVs in `data/raw/` and **retrains from scratch on the
full set** — so it always learns from everything, and never forgets earlier months. **More months = better
forecasting.** That is the single biggest lever.

**Want to change *how* it learns?** Two files:
- `config.py` — the knobs: `FORECAST_HORIZON_MIN` (predict how far ahead, default 30), `LOOKBACK_WINDOW_MIN`
  (how much past it looks at, 60), `CONTEXT_WINDOW_MIN` (activity window, 360), `MIN_FLARE_CLASS_FOR_POSITIVE`
  (smallest flare worth predicting, C).
- `forecast.py` — the features (`build_features`) and the model settings (`run_forecast`).

---

## 6a. Tuning & fixing during training — the full playbook

Sometimes after `ingest` the results look "off" — too many flares, flares missed, durations too long, too
many false alarms, etc. **Almost everything is fixable by changing one number and re-ingesting.** You do
**not** edit the algorithms; you turn knobs.

### How to change a setting (two ways)
1. **Edit `solarflare/config.py`** (the normal way for detection/forecast knobs). Every threshold lives
   there with a comment. Change the number, save.
2. **Environment variable** — only a *few* values support this (paths + flux scale + auth + scheduler):
   `SOLARFLARE_DATA_DIR`, `SOLARFLARE_DB_PATH`, `SOLARFLARE_SXR_SCALE`, `SOLARFLARE_API_KEY`,
   `SOLARFLARE_AUTO_INGEST`, `SOLARFLARE_CHECK_INTERVAL`, `SOLARFLARE_RUN_SCHEDULER`. **All the detection
   and forecast thresholds below are edited in `config.py`** (they are *not* env-overridable).

### The tuning loop (always the same 4 steps)
```powershell
# 1. edit the number in solarflare/config.py
python -m pytest                       # 2. make sure nothing broke (should stay 16 passed)
python -m solarflare.cli ingest --force   # 3. rebuild catalogue + retrain on all data
python -m solarflare.cli validate         # 4. check completeness/purity; read the ingest summary too
```
The `ingest` printout (TPR, FAR, lead, master_events, status_counts) + the `validate` output are how you
see whether your change helped. A change only takes effect on the **next `ingest --force`**.

### A — Nowcast detection problems (finding flares)

| Symptom you see | What's happening | Knob to change (in `config.py`) | Which way |
|---|---|---|---|
| **Too many tiny "flares"** / catalogue huge | detector too sensitive | `SXR_RISE_SIGMA` (12) | **raise** (e.g. 15) |
| | | `MIN_EVENT_DURATION_S` (120) | **raise** (e.g. 300) |
| | sub-peaks split too eagerly | `SXR_PEAK_PROMINENCE_STD_FRAC` (0.5) | **raise** (e.g. 0.8) |
| **Real flares missed** / completeness low | detector too strict | `SXR_RISE_SIGMA` | **lower** (e.g. 8) |
| | | `SXR_PEAK_PROMINENCE_STD_FRAC` | **lower** (e.g. 0.3) |
| **Events last too long** (hours) | episodes not ending | `SXR_EXIT_THRESHOLD_RATIO` (1.2) | **raise** (e.g. 1.3) |
| | hard ceiling on length | `SXR_MAX_EVENT_DURATION_MIN` (360) | **lower** (e.g. 120) |
| | spanning data holes | `EVENT_GAP_BREAK_S` (300) | **lower** (e.g. 120) |
| **One flare split into pieces** | over-splitting | `SXR_PEAK_MIN_SEPARATION_S` (480) | **raise** (e.g. 900) |
| | baseline drifting up | `BACKGROUND_WINDOW_MIN` (480) | **raise** (e.g. 720) |
| **Two close flares merged into one** | under-splitting | `SXR_PEAK_MIN_SEPARATION_S` | **lower** (e.g. 300) |
| **HXR (hard X-ray) events too long** | impulsive cap | `HXR_MAX_EVENT_DURATION_MIN` (30) | **lower** |
| **Class letters look wrong** (e.g. everything is M/X, or all A/B) | flux scale uncalibrated | `SOLEXS_COUNTS_TO_FLUX` / `SOLARFLARE_SXR_SCALE` | **calibrate** (see C below) |

### B — Forecast problems (predicting flares)

| Symptom | Knob (in `config.py`) | Which way |
|---|---|---|
| **Too many false alarms** (FAR high) | `ALERT_PROBABILITY_THRESHOLD` (0.5) | **raise**, or keep `OPTIMIZE_ALERT_THRESHOLD=True` to auto-pick |
| | `MIN_FLARE_CLASS_FOR_POSITIVE` ("C") | **raise** to "M" (only predict bigger flares) |
| **Misses too many flares** (TPR low) | `ALERT_PROBABILITY_THRESHOLD` | **lower** |
| | add more flare-active months | (re-`convert` + `ingest`) |
| **Want earlier warning** | `FORECAST_HORIZON_MIN` (30) | **raise** (e.g. 45) — note: longer lead is harder |
| **Model "sees" too little/much past** | `LOOKBACK_WINDOW_MIN` (60), `CONTEXT_WINDOW_MIN` (360) | adjust |
| **"insufficient data"** message | the window has too few ≥C flares | ingest more active months |

> TPR and FAR trade off against each other: lowering the alert threshold catches more flares (TPR up) but
> raises false alarms (FAR up). `OPTIMIZE_ALERT_THRESHOLD=True` (default) picks the balance automatically
> on the validation split; set it `False` and set `ALERT_PROBABILITY_THRESHOLD` to force a fixed level.

### C — Fixing the class letters (counts→flux calibration)
SoLEXS is in counts, not GOES W/m². `SOLEXS_COUNTS_TO_FLUX` (default `1e-8`) maps counts→flux so the A–X
letter is meaningful. **Detection, lead time and the forecast are unaffected** (they work in count space) —
this only sets the displayed class letter. To anchor it to reality: match a few of your strong SXR peaks to
the GOES class at the same UTC, then
```python
from solarflare.calibrate import fit_counts_to_flux
scale = fit_counts_to_flux([(peak_counts, goes_flux), ...])   # >= 2 pairs
```
and set `SOLARFLARE_SXR_SCALE=<scale>` (env) or edit `SOLEXS_COUNTS_TO_FLUX` in `config.py`, then re-ingest.

### D — If the code itself needs a change (not just a number)
- New/changed **features** for the forecaster → `forecast.py` (`build_features`).
- Different **model** or model settings → `forecast.py` (`run_forecast`).
- Change **how episodes are split** beyond the knobs → `nowcast.py` (`_split_episode`, `detect_events`).
- After **any** code change, run `python -m pytest` (must stay green) before `ingest`.

> ⚠️ Golden rule: change **one** knob at a time, re-`ingest`, re-`validate`, and compare to the previous
> numbers. Changing several at once makes it impossible to tell what helped.

---

## 7. Scientific validation

```powershell
python -m solarflare.cli validate          # or click "Re-validate" in the dashboard
```
`validate.py` fetches the **official NOAA/SWPC GOES flare list** from **HEK** for the catalogue's date
range, then reports:
- **completeness** = fraction of real GOES flares we detected (recall),
- **purity** = fraction of our detections that match a real GOES flare (precision),
overall and for ≥C / ≥M, matched within ±12 min.

The reference is cached to `data/external/goes_flares.csv` (works offline afterwards). To use your own
reference instead, drop `data/external/goes_flares_user.csv` with columns `peak_time,goes_class`.
Validation also runs automatically (in the background) after each ingest.

---

## 8. Going live

When you `serve` (or run Docker), a background **scheduler** thread starts (`scheduler.py`, launched from
the FastAPI lifespan). Every `SOLARFLARE_CHECK_INTERVAL` seconds it:
1. **auto-converts** any new `.zip` in `data/incoming/` (then moves them to `data/incoming/_converted/`),
2. checks a fingerprint of `data/raw`; if it changed, **re-ingests** (rebuild catalogue + retrain +
   refresh alerts + invalidate the live chart cache).

So the operational loop is **"drop new Aditya-L1 files → the system updates itself."** Aditya-L1 has
downlink/processing latency, so this is **near-real-time**, bounded by ISSDC availability — not a live
socket. Disable with `SOLARFLARE_AUTO_INGEST=0`. The scheduler only runs when
`SOLARFLARE_RUN_SCHEDULER=1` (set automatically by `serve` and Docker; off under tests).

---

## 9. Repository map

Top level:

```
Solar_Flares/
├── solarflare/        the Python package (all backend + science)
├── frontend/dist/     the dashboard (React+Plotly, vendored, served as static files)
├── tests/             pytest suite
├── docs/              this guide + architecture doc (md/docx)
├── scripts/           start.ps1 / start.sh launchers
├── data/              raw/ processed/ incoming/ external/ + SQLite DB   (gitignored)
├── outputs/           pipeline artifacts: catalogues, timeline, model, summary  (gitignored)
├── pyproject.toml     package metadata + dependencies + pytest config
├── Dockerfile         single-image build (API + UI + scheduler)
├── docker-compose.yml run with a persistent ./data volume
├── .env.example       all environment variables, documented
├── .gitignore         ignores data/db/outputs; KEEPS frontend/dist
└── README.md          quickstart (this guide has the depth)
```

### `solarflare/` — file by file

> Reading order that matches data flow: **config → schema → io → preprocess → nowcast → forecast →
> pipeline → store → ingest → validate → notify → scheduler → api → cli**.

**`config.py`** — *single source of truth.* All paths, the canonical column names, every threshold
(detection, forecasting), the flare-class table, and runtime knobs. Most are overridable via
`SOLARFLARE_*` environment variables. `ensure_dirs()` creates the data/output tree;
`default_incoming_dir()` decides where `convert` looks for zips. **Edit here to tune behavior.**

**`schema.py`** — *the data contract.* `validate_light_curve(df)` enforces that every light curve has
exactly `[time, sxr_flux, hxr_flux, sxr_quality, hxr_quality]` with the right dtypes (UTC, monotonic,
0/1 quality). Raises `SchemaError` otherwise. Every loader's output passes through here, so the rest of
the code can trust its input.

**`io/pradan.py`** — *PRADAN ZIP → CSV converter.*
- `read_solexs_lc(path)` reads a SoLEXS `.lc.gz` FITS (TIME=Unix s, COUNTS).
- `read_hel1os_lc(path)` reads a HEL1OS `lightcurve_czt1.fits`, picking the widest energy band (CTR).
- `convert_solexs_archive` / `convert_hel1os_archive` extract one ZIP, write one CSV, clean up temp.
- `convert_all(source_dir, include)` loops over all matching zips. Runs from `cli convert`.

**`io/loaders.py`** — *CSV → canonical light curve.* `build_light_curve()`:
reads all `data/raw/**/*.csv`, parses time, **segments** on >1 h gaps, resamples each segment to a 1-Hz
grid (nearest within ±2 s), interpolates gaps ≤10 s, flags missing/interpolated samples, scales SoLEXS
counts → flux, validates the schema, and caches to `data/processed/light_curve_standardized.parquet`.
`load_raw_frames()` is the lower-level reader. **This is the data backbone.**

**`preprocess.py`** — *causal feature engineering.* `add_features(df)` adds trailing-window (never
future-looking) columns: `sxr_bg/sxr_smooth/sxr_excess/sxr_ratio`, `hxr_bg/hxr_smooth/hxr_excess/
hxr_noise`. Gaps are filled for the rolling math but excess is zeroed where the original sample was
missing, so the detector never fires on fabricated data.

**`nowcast.py`** — *flare DETECTION + fusion.*
- `_hysteresis(enter, exit_)` — Schmitt-trigger active mask (enter high, exit low) so noise doesn't
  fragment a flare and a return to background separates flares.
- `detect_events(...)` — one event per active episode (onset, peak=argmax excess, end), capped at a
  max duration (the HXR fix).
- `detect_sxr` / `detect_hxr` — apply the per-channel thresholds (`config`) + a min-duration filter.
- `classify_flare(flux)` — peak flux → GOES class (e.g. `C7.8`).
- `fuse(sxr, hxr)` — temporal-coincidence match → master catalogue rows
  (`confirmed`/`sxr_only`/`hxr_only`, Neupert lead).
- `run_nowcast(df)` — orchestrates: `add_features` → detect both → `fuse`. Returns master/sxr/hxr +
  features.

**`forecast.py`** — *flare FORECASTING.* `build_features` (per-minute causal features), `build_labels`
(≥C within horizon), `evaluate` (flare-based TPR/FAR/lead), `_best_threshold_f1`, and `run_forecast`
(train chronologically, threshold on val, test metrics, full-timeline probability + alert episodes,
trained model). **Leakage-safe by construction.**

**`calibrate.py`** — *SoLEXS→GOES flux fit.* `fit_counts_to_flux([(counts, goes_flux), ...])` returns the
linear scale; set it via `SOLARFLARE_SXR_SCALE` / `config.SOLEXS_COUNTS_TO_FLUX`. Only affects displayed
flux/class — detection & forecasting work in count space. (Not auto-run yet — see §14.)

**`validate.py`** — *independent GOES cross-check.* `fetch_goes_flares` (HEK query, SWPC source),
`get_reference` (user CSV → cache → live fetch), `validate_catalogue` (completeness/purity by class),
`run_validation(master)`. Network uses TLS with an insecure fallback for corporate proxies.

**`synthetic.py`** — *test/validation data generator.* `generate(hours, flares)` makes a canonical light
curve with injected FRED flares + Neupert HXR pulses + ground-truth list. Used by tests (and demos).

**`pipeline.py`** — *orchestration.* `run_pipeline(save=True)` = build_light_curve → run_nowcast →
run_forecast → write `outputs/` (catalogues, timeline, alerts, model, summary) → return a summary dict
(with `_master` and `_forecast` attached in-memory for the caller).

**`store.py`** — *persistence.* SQLAlchemy models `Flare`, `ForecastAlert`, `IngestRun`, `AppState`;
`engine()` (SQLite, WAL); `upsert_flares` / `upsert_alerts` (fingerprint **dedup**); `record_run`;
`set_state`/`get_state`; read helpers `get_flares`/`get_alerts`/`get_runs`/`summary_counts`. Swap the
engine URL for Postgres.

**`ingest.py`** — *operational entry point.* `raw_fingerprint()` + `has_new_raw_data()` (change
detection); `ingest(force)` (run pipeline → upsert → record run → set status → background GOES validation
→ notify ≥M flares); `run_validation_sync/async`, `get_validation`, `monitor_status`.

**`scheduler.py`** — *the live daemon.* `auto_convert_incoming()` (convert zips in `data/incoming/`),
`_loop()` (every interval: convert + ingest-if-changed + invalidate live cache), `start()/stop()`.
Launched from the API lifespan when `SOLARFLARE_RUN_SCHEDULER=1`.

**`notify.py`** — *outbound alerts.* `dispatch(subject, text)` fans out to webhook (`SOLARFLARE_WEBHOOK_URL`)
and email (`SOLARFLARE_SMTP_*`); `notify_new_flares(flares, min_class)`. Enabled only when configured;
never raises.

**`cli.py`** — *command line.* argparse subcommands `convert / ingest / validate / status / serve` →
call the modules above. `serve` sets `SOLARFLARE_RUN_SCHEDULER=1` then launches Uvicorn.

**`api/app.py`** — *FastAPI app.* Lifespan (init DB + start scheduler); `require_key` auth dependency;
REST endpoints (§13) incl. `GET /api/forecast/timeline` (downsampled probability + flare markers for the
Forecasting page); WebSocket `/ws/live`; serves `frontend/dist` as the UI. Ingest/validate run in
background threads so requests never block.

**`api/live.py`** — *WebSocket replay engine.* `_load_replay_data()` (downsample the processed curve to
~4000 points, align forecast probability + events, cached); `replay_socket(ws)` (stream `init` then
`frame` batches at a client-controlled speed; handles pause/resume/restart/speed); `invalidate_cache()`
(called after each ingest).

### `frontend/dist/`  (the dashboard — real React, no build step)
- **`index.html`** — the shell: the dark theme/CSS, loads the vendored libraries, then loads `app.js`.
- **`app.js`** — the whole React app (written with `htm`, no compiler). Contains:
  - **`api`** — the single API-client object: the *only* place that knows endpoint URLs/shapes; it parses
    times, fills nulls, normalizes fields → **prevents data/API mismatch**.
  - **Components:** `LiveMonitor` (3 shared-X Plotly panels via the WebSocket, with working
    pause/resume/restart/speed + zoom/pan/follow), `Gauge` & `ForecastTimeline` (forecasting), `Kpis`,
    `Catalogue` (sort+filter), `Alerts`, `Validation`, `Analytics`, and `App` (nav + 5-second REST polling).
  - **To change the UI:** edit `app.js` (logic/components) and `index.html` (look/theme).
- **`vendor/`** — pre-downloaded `react`, `react-dom`, `htm`, `plotly.min.js` (UI needs no npm, no internet).
  Committed on purpose.

### `tests/`
- **`test_science.py`** — schema validation, **injected-flare detection**, HXR max-duration, Neupert lead,
  **leakage/causality**, and **GOES validation** matching math.
- **`test_store_api.py`** — **dedup** (re-ingest = 0), catalogue filtering, API smoke, auth.

### `scripts/`
- **`start.ps1` / `start.sh`** — convert (if `data/raw` empty) → ingest (if no DB) → serve.

### `data/` (gitignored)
- `raw/solexs`, `raw/hel1os` — converted CSVs (the loader's input).
- `incoming/` — drop raw zips here for the scheduler (`_converted/` holds processed ones).
- `processed/` — `light_curve_standardized.parquet` (cached canonical curve);
  `window_cache/` is reserved for a future incremental cache (currently unused).
- `external/` — `goes_flares.csv` (GOES cache), optional `goes_flares_user.csv`.
- `flare_ops.db` — the SQLite database.

### `outputs/` (gitignored)
Latest run artifacts (see §11).

---

## 10. End-to-end execution flow

```
cli convert ─► io.pradan.convert_all ─► data/raw/**/*.csv

cli ingest ─► ingest.ingest(force)
   has_new_raw_data?  ─► pipeline.run_pipeline
       io.loaders.build_light_curve ─► canonical 1-Hz DataFrame (+ parquet cache)
       nowcast.run_nowcast ─► preprocess.add_features ─► detect_sxr/detect_hxr ─► fuse ─► master
       forecast.run_forecast ─► build_features ─► build_labels ─► train HGB ─► metrics + timeline + model
       write outputs/*
   store.upsert_flares / upsert_alerts (dedup)  ─► record_run ─► set_state
   run_validation_async (GOES, background)  +  notify_new_flares (≥M)

cli serve ─► uvicorn(api.app) ─► lifespan: store.init_db + scheduler.start
   REST  (reads store + outputs)   WS /ws/live (api.live replay)   static UI (frontend/dist)
   scheduler loop: auto_convert_incoming + ingest-if-changed   (every CHECK_INTERVAL s)
```

---

## 11. Data & output artifacts

| Path | Written by | Contents |
|---|---|---|
| `data/raw/solexs/*.csv` | `convert` | `ISOT, COUNTS` (soft X-ray) |
| `data/raw/hel1os/*.csv` | `convert` | `ISOT, CTR` (hard X-ray) |
| `data/processed/light_curve_standardized.parquet` | `build_light_curve` | canonical 1-Hz light curve |
| `data/external/goes_flares.csv` | `validate` | cached official GOES flare list |
| `data/flare_ops.db` | `store` | tables: `flares`, `forecast_alerts`, `ingest_runs`, `app_state` |
| `outputs/nowcast_master_catalogue.csv` | `pipeline` | fused flare catalogue (the headline output) |
| `outputs/nowcast_sxr_events.csv` / `_hxr_events.csv` | `pipeline` | per-channel detections |
| `outputs/forecast_timeline.csv` | `pipeline` | per-minute flare probability |
| `outputs/forecast_alerts.csv` | `pipeline` | debounced alert episodes |
| `outputs/forecaster_model.joblib` | `pipeline` | the trained model |
| `outputs/pipeline_summary.txt` / `.json` | `pipeline` | human + machine run summary |

---

## 12. Configuration reference

Set via environment variables (see `.env.example`) or edit `solarflare/config.py`.

| Variable / param | Default | Meaning |
|---|---|---|
| `SOLARFLARE_DATA_DIR` | `./data` | where data + DB live (use a non-OneDrive path) |
| `SOLARFLARE_DB_PATH` | `<data>/flare_ops.db` | SQLite path (or a Postgres URL via code) |
| `SOLARFLARE_API_KEY` | empty | if set, mutating endpoints require `X-API-Key` |
| `SOLARFLARE_AUTO_INGEST` | `1` | scheduler ingests on new data |
| `SOLARFLARE_CHECK_INTERVAL` | `60` | scheduler interval (seconds) |
| `SOLARFLARE_RUN_SCHEDULER` | `0` | scheduler on/off (serve & Docker set it to 1) |
| `SOLARFLARE_SXR_SCALE` | `1e-8` | SoLEXS counts→flux calibration scale |
| `SOLARFLARE_WEBHOOK_URL` / `SOLARFLARE_SMTP_*` | empty | notification channels |
| `BACKGROUND_WINDOW_MIN` (config) | `480` | trailing background window (8 h) |
| `SXR_RISE_SIGMA` / `SXR_EXIT_SIGMA` (config) | `12` / `6` | SXR enter/exit, in units of photon shot noise |
| `SXR_PEAK_MIN_PROMINENCE_FLUX` (config) | `2e-6` | a peak must RISE this much to be its own flare |
| `FORECAST_LABEL_SOURCE` (config) | `goes` | train on the INDEPENDENT NOAA list (`self` = circular) |
| `HXR_RISE_THRESHOLD_SIGMA` / `_EXIT_` (config) | `3.5` / `2.0` | HXR enter/exit (σ over noise) |
| `MIN_EVENT_DURATION_S` (config) | `120` | reject sub-2-min blips |
| `SXR_SPLIT_EPISODES` (config) | `True` | split a long episode into one event per local peak |
| `SXR_PEAK_MIN_SEPARATION_S` (config) | `480` | min spacing between split sub-flare peaks (8 min) |
| `SXR_PEAK_PROMINENCE_STD_FRAC` (config) | `0.5` | how prominent a sub-peak must be to split out |
| `EVENT_GAP_BREAK_S` (config) | `300` | break an episode at any >5-min data hole (no gap-spanning events) |
| `SXR_MAX_EVENT_DURATION_MIN` (config) | `360` | hard cap on soft-X-ray event length (6 h) |
| `HXR_MAX_EVENT_DURATION_MIN` (config) | `30` | HXR impulsive cap (the bug fix) |
| `FORECAST_HORIZON_MIN` / `LOOKBACK_WINDOW_MIN` / `CONTEXT_WINDOW_MIN` | `30/60/360` | forecast windows |
| `MIN_FLARE_CLASS_FOR_POSITIVE` (config) | `C` | smallest class counted as a positive label |

---

## 12a. Enabling notifications (Slack / email) — optional

The app sends an alert whenever a **new flare of class M or above** is ingested. This is **off by
default** and the whole system runs fine without it — each channel switches **on only when you set its
environment variables** (in a `.env` file, copied from `.env.example`). No code changes are ever needed.
Handled by `notify.py`; a failed notification never breaks ingest.

**Slack (or any Slack-compatible incoming webhook) — easiest:**
1. In Slack, create an **Incoming Webhook** for the channel you want and copy the URL.
2. In your `.env`, add one line:
   ```
   SOLARFLARE_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
   ```
3. Restart the app. New ≥M-class flares now post to that channel automatically.

**Email (SMTP):** add these to your `.env` (example uses Gmail):
```
SOLARFLARE_SMTP_HOST=smtp.gmail.com
SOLARFLARE_SMTP_PORT=587
SOLARFLARE_SMTP_USER=youraddress@gmail.com
SOLARFLARE_SMTP_PASS=your-app-password        # Gmail: use an "App Password", NOT your login password
SOLARFLARE_SMTP_FROM=youraddress@gmail.com
SOLARFLARE_SMTP_TO=teammate1@example.com,teammate2@example.com   # comma-separated for multiple
```
Restart the app. It connects over STARTTLS on the given port and emails every address in `_TO`.

**Notes**
- You can enable **both** channels at once (each fires independently), or **neither** (default).
- Only `SMTP_HOST` + `SMTP_TO` are strictly required for email; `USER`/`PASS` are needed only if your
  mail server requires login (most do). `FROM` defaults to `surya-kavach@localhost` if omitted.
- To change the minimum class that triggers a notification, edit the `min_class` argument of
  `notify_new_flares` (called from `ingest.py`) — e.g. `"C"` for more alerts, `"X"` for only the biggest.
- To add a new channel (SMS, Telegram, …): write an adapter in `notify.py` and call it inside `dispatch`.

---

## 13. API reference

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/health` | — | liveness |
| GET | `/api/monitor` | — | status, counts, last ingest |
| GET | `/api/summary` | — | monitor + latest run metrics |
| GET | `/api/catalogue?limit=&status=` | — | master flare catalogue |
| GET | `/api/alerts?limit=` | — | forecast alert episodes |
| GET | `/api/runs?limit=` | — | ingest run history |
| GET | `/api/validation` | — | latest GOES completeness/purity |
| GET | `/api/forecast/timeline?limit=` | — | downsampled probability series + flare markers (Forecasting page) |
| POST | `/api/ingest?force=` | key | trigger an ingest (background) |
| POST | `/api/validate` | key | recompute GOES validation (background) |
| WS | `/ws/live` | — | live replay frames + events |

Interactive docs at `/docs` when the server runs.

---

## 14. Extending it

Each item lists the **exact file(s)** to edit.

| You want to… | Do this | File(s) |
|---|---|---|
| **Add more data / a new month** | download zips → `convert --include YYYYMM` → `ingest --force` | (no code) |
| **Make class letters physically exact** | gather a few matched (SoLEXS counts, GOES flux) pairs, run `fit_counts_to_flux`, set `SOLARFLARE_SXR_SCALE`; optionally call it automatically during ingest | `calibrate.py`, `config.py`, `ingest.py` |
| **Detect fewer / more flares (tune sensitivity)** | change `SXR_RISE_SIGMA`, `SXR_PEAK_MIN_PROMINENCE_FLUX`, `BACKGROUND_WINDOW_MIN` | `config.py` |
| **Tune episode splitting** (done) | multi-peak split is implemented; adjust peak separation / prominence | `config.py` (`SXR_PEAK_MIN_SEPARATION_S`, `SXR_PEAK_PROMINENCE_STD_FRAC`) |
| **Better forecasting (features/model)** | add features; tune/replace the classifier | `forecast.py` (`build_features`, `run_forecast`) |
| **Add walk-forward CIs / a model card** | add a `walk_forward()` and write a card artifact in `outputs/` | `forecast.py`, `pipeline.py` |
| **True incremental streaming (efficiency)** | maintain a rolling buffer + process only new minutes | new `stream.py` + wire into `scheduler.py` |
| **More notification channels (SMS/Telegram)** | add an adapter + call it in `dispatch` | `notify.py` |
| **Use Postgres** | change the engine URL (and run a Postgres container) | `store.py` (`engine()`) |
| **Add/secure API endpoints** | add routes; the `require_key` dependency already exists | `api/app.py` |
| **Change the dashboard** | edit components/logic in `app.js`, theme in `index.html` | `frontend/dist/app.js`, `frontend/dist/index.html` |
| **A real Vite/TypeScript frontend** | scaffold a Vite app that builds to `frontend/dist` | new `frontend/` project |

---

## 15. Testing

```powershell
python -m pytest                 # all
python -m pytest tests/test_science.py -k leakage -v
```
Add tests in `tests/`. Use `solarflare.synthetic.generate()` for deterministic light curves with known
flares (no network, fast). The `isolated_store` fixture in `test_store_api.py` shows how to point the DB
at a temp file for isolation.

---

## 16. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `'solarflare' is not recognized as ... a cmdlet/program` | the short command isn't installed — run `pip install -e .` once, **or** use `python -m solarflare.cli <command>` (works from the project folder without installing) |
| `Device or resource busy` deleting the DB | the server holds it — stop `serve` first |
| HEK/GOES fetch fails or is slow | network/corporate cert — it falls back to insecure TLS, then cache; first fetch ~1–2 min, then cached. Or supply `data/external/goes_flares_user.csv` |
| `pip install` cert error | add `--trusted-host pypi.org --trusted-host files.pythonhosted.org` |
| Dashboard blank | ensure `frontend/dist/vendor/*` exist (committed); they're loaded locally |
| Port 8000 in use | `serve --port 8010` |
| Big HEL1OS zips fill disk | converter cleans each temp extract; ensure free space for the largest zip |
| Forecast says "insufficient data" | the window has too few ≥C flares — ingest more/active data |
| OneDrive sync stalls | set `SOLARFLARE_DATA_DIR` to a local (non-OneDrive) folder |

---

## 17. Team / GitHub workflow — please read this fully

This section answers the most common confusion: **"if my teammate trains the model and pushes to GitHub,
will I get their trained model when I pull?"** The honest answer is **no, not automatically** — and this
section explains why and what to do instead. Read it once and you won't need anyone to explain it again.

### 17.1 The single most important idea

A trained model is **just the output of (code + data)**. It is not magic and it does not "accumulate"
across people. Every time someone runs `ingest`, the model is **rebuilt from scratch** using **only the
data sitting on that person's own computer** (everything in their `data/raw/` folder). There is no
merging of "Person A's model" and "Person B's model."

So the thing that actually matters to share is the **data**, not the model. Whoever has all the data can
recreate the full model in a few minutes.

### 17.2 What travels through GitHub, and what does NOT

GitHub only carries what is committed. Our `.gitignore` is set up like this:

| Goes to GitHub (committed) | Stays on your computer only (git-ignored) |
|---|---|
| All code (`solarflare/`, `tests/`, `scripts/`) | `data/` — the raw zips and converted CSVs (too big) |
| The dashboard (`frontend/dist/`) | `*.db` — the database (`flare_ops.db`) |
| All docs (`docs/`, `README.md`) | `outputs/*.csv` and summaries (generated) |
| **The trained model** (`outputs/forecaster_model.joblib`) ✅ | `.env` (secrets), caches, virtual env |

> We **deliberately allow the trained model file through Git** (it's only ~1 MB) so you *can* `git pull`
> the latest model. But see the big warning in 17.4 — this is a convenience, not a substitute for sharing
> data.

**This means:** when a teammate `git push`es after training, GitHub receives their **code** and their
**model file** — but **never their data or their database**. When you `git pull`, you get their code and
their model file, but you do **not** get the months of data they trained on.

### 17.3 The data problem (why data can't go through GitHub)

This is the core limitation, stated plainly:

- The converted data in `data/raw/` is **hundreds of megabytes**. GitHub rejects any single file over
  **100 MB** and is not meant to store large datasets at all.
- The raw PRADAN `.zip` downloads are even bigger (gigabytes).
- So **data must be shared by some other channel** — Google Drive, OneDrive, a shared network folder, or
  even WhatsApp for the smaller CSVs. **There is no way around this.** Git is for code, not data.

### 17.4 So, "Person A trains a few months, Person B trains the rest" — does it combine? NO.

If Person A ingests Feb–Apr and Person B ingests May–Jul, and both push:

- Their **code** merges normally (that's what Git is good at). ✅
- Their **model files do NOT merge.** The model is one file. Whoever pushes **last overwrites** the
  other — you do not get a model that learned from both halves. ❌
- And neither person's **data** went to GitHub at all. ❌

So pulling does **not** give you a "more trained" model. It gives you whichever single model was pushed
last, trained on only that one person's months.

### 17.5 The correct way to train together (do this)

**Split the *converting*, but do the *training* in ONE place.**

1. Everyone shares code normally through GitHub (`push` / `pull` / branches / PRs).
2. Divide the **data** work: e.g. Person A downloads + `convert`s Feb–Apr, Person B does May–Jul.
   Converting just makes CSVs in their own `data/raw/solexs` and `data/raw/hel1os`.
3. Each person zips up **their** `data/raw/solexs` + `data/raw/hel1os` CSV files and sends them to one
   chosen "training" person (via Drive/OneDrive/WhatsApp — **not** GitHub).
4. That one person drops **everybody's CSVs together** into a single `data/raw/` and runs **once**:
   ```powershell
   python -m solarflare.cli ingest --force
   ```
   Now the model is trained on **all months combined** — the real, fully-trained model.
5. (Optional) That person commits the resulting `outputs/forecaster_model.joblib` and pushes. Now everyone
   else can `git pull` to get the finished, all-months model **without re-training** — because we allow
   that one file through Git (17.2).

**In one line:** share *data* off-GitHub and train *centrally*; use GitHub to share the *code* and
(optionally) hand out the *final* model.

### 17.6 Quick checklist for a new teammate

```powershell
git clone <repo-url>
cd Solar_Flares
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .               # registers the short `solarflare` command (optional)
# get the shared data CSVs from the team drive -> put them in data/raw/solexs and data/raw/hel1os
python -m solarflare.cli ingest --force      # builds catalogue + trains model on whatever data you placed
python -m solarflare.cli serve               # open http://127.0.0.1:8000
```
If the team already committed a finished model, you can skip `ingest` and just `serve` to view results —
but you'll still need the shared data if you want to *re-train* or change anything.

---

*Questions this guide doesn't answer? The code is small and commented — start at the file named in §9 for
the feature you care about.*
