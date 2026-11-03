"""The e2e stack's Withings: an HTTP shell around the in-process fake of the backend tests.

All protocol logic (OAuth, tokens, the data endpoints, notify, failure injection, the rules that
make a regressed client fail) is ``withingscore.FakeWithings`` from ``backend/tests``, mounted
read-only at ``/protocol`` so the two fakes cannot drift. This file only
  - turns an HTTPS request into a ``withingswire.Request`` and the ``Response`` back,
  - does the HEAD check of ``notify`` and the webhook POST over the network, and
  - serves the control endpoints under ``/__e2e/``, on a separate plain-HTTP port that the compose
    file publishes nowhere.
It must not grow a second copy of the protocol: ``backend/tests/test_withings_e2e_service.py``
fails when it names a protocol constant or defines one. Synthetic data only; the real Withings
is never contacted.
"""

from __future__ import annotations

import json
import os
import secrets
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlencode, urlsplit

sys.path.insert(0, os.environ.get("FAKE_WITHINGS_PROTOCOL", "/protocol"))

from withingscore import FakeWithings  # noqa: E402
from withingsnotify import HeadCheck  # noqa: E402
from withingswire import (  # noqa: E402
    WEBHOOK_RESPONSE_TIMEOUT,
    ConnectionDropped,
    Request,
    Response,
    Unroutable,
    webhook_form,
)

MAX_BODY = 1 << 20
MAX_DELAY = 60.0
FORBIDDEN_KINDS = {"mutate"}  # needs a Python callable


class RealClock:
    def now(self) -> float:
        return time.time()


class Service:
    """One fake, its lock (the fake is not thread-safe) and the e2e-only settings."""

    def __init__(self, env: dict[str, str]) -> None:
        self.env = env
        self.lock = threading.Lock()
        self.fake = self._new_fake()

    def _new_fake(self) -> FakeWithings:
        # Callback URLs: the compose receiver is plain HTTP on a service name, which the strict
        # default refuses (status 293). This opt-out exists for this service only.
        fake = FakeWithings(
            clock=RealClock(),
            seed=int(self.env.get("FAKE_WITHINGS_SEED", "0")),
            strict_callbacks=False,
        )
        self.client_id = self.env.get("FAKE_WITHINGS_CLIENT_ID", "e2e-withings-client")
        self.client_secret = self.env.get("FAKE_WITHINGS_CLIENT_SECRET") or secrets.token_hex(16)
        redirect = self.env.get(
            "FAKE_WITHINGS_REDIRECT_URI", "http://127.0.0.1:8000/api/oauth/withings/callback"
        )
        fake.add_app(self.client_id, self.client_secret, [redirect])
        self.user = fake.add_user(days=14, ecg_count=1)
        return fake

    # ------------------------------------------------------------------ outbound calls

    def rewrite(self, url: str) -> str:
        """The URL the fake can reach: the callback URL's host is the stack's public one, which
        the container cannot reach, so scheme and host become ``FAKE_WITHINGS_CALLBACK_BASE``."""
        base = self.env.get("FAKE_WITHINGS_CALLBACK_BASE")
        if not base:
            return url
        parts, target = urlsplit(url), urlsplit(base)
        return parts._replace(scheme=target.scheme, netloc=target.netloc).geturl()

    def send(self, method: str, url: str, data: bytes | None = None) -> int:
        request = urllib.request.Request(  # noqa: S310 - http(s) only, checked by the fake
            self.rewrite(url), data=data, method=method,
            headers={"content-type": "application/x-www-form-urlencoded"} if data else {},
        )  # fmt: skip
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=WEBHOOK_RESPONSE_TIMEOUT) as reply:
                return int(reply.status)
        except urllib.error.HTTPError as error:
            return int(error.code)
        except (urllib.error.URLError, OSError, ValueError):
            return 599

    # --------------------------------------------------------------------- the protocol

    def respond(self, request: Request) -> Response | None:
        """The fake's answer, or ``None`` when the connection is to be dropped."""
        try:
            with self.lock:
                step = self.fake.start(request)
            if isinstance(step, HeadCheck):
                status = self.send("HEAD", step.url)
                with self.lock:
                    step = step.finish(status)
        except Unroutable:
            return Response(421, {}, b"misdirected request")
        except ConnectionDropped:
            return None
        return step

    # ------------------------------------------------------------------------ control

    def control(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        route = (method, path.removeprefix("/__e2e"))
        if route == ("GET", "/ready"):
            return 200, {"ready": True}
        if route == ("POST", "/reset"):
            with self.lock:
                self.fake = self._new_fake()
                tokens = self.fake.tokens_for(self.client_id, self.user.userid)
                return 200, {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "userid": self.user.userid,
                    "tokens": tokens,
                }
        if route == ("GET", "/state"):
            with self.lock:
                return 200, self._state()
        if route == ("POST", "/inject"):
            kind = body.get("kind")
            if kind in FORBIDDEN_KINDS or not isinstance(body.get("at"), (int, list)):
                return 400, {"error": "kind or at not accepted"}
            opts = {k: body[k] for k in ("when", "status", "wait_seconds", "http_status") if k in body}
            if "seconds" in body:
                opts["seconds"] = min(float(body["seconds"]), MAX_DELAY)
            with self.lock:
                self.fake.inject(kind, at=body["at"], endpoint=body.get("endpoint"), **opts)
            return 200, {"injected": kind}
        if route == ("POST", "/send-webhook"):
            return 200, {"deliveries": self._send_webhook(body)}
        return 404, {"error": "no such control endpoint"}

    def _state(self) -> dict[str, Any]:
        fake = self.fake
        return {
            "request_count": fake.request_count,
            "endpoint_counts": dict(fake.endpoint_counts),
            "log": [asdict(entry) for entry in fake.log],  # secrets are masked by the fake
            "subscriptions": [  # the URL's path holds the webhook secret: scheme and host only
                {
                    "userid": s.userid, "appli": s.appli, "comment": s.comment,
                    "origin": "{0.scheme}://{0.netloc}".format(urlsplit(s.callbackurl)),
                    "failing": s.failing_since is not None,
                }
                for s in fake.subscriptions
            ],
            "failures": [{"kind": f.kind, "fired": f.fired} for f in fake.failures],
        }  # fmt: skip

    def _send_webhook(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        """One attempt per subscribed callback URL (the retry schedule spans hours: not here)."""
        userid, appli = int(body["userid"]), int(body["appli"])
        form = body.get("form") or webhook_form(appli, userid, **body.get("fields", {}))
        with self.lock:
            targets = self.fake.webhook_targets(userid, appli)
        results = []
        for sub in targets:
            status = self.send("POST", sub.callbackurl, urlencode(form).encode())
            with self.lock:
                self.fake.record_delivery(sub, 200 <= status < 300)
            results.append({"status": status, "ok": 200 <= status < 300})
        return results


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def api_handler(service: Service) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: Any) -> None:  # method and path only, never the query
            sys.stderr.write(f"{self.command} {urlsplit(self.path).path}\n")

        def _serve(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(min(length, MAX_BODY))
            host = (self.headers.get("host") or "").split(":")[0]
            headers = {name.lower(): value for name, value in self.headers.items()}
            request = Request(self.command, f"https://{host}{self.path}", headers, body)
            response = service.respond(request)
            if response is None:
                self.close_connection = True
                return
            if response.delay:
                time.sleep(min(response.delay, MAX_DELAY))
            payload = b"" if self.command == "HEAD" else response.body
            self.send_response(response.status_code)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = _serve  # noqa: N815

    return Handler


def control_handler(service: Service) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: Any) -> None:
            sys.stderr.write(f"control {self.command} {urlsplit(self.path).path}\n")

        def _serve(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(min(length, MAX_BODY))
            try:
                body = json.loads(raw) if raw else {}
                if not isinstance(body, dict):
                    raise ValueError
                status, payload = service.control(self.command, urlsplit(self.path).path, body)
            except (ValueError, KeyError, TypeError):
                status, payload = 400, {"error": "bad request"}
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _serve  # noqa: N815

    return Handler


def main() -> None:
    env = dict(os.environ)
    if env.get("OWNVITALS_ENVIRONMENT") != "test":
        sys.exit("fake-withings starts only with OWNVITALS_ENVIRONMENT=test")
    service = Service(env)
    api = ThreadingHTTPServer(("0.0.0.0", int(env.get("FAKE_WITHINGS_PORT", "443"))), api_handler(service))  # noqa: S104
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain("/certs/withings.pem", "/certs/withings.key")
    api.socket = context.wrap_socket(api.socket, server_side=True)
    control = ThreadingHTTPServer(  # noqa: S104 - the compose network only; no port is published
        ("0.0.0.0", int(env.get("FAKE_WITHINGS_CONTROL_PORT", "9101"))), control_handler(service)
    )
    threading.Thread(target=control.serve_forever, daemon=True).start()
    api.serve_forever()


if __name__ == "__main__":
    main()
