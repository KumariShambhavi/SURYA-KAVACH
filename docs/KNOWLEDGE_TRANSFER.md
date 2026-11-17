# SURYA-KAVACH — Full Knowledge Transfer (KT)

**Read this first.** It is the single source of truth for *what the system does, how it works, why it
works that way, what is finished, and what is not*. Written for three readers at once:

- 🔬 **the scientist** — is the physics right?
- 📊 **the researcher** — is the methodology honest?
- 💻 **the coder** — where is the code and how do I change it?

Plain language. No jargon without an explanation.

---

## 0. The 60-second version

**What it is:** a system that reads ISRO Aditya-L1 X-ray data and **finds solar flares** (nowcasting),
then checks its answers against the **official NOAA/SWPC flare catalogue**.

**What works (the deliverable):**
> **It detects 78% of all M-class-and-above solar flares, and 71% of what it flags at that level is
> real.** Measured 1-to-1 against the complete official catalogue (2,117 flares, Feb–Aug 2024).

**What does NOT work (and we say so):**
> **Forecasting a flare *before* it starts, using X-ray data alone, is impossible — we tested it and
> proved it.** AUC 0.531 (0.5 = coin flip). This is a *finding*, not a failure. See §7.

**Problem statement:** PS-15 says *"Forecasting **and/or** Nowcasting"*. Our nowcast satisfies it.

---

## 1. What the system actually does, start to finish

Think of it as a **factory line** with 6 stations. Data goes in one end, a validated flare catalogue
comes out the other.

```
 [1] DOWNLOAD          [2] CONVERT           [3] CLEAN
 PRADAN .zip files  →  unzip, read FITS,  →  put both instruments on one
 (SoLEXS + HEL1OS)     write tidy CSVs       shared 1-per-second timeline
                                                    │
                                                    ▼
 [6] SHOW           [5] CHECK              [4] FIND FLARES
 dashboard,      ←  compare with the    ←  detect + classify (A–X),
 database,          official NOAA           fuse soft+hard X-ray
 REST API           flare list
```

| Station | What happens | Code |
|---|---|---|
| **1. Download** | You get `.zip` files from ISSDC PRADAN (manual — needs your login) | *(you do this)* |
| **2. Convert** | Unzip → read the FITS files inside → write simple CSVs | `io/pradan.py` |
| **3. Clean** | Merge both instruments onto one 1-second timeline; mark gaps | `io/loaders.py` |
| **4. Find flares** | Compute background, spot the flares, classify them, fuse the two channels | `preprocess.py`, `nowcast.py` |
| **5. Check** | Score ourselves against the real NOAA/SWPC flare list | `validate.py` |
| **6. Show** | Save to database, serve an API + live dashboard | `store.py`, `api/`, `frontend/` |

Two commands run the whole thing:

```powershell
python -m solarflare.cli convert --include 202406   # station 2
python -m solarflare.cli ingest  --force            # stations 3-6 (also retrains the model)
python -m solarflare.cli serve                      # open http://127.0.0.1:8000
```

---

## 2. 🔬 THE SCIENCE — how we find a flare (in plain words)

### 2.1 The two instruments

| | **SoLEXS** (soft X-ray) | **HEL1OS** (hard X-ray) |
|---|---|---|
| What it sees | **Hot gas** heated by the flare | **Fast electrons** slamming into the Sun |
| Behaviour | Slow rise, slow decay (minutes–hours) | Sharp spike (seconds–minutes) |
| Used for | **Detecting + classifying** the flare (A–X) | **Cross-confirming** it |
| Reality check | Good, dense data | **~83% of samples are zero** — a genuinely weak signal |

The A–X scale (A < B < C < M < X) is a **×10 ladder** of brightness. C is a small flare, M is medium,
X is big.

### 2.2 How we say "that's a flare"

A flare is a **bump above the normal background**. So:

1. **Smooth** the signal over 15 seconds (removes single-photon jitter).
2. **Estimate the background** = the 5th percentile of the *smoothed* signal over the last 8 hours.
   (i.e. "how quiet has the Sun been lately?" — trailing window only, never looks into the future).
3. **Excess** = smoothed − background. That's "how much brighter than usual".
4. **Trigger** when the excess is **statistically significant**:
   ```
   flare starts when   excess ≥ 12 × σ
   flare ends   when   excess <  6 × σ      (a "Schmitt trigger" — stops flickering)
   ```
   where **σ = √(k · background / N)** — the *shot noise* you'd expect from counting photons.
5. **Split** long busy periods into individual flares at their **peaks**, requiring each peak to
   genuinely **rise** by at least 2×10⁻⁶ W/m² above its neighbours (a real flare *rises*; it doesn't
   just sit high).
6. **Classify** the peak brightness → A/B/C/M/X.
7. **Fuse**: if a HEL1OS burst happens within ±10 minutes of a SoLEXS flare → mark it **confirmed**
   (both instruments agree). Otherwise `sxr_only` or `hxr_only`.

### 2.3 ⚠️ The bug that used to be here (important — this is the story to tell)

The original detector compared the **smoothed** signal to the **5th percentile of the RAW, noisy**
signal. That sounds harmless. It isn't.

Photon counting is **Poisson noise**: with ~60 counts/second, the noise is ±13%. A *low percentile* of a
noisy series sits far **below** its average. So the ratio "smooth ÷ background" was **biased high even
when nothing was happening**.

> **Measured: the detector was above its own trigger threshold 62% of the time — on a quiet Sun.
> It was detecting photon noise.**

That is why it produced 6,600 "flares". Replacing the ratio test with the **significance test** above
fixed it. There is now a test (`test_quiet_sun_produces_no_flares`) that fails if this ever returns.

### 2.4 Calibration (counts → flux)

SoLEXS reports **counts**, but flare classes are defined in **W/m²**. We convert with:

```
flux = k × counts        k = 1.898e-8
```

`k` was **fit to reality**: we time-matched **1,475** of our detected peaks to the official GOES flares
and did a robust (outlier-resistant Huber) fit. Reproduce it any time:

```powershell
python scripts/fit_calibration.py      # writes outputs/calibration_pairs.csv + prints k
```

**Sanity check that it works:** we detect **597 flares ≥M** — NOAA recorded **542**. We detect **57
X-class** — NOAA recorded **39**. Close, in the right ballpark.

---

## 3. 📊 THE RESEARCHER — is the methodology honest?

This section exists because **the easiest way to get impressive numbers is to cheat by accident.**
Here is every trap and how we avoid it.

### 3.1 Ground truth: the NOAA/SWPC GOES flare list

We compare our detections to the **official** flare list, fetched from **HEK**.

> 🔴 **We had a serious bug here.** HEK returns **100 results per page** and sets `overmax: true` when
> there are more. Our code **never asked for page 2.** So our "official list" contained **195 flares
> and ZERO X-class events** — it was missing the **X8.7 flare of 14 May 2024**, the biggest of the
> solar cycle. **Every completeness/purity number was computed against a broken reference.**
>
> Fixed (`validate.py` now pages). The real list has **2,117 flares** in our window.

### 3.2 Matching must be one-to-one

If we split one real flare into 3 detections, a naive matcher counts **3 hits** — rewarding us for
fragmentation. We now use **exclusive greedy 1:1 matching** (`validate.match_one_to_one`): each real
flare can be claimed by **one** detection, and vice versa.

- **Completeness** (recall) = *of the real flares, how many did we find?*
- **Purity** (precision) = *of our detections, how many were real?*

### 3.3 The results (honest, current)

| Tier | Real (GOES) | We found | Matched | **Completeness** | **Purity** |
|---|---|---|---|---|---|
| All | 2,117 | 3,033 | 1,337 | 0.632 | 0.441 |
| ≥C | 2,086 | 2,501 | 1,262 | 0.605 | 0.505 |
| **≥M** | **542** | **597** | **425** | **0.784** | **0.712** |

**Read it as:** *"We catch about 4 out of every 5 significant (M-class+) flares, and about 7 out of 10
of the ones we flag at that level are genuinely real."*

### 3.4 No data leakage

"Leakage" = the model secretly sees the future, so it looks smart in testing and is useless in reality.

- Every feature uses **trailing windows only** (`preprocess.py`, `forecast.build_features`).
- We removed a `.bfill()` that **back-filled a future value into the past** (proven: changing the
  sample at t=5 changed the feature at t=0).
- Train/validation/test are split **chronologically**, never shuffled.
- Tests: `test_preprocess_features_are_causal`, `test_features_are_causal_no_future_leak`.

### 3.5 Never score yourself on your own homework

The forecaster originally learned to predict **its own detector's output**. That is **circular** — it
measures agreement with our own bugs, not the Sun. It produced a flattering **AUC 0.66**.

We now train and score on the **independent NOAA/SWPC list**
(`config.FORECAST_LABEL_SOURCE = "goes"`). The honest number is **AUC 0.531**.

### 3.6 Never predict a moment you never observed

`build_features` puts everything on a continuous 1-minute grid — including **June 2024, a month with
zero data**. Worse, `(NaN > NaN) → False → 0.0` silently **fabricated** a valid-looking feature, so all
**43,200 June minutes got predictions** and were scored against real labels.

Fixed: we now require a real trailing observation (`sxr_lb_mean` present). June contributes **45** rows
instead of 43,200.

---

## 4. 🔬📊 THE HEADLINE NEGATIVE RESULT (this is your strongest slide)

> **We tested X-ray-only flare forecasting properly, and it does not work. Anywhere.**

| Target | Horizon | Test AUC (0.5 = random) |
|---|---|---|
| GOES ≥C onset | 30 / 60 / 120 min | 0.546 / 0.531 / 0.531 |
| GOES ≥M onset | 30 / 60 / 120 min | 0.563 / 0.552 / 0.539 |
| GOES ≥C / ≥M | 3 h / 6 h / 12 h / 24 h | all ≈ random; **persistence beats the model** |

Current build: **AUC 0.531**, **Brier Skill Score −0.463** (below 0 = *worse than just guessing the
average*).

**Why — and this is real physics, not an excuse:**

Flares are **magnetic** events. Energy builds up in twisted magnetic fields in an active region, then
snaps. **X-ray light only appears once the snap has already begun.** So X-ray flux tells you a flare
**is happening** — it cannot tell you one **is coming**.

Every credible operational forecaster uses **photospheric magnetic-field data** (SHARP parameters from
SDO/HMI) — published results reach TSS ≈ 0.76 with those.

**And the decisive point for PS-15:**
> **Aditya-L1 carries no magnetograph.** (Its payloads are VELC, SUIT, SoLEXS, HEL1OS, ASPEX, PAPA and
> MAG — and MAG is an *in-situ* interplanetary magnetometer, not a solar imager.)
> **So flare-onset forecasting from Aditya-L1 alone is fundamentally impossible.**

We also ran an **ablation** nobody had run: **does the hard X-ray channel help forecasting?**
**No.** SXR-only AUC **0.6628** vs dual-channel **0.6610** — adding HEL1OS made it *slightly worse*.
HEL1OS's value is **cross-confirmation in the nowcast**, not prediction.

**➡️ Next phase (the honest recommendation):** ingest **SHARP/HMI magnetogram** features. That is the
established route to genuine forecast skill.

---

## 5. 💻 THE CODER — every file, what it does

```
solarflare/
├── config.py        ⭐ EVERY tunable number lives here. Change behaviour here first.
├── cli.py              the `convert / ingest / validate / status / serve` commands
├── pipeline.py         orchestrator: light curve → nowcast → forecast → save
├── ingest.py           runs the pipeline, writes to the DB, prunes stale rows, locks
│
├── io/
│   ├── pradan.py       PRADAN .zip → FITS → CSV. Picks the HEL1OS 20–40 keV band (best SNR).
│   └── loaders.py      CSVs → one canonical 1-Hz table [time, sxr_flux, hxr_flux, quality flags]
│
├── preprocess.py    ⭐ causal features: smooth, background, excess, Poisson noise (σ)
├── nowcast.py       ⭐ THE DETECTOR: significance trigger, peak splitting, fusion, A–X classes
├── forecast.py         features + labels + HistGradientBoosting + skill baselines
├── calibrate.py        counts → flux fitting helper
├── validate.py      ⭐ fetches the OFFICIAL GOES list (paged!) + 1:1 scoring
├── store.py            SQLite: flares, alerts, runs, state. Dedup + prune.
├── schema.py           validates the canonical table shape
├── synthetic.py        fake-but-realistic data for tests (Poisson noise, gaps)
├── scheduler.py        background thread: auto-convert + auto-ingest new data
├── notify.py           email / webhook alerts (off unless configured)
└── api/
    ├── app.py          FastAPI REST endpoints + security
    └── live.py         WebSocket replay engine for the live chart

frontend/dist/          the dashboard — plain React via CDN-style vendored files. NO npm, NO build.
├── index.html          shell + all CSS
└── app.js           ⭐ the whole UI: 8 tabs, one API client at the top

scripts/
├── build_docx.py       renders the architecture .md → .docx (single source of truth)
├── fit_calibration.py  re-fits the counts→flux scale; writes outputs/calibration_pairs.csv
└── start.ps1 / .sh     one-command start

tests/                  16 tests. See §6 — they now have teeth.
docs/Final_Project_Audit/   the full adversarial audit + evidence
```

### 5.1 The most important file: `config.py`

**You almost never need to touch the algorithms — just the numbers.**

| Knob | Default | What it does |
|---|---|---|
| `SOLEXS_COUNTS_TO_FLUX` | `1.898e-8` | counts → W/m². **Sets every flare's class letter.** |
| `SXR_RISE_SIGMA` / `SXR_EXIT_SIGMA` | `12` / `6` | how significant a bump must be to count as a flare |
| `SXR_PEAK_MIN_PROMINENCE_FLUX` | `2e-6` | how much a peak must *rise* to be its own flare |
| `SXR_PEAK_MIN_SEPARATION_S` | `480` | two peaks closer than 8 min = one flare |
| `BACKGROUND_WINDOW_MIN` | `480` | the trailing 8-hour "how quiet is it normally" window |
| `EVENT_GAP_BREAK_S` | `300` | never let one event span a hole in the data |
| `COINCIDENCE_WINDOW_MIN` | `10` | how close SXR and HXR must be to be "confirmed" |
| `FORECAST_LABEL_SOURCE` | `goes` | **`goes`** = honest. `self` = circular (ablation only). |
| `FORECAST_HORIZON_MIN` | `30` | predict a flare within the next N minutes |

**The tuning loop is always the same:**
```powershell
# 1. change ONE number in solarflare/config.py
python -m pytest                        # 2. still 16 passed?
python -m solarflare.cli ingest --force # 3. rebuild + retrain
python -m solarflare.cli validate       # 4. did completeness/purity improve?
```
A full symptom→knob table lives in **DEVELOPER_GUIDE.md §6a**.

### 5.2 Gotchas that will bite you (learned the hard way today)

| Gotcha | Why it matters |
|---|---|
| **The DB is a *mirror*, not a log** | `ingest` **prunes** events the current detector no longer produces. Before this fix, every re-tune *appended*, and the DB became the union of every detector version ever run — **18,920 rows for a 3,328-event catalogue**, dragging reported purity from 0.71 → 0.23. |
| **`(NaN > x)` is `False`, not `NaN`** | It silently fabricates data. That's how June got 43,200 fake predictions. |
| **pandas 3.0: `.asi8` is microseconds** | Not nanoseconds. A `/1e9` was off by 1000×. |
| **A chart can only break a line at a `NaN`** | Our data has *no rows* across a gap, so Plotly drew a straight line across all of June. We now insert an explicit NaN break. |
| **Restart the server after code changes** | The dashboard 404s on new endpoints otherwise. And **Ctrl+F5** — Edge caches `app.js`. |

---

## 6. Tests — and why they used to be worthless

**16 tests, all passing.** But until today they were **not evidence**:

> The synthetic Sun had noise of **σ/µ = 0.040**. The real instrument is **0.80**. It was **20× too
> quiet to ever trip the detector bug**, so *13/13 tests passed while the detector was firing 62% of the
> time on noise.*

What we changed:
- Synthetic data is now **Poisson** (real photon statistics) and has **gaps + NaNs**.
- 🔑 **`test_quiet_sun_produces_no_flares`** — a quiet Sun must yield **ZERO** detections. *This is the
  test that would have caught the original bug. The fixed detector passes it.*
- **`test_quiet_sun_trigger_duty_cycle_is_tiny`** — must be above threshold <1% of the time (was 62%).
- **`test_preprocess_features_are_causal`** — the old leakage test checked the *wrong module* and
  skipped the exact window where a leak appears.
- Fixed `assert len(master) >= len(truth)` — it **pointed the wrong way** and passed with 6,000 false
  positives.

---

## 7. ✅ WHAT IS DONE

**Science**
- ✅ Poisson-significance detector (replaced an unsound ratio test that fired on noise)
- ✅ Episode splitting at true peaks, with an **absolute** rise requirement
- ✅ Events can never span a data gap; hard 6-hour duration cap
- ✅ SXR↔HXR fusion → confirmed / sxr-only / hxr-only
- ✅ GOES-anchored calibration, **reproducible** (`scripts/fit_calibration.py`, 1,475 pairs)
- ✅ **Complete** NOAA/SWPC ground truth (HEK **paging** fixed: 195 → 2,117 flares)
- ✅ **Exclusive 1:1** matching for completeness/purity

**Research integrity**
- ✅ Leakage removed (`.bfill()`), causal-only features, chronological split
- ✅ Forecast labelled from **independent** GOES, not our own detector
- ✅ No predictions for unobserved time
- ✅ Skill baselines: **persistence** (TSS) + **climatology** (Brier Skill Score)
- ✅ **Ablation**: hard X-ray adds **nothing** to forecasting — documented
- ✅ **Negative result** published with evidence

**Engineering**
- ✅ SQLite with dedup **and pruning** (DB mirrors the catalogue)
- ✅ FastAPI REST + WebSocket; 8-tab dashboard (no npm, no build step)
- ✅ Security: CORS locked, bounded `limit` (the `limit=-1` full-dump hole), constant-time key
  compare, WebSocket origin check, **TLS never silently downgraded**, `.env` actually loads
- ✅ Ingest lock moved *into* `ingest()` (the scheduler used to bypass the endpoint's lock)
- ✅ NaN→`null` sanitising (`/api/summary` used to return a hard 500)
- ✅ Charts break honestly at data gaps
- ✅ Every forecast surface flagged **EXPERIMENTAL** with live (not hardcoded) numbers
- ✅ 16 meaningful tests · docs + `.docx` generated from a single source

---

## 8. ⬜ WHAT IS LEFT

### 🔴 Blocking the submission (yours)
1. **The `.pptx` still contains false claims** — it says the forecaster *"beats persistence &
   climatology"* with a *"135-min lead"*, says **"HELIOS"** (it is **HEL1OS** — the exact instrument in
   the problem statement), and lists a tech stack (TypeScript, pandera, Vite, Tailwind, TanStack,
   vitest) that **does not exist in this repo**. It is the only artifact that contradicts your own code.

### 🟠 The real scientific next step
2. **SHARP / HMI magnetogram features** — the only route to genuine forecast skill (§4).

### 🟡 Worth doing
3. **Confidence intervals** (block-bootstrap by day) on every metric — currently point estimates.
4. **Walk-forward validation** instead of one chronological split.
5. **Model card** + committed feature importances (explainability).
6. **More HEL1OS for July/August** — you have only **25 and 7 files**; that is why only 141 events are
   dual-confirmed. This is the single most valuable download.
7. **June 2024** (both instruments) — the one calendar hole.

### ⚪ Engineering polish
8. Docker: put `outputs/` on the volume, run as non-root, pin dependencies, add a healthcheck.
9. `store.upsert_flares` is **N+1** (one SELECT per row) — batch it.
10. API pagination; cache the 8 MB forecast-timeline CSV instead of re-parsing per request.
11. Accessibility: keyboard-navigable nav, contrast fixes (currently WCAG-A failures).
12. True incremental streaming (today every ingest recomputes the full window).

---

## 9. Where to look when something is wrong

| Symptom | Look at |
|---|---|
| Too many / too few flares | `config.SXR_RISE_SIGMA`, `SXR_PEAK_MIN_PROMINENCE_FLUX` → §5.1 |
| Class letters look wrong | `scripts/fit_calibration.py`, `config.SOLEXS_COUNTS_TO_FLUX` |
| Completeness/purity look odd | `validate.py` — is the GOES cache complete? (`data/external/goes_flares.csv`) |
| Dashboard numbers ≠ database | Restart the server; **Ctrl+F5**. Then check `/api/stats/class-distribution`. |
| DB row count ≠ catalogue | Pruning failed — see `ingest._ingest_locked` |
| `'solarflare' is not recognized` | Use `python -m solarflare.cli <cmd>` |
| Port 8000 in use | An old server is running: `netstat -ano \| findstr :8000` → `taskkill /PID <pid> /F` |

**Full audit + evidence:** [`Final_Project_Audit/`](Final_Project_Audit/)
**Per-file deep dive, setup, tuning playbook:** [`DEVELOPER_GUIDE.md`](DEVELOPER_GUIDE.md)
**Design + diagrams:** [`SURYA-KAVACH_Architecture_and_Design.md`](SURYA-KAVACH_Architecture_and_Design.md)

---

## 10. The one paragraph to memorise before you present

> *"We built a dual-channel solar flare **nowcasting** system on Aditya-L1 SoLEXS + HEL1OS data. It
> detects **78% of all M-class-and-above flares at 71% purity**, validated one-to-one against the
> **complete** official NOAA/SWPC catalogue of 2,117 flares. Along the way we found our ground truth was
> silently truncated to 195 flares — we fixed it. We found our detector was triggering on **photon
> noise** 62% of the time — we replaced it with a Poisson-significance test. And we tested flare
> **forecasting** rigorously against independent ground truth and found **X-ray-only forecasting has no
> skill at any class or horizon** — because flares are **magnetic** events and **Aditya-L1 carries no
> magnetograph**. That is a real scientific result, and it points directly at the next step: SHARP
> magnetogram data."*

That story — *we found our own bugs, we proved our own negative result* — is worth more than a polished
deck of numbers that collapse under a single question.
