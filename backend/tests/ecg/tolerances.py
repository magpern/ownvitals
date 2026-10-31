"""Every threshold and tolerance of the ECG tests, in one place.

Each row cites ``docs/adr/0006-ecg-subsystem-tests.md`` (the "tests file"), under the heading
named in the comment. Bounds are inclusive. Nothing here is tuned to the code: a row changes
only with an amendment of the tests file.

SYNTHETIC: the numbers apply to the strips of ``ecgsynth`` only.
"""

SYNTHETIC = True

# --- tests file, "Beat matching and the gate": the table of conditions --------------------------

POOLED_FLOOR = 0.99  # pooled Se and +P per condition and rate, for most conditions
POOLED_FLOOR_NOISY = 0.98  # "White or EMG-like noise 6 dB"
STRIP_FLOOR = 0.95  # Se and +P of every accepted strip
MATCH_TOLERANCE_S = 0.150  # EC57

# condition -> (minimum share of strips that pass the gate, pooled Se/+P floor or None)
# "fails >= x %" rows are stored as a maximum share of passing strips (1 - x).
GATE_PASS_MIN: dict[str, float] = {
    "clean": 1.00,
    "white12": 0.95,
    "wander": 0.95,
    "mains": 0.95,
    "amplitude_0.3mv": 0.95,
    "inverted": 0.95,
    "af": 0.95,
    "ectopic": 0.95,
    "motion_edge": 0.90,
}
GATE_PASS_MAX: dict[str, float] = {
    "motion_half": 0.05,  # "fails >= 95 %"
    "dropout_start": 0.0,  # "fails 100 %"
    "dropout_middle": 0.0,
    "dropout_end": 0.0,
    "flat_line": 0.0,
    "all_zeros": 0.0,
    "clipping": 0.0,
}
POOLED_MIN: dict[str, float] = {
    "clean": POOLED_FLOOR,
    "white12": POOLED_FLOOR,
    "wander": POOLED_FLOOR,
    "mains": POOLED_FLOOR,
    "amplitude_0.3mv": POOLED_FLOOR,
    "inverted": POOLED_FLOOR,
    "white6": POOLED_FLOOR_NOISY,
    "emg6": POOLED_FLOOR_NOISY,
    "af": POOLED_FLOOR,
    "ectopic": POOLED_FLOOR,
    "motion_edge": POOLED_FLOOR,
}

# --- tests file, "HR and HRV" -------------------------------------------------------------------

CLEAN_CONDITIONS = ("clean", "amplitude_0.3mv", "inverted", "respiratory")
NOISY_CONDITIONS = ("white12", "white6", "emg6", "wander", "mains", "motion_edge", "ectopic")

RR_ERROR_RMS_MS = 2.0  # clean passed sinus strips

HR_BPM = {"clean": 0.5, "noisy": 1.0}
SDNN_MS = {"clean": (3.0, 0.05), "noisy": (4.5, 0.10)}  # max(absolute ms, relative share)
PNN50_PAIRS = {"clean": 1, "noisy": 2}  # allowed pairs, plus the band

# RMSSD, ms, by the generator's mean HR: (first bpm, first bpm of the next band, clean, noisy).
# Follows the ADR at this branch's base: noisy 60-99 bpm is 6.5 ms (a follow-up issue may widen
# it to 7.0 ms; the observed worst-case margin is reported in docs/dev/ecg.md).
RMSSD_BANDS = (
    (40.0, 60.0, 6.0, 10.5),
    (60.0, 100.0, 4.0, 6.5),
    (100.0, 140.0, 5.0, 8.0),
    (140.0, 180.0001, 5.5, 8.5),
)

# --- tests file, "SQI bounds and components" -----------------------------------------------------

KSQI_CLEAN_60BPM_MIN = 5.0  # strictly greater (Li 2008)
KSQI_WHITE_NOISE = (2.0, 4.0)  # 3 +- 1
PSQI_PASS_10HZ_MIN, PSQI_30HZ_MAX = 0.99, 0.01
BASSQI_0_3HZ_MAX, BASSQI_10HZ_MIN = 0.01, 0.99

# --- tests file, "Filter responses (section 5)" ---------------------------------------------------

PASSBAND_DB = (-1.0, 0.0)  # 1-30 Hz
CORNER_DB, CORNER_TOLERANCE_DB = -6.0, 0.3  # 0.5 Hz and 40 Hz
MAINS_NARROW_MAX_DB = -40.0  # 50 and 60 Hz, +- 0.1 Hz
MAINS_WIDE_MAX_DB = -25.0  # 50 and 60 Hz, +- 0.5 Hz
DRIFT_MAX_DB = -60.0  # 0.05 Hz
REAL_FUNCTION_TOLERANCE_DB = 1.0  # sinusoids through the real function, mid-signal
PULSE_PEAK_SHIFT_SAMPLES = 0.5  # zero phase

# --- tests file, "Seeds and other tests" ---------------------------------------------------------

MAX_FILE_BYTES = 256 * 1024  # under backend/tests/ecg/


def rmssd_tolerance_ms(hr_bpm: float, group: str) -> float:
    """The RMSSD tolerance for a generator mean HR; ``group`` is ``clean`` or ``noisy``."""
    for low, high, clean, noisy in RMSSD_BANDS:
        if low <= hr_bpm < high:
            return clean if group == "clean" else noisy
    raise ValueError(f"no RMSSD band for {hr_bpm} bpm")


def group_of(condition: str) -> str | None:
    """``clean`` or ``noisy`` for a condition with an HRV truth, ``None`` otherwise."""
    if condition in CLEAN_CONDITIONS:
        return "clean"
    if condition in NOISY_CONDITIONS:
        return "noisy"
    return None
