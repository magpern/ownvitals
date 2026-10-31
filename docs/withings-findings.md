# Withings reverse-engineering findings

Date: 2026-10-31. Target: Withings Health Mate for Android, `com.withings.wiscale2` **26.42.0**
(versionCode 202610080), pulled from a Samsung SM-S936B over adb.

Sources and how much to trust them:

| Source | What it gives | Confidence |
|---|---|---|
| APK DEX string/type tables (own parser) | Endpoint names, class/package names, enum constant names | Observed |
| JADX 1.5.6 decompilation of `base.apk` | Enum values, constants, data models | Observed (code is R8-obfuscated outside `com.withings.*`) |
| Strings of native libs in `split_config.arm64_v8a.apk` | Names of algorithm functions | Observed names; behavior inferred |
| Withings public OpenAPI spec (developer.withings.com, Oct 2026) | Public endpoints, meastypes, enums | Authoritative for the public API |
| `jaroslawhartman/withings-sync` | Working OAuth + `getmeas` client, Body Scan meastypes | Practical reference |

Nothing here was observed at runtime (no traffic capture, no execution). Behavior described as
"the app computes X" is inferred from class and function names plus decompiled signatures.

---

## 1. Two APIs: the app's private API and the public API

**The app does not use the public API.** It talks to a private API:

- Hosts: `https://scalews.withings.net/cgi-bin` (prod), `scalewsbeta…`, plus US medical cloud
  `scalews.us.withingsmed.com` and China/corp variants.
- Auth: account login (`auth?action=login`, `session?action=new|renew`), 2FA/TOTP endpoints,
  JWT/STS. Not OAuth.
- About 200 actions, including several you can't reach from the public API:
  - `v2/measure?action=getvasistas` / `getmeashf`: high-frequency ("vasistas") series
  - `v2/signal?action=list|get`: raw, compressed multi-lead ECG signals
  - `v2/healthscore?action=list`, `v2/vitality?action=list`, `v2/healthindicator?action=list`,
    `v2/healthinsight?action=get`, `v2/trajectory?action=list`, `v2/prediction?action=get`:
    the **server-side computed premium scores**
  - `v2/medicalreport?action=generate`, `v2/ecgreview?…` (clinician ECG review),
    `v2/stetho?action=requestanalysis`

**Recommendation: don't build on the private API.** It needs the user's Withings password
instead of OAuth, it probably breaks Withings' terms, and it can change without notice. Everything
the dashboard needs is either in the public API or can be computed from it (see §6).

**Public API**: `https://wbsapi.withings.net`, OAuth2 authorization-code flow, free tier.

---

## 2. Public API facts that shape the backend

### OAuth2
- Authorize URL: `https://account.withings.com/oauth2_user/authorize2?response_type=code&client_id=…&scope=…&redirect_uri=…&state=…`
- Token endpoint: `POST https://wbsapi.withings.net/v2/oauth2` with `action=requesttoken`,
  `grant_type=authorization_code|refresh_token`.
- **The authorization code is valid for only 30 seconds**, so the callback handler must exchange it
  immediately (server-side, not via a queued job).
- Access token `expires_in` = 10800 s (3 h). Response includes `userid`, `scope`, `refresh_token`.
- The refresh token **rotates**: every refresh returns a new one and the old one stops working.
  Persist the new token in the same transaction, and never refresh the same connection twice at
  once (withings-sync explicitly avoids retrying token requests for this reason).
- Scopes: `user.info`, `user.metrics`, `user.activity`, `user.sleepevents`.
- Other actions: revoke user access (for disconnect and account deletion), get user list, and
  **"Get access to demo account"**, a free demo dataset for CI and integration tests.
- Errors arrive as **HTTP 200 with a non-zero `status`** in the JSON. Always check `status`.

### Limits (free / standard plan)
- **120 requests per minute per application** (not per user). Exceeding it returns `status: 601`
  (HTTP 200). Enterprise: 1,500/min.
- **5,000 active users** per application on the free plan (a third-party source says 1,000; check
  the developer dashboard).
- **Restricted mode**: if the notification callback URL isn't HTTPS on a proper domain and port
  443, the application is limited to **10 linked users**. Fine for development, not for production.

### Notifications (webhooks)
- `POST /notify action=subscribe` with `callbackurl`, `appli`, and the user's Bearer token. This is
  done **per user, per appli**.
- The callback must answer **HTTP HEAD with 2xx** (Withings verifies it at subscribe time), use
  HTTPS on port 443 with a domain name (no IP or localhost), and be at most 255 characters.
- Payload: `application/x-www-form-urlencoded` (userid, appli, startdate/enddate…). Respond 2xx
  within a few seconds, then fetch the data asynchronously.
- Retries: 5 cycles over about 5 h (+10 s, +1 min, +1 h, +4 h). After 20 days of failures the
  subscription is auto-cancelled. **Missed notifications are never redelivered**, so a backfill
  using `lastupdate` is mandatory.
- Typical delay is under 2 min, with no SLA. Notifications are not signed, so treat them as a
  hint only.

| appli | Scope | Meaning |
|---|---|---|
| 1 | user.metrics | Weight & body composition (**Body Scan**) |
| 2 | user.metrics | Temperature |
| 4 | user.metrics | Blood pressure & heart rate (**Body Scan HR**) |
| 54 | user.metrics | ECG measurement (**Body Scan ECG**) |
| 55 | user.metrics | ECG measurement failed |
| 58 | user.metrics | Glucose |
| 61 | user.metrics | Stethoscope |
| 62 | user.metrics | HRV |
| 63 | user.metrics | Urine biomarkers (U-Scan) |
| 16 | user.activity | Activity (steps, distance, workouts) |
| 44 | user.activity | Sleep summary |
| 50/51/52/98/99/100 | user.sleepevents | Bed in/out events |
| 46 | user.info | User profile change |
| 53 | — | Unassociated device setup |

### Data endpoints we need
| Endpoint | Incremental key | Notes |
|---|---|---|
| `POST /measure action=getmeas` | `lastupdate`, `offset`/`more` | All body metrics. `category=1` real, `2` objectives |
| `POST /v2/heart action=list` | start/end, `offset`/`more` | ECG list: `ecg.signalid`, `afib`, `afib_details`, `heart_rate`, BP |
| `POST /v2/heart action=get` | `signalid` | `signal[]` in **µV**, `sampling_frequency` (Hz), `wearposition`, `model` |
| `POST /v2/measure action=getactivity` | `lastupdate` | Daily steps, distance, calories, HR zones |
| `POST /v2/measure action=getintradayactivity` | start/end | Minute data incl. HR, SpO2, HRV, RR, core temperature. **Expensive to backfill** (short windows) |
| `POST /v2/measure action=getworkouts` | `lastupdate` | Workouts |
| `POST /v2/sleep action=getsummary` | `lastupdate` | Nightly summary (durations, efficiency, latency, WASO, AHI, HR, RR…) |
| `POST /v2/sleep action=get` | start/end | High-resolution sleep series (hr, rr, snoring, sdnn_1, rmssd…) |
| `POST /v2/user action=getdevice` | — | Devices, battery, firmware, last session |
| `POST /v2/user action=getgoals` | — | Steps, sleep and weight goals |

Spec quirk: the published OpenAPI file tells actions apart by **trailing spaces in the path keys**
(`/v2/measure`, `/v2/measure `, …), so off-the-shelf client generators fail. Write a thin
hand-written client with schema validation.

### Values and enums
- Real value = `value × 10^unit`.
- `attrib`: 0 = device, known user; **1 = device, ambiguous (could be another household member)**;
  2 = manual; 4 = manual at signup; 5 = BPM auto; 7 = confirmed; 8 = same as 0;
  **15 = performed in guided conditions (Nerve Health guided scan)**.
- `position` / `wearposition` (segmental and ECG): 2 right arm, 3 left arm, 4 right foot,
  5 left foot, 8 left body, 9 right body, 10 left leg, 11 right leg, 12 torso, 13/14 hands.
- `model_id`: **Body Scan = 10**, Body Cardio = 6, Body Comp = 18, Body Pro = 9, BPM Core = 44,
  ScanWatch = 93, ScanWatch 2 = 94.
- ECG `afib`: 0 negative, 1 positive, 2 inconclusive. `afib_details`: −1 not processed,
  0 negative, 1 positive, 2 inconclusive, 5 noise, 6 low HR, 7 high HR, 9 negative normal HR,
  10 negative high HR, 11 positive normal HR, 12 positive high HR, 13 no diagnosis.

### Measure types (public `getmeas`; Body Scan rows in bold)
| Type | Meaning | Type | Meaning |
|---|---|---|---|
| **1** | Weight (kg) | **135** | QRS duration (ms) |
| 4 | Height (m) | **136** | PR interval (ms) |
| **5** | Fat-free mass (kg) | **137** | QT interval (ms) |
| **6** | Fat ratio (%) | **138** | QTc (ms) |
| **8** | Fat mass (kg) | 139 | AFib from PPG |
| 9/10 | Diastolic/Systolic BP | **155** | Vascular age |
| **11** | Heart pulse (bpm) | **167** | Nerve Health Score (medical, feet) |
| 12/71/73 | Temperature/Body/Skin | **168** | Extracellular water (kg) |
| 54 | SpO2 (%) | **169** | Intracellular water (kg) |
| **76** | Muscle mass (kg) | **170** | Visceral fat (index) |
| **77** | Hydration (kg) | **173/174/175** | Segmental fat-free / fat / muscle (with `position`) |
| **88** | Bone mass (kg) | **196** | Nerve Response Score (non-medical) |
| 91 | Pulse wave velocity (m/s) | **226** | Basal metabolic rate |
| 123 | VO2 max | **227** | Metabolic age |
| **130** | AFib result (ECG) | **229** | Electrochemical Skin Conductance (ESC) |
| 147/148/151/204/205/248/249/251 | U-Scan urine biomarkers | 158/159, 197/198 | Left/right foot nerve / EDA (seen in withings-sync, not in the current spec) |

**Store unknown meastypes too.** New ones keep appearing (248–251 are recent).

---

## 3. What the app computes on the device (and what it pays for)

### Native algorithm libraries (arm64 split)
| Library | Exported functions (observed) | What it means for us |
|---|---|---|
| `libsudo.so` | `sudo_process`, `nativeProcessSudoMeasures(values, modes, dates…)` | Nerve-health diagnosis runs over a **time series** of measures with a mode per measure |
| `libecg-lib.so`, `libecgw2-lib.so` | IIR filters: 0.3/0.5 Hz high-pass, 40 Hz low-pass, **50/60 Hz notch**, at 250/300/500 Hz; QRS detection, running median, line-noise detector | A standard ECG preprocessing chain we can reproduce |
| `libsleepscore.so` | `calculate_sleep_score_v3`, duration, recovery, interruptions, regularity, night HR, night HRV, time to fall asleep / wake up, AHI, `withings_index`, breathing quality | Sleep-score components |
| `libvitality.so` | recovery indicator (sleep duration, night HR, HRV), effort indicator (steps, active minutes), **vitals malus** (night temperature, SpO2, respiratory rate), `vitality_calculate_dynamic_thresholds`, `vitality_check_trend_significancy` | Readiness model built on **personal dynamic baselines** |
| `libbodybalance.so` | metric trajectory with sigmoid/linear/cubic segments, confidence intervals, goal completion date | "Body Path" weight trajectory |
| `libcycle.so`, `libactirec.so`, `libstetho.so` | Cycle prediction, activity recognition, stethoscope filter | Not relevant to Body Scan |

These are Withings-proprietary, Android-linked binaries. **We do not run them.** We build our own
models around the same inputs.

### Decompiled constants and enums
- **Nerve health** (`NeuropathyFeature`): the scale's regulatory mode decides which type you get:
  `Medical → 167`, `NonMedical → 196` (Nerve Response Score), `B2b → 229` (ESC). Score range 0–100.
  The sync engine must look for **all three** types.
- `SudoAlgorithm`: modes `ROUTINE = 0`, `GUIDED_SCAN = 1` (matches `attrib 15`). Status:
  `NORMAL = 0`, `SIGNS_OF_NEUROPATHY = 1`, `PENDING = 2`, `INCONCLUSIVE = 3`. "Pending" means not
  enough measures yet, so the diagnosis needs history. UI strings include
  "MonthlyNerveScoreUpdate" and "NeuropathySignsDetected".
- **ECG** (`library/ecg`): `DiagnosticResult` = AFib status, algorithm version, heart rate,
  **RMSSD, SDNN, pNN50**, intervals **PR, QRS, QT, QTc**. `AfibStatus` adds `NO_SIGNAL(3)` and
  `OTHER(4)` beyond the public enum.
- **6-lead ECG**: `EcgLeadsProvider.ComputeResult` holds `dI, dII, dIII, aVR, aVL, aVF`. The app
  derives 6 leads from 2 recorded leads (Einthoven/Goldberger). The public `heart get` returns one
  `signal[]`. **Unknown: which lead(s) the public API returns for Body Scan.** Verify with your own
  data in phase 1.
- Heart rate: `NORMAL_MIN_VALUE = 60`, `HIGH_MIN_VALUE = 100`.
- SpO2: normal ≥ 95, below average ≥ 90, low < 90 (gauge from 85).
- Blood pressure: classification in `EU | US | CN` variants. Health-score "high BP" cut-offs: EU
  135/85, US 130/80.
- VO2 max: five bands (VeryPoor → Excellent) from an age×sex norm table.
- Trend enum: UP / STABLE / DOWN. Fever level: NONE / MILD / HIGH.

### Health Improvement Score (premium "His")
The score cards ship as JSON inside the APK: 42 rows of piecewise bands. For each criterion, keyed
by age range and sex, there are `bad / average / good / perfect` boundaries on the low side and
the high side, plus `max_score` and `precision`. Verticals: Body, Heart, Sleep, Activity,
Nutrition. Main criteria (18–40 bracket unless noted):

| Criterion | Perfect | Good | Average | Bad |
|---|---|---|---|---|
| BMI (18–65) | 20–21 | 18.5–25 | 16–30 | <15 or >40 |
| Fat ratio, man 18–40 | 7–10 % | 6–20 % | 5–25 % | <3 % or >40 % |
| Fat ratio, woman 18–40 | 15–18 % | 13–27 % | 11–34 % | <8 % or >45 % |
| Night HR, man 18–40 | ≤50 | ≤64 | ≤70 | >80 |
| Vascular age − age, man | ≤−5 | ≤0 | ≤3 | >5 |
| Vascular age − age, woman | ≤−7 | ≤−2 | ≤0 | >3 |
| ECGs with AFib (count) | 0 | ≤1 | ≤5 | >10 |
| High-BP measure ratio (40+) | 0 | ≤15 % | ≤50 % | >70 % |
| Daily steps (18–65) | ≥12,000 | ≥6,000 | ≥3,000 | <2,000 |
| Weekly active minutes (18–65) | ≥1,000 | ≥600 | ≥300 | <100 |
| Sleep AHI | 0 | ≤15 | ≤30 | >50 |

Fat-ratio bands shift upward by roughly 2–3 percentage points for each older age band (40–60, 60+).
Thresholds for 65+ BMI, steps and active minutes are looser. The raw table can be
re-extracted from the APK's DEX string pool (search for `"criteria_id"`).

### Features behind the Withings+ paywall (`PaywallOrigin` enum)
`His` (Health Improvement Score), `Vitality`, `HealthTabReadiness`, `BodyPath` (weight trajectory),
`InsightTrend`, `CardioEfficiency`, `CardioResponsiveness`, `ContinuousTemperature`,
`HealthAssistant` (AI chat), `YearInReview`, `Program`, `Protect`, `StethoAi`, `CycleTracking`,
`Checkup`, and also **`DataExport`**. All of these except clinician review (Checkup) can be
rebuilt from public-API data.

---

## 4. Lessons from `withings-sync`
- Small working client: authorize → exchange code → refresh on every run → `getmeas` with
  `category=1`, `startdate`, `enddate`.
- Retries on HTTP 408/429/5xx and API `status 601` with exponential backoff, honoring
  `body.wait_seconds` when present.
- Never retries token requests, because the token rotates.
- Validates the token response (both tokens and `userid` present) before overwriting stored state.
- It also has a known bug: type 170 (visceral fat) is labelled "Extracellular Water" in its table.
  So don't copy its labels; use the spec.
- Single user, file-based tokens, polling only, no webhooks. Our design replaces all of that.

---

## 5. Open questions to verify against real data (phase 1)
1. Which ECG lead(s) `heart get` returns for Body Scan (model 10), and its sampling frequency.
2. Whether Body Scan writes nerve health as 167, 196, or 229 for this EU account, and whether
   per-foot values (158/159, 197/198) still appear.
3. Whether `getmeas` with `lastupdate` ever reports deletions (assume not, and reconcile).
4. Whether ECG notifications (appli 54) carry a `signalid` or only a time window.
5. Whether vascular age (155) and PWV (91) come from Body Scan, and whether 135–138 intervals are
   present for every ECG.
6. Which region the account is in (EU `wbsapi.withings.net` vs US medical cloud). The app has
   separate US hosts, so keep the API base URL per connection.

---

## 6. Feature parity: app feature → public data → our approach
| App feature | Public data | Our implementation | Feasible |
|---|---|---|---|
| Weight & body-composition trends | getmeas 1,5,6,8,76,77,88,170,226,227 | Trend + baseline engine | Full |
| Segmental body (3D) | 173/174/175 + `position` | 2D body map first, 3D later | Full |
| Body water / edema hints | 168/169 | ECW/TBW ratio tracking | Full |
| ECG list, viewer, PDF | heart list/get | Canvas viewer, 25 mm/s × 10 mm/mV grid, PDF export | Full (lead count TBD) |
| AFib + intervals | 130, 135–138, `afib_details` | Show Withings result verbatim + our rhythm metrics | Full |
| Nerve health score | 167/196/229, attrib 15 | Values: full. Diagnosis: our own longitudinal model | Partial |
| Vascular age | 155 | vs chronological age, score card | Full |
| Health Improvement Score | getmeas + activity + sleep + profile | Score-card engine (§3) | Full |
| Vitality / readiness | sleep, HR, HRV, steps, temperature, SpO2 | Personal-baseline model | Needs a watch or Sleep Analyzer |
| Body Path (trajectory) | weight series + getgoals | Robust regression with CI | Full |
| Insight trends, Year in review | everything | Trend engine + Claude narrative | Full |
| Health Assistant (AI) | everything | Claude with tool access to the user's stats | Full (opt-in) |
| Data export | everything | CSV / JSON / FIT | Full |
| Cardio Check-up (clinician review) | — | Not replicable; offer a "share with doctor" PDF | No |
