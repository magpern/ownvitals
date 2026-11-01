"""Accounts, sessions and sign-in (ADR 0003), ported from classbeat at ``d806051``.

``ownvitals.auth`` is the web issuer of ``UserContext`` (``deps``) and the only package that
touches the account tables (``ownvitals.auth.tables``; guard 8 in
``tests/test_auth_tables_guard.py``). Callers import the module they need
(``ownvitals.auth.purge`` in the worker, ``ownvitals.auth.accounts`` in the CLI,
``ownvitals.auth.deps`` in feature routes). This package module imports nothing: classbeat's lazy
re-export table (PEP 562, ``import_module`` with a computed name) is not ported, because the
egress guard refuses dynamic imports. So the worker, which needs only ``purge`` and ``owners``,
never loads the web-only sign-in and mail modules (``oauth_client``, ``ownvitals.mail``;
``tests/egress_worker.py``).
"""
