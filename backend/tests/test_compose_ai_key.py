"""The model provider's key reaches the worker only (ADR 0014 section 1): ``compose.yaml`` mounts
the secret file ``anthropic_api_key`` into the worker and no other service, together with
``app_secret_key`` (the AI audit key), and no compose file at the repository root gives the key to
another service in any spelling: a secrets entry (short or long, by source or by target), an alias
secret over the same file, a secret read from the environment, an environment variable, or a bind
mount of the secrets directory or of a directory above it.

Reads the parsed YAML (merge keys resolved, so the shared ``x-app`` anchor counts for every service
that uses it). Fails closed: a host bind source it cannot place under the repository is refused.
The file contents are synthetic placeholders; no key is read."""

from pathlib import Path
from typing import Any

import pytest

from test_compose_host_sql import COMPOSE_GLOBS, load_compose
from test_compose_roles import ROOT

KEY = "anthropic_api_key"
KEY_FILE = f"./secrets/{KEY}"
KEY_SERVICE = "worker"
# What the worker needs besides the database password for an AI call: the key and the audit key.
WORKER_AI_SECRETS = {KEY, "app_secret_key"}


@pytest.fixture(scope="module")
def compose() -> str:
    return (ROOT / "compose.yaml").read_text(encoding="utf-8")


def _strings(value: Any) -> list[str]:
    """Every key and scalar in a parsed YAML value, as text."""
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (str(k), *_strings(v))]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return [] if value is None else [str(value)]


def _mentions_key(value: Any) -> bool:
    return any("anthropic" in s.lower() for s in _strings(value))


def _bind_source(entry: Any) -> str | None:
    """The host path of a volume entry, or None for a named, anonymous or tmpfs volume."""
    if isinstance(entry, dict):
        return str(entry.get("source", "")) if entry.get("type") == "bind" else None
    text = str(entry)
    if ":" not in text:
        return None if text.startswith("/") else text  # an anonymous volume, or refused below
    source = text.split(":", 1)[0]
    return source if source.startswith((".", "/", "~", "$")) or len(source) == 1 else None


def _exposes_secrets(source: str) -> bool:
    """A bind source that is, holds or may hold ``./secrets``: only ``./<dir>/...`` paths whose
    first directory is not ``secrets`` are known not to."""
    parts = source.replace("\\", "/").split("/")
    if parts[0] != "." or len(parts) < 2:
        return True  # absolute, home, variable, drive letter, `..`, `.`, bare relative
    first = next((p for p in parts[1:] if p not in ("", ".")), "")
    return first in ("", "..", "secrets")


def _secret_entry(entry: Any) -> tuple[str, str]:
    """(source, target) of a service's secrets entry."""
    if isinstance(entry, dict):
        source = str(entry.get("source", ""))
        return source, str(entry.get("target", source))
    return str(entry), str(entry)


def key_leaks(text: str) -> list[str]:
    """Every way one compose file gives the key to a service other than the worker, or gives it
    to the worker other than as the ``anthropic_api_key`` file secret."""
    loaded = load_compose(text)
    leaks: list[str] = []
    definitions = loaded.get("secrets") or {}
    assert isinstance(definitions, dict)
    for name, spec in definitions.items():
        if name == KEY and spec != {"file": KEY_FILE}:
            leaks.append(f"secret {name}: only `file: {KEY_FILE}`")
        elif name != KEY and _mentions_key(spec):
            leaks.append(f"secret {name}: an alias over the key")
    services = loaded.get("services") or {}
    assert isinstance(services, dict)
    for service, spec in services.items():
        assert isinstance(spec, dict), service
        for entry in spec.get("volumes") or []:
            source = _bind_source(entry)
            if source is not None and _exposes_secrets(source):
                leaks.append(f"{service}: bind mount {source!r} may hold secrets/")
        for key, value in spec.items():
            if key != "secrets" and _mentions_key(value):
                leaks.append(f"{service}: {key} names the key")
        for entry in spec.get("secrets") or []:
            source, target = _secret_entry(entry)
            if KEY not in (source, target) and not _mentions_key(entry):
                continue
            if service != KEY_SERVICE:
                leaks.append(f"{service}: mounts the key")
            elif source != KEY or target != KEY:
                leaks.append(f"{service}: the key is not /run/secrets/{KEY}")
    return leaks


def worker_ai_secrets(text: str) -> set[str]:
    worker = (load_compose(text).get("services") or {}).get(KEY_SERVICE) or {}
    return {_secret_entry(entry)[0] for entry in worker.get("secrets") or []}


def check_compose_files(root: Path) -> None:
    files = sorted({p for pattern in COMPOSE_GLOBS for p in root.glob(pattern)})
    assert files, "no compose file"
    for path in files:
        leaks = key_leaks(path.read_text(encoding="utf-8"))
        assert not leaks, f"{path.name}: {leaks}"
    assert worker_ai_secrets((root / "compose.yaml").read_text("utf-8")) >= WORKER_AI_SECRETS


def passes(compose: str) -> bool:
    return not key_leaks(compose) and worker_ai_secrets(compose) >= WORKER_AI_SECRETS


def test_the_key_and_the_app_secret_are_mounted_into_the_worker_only() -> None:
    check_compose_files(ROOT)


def test_web_does_not_get_the_key(compose: str) -> None:
    web = load_compose(compose)["services"]["web"]
    assert KEY not in {_secret_entry(entry)[0] for entry in web.get("secrets") or []}


WEB_SECRETS = "      - connection_kek\n"
WORKER_SECRETS = f"      - app_secret_key\n      - {KEY}\n"
SHARED_ENV = "    OWNVITALS_ENVIRONMENT: dev\n"
WEB_SMTP = "      OWNVITALS_SMTP_TLS: none\n"
DEFINITION = f"  {KEY}:\n    file: {KEY_FILE}\n"
WORKER_DEPENDS = "    stop_grace_period: 30s\n"

REFUSED = {
    "in web": (WEB_SECRETS, WEB_SECRETS + f"      - {KEY}\n"),
    "in web, long": (WEB_SECRETS, WEB_SECRETS + f"      - source: {KEY}\n        target: k\n"),
    "by target in web": (
        WEB_SECRETS,
        WEB_SECRETS + f"      - source: database_password\n        target: {KEY}\n",
    ),
    "an alias in web": (
        DEFINITION,
        DEFINITION + f"  ai_key:\n    file: {KEY_FILE}\n",
        WEB_SECRETS,
        WEB_SECRETS + "      - ai_key\n",
    ),
    "from the environment": (DEFINITION, f"  {KEY}:\n    environment: ANTHROPIC_API_KEY\n"),
    "from another file": (DEFINITION, f"  {KEY}:\n    file: ./secrets/app_secret_key\n"),
    "in the shared environment": (SHARED_ENV, SHARED_ENV + "    ANTHROPIC_API_KEY: synthetic\n"),
    "in web's environment": (WEB_SMTP, WEB_SMTP + "      OWNVITALS_ANTHROPIC_KEY_FILE: k\n"),
    "in the worker's environment": (
        WORKER_DEPENDS,
        WORKER_DEPENDS + "    environment:\n      ANTHROPIC_API_KEY: synthetic\n",
    ),
    "under another name in the worker": (
        WORKER_SECRETS,
        f"      - app_secret_key\n      - source: {KEY}\n        target: key\n",
    ),
    "missing from the worker": (WORKER_SECRETS, "      - app_secret_key\n"),
    "the app secret missing from the worker": (WORKER_SECRETS, f"      - {KEY}\n"),
    "the secrets directory in web": (
        WEB_SMTP,
        WEB_SMTP + "    volumes:\n      - ./secrets:/s:ro\n",
    ),
    "the key file in web": (
        WEB_SMTP,
        WEB_SMTP + f"    volumes:\n      - {KEY_FILE}:/run/k:ro\n",
    ),
    "the repository in web": (WEB_SMTP, WEB_SMTP + "    volumes:\n      - .:/repo:ro\n"),
    "a parent in web": (WEB_SMTP, WEB_SMTP + "    volumes:\n      - ../:/up:ro\n"),
    "a variable path in web": (WEB_SMTP, WEB_SMTP + "    volumes:\n      - ${PWD}:/r\n"),
    "an absolute path in web": (WEB_SMTP, WEB_SMTP + "    volumes:\n      - /srv/ov:/r\n"),
    "a long bind in web": (
        WEB_SMTP,
        WEB_SMTP + "    volumes:\n      - type: bind\n        source: ./secrets\n"
        "        target: /s\n",
    ),
}


def _apply(compose: str, pairs: tuple[str, ...]) -> str:
    planted = compose
    for old, new in zip(pairs[::2], pairs[1::2], strict=True):
        assert planted.count(old) == 1, old
        planted = planted.replace(old, new)
    load_compose(planted)  # still valid YAML
    return planted


@pytest.mark.parametrize("pairs", REFUSED.values(), ids=REFUSED.keys())
def test_the_key_check_refuses_every_spelling(compose: str, pairs: tuple[str, ...]) -> None:
    assert passes(compose)
    assert not passes(_apply(compose, pairs))


NEAR_MISSES = {
    "another secret in web, long": (
        WEB_SECRETS,
        WEB_SECRETS + "      - source: database_password\n        target: db_password\n",
    ),
    "a repository directory in web": (
        WEB_SMTP,
        WEB_SMTP + "    volumes:\n      - ./e2e/.certs/cacert.pem:/c.pem:ro\n",
    ),
    "a named volume in web": (WEB_SMTP, WEB_SMTP + "    volumes:\n      - db-data:/d\n"),
    "the key by its long name in the worker": (
        WORKER_SECRETS,
        f"      - app_secret_key\n      - source: {KEY}\n        target: {KEY}\n",
    ),
}


@pytest.mark.parametrize("pairs", NEAR_MISSES.values(), ids=NEAR_MISSES.keys())
def test_the_key_check_allows_what_does_not_leak(compose: str, pairs: tuple[str, ...]) -> None:
    assert passes(_apply(compose, pairs))


def _files_pass(root: Path, files: dict[str, str]) -> bool:
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    try:
        check_compose_files(root)
    except AssertionError:
        return False
    return True


@pytest.mark.parametrize(
    ("override", "ok"),
    [
        ("services:\n  worker:\n    secrets:\n      - connection_kek\n", True),
        (f"services:\n  web:\n    secrets:\n      - {KEY}\n", False),
        (f"services:\n  fake-model:\n    image: x\n    secrets:\n      - {KEY}\n", False),
        ("services:\n  web:\n    volumes:\n      - ./secrets:/s\n", False),
        (f"secrets:\n  {KEY}:\n    environment: ANTHROPIC_API_KEY\n", False),
    ],
)
def test_every_compose_file_at_the_root_is_checked(
    compose: str, tmp_path: Path, override: str, ok: bool
) -> None:
    files = {"compose.yaml": compose, "compose.e2e.yaml": override}
    assert _files_pass(tmp_path, files) is ok
