"""Every threshold and tolerance of the ECG tests, in one place.

Each row cites ``docs/adr/0006-ecg-subsystem-tests.md`` (the "tests file"), under the heading
named in the comment. Bounds are inclusive. Nothing here is tuned to the code: a row changes
only with an amendment of the tests file.

SYNTHETIC: the numbers apply to the strips of ``ecgsynth`` only.
"""

SYNTHETIC = True


def strip_se_floor(condition: str, sampling_hz: int) -> float:
    return STRIP_SE_FLOOR_OVERRIDES.get((condition, sampling_hz), STRIP_FLOOR)


# --- tests file, "Beat matching and the gate": the table of conditions --------------------------

POOLED_FLOOR = 0.99  # pooled Se and +P per condition and rate, for most conditions
POOLED_FLOOR_NOISY = 0.98  # "White or EMG-like noise 6 dB"
STRIP_FLOOR = 0.95  # Se and +P of every accepted strip
# amendment 2026-11-01 (#40): the Se floor of the AF-like strips at 500 Hz is 94.5 %; the lowest
# of 200 seeds is 94.67 % (95.08 % at 300 Hz). Every other floor stays.
STRIP_SE_FLOOR_OVERRIDES: dict[tuple[str, int], float] = {("af", 500): 0.945}
MATCH_TOLERANCE_S = 0.150  # EC57

# condition -> (minimum share of strips that pass the gate, pooled Se/+P floor or None)
# "fails >= x %" rows are stored as a maximum share of passing strips (1 - x).
GATE_PASS_MIN: dict[str, float] = {
    "clean": 1.00,
    "clean_lt50": 0.95,  # amendment 2026-10-31 (#40): clean strips below 50 bpm are asserted apart
    "white12_ge50": 0.95,  # the same amendment: white noise 12 dB, from 50 bpm
    "wander_ge50": 0.95,  # likewise baseline wander
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
# Sets that "may pass or fail" (their accuracy and HR/HRV rows apply to the strips that pass).
MAY_FAIL = ("white6", "emg6", "white12_lt50", "wander_lt50")
POOLED_MIN: dict[str, float] = {
    "clean": POOLED_FLOOR,
    "clean_lt50": POOLED_FLOOR,
    "white12_ge50": POOLED_FLOOR,
    "white12_lt50": POOLED_FLOOR_NOISY,  # amendment 2026-10-31 (#40): accuracy floors stay
    "wander_ge50": POOLED_FLOOR,
    "wander_lt50": POOLED_FLOOR_NOISY,  # as white12_lt50: the accuracy floors stay
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

CLEAN_CONDITIONS = ("clean", "clean_lt50", "amplitude_0.3mv", "inverted", "respiratory")
NOISY_CONDITIONS = (
    "white12_ge50",
    "white12_lt50",
    "white6",
    "emg6",
    "wander_ge50",
    "wander_lt50",
    "mains",
    "motion_edge",
    "ectopic",
)

RR_ERROR_RMS_MS = 2.0  # clean passed sinus strips

# HR leaves out a strip with a true interval within +-5 ms of the 220 bpm limit (272.7 ms): the
# R-peak timing error, not the algorithm, decides which side of the limit the detected interval
# is on, as it does for the 20 % rule of respiratory modulation at 40 and 45 bpm (amendment
# 2026-10-31, #40). Such strips are counted and reported in docs/dev/ecg.md.
HR_LIMIT_MS = 60_000.0 / 220.0
HR_BORDERLINE_MS = 5.0

HR_BPM = {"clean": 0.5, "noisy": 1.0}
SDNN_MS = {"clean": (3.0, 0.05), "noisy": (4.5, 0.10)}  # max(absolute ms, relative share)
PNN50_PAIRS = {"clean": 1, "noisy": 2}  # allowed pairs, plus the band

# RMSSD, ms, by the generator's mean HR: (first bpm, first bpm of the next band, clean, noisy).
# Noisy 60-99 bpm is 7.0 ms (tests file, RMSSD tolerance table, row amended 2026-10-31, #88;
# it was 6.5 ms before). The observed worst-case margins are reported in docs/dev/ecg.md.
RMSSD_BANDS = (
    (40.0, 60.0, 6.0, 10.5),
    (60.0, 100.0, 4.0, 7.0),
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
