"""Mail that originates in the worker, through the outbox of ADR 0007 (#194).

``ownvitals.notify.outbox`` writes and reads the outbox (the worker and web load it, so it imports
nothing web-only); ``ownvitals.notify.sender`` sends it from web through ``ownvitals.mail`` and is
never imported by worker code (``tests/egress_worker.py``). ``ownvitals.notify.templates`` holds
the fixed template of each kind. This package imports neither at import time.
"""
