"""Envelope encryption (``envelope``, pure) and the key ring (``keks``), ADR 0004 sections 2 to 4.

Only ``ownvitals.connections``, ``ownvitals.auth`` (TOTP) and ``ownvitals.crypto.keks`` import
``ownvitals.crypto.envelope`` or a cryptographic primitive package (``cryptography``);
``tests/test_crypto_guard.py`` enforces the list. ``aad`` holds the key kinds, key ids and AAD
strings the envelope re-exports (pure strings, no key material). See ``docs/dev/crypto.md``.
"""
