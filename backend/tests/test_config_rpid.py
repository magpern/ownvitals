"""The prod refusal of a loopback WebAuthn relying-party id (issue #382), on the pure check.

Synthetic values only: every host here is a made-up name.
"""

import pytest

from ownvitals.config_rpid import check_rp_id

LOOPBACK = [
    "localhost",
    "localhost.",
    "LOCALHOST",
    "LocalHost.",
    "localhost..",
    "foo.localhost",
    "foo.localhost.",
    "FOO.LOCALHOST",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "localhost6",
]


@pytest.mark.parametrize("host", LOOPBACK)
def test_a_loopback_public_host_is_refused_in_prod(host: str) -> None:
    with pytest.raises(ValueError, match="must not be localhost"):
        check_rp_id(f"https://{host}", None, "prod")


@pytest.mark.parametrize("rp_id", LOOPBACK)
def test_a_set_loopback_relying_party_id_is_refused_in_prod(rp_id: str) -> None:
    with pytest.raises(ValueError, match="must not be localhost"):
        check_rp_id(f"https://{rp_id}", rp_id, "prod")


@pytest.mark.parametrize("host", ["localhost.", "foo.localhost."])
def test_a_trailing_dot_localhost_still_works_outside_prod(host: str) -> None:
    check_rp_id(f"http://{host}:5173", None, "dev")


@pytest.mark.parametrize(
    ("url", "rp_id"),
    [
        ("https://app.ownvitals.test", None),
        ("https://app.ownvitals.test", "ownvitals.test"),
        ("https://localhost-app.ownvitals.test", None),
        ("https://notlocalhost.test", None),
        ("https://localhost.ownvitals.test", None),
        ("https://mylocalhost", None),
    ],
)
def test_a_name_that_only_contains_localhost_is_accepted_in_prod(
    url: str, rp_id: str | None
) -> None:
    check_rp_id(url, rp_id, "prod")
