"""Pure statistical methods for irregular, autocorrelated series (PLAN §7.1 step 5).

* ``series``: ``Series`` (times in days, float values) and ``InputSummary`` (n and coverage);
* ``baseline``: robust rolling median and MAD over time windows;
* ``ewma``: exponentially weighted average weighted by the time gap;
* ``trend``: Theil-Sen slope with the Mann-Kendall test and the Hamed-Rao correction;
* ``changepoint``: PELT change points, gated by the minimum-data policy.

numpy and scipy only; nothing from the database, web, Withings, job or egress code (the guard
test of ``ownvitals.analysis`` covers this package). See ``docs/dev/analysis-methods.md``.
"""
