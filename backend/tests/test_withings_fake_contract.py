"""Contract tests: what the Withings fake answers, and every synthetic fixture, validates against
the documented response shapes (``withingsshapes``) for each endpoint of PLAN 4.2. Fakes and
synthetic data only; nothing touches the network.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from withingscore import FakeWithings
from withingsdata import generate_dataset
from withingsfake import call, fake_client, make_fake
from withingsshapes import BODIES, validate, validate_response
from withingswire import SYNTHETIC_HEADER, sign

FIXTURES = Path(__file__).parent / "fixtures" / "withings"
PLAN_4_2_ENDPOINTS = {  # PLAN 4.2 "Endpoints", as ``path:action``
    "measure:getmeas",
    "v2/heart:list",
    "v2/heart:get",
    "v2/measure:getactivity",
    "v2/measure:getintradayactivity",
    "v2/measure:getworkouts",
    "v2/sleep:get",
    "v2/sleep:getsummary",
    "v2/user:getdevice",
    "v2/user:getgoals",
    "notify:subscribe",
    "notify:list",
    "notify:update",
    "notify:revoke",
    "v2/oauth2:requesttoken",
    "v2/oauth2:revoke",
}
HOOK = "https://app.example.test/api/webhooks/withings/synthetic-secret"


def _fixtures() -> list[Path]:
    return sorted(FIXTURES.glob("*.json"))


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_fixture_is_labelled_synthetic_and_matches_the_documented_shape(path: Path) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert path.name.startswith("synthetic_")
    assert doc["synthetic"] is True
    endpoint = "" if doc["endpoint"] == "errors" else doc["endpoint"]  # errors have no body shape
    assert validate_response(endpoint, doc["response"]) == []


def test_every_documented_endpoint_has_a_fixture() -> None:
    covered = {json.loads(p.read_text(encoding="utf-8"))["endpoint"] for p in _fixtures()}
    assert set(BODIES) - covered == {"notify:update", "notify:revoke"}  # empty bodies, as subscribe
    assert set(BODIES) >= PLAN_4_2_ENDPOINTS


def test_the_validator_is_not_vacuous() -> None:
    good = json.loads((FIXTURES / "synthetic_measure_getmeas.json").read_text("utf-8"))["response"]
    assert validate_response("measure:getmeas", good) == []
    missing = json.loads(json.dumps(good))
    del missing["body"]["measuregrps"][0]["grpid"]
    assert validate_response("measure:getmeas", missing)
    wrong_type = json.loads(json.dumps(good))
    wrong_type["body"]["measuregrps"][0]["measures"][0]["value"] = "71250"
    assert validate_response("measure:getmeas", wrong_type)
    unknown_enum = json.loads(json.dumps(good))
    unknown_enum["body"]["measuregrps"][0]["attrib"] = 99
    assert validate_response("measure:getmeas", unknown_enum)
    assert validate_response("measure:getmeas", unknown_enum, strict=False) == []
    assert validate({"a": 1, "extra": 2}, {"a": "int"}) == []  # unknown fields are tolerated


async def _every_endpoint(
    fake: FakeWithings, userid: int, client: Any
) -> dict[str, dict[str, Any]]:
    """Call each endpoint of PLAN 4.2 once and return the answers by ``path:action``."""
    app = next(iter(fake.apps.values()))
    redirect = next(iter(app.redirect_uris))
    data = fake.users[userid].data
    night = data.sleep_series[0]["series"][0]
    out: dict[str, dict[str, Any]] = {}
    code = fake.authorize(app.client_id, userid)
    out["v2/oauth2:requesttoken"] = tokens = await call(
        client,
        "/v2/oauth2",
        "requesttoken",
        client_id=app.client_id,
        client_secret=app.client_secret,
        grant_type="authorization_code",
        code=code,
        redirect_uri=redirect,
    )
    refreshed = await call(
        client,
        "/v2/oauth2",
        "requesttoken",
        client_id=app.client_id,
        client_secret=app.client_secret,
        grant_type="refresh_token",
        refresh_token=tokens["body"]["refresh_token"],
    )
    out["requesttoken (refresh)"] = refreshed
    token = refreshed["body"]["access_token"]
    ecg = data.ecg[0]["list"]
    window = {"startdateymd": "2000-01-01", "enddateymd": "2100-01-01"}
    calls: list[tuple[str, str, dict[str, Any]]] = [
        ("/measure", "getmeas", {"lastupdate": 0}),
        ("/v2/heart", "list", {}),
        ("/v2/heart", "get", {"signalid": ecg["ecg"]["signalid"]}),
        ("/v2/measure", "getactivity", window),
        ("/v2/measure", "getintradayactivity", {}),
        ("/v2/measure", "getworkouts", {"lastupdate": 0}),
        (
            "/v2/sleep",
            "get",
            {
                "startdate": night["startdate"],
                "enddate": night["enddate"],
                "data_fields": "hr,rr,snoring,sdnn_1,rmssd",
            },
        ),
        ("/v2/sleep", "getsummary", window),
        ("/v2/user", "getdevice", {}),
        ("/v2/user", "getgoals", {}),
        ("/notify", "subscribe", {"callbackurl": HOOK, "appli": 1, "comment": "synthetic"}),
        ("/notify", "list", {}),
        ("/notify", "get", {"callbackurl": HOOK, "appli": 1}),
        ("/notify", "update", {"callbackurl": HOOK, "appli": 1, "new_appli": 4}),
        ("/notify", "revoke", {"callbackurl": HOOK}),
    ]
    for path, action, params in calls:
        out[f"{path.strip('/')}:{action}"] = await call(client, path, action, token, **params)
    nonce = await call(
        client,
        "/v2/signature",
        "getnonce",
        client_id=app.client_id,
        timestamp=1,
        signature=sign(app.client_secret, "getnonce", app.client_id, 1),
    )
    out["v2/signature:getnonce"] = nonce
    out["v2/oauth2:revoke"] = await call(
        client,
        "/v2/oauth2",
        "revoke",
        client_id=app.client_id,
        userid=userid,
        nonce=nonce["body"]["nonce"],
        signature=sign(app.client_secret, "revoke", app.client_id, nonce["body"]["nonce"]),
    )
    return out


async def test_every_endpoint_of_plan_4_2_answers_in_the_documented_shape() -> None:
    fake, _, user = make_fake(seed=3)
    async with fake_client(fake) as client:
        answers = await _every_endpoint(fake, user.userid, client)
    assert set(answers) >= PLAN_4_2_ENDPOINTS
    for key, payload in answers.items():
        endpoint = "v2/oauth2:requesttoken" if key.startswith("requesttoken") else key
        assert payload["status"] == 0, (key, payload)
        assert validate_response(endpoint, payload) == [], key
    body = {k: v["body"] for k, v in answers.items()}
    assert body["measure:getmeas"]["measuregrps"]
    assert body["v2/heart:get"]["signal"]
    assert body["v2/measure:getintradayactivity"]["series"]
    assert body["v2/sleep:get"]["series"][0]["hr"]
    assert body["notify:list"]["profiles"]
    assert all(e.http_status == 200 for e in fake.log)


async def test_quirky_data_validates_only_when_unknown_values_and_gaps_are_tolerated() -> None:
    fake, _, user = make_fake(seed=5, quirks=True, ecg_count=3)
    async with fake_client(fake) as client:
        answers = await _every_endpoint(fake, user.userid, client)
    for key, payload in answers.items():
        endpoint = "v2/oauth2:requesttoken" if key.startswith("requesttoken") else key
        assert validate_response(endpoint, payload, strict=False) == [], key
    meas = answers["measure:getmeas"]
    assert validate_response("measure:getmeas", meas), "the quirks are visible to a strict reader"


async def test_errors_are_http_200_with_a_status_and_match_the_error_envelope() -> None:
    fake, app, user = make_fake()
    tokens = fake.tokens_for(app.client_id, user.userid)
    narrow = fake.tokens_for(app.client_id, fake.add_user().userid, "user.info")
    async with fake_client(fake) as client:
        replies = {
            "bad token": await call(client, "/measure", "getmeas", "nope"),
            "no scope": await call(client, "/measure", "getmeas", narrow["access_token"]),
            "bad params": await call(client, "/v2/sleep", "get", tokens["access_token"]),
            "bad action": await call(client, "/v2/sleep", "frobnicate", tokens["access_token"]),
            "bad code": await call(
                client,
                "/v2/oauth2",
                "requesttoken",
                client_id=app.client_id,
                client_secret=app.client_secret,
                grant_type="authorization_code",
                code="x",
                redirect_uri="x",
            ),
        }
    statuses = {name: reply["status"] for name, reply in replies.items()}
    assert statuses == {
        "bad token": 401,
        "no scope": 214,
        "bad params": 503,
        "bad action": 2554,
        "bad code": 304,
    }
    for reply in replies.values():
        assert validate_response("", reply) == []
    assert {e.http_status for e in fake.log} == {200}


async def test_every_response_is_labelled_synthetic() -> None:
    fake, app, user = make_fake()
    token = fake.tokens_for(app.client_id, user.userid)["access_token"]
    async with fake_client(fake) as client:
        reply = await client.post(
            "https://wbsapi.withings.net/v2/user",
            data={"action": "getgoals"},
            headers={"authorization": f"Bearer {token}"},
        )
    assert reply.headers[SYNTHETIC_HEADER] == "true"
    assert generate_dataset(1).to_dict()["synthetic"] is True


def test_the_protocol_core_needs_nothing_but_the_standard_library() -> None:
    """The e2e service (#36) mounts ``withingscore`` without the test suite or the app."""
    code = (
        "import sys\n"
        "for name in ('pytest', 'httpx', 'numpy', 'scipy', 'pydantic', 'ownvitals', 'netguard'):\n"
        "    sys.modules[name] = None\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import withingscore\n"
        "print(type(withingscore.FakeWithings()).__name__)\n"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", code, str(Path(__file__).parent)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "FakeWithings"
