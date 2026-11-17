# Audit Evidence — raw measurements

Every number here was produced by executing code against the committed data.
Reproduction scripts are listed at the bottom.

---

## E1. The detector triggers on quiet-Sun photon noise (ROOT CAUSE)

`preprocess.add_features` computed:
- numerator `sxr_smooth` = **15-second rolling mean** of counts
- denominator `sxr_bg` = **5th percentile of the RAW (un-smoothed) counts** over 8 h

SoLEXS counts are Poisson: **mean 197.2, std 769.2 → σ/µ = 3.90**. A low percentile of a noisy
series sits far below its mean, so `ratio = smooth / bg` is biased **high on quiet Sun**.

```
sxr_ratio  median = 1.803        (a quiet Sun should sit at ~1.0)
% of ALL seconds >= ENTER threshold (1.5) : 62.2%
% of ALL seconds >= EXIT  threshold (1.2) : 77.9%
```
**A flare detector that is above its trigger threshold 62% of the time is not detecting flares.**
This produces 6,295 SXR events / 6,607 master events — one every ~16.6 minutes for seven months.

### After the applied fix (background from the smoothed series, `.bfill()` removed)
```
sxr_ratio  median = 1.577
% seconds >= 1.5 : 53.4%   (was 62.2%)
SXR events       : 5,859   (was 6,295)
```
**Still not sufficient.** The ratio statistic itself is unsound — dividing by a near-zero
percentile makes it explode:
```
sxr_ratio percentiles:  p50=1.58  p90=6.72  p95=15.53  p99=268.99  p99.9=4212.32
even ratio >= 8.0 is exceeded 8.56% of the time
```
→ **Replace the ratio trigger with a significance trigger (`excess >= k·sigma`) plus an absolute floor.**

---

## E2. The quiet Sun is classified as a C-class flare

```
sxr_flux  5th pct (quiet sun) = 1.075e-07  -> B1.1
sxr_flux  MEDIAN              = 1.666e-06  -> C1.7     <-- median of 7 months
sxr_flux  95th pct            = 1.631e-05  -> M1.6
```
The pipeline believes the Sun sat in a **continuous C1.7 flare for seven months**.

Resulting catalogue vs reality:

| class | pipeline | NOAA (Feb–Aug 2024) |
|---|---|---|
| ≥C | 4,010 | a few per day |
| M | 567 | ~150–200 |
| **X** | **69** | **~10** |

Cause: `SOLEXS_COUNTS_TO_FLUX = 2.687e-8` was fit as `flux = k·counts` **through the origin** on
flare *peaks*; SoLEXS counts contain a large non-solar pedestal, so forcing `b = 0` inflates `k`
at low count rates and lifts the quiet Sun to C-class. Classification also uses **total** flux
(`nowcast.detect_sxr` → `level = sxr_smooth`), not background-subtracted excess.

---

## E3. ABLATION — the dual-channel USP is not supported (NEW EXPERIMENT)

Same split, same model, same hyper-parameters; only the feature set changes.

| variant | test AUC | val AUC | model TSS | persistence TSS | BSS |
|---|---|---|---|---|---|
| **A) SXR only** | **0.6628** | 0.7044 | **0.184** | 0.139 | **0.054** |
| B) HXR only | 0.5563 | 0.5694 | 0.066 | 0.139 | **−0.079** |
| C) SXR+HXR (no xcorr) | 0.6593 | 0.6561 | 0.168 | 0.139 | 0.027 |
| **D) FULL (shipped model)** | **0.6610** | 0.6548 | 0.179 | 0.139 | 0.021 |

> **SXR-only beats the dual-channel model on every metric.** Δ(AUC) = **−0.0018** for adding HXR.
> **HXR-only scores *below* the persistence baseline** (TSS 0.066 < 0.139) and has a **negative**
> Brier Skill Score.

### Permutation importance (full model, on test) — explainability, previously absent
```
sxr_lb_mean        +0.0609      <-- top 5 are ALL soft X-ray
sxr_ctx_max        +0.0344
sxr_lb_slope       +0.0289
sxr_ctx_activity   +0.0206
sxr_ctx_mean       +0.0088
hxr_lb_std         +0.0052      <-- best hard X-ray feature: ~12x weaker
...
sxr_hxr_xcorr      +0.0013      <-- the "fusion" feature is near-worthless
hxr_ctx_max        -0.0151      <-- actively harmful
```

**Likely contributing causes (both fixable, and both are honest talking points):**
1. **Wrong HEL1OS band.** `pradan.py` selects the **widest** CZT band = the high-energy,
   background-dominated end, which is **74% zeros**. The flare-sensitive non-thermal band is the
   **low-energy** end (~15–30 keV).
2. **HXR coverage collapses exactly in the test window.** In May 27 – Aug 5, **89.4%** of the
   window has no usable hard X-ray data.

---

## E4. 46.8% of the data is silently discarded

`forecast.run_forecast` line ~237: `valid = fdf.dropna()`
```
minute-grid rows built : 305,280
after dropna()         : 162,412
SILENTLY DROPPED       : 142,868   (46.8%)
```
All top NaN offenders are hard X-ray features (`sxr_hxr_xcorr` 137,844 NaN; `hxr_lb_*` ~130,000).
So a row with perfectly good soft X-ray data is thrown away because HXR is missing — even though
the ablation (E3) shows **SXR alone forecasts better**.

`HistGradientBoostingClassifier` has **native NaN support**; the `dropna()` is unnecessary.

### Measured effect of removing it
| | rows | test AUC | TSS | BSS |
|---|---|---|---|---|
| CURRENT (`dropna`) | 162,412 | 0.6610 | 0.179 | **+0.021** |
| **FIX (native NaN)** | **305,278** | 0.6439 | **0.207** | **+0.216** |

→ **2× the data, TSS up, and Brier Skill Score improves 10×.**

Usable data by month after `dropna` (note Jul/Aug are nearly absent):
```
2024-02  29,573   2024-05  40,894
2024-03  35,638   2024-07  14,976
2024-04  36,370   2024-08   4,961
```

---

## E5b. ✅ RESULT AFTER FIXING THE PAGINATION BUG (supersedes E2/E6 severity)

The complete NOAA/SWPC reference, fetched with paging:

```
COMPLETE reference : 2,155 flares      (truncated version: 195)
class counts       : C=1,567   M=518   X=39   B=31
X-class            : 39        (truncated version had ZERO)
per month          : Feb 294 · Mar 314 · Apr 257 · May 384 · Jun 318 · Jul 339 · Aug 248
```

**Re-validating the shipped catalogue against the COMPLETE reference:**

| tier | n_goes | n_ours | completeness | **purity** |
|---|---|---|---|---|
| all | 2,154 | 6,295 | 0.649 | **0.245** |
| ≥C | 2,123 | 4,010 | 0.632 | **0.354** |
| **≥M** | 556 | 632 | **0.746** | **0.650** |

*(previously, against the truncated list: completeness 0.774 / purity **0.016**)*

### Retractions
The following claims in the first pass were **wrong** (they used domain memory, not data):

| claim | actual |
|---|---|
| ❌ "pipeline 69 X-class vs NOAA **~10**" | NOAA recorded **39** X-class. Ours 69 → **1.8×**, not 7×. |
| ❌ "pipeline 567 M-class vs NOAA **~150–200**" | NOAA recorded **518** M-class. Ours 567 → **nearly exact.** |

**Conclusion:** the calibration is substantially better than first reported, and the "1.6% purity"
crisis was mostly an artifact of the truncated reference. The **≥M detector (75% completeness at
65% purity) is a legitimately good, defensible result.**

The C-class tier still over-detects (~4,010 ours vs 2,123 real, purity 0.354), which is consistent
with the quiet-Sun trigger bias in **E1** — that bug is still real and still worth fixing.

---

## E5. The ground truth was silently truncated (CRITICAL — now fixed)

HEK returns **100 events per page** and sets `"overmax": true` when more exist.
`validate.fetch_goes_flares` issued **one query per 30-day chunk and never paged**.

```
May 2024, single query (what the code did):
   results returned : 100      <-- hard cap
   SWPC-authored    : 14
   response flags   : {'overmax': True}   <-- HEK said "there's more"; code ignored it
PAGE 2:
   results returned : 100
   SWPC on page 2   : 11   e.g. C8.7 @ 2024-05-02T17:32, M2.7 @ 2024-05-02T20:57
```

Consequences for the reference that **all** completeness/purity numbers were computed against:
```
total flares      : 195
class counts      : C=157  M=22  B=16   X= 0     <-- ZERO X-class
May 2024 entries  : 28, and they jump from May 2 -> May 31
```
**May 2024 contained the X8.7 flare of 14 May — the largest of Solar Cycle 25 — and the entire
Gannon-storm period (May 8–15). None of it is in the reference.**

> Therefore **every completeness and purity figure ever reported by this project is invalid**,
> including the "~77% completeness" in the README and the .pptx.

**Status:** `fetch_goes_flares` now pages until `overmax` is false (`MAX_HEK_PAGES = 200`).
A full re-fetch + re-validation is required.

---

## E6. Completeness is barely above chance; matching double-counts

`validate.matched()` allows **many** detections to match **one** GOES flare (no exclusivity):
> 3 fragments around 1 real flare → reported purity **1.0** (correct 1:1 answer: **0.333**).

Against the (truncated) 195-event reference:

| | pipeline | random times, same N & span |
|---|---|---|
| completeness (all) | 0.795 | **0.390 ± 0.046** |
| purity (all) | 0.030 | 0.014 ± 0.002 |

**A random number generator firing 6,295 times scores 0.39 completeness.** The reported 0.795 is
~2× chance — *with* the double-counting bug helping it.

---

## E7. The "34-minute lead time" is an artifact of an always-on alarm

```
test minutes with proba >= alert_threshold (0.2) : 77.1%
```
The siren is on **77% of the time**. `evaluate()` scores a hit if *any* alert minute falls in
`[onset−30, onset]`, and lead is capped at 30 min by construction — hence
`median_lead_onset = 29.43 min` (i.e. essentially always "already on").
121 alert episodes for 949 flares ⇒ ~8 flares per episode; episodes are hours long.

`FAR` is computed **per episode**: an episode is "true" if any flare onsets within `[start−30,
end+30]`. **A detector that alerts 100% of the time scores FAR ≈ 0 under this definition.**

---

## E8. Proven leakage (`.bfill()`) — now fixed

`preprocess.py` line 47 was `out[raw_col].ffill().bfill()`.
`bfill` fills a leading NaN run with the **next valid (future)** value:

> Changing a single sample at **t = 5** changed `sxr_smooth` at **t = 0** from `1.000e-07` → `9.000e-04`.

Reachable in the real data (`hxr_flux` first valid index = 6). `hxr_noise` had an explicit
`.bfill()` of a **future** rolling statistic. **Both removed.**

---

## E9. The tests cannot catch any of this

```
synthetic.generate()  quiet-Sun sigma/mu = 0.040
real SoLEXS           quiet-Sun sigma/mu = 0.80   (Poisson floor alone = 0.26)
```
The synthetic fixture is **20× quieter than the instrument**, so `sxr_ratio ≈ 1.000` and the
threshold is never tripped → **13/13 tests pass on a detector that fires 62% of the time.**
The synthetic data also has **no gaps, no NaNs, and quality hard-coded to 0**, which makes the
`ffill`/`bfill` bugs **structurally unreachable** in every test.

Additional test defects:
- `test_injected_flares_are_detected`: `assert len(master) >= len(truth)` — **passes with 6,000
  false positives** (assertion points the wrong way).
- `test_features_are_causal_no_future_leak`: tests `forecast.build_features` only — **not**
  `preprocess.add_features`, where the proven leak lives — and explicitly excludes the window
  where a trailing-window leak would show.
- `test_hxr_events_respect_max_duration` / `test_neupert_lead_is_positive`: guarded by
  `if len(...)` → **pass silently when the pipeline detects nothing.**

---

## E10. Data coverage reality

```
HXR NaN fraction on the 1 Hz grid : 32.82%
missing HXR runs                  : 334
LONGEST HXR gap                   : 2,116,809 s = 24.5 DAYS  (ffill'd across!)
runs > 8 h                        : 18   (=> the entire 8-h background window is fabricated)
BOTH channels good                : 63.03% of grid rows (112.7 of 212 calendar days)
```
HEL1OS files by month: Feb 70 · Mar 110 · Apr 91 · May 133 · **Jul 25 · Aug 7**.
July/August hard X-ray is effectively absent — and that is exactly the test window.

---

## Reproduction

The audit scripts live in the session scratchpad; the key ones are reproduced here as
`docs/Final_Project_Audit/scripts/` is not committed. To re-derive:

| Finding | How |
|---|---|
| E1, E2 | `add_features(parquet)` → percentiles of `sxr_ratio`, `sxr_flux`; `classify_flare()` |
| E3 | train `HistGradientBoostingClassifier` on feature subsets (`sxr_*`, `hxr_*`, all) with the same chronological split; `sklearn.inspection.permutation_importance` |
| E4 | `len(build_features(df))` vs `len(build_features(df).dropna())` |
| E5 | `GET https://www.lmsal.com/hek/her` with and without `page=2`; inspect `overmax` |
| E6 | permutation null: draw N random peak times over the same span, score with `validate.validate_catalogue` |
| E7 | fraction of `outputs/forecast_timeline.csv` test rows with `proba >= alert_threshold` |
| E8 | mutate `df` at t=5, recompute `add_features`, compare row t=0 |
| E9 | `std/mean` of `synthetic.generate()` vs a real SoLEXS CSV |
