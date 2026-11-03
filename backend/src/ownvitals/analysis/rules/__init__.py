"""The v1 finding rules as pure functions (ADR 0010; PLAN §7.2).

* ``core``: the two bands, confidence, severity, the result types and the shared trend rule;
* ``episodes``: ``origin`` and the episode state transitions;
* ``body``: B1 to B6; ``heart``: C1, C1x and C2.

Nothing here touches a database, the network or a clock: the evaluation day and ``today`` are
arguments. See ``docs/dev/analysis-rules.md``.
"""
