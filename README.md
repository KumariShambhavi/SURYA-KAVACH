# SURYA-KAVACH — Aditya-L1 Solar Flare Operations Center

Dual-channel (SoLEXS **soft** + HEL1OS **hard** X-ray) **solar-flare nowcasting** from ISRO's
Aditya-L1, built for **Bharatiya Antariksh Hackathon 2026 — Problem Statement 15**
(*"Forecasting **and/or** Nowcasting of Solar Flares…"*).

It ingests raw ISSDC PRADAN data, **detects and classifies flares (A–X)**, fuses the soft + hard
X-ray catalogues into cross-confirmed events, **validates against the complete official NOAA/SWPC
GOES catalogue**, persists everything to a database, and serves a live mission-control dashboard
with a near-real-time scheduler.

> ⭐ **New here / on the team? START WITH [`docs/KNOWLEDGE_TRANSFER.md`](docs/KNOWLEDGE_TRANSFER.md)** —
> the full KT: what the system does, the science, the methodology, every file, what's done, what's left.
> Then [`docs/DEVELOPER_GUIDE.md`](docs/DEVELOPER_GUIDE.md) for setup/tuning detail.
> Full design + diagrams: [`docs/SURYA-KAVACH_Architecture_and_Design.md`](docs/SURYA-KAVACH_Architecture_and_Design.md) (`.docx`).
> **Read [`docs/Final_Project_Audit/`](docs/Final_Project_Audit/) for the full adversarial audit,
> including the negative result below and the bugs it uncovered.**

### Headline result (validated, real data)

> **Detects 78% of all M-class-and-above solar flares at 71% purity (F1 = 0.75)** — scored with
> **exclusive 1:1 matching** against the **complete NOAA/SWPC GOES catalogue** (2,117 flares,
> Feb–Aug 2024), on 6 months of real Aditya-L1 data (15.5 M samples).
> 141 events are **dual-channel cross-confirmed** (soft *and* hard X-ray).

| Tier | GOES flares | detected | matched | **completeness** | **purity** | **F1** |
|---|---|---|---|---|---|---|
| All | 2,117 | 3,033 | 1,337 | 0.632 | 0.441 | 0.520 |
| ≥C | 2,086 | 2,501 | 1,262 | 0.605 | 0.505 | 0.550 |
| **≥M** | **542** | **597** | **425** | **0.784** | **0.712** | **0.746** |

Our ≥M count (597) is within 10% of NOAA's (542); X-class 57 vs 39. Class letters come from a
**robust Huber fit** of `goes_flux = k·counts + b` over **1,475 time-matched pairs** — reproducible
via `scripts/fit_calibration.py` (pairs written to `outputs/calibration_pairs.csv`).

### ⚠️ Honest negative result: X-ray-only *forecasting* has no skill

We built a forecaster, then tested it **properly** — trained and scored on the **independent**
NOAA/SWPC flare list rather than on our own detections. The result:

| Target | Horizon | Test AUC (0.5 = random) |
|---|---|---|
| GOES ≥C onset | 30 / 60 / 120 min | 0.546 / 0.531 / 0.531 |
| GOES ≥M onset | 30 / 60 / 120 min | 0.563 / 0.552 / 0.539 |

**Every configuration is indistinguishable from random** (Brier Skill Score **−0.463** — worse than
climatology). This is **not** a modelling failure: *soft and hard X-ray flux tell you a flare is
happening, not that one is coming.* Operational flare forecasters use **photospheric magnetic-field
data** (e.g. SHARP parameters from SDO/HMI) for exactly this reason.

We report this as a **rigorous negative result with the evidence attached**, rather than the
inflated numbers we would have obtained by training on our own detector's output (which is circular
and produced a misleading AUC of 0.66 — see the audit).

**Next phase:** ingest active-region magnetogram features (SHARP) alongside the X-ray channels —
that is the established route to genuine forecast skill.

### Status (as built)
- ✅ PRADAN ingestion · causal preprocessing · **Poisson-significance nowcast detector** · SXR+HXR
  fusion · A–X classification (GOES-anchored calibration) · **validation vs the complete NOAA list**
  · SQLite persistence with dedup · FastAPI REST + WebSocket · live dashboard · scheduler ·
  notifications · Docker · 16 passing tests.
- 🟡 "Live" = near-real-time (re-ingests the affected window, not per-minute streaming).
- ⬜ Not yet: SHARP/magnetogram forecasting, walk-forward CIs + model card, incremental streaming.
  See the Developer Guide §14 and the audit's roadmap.

---

## Quick start

```powershell
# from the project root (Windows)
pip install -e .                         # or: pip install -r the deps in pyproject
python -m solarflare.cli convert         # raw PRADAN *.zip in this folder -> data/raw/*.csv
python -m solarflare.cli ingest --force  # build catalogue + train forecaster -> data/flare_ops.db
python -m solarflare.cli serve           # dashboard at http://127.0.0.1:8000
```

Or one command: `./scripts/start.ps1` (Windows) / `./scripts/start.sh` (macOS/Linux).
Or Docker: `docker compose up --build` → http://localhost:8000.

> **⚠️ "`solarflare` is not recognized as ... a cmdlet/program"?**
> The short `solarflare` command only exists **after** `pip install -e .` registers it on your PATH. If
> you see that error, either run `pip install -e .` once, **or** just use the always-works module form
> from the project folder: replace `solarflare` with **`python -m solarflare.cli`**.
> Example: `python -m solarflare.cli convert --include 202404`. Both forms do exactly the same thing.

The dashboard is self-contained (React + Plotly are vendored in `frontend/dist/vendor`), so it needs
**no `npm` and no internet**.

---

## Data

Put the PRADAN downloads in the project root (or `data/incoming/`):
`AL1_SLX_L1_*.zip` (SoLEXS soft X-ray) and `HLS_*.zip` (HEL1OS hard X-ray). `convert` unpacks the nested
FITS and writes `data/raw/solexs/*.csv` (ISOT, COUNTS) and `data/raw/hel1os/*.csv` (ISOT, CTR).

The pipeline is **incremental**: it works on whatever window you have and scales to the full multi-year
catalogue as you add more — re-ingesting the same data inserts **0 duplicates**.

Currently loaded & verified: **Feb, Mar, Apr, May, Jul, Aug 2024** (177 SoLEXS + 436 HEL1OS files, ~15.5 M
1-Hz samples → **3,328 flare events**, of which **141 are dual-channel cross-confirmed**).
Ground truth: the **complete** NOAA/SWPC GOES catalogue (2,117 flares) fetched from HEK with paging.

**Tuning / fixing during training** (too many or missing flares, events too long, too many false alarms,
wrong class letters, …): every behaviour is a single number in `solarflare/config.py` — see the full
symptom→knob playbook in **Developer Guide §6a**. The loop is always: edit one knob → `pytest` →
`ingest --force` → `validate`.

---

## How it works (one paragraph)

`io` loads & cleans both channels into one **canonical 1-Hz schema**
(`time, sxr_flux, hxr_flux, sxr_quality, hxr_quality`). `preprocess` adds **causal** (trailing-window)
background / excess / Poisson-noise features. `nowcast` runs a **significance-threshold** detector per
channel (`excess >= 12*sigma`, sigma = the analytic photon shot noise) with hysteresis and peak-splitting,
classifies peaks (A–X), and **fuses** soft + hard detections by temporal coincidence into a master
catalogue (confirmed / sxr-only / hxr-only) with the Neupert lead. `forecast` builds sliding-window
features and trains a leakage-safe **HistGradientBoosting** classifier, evaluated chronologically
(TPR / FAR / lead-time). `store` persists to SQLite (Postgres-ready) with fingerprint dedup; `api`
serves REST + a WebSocket live replay; the React dashboard visualizes it.

Full design: [`docs/SURYA-KAVACH_Architecture_and_Design.md`](docs/SURYA-KAVACH_Architecture_and_Design.md)
(also `.docx` with diagrams).

---

## CLI

> Run as **`python -m solarflare.cli <command>`** (always works from the project folder). The shorter
> `solarflare <command>` form below works only after `pip install -e .`. If you get
> "`solarflare` is not recognized", use the `python -m solarflare.cli` form.

| Command | Purpose |
|---|---|
| `solarflare convert [--source DIR] [--include 202403]` | PRADAN zips → CSVs |
| `solarflare ingest [--force]` | run pipeline → persist to DB |
| `solarflare validate` | cross-check the catalogue against the official GOES list |
| `solarflare status` | operations status (counts, last ingest) |
| `solarflare serve [--host --port]` | run API + dashboard (+ live scheduler) |

## Scientific validation (independent ground truth)

`solarflare validate` (or the dashboard's **Validation** panel) fetches the official **NOAA/SWPC GOES**
flare list from HEK for the loaded date range and reports **completeness** (fraction of real GOES flares
we detected) and **purity** (fraction of our detections that are real), overall and for ≥C / ≥M classes.
Results are cached to `data/external/goes_flares.csv` (works offline after first fetch); you can also drop
your own `data/external/goes_flares_user.csv` (`peak_time,goes_class`) to use a manual reference.

## Going live (near-real-time)

When served (`solarflare serve` or Docker), a background **scheduler** runs every
`SOLARFLARE_CHECK_INTERVAL` seconds: it auto-converts any new PRADAN zips dropped into `data/incoming/`,
re-ingests when the raw data changed, retrains the forecaster, fires alerts, and refreshes the dashboard.
So the operational loop is: **drop new Aditya-L1 files → the system updates itself.** (Aditya-L1 has
downlink/processing latency, so this is near-real-time, bounded by ISSDC availability — not a live stream.)
Disable with `SOLARFLARE_AUTO_INGEST=0`.

## API (selected)

`GET /api/health` · `GET /api/monitor` · `GET /api/summary` · `GET /api/catalogue` · `GET /api/alerts` ·
`GET /api/runs` · `POST /api/ingest` (auth) · `WS /ws/live`.
Set `SOLARFLARE_API_KEY` to require an `X-API-Key` header on mutating endpoints.

## Configuration

All tunables live in `solarflare/config.py`; key ones are overridable via env (see `.env.example`):
`SOLARFLARE_DATA_DIR`, `SOLARFLARE_DB_PATH`, `SOLARFLARE_API_KEY`, `SOLARFLARE_AUTO_INGEST`,
`SOLARFLARE_SXR_SCALE`, and the notification (`SOLARFLARE_WEBHOOK_URL`, `SOLARFLARE_SMTP_*`) settings.

## Team / GitHub workflow

Code is committed; **data is not** (too large — `data/`, `*.zip`, `*.db`, `outputs/` are gitignored).
The dashboard (`frontend/dist/`, vendored React+Plotly) **is** committed, so a clone runs with no `npm`.

A teammate after cloning:
```bash
git clone <repo> && cd Solar_Flares
pip install -e .
# download your own PRADAN zips into the folder (or data/incoming/), then:
python -m solarflare.cli convert --include 202404
python -m solarflare.cli ingest --force      # builds catalogue + trains the model locally
python -m solarflare.cli serve
```
**Training as a team — read this:** a trained model is just the output of *(code + data)* and does **not**
combine across people. Pulling a teammate's code does **not** merge their training into yours. The data is
too big for GitHub (100 MB/file limit), so **share data off-GitHub** (Drive/OneDrive/WhatsApp) and **train
in one place**: split the *converting*, then one person ingests everyone's CSVs together for the full
model. The trained `outputs/forecaster_model.joblib` (~1 MB) **is** committed to Git, so others can `git
pull` the finished model without re-training. Full step-by-step explanation in **Developer Guide §17**.

## Tests

```bash
python -m pytest          # schema, injected-flare detection, HXR-duration, leakage/causality,
                          # dedup, API smoke, auth, GOES validation matching
```

## Layout

```
solarflare/   io · preprocess · nowcast · forecast · pipeline · store · ingest · notify · api · cli · synthetic
frontend/dist React + Plotly dashboard (vendored, no build step)
tests/        pytest suite
docs/         architecture & design (md / docx)
scripts/      start.ps1 / start.sh
data/         raw/ processed/ + SQLite DB (gitignored)
```

## Notes & honesty

- **Forecasting from X-ray alone has NO skill — and we prove it.** Trained and scored on the
  **independent** NOAA/SWPC flare list, every target and horizon gives **AUC 0.46–0.56** (random) and a
  **negative Brier Skill Score (−0.463)**. The X-ray channels tell you a flare *is happening*, not that
  one *is coming*. **Real forecast skill requires photospheric magnetic-field data (SHARP/HMI)** — that
  is our next phase. We publish this as a negative result rather than the misleading AUC 0.66 obtained
  by the (circular) practice of training on our own detector's output.
- **The nowcast is the deliverable, and it is validated.** **78% completeness at 71% purity for ≥M**
  (F1 0.75), scored with **exclusive 1:1 matching** (an earlier version double-counted, inflating purity)
  against the **complete** 2,117-flare NOAA/SWPC catalogue. 141 events are dual-channel cross-confirmed.
- **The detector uses a Poisson-significance trigger**, not a ratio test. The original ratio test
  (`smooth / 5th-percentile-background ≥ 1.5`) was biased high on quiet Sun by the Poisson percentile
  effect and fired **62% of the time**. See the audit for the analysis.
- **Our GOES reference was silently truncated** (HEK paginates at 100 events; the fetch never paged), so
  every completeness/purity number prior to the audit was scored against **195** flares instead of
  **2,117** (in-window) — and against a list containing **zero X-class events**. Fixed; all numbers here are post-fix.
- **Class letters are GOES-anchored:** the SoLEXS counts→flux scale (`SOLEXS_COUNTS_TO_FLUX`, default
  2.687e-8) was fit to NOAA/GOES peak fluxes via `solarflare/calibrate`; re-fit per dataset.
  Known limitation: the fit forces a zero intercept, which inflates the quiet-Sun level (audit §12 #2).
