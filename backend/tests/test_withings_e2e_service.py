"""The e2e Withings service (#36, ``e2e/fake-withings/server.py``) is a shell around the protocol
code of the in-process fake and must not reimplement it; compose publishes no port for it.

Everything is in-process: the service's ``respond`` and ``control`` are called directly (its
outbound HEAD and webhook calls are replaced), so no socket is opened. Fakes and synthetic data
only.
"""

import ast
import importlib.util
from typing import Any
from urllib.parse import urlencode

import pytest
import yaml

import withingscore
import withingsnotify
import withingsrules
import withingsstore
import withingswire
from dbtools import BACKEND_DIR
from withingscore import FakeWithings
from withingswire import API_HOST, Request

ROOT = BACKEND_DIR.parent
SERVER = ROOT / "e2e" / "fake-withings" / "server.py"
PROTOCOL_MODULES = (withingscore, withingswire, withingsrules, withingsnotify, withingsstore)
FORM = {"content-type": "application/x-www-form-urlencoded"}
CALLBACK = "https://app.example.test/api/webhooks/withings/synthetic-secret"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("e2e_fake_withings_server", SERVER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def server() -> Any:
    return _load()


class Sent:
    """Replaces ``Service.send``: records the calls, answers with ``status``."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.calls: list[tuple[str, str, bytes | None]] = []

    def __call__(self, method: str, url: str, data: bytes | None = None) -> int:
        self.calls.append((method, url, data))
        return self.status


@pytest.fixture
def service(server: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(server.Service, "send", staticmethod(lambda *a, **k: 200), raising=False)
    return server.Service({"FAKE_WITHINGS_CLIENT_SECRET": "synthetic-e2e-secret"})


def _call(service: Any, path: str, token: str, **form: object) -> dict[str, Any]:
    headers = {**FORM, "authorization": f"Bearer {token}"}
    body = urlencode({k: str(v) for k, v in form.items()}).encode()
    reply = service.respond(Request("POST", f"https://{API_HOST}{path}", headers, body))
    return reply.json()


# ---------------------------------------------------------- one protocol, not two copies


def test_the_service_runs_the_protocol_code_of_the_backend_tests(server: Any) -> None:
    assert server.FakeWithings is withingscore.FakeWithings
    assert server.Request is withingswire.Request
    assert server.webhook_form is withingswire.webhook_form


def _defined(module: Any) -> set[str]:
    return {n for n, v in vars(module).items() if getattr(v, "__module__", None) == module.__name__}


def test_the_service_defines_nothing_the_protocol_modules_define() -> None:
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    own = {
        n.name
        for n in tree.body
        if isinstance(n, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    }
    own |= {
        t.id
        for n in tree.body
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    protocol = set().union(*map(_defined, PROTOCOL_MODULES))
    protocol |= {n for m in PROTOCOL_MODULES for n in vars(m) if n.isupper()}
    assert own & protocol == set()


def test_the_service_names_no_endpoint_host_scope_or_signing_primitive() -> None:
    text = SERVER.read_text(encoding="utf-8")
    tree = ast.parse(text)
    docstrings = {
        id(first.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef)
        for first in n.body[:1]
        if isinstance(first, ast.Expr)
    }
    literals = {
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    }
    protocol_words = {
        withingswire.API_HOST,
        withingswire.ACCOUNT_HOST,
        withingswire.AUTHORIZE_PATH,
        "requesttoken",
        "getnonce",
        "getdemoaccess",
        "subscribe",
        "access_token",
        "refresh_token",
        *withingswire.SCOPES,
        *(path for path, _ in withingsstore.DATA_ROUTES),
        *(action for _, action in withingsstore.DATA_ROUTES),
    }
    hits = {w for w in protocol_words for lit in literals if w in lit}
    assert hits == set()
    imported = {
        a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    } | {
        n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    }
    assert not imported & {"hmac", "hashlib", "httpx", "withingsstore", "withingsdata"}


def test_the_service_answers_what_the_in_process_fake_answers(server: Any) -> None:
    """Same seed, same requests, same bytes: the shell adds nothing to the protocol."""
    service = server.Service({"FAKE_WITHINGS_CLIENT_SECRET": "synthetic-e2e-secret"})
    twin = FakeWithings(clock=server.RealClock(), seed=0, strict_callbacks=False)
    twin.add_app(
        service.client_id, service.client_secret, service.fake.apps[service.client_id].redirect_uris
    )
    twin.add_user(days=14, ecg_count=1)
    token = service.fake.tokens_for(service.client_id, service.user.userid)["access_token"]
    twin_token = twin.tokens_for(service.client_id, service.user.userid)["access_token"]
    assert token == twin_token
    mine = _call(service, "/measure", token, action="getmeas", meastypes="1")
    theirs = twin.handle(
        Request(
            "POST", f"https://{API_HOST}/measure",
            {**FORM, "authorization": f"Bearer {twin_token}"},
            urlencode({"action": "getmeas", "meastypes": "1"}).encode(),
        )
    ).json()  # fmt: skip
    assert mine == theirs
    assert mine["status"] == 0


# ------------------------------------------------------------------- the control endpoints


def test_reset_gives_a_fresh_world_with_a_token_pair(service: Any) -> None:
    status, first = service.control("POST", "/__e2e/reset", {})
    assert status == 200
    assert (
        _call(service, "/v2/user", first["tokens"]["access_token"], action="getgoals")["status"]
        == 0
    )
    service.control("POST", "/__e2e/inject", {"kind": "status", "at": 1})
    service.control("POST", "/__e2e/reset", {})
    _, state = service.control("GET", "/__e2e/state", {})
    assert (state["request_count"], state["failures"], state["log"]) == (0, [], [])


def test_inject_fails_the_chosen_request_and_state_counts_without_secrets(service: Any) -> None:
    _, world = service.control("POST", "/__e2e/reset", {})
    token = world["tokens"]["access_token"]
    assert (
        service.control("POST", "/__e2e/inject", {"kind": "status", "at": 1, "status": 601})[0]
        == 200
    )
    assert _call(service, "/v2/user", token, action="getgoals")["status"] == 601
    assert _call(service, "/v2/user", token, action="getgoals")["status"] == 0
    _, state = service.control("GET", "/__e2e/state", {})
    assert state["request_count"] == 2
    assert [f["fired"] for f in state["failures"]] == [1]
    assert token not in repr(state)
    assert world["client_secret"] not in repr(state)


@pytest.mark.parametrize(
    "body", [{"kind": "mutate", "at": 1}, {"kind": "drop"}, {"kind": "drop", "at": "1"}]
)
def test_inject_refuses_what_it_cannot_do(service: Any, body: dict[str, Any]) -> None:
    assert service.control("POST", "/__e2e/inject", body)[0] == 400


def test_an_unknown_control_path_is_a_404(service: Any) -> None:
    assert service.control("GET", "/__e2e/nope", {})[0] == 404
    assert service.control("GET", "/other", {})[0] == 404


def test_subscribe_then_send_webhook_reaches_the_rewritten_receiver(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = Sent(200)
    monkeypatch.setattr(server.Service, "send", lambda self, *a: sent(*a))
    service = server.Service({"FAKE_WITHINGS_CLIENT_SECRET": "synthetic-e2e-secret"})
    _, world = service.control("POST", "/__e2e/reset", {})
    token = world["tokens"]["access_token"]
    reply = _call(service, "/notify", token, action="subscribe", callbackurl=CALLBACK, appli=1)
    assert reply["status"] == 0
    assert [c[0] for c in sent.calls] == ["HEAD"]  # the HEAD check of the callback URL
    _, state = service.control("GET", "/__e2e/state", {})
    assert state["subscriptions"] == [
        {
            "userid": world["userid"],
            "appli": 1,
            "comment": "",
            "origin": "https://app.example.test",
            "failing": False,
        }
    ]
    assert "synthetic-secret" not in repr(state)  # the path holds the webhook secret
    status, out = service.control(
        "POST", "/__e2e/send-webhook",
        {"userid": world["userid"], "appli": 1, "fields": {"startdate": 1, "enddate": 2}},
    )  # fmt: skip
    assert (status, out) == (200, {"deliveries": [{"status": 200, "ok": True}]})
    method, url, data = sent.calls[-1]
    assert (method, url) == ("POST", CALLBACK)
    assert data is not None
    assert dict(x.split("=") for x in data.decode().split("&")) == {
        "userid": str(world["userid"]), "appli": "1", "startdate": "1", "enddate": "2",
    }  # fmt: skip


def test_the_callback_base_redirects_the_service_own_calls(server: Any) -> None:
    service = server.Service({"FAKE_WITHINGS_CALLBACK_BASE": "http://web:8000"})
    assert service.rewrite(CALLBACK) == "http://web:8000/api/webhooks/withings/synthetic-secret"
    assert server.Service({}).rewrite(CALLBACK) == CALLBACK


def test_the_default_fake_refuses_the_compose_receiver_and_only_the_service_opts_out(
    server: Any,
) -> None:
    compose_hook = "http://web:8000/api/webhooks/withings/synthetic-secret"
    strict = FakeWithings()
    app, user = strict.add_app(), strict.add_user(days=2, ecg_count=0)
    token = strict.tokens_for(app.client_id, user.userid)["access_token"]
    body = urlencode({"action": "subscribe", "callbackurl": compose_hook, "appli": 1}).encode()
    reply = strict.handle(
        Request(
            "POST", f"https://{API_HOST}/notify", {**FORM, "authorization": f"Bearer {token}"}, body
        )
    )
    assert reply.json()["status"] == withingswire.STATUS_BAD_CALLBACK
    assert server.Service({}).fake.strict_callbacks is False  # e2e only, set in one place


# ------------------------------------------------------------------------- the compose file


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    compose = yaml.safe_load((ROOT / "compose.e2e.yaml").read_text(encoding="utf-8"))
    return compose["services"]["fake-withings"]


def test_compose_publishes_no_port_and_exposes_no_host_binding(spec: dict[str, Any]) -> None:
    assert "ports" not in spec
    assert "network_mode" not in spec
    assert "expose" not in spec or all(p not in ("443", 443) for p in spec["expose"])


def test_compose_mounts_the_protocol_code_read_only_and_answers_as_withings(
    spec: dict[str, Any],
) -> None:
    assert "./backend/tests:/protocol:ro" in spec["volumes"]
    assert all(v.endswith(":ro") for v in spec["volumes"])
    assert set(spec["networks"]["default"]["aliases"]) == {API_HOST, withingswire.ACCOUNT_HOST}
    assert spec["environment"]["OWNVITALS_ENVIRONMENT"] == "test"


def test_the_image_is_the_standard_library_shell_and_nothing_else() -> None:
    dockerfile = (ROOT / "e2e" / "fake-withings" / "Dockerfile").read_text(encoding="utf-8")
    assert "pip" not in dockerfile
    assert "uv " not in dockerfile
    assert "USER fakewithings" in dockerfile
    files = (p.name for p in (ROOT / "e2e" / "fake-withings").iterdir() if p.is_file())
    assert sorted(files) == [
        "Dockerfile",
        "server.py",
    ]


# ------------------------------------------------------------------------ the start-up guard


@pytest.mark.parametrize("environment", [None, "", "dev", "prod", "Test"])
def test_the_service_refuses_to_start_outside_the_test_environment(
    server: Any, monkeypatch: pytest.MonkeyPatch, environment: str | None
) -> None:
    monkeypatch.delenv("OWNVITALS_ENVIRONMENT", raising=False)
    if environment is not None:
        monkeypatch.setenv("OWNVITALS_ENVIRONMENT", environment)
    with pytest.raises(SystemExit, match="OWNVITALS_ENVIRONMENT=test"):
        server.main()


def test_the_service_gets_past_the_guard_in_the_test_environment(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Past(Exception):
        pass

    def stop(*_args: object) -> None:
        raise Past

    monkeypatch.setenv("OWNVITALS_ENVIRONMENT", "test")
    monkeypatch.setattr(server, "Service", stop)
    with pytest.raises(Past):
        server.main()
