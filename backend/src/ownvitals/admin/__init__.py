"""The admin area (ADR 0002 section 4; classbeat ADR 0011; ownvitals #12): the gate and router
shell (``routes``), the audited maint database path (``db``), the audit log's actions and params
(``audit``), and the feature routes (``audit_routes``, ``settings``, ``jobs``). Admins never read
health data: ``ownvitals_maint`` has no grant on any health table (``docs/dev/admin.md``)."""
