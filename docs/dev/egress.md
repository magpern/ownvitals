# Egress: the outbound allowlist and mail

PLAN §2 ("the worker owns every outbound call") and roadmap Amendment 1. The package
`ownvitals.egress` (`backend/src/ownvitals/egress/`) is the only code that builds an HTTP client,
a transport or a `urllib` opener; `ownvitals.mail` is the only module allowed to open an SMTP
connection. The lists are code: nothing in the environment widens one. Adding a host is a code
change in `egress/core.py` and a security-critical review.

## What each process can reach

| Process | Entry point | Reaches |
|---|---|---|
| worker | `withings_async_client()` | `wbsapi.withings.net`, `account.withings.com` (every Withings call: sync, refresh, revoke, notify) |
| worker | the process-wide urllib opener (`install_worker_allowlist`) | the same, for any library that calls `urlopen` |
| worker, AI enabled | `model_async_client()` | `api.anthropic.com` (M7) |
| web | `google_oauth_client()`, `google_async_client()` | `oauth2.googleapis.com` (token), `www.googleapis.com` (JWKS) |
| web | `exchange_withings_code()` (`egress/code_exchange.py`) | one request it builds itself: `POST https://wbsapi.withings.net/v2/oauth2`, `action=requesttoken`, `grant_type=authorization_code` |
| web | `ownvitals.mail.Mailer` | the configured SMTP server (`smtp_host`, a setting, not a list entry) |

`tests/test_egress_runtime.py` enumerates it: every entry point against every candidate request,
in web and in the worker with AI off and on. A new client builder in the package fails that test
until it joins the enumeration.

**Process roles.** `install_worker_allowlist(ai_enabled=settings.ai_insights)` makes the process
the worker. Only two modules may call it (the guard's `WORKER_ROLE_MODULES`): the worker's entry
point `ownvitals.worker.__main__` (#9), first thing at start, and the probe CLI
`ownvitals.withings.probe` (#29); tests may too. Neither module exists yet: an issue that names
its entry point differently changes that list in its PR. From then on the
web-only entry points (Google, the code exchange, mail) raise `OutboundBlocked` before any
connection (`refuse_in_worker`), and the worker-only clients work only there: in the web process
`withings_async_client()` and `model_async_client()` raise `OutboundBlocked`, so web can make no
refresh, revoke or other Withings call. `ai_enabled` must be a real `bool`; with it false the
model host is on no list. Both checks run when a client is built and again on every request.

## Using it

```python
from ownvitals import egress

# Worker: Withings API calls (PLAN §4.2). Total deadline per request, body included (30 s).
async with egress.withings_async_client() as client:
    response = await client.post("https://wbsapi.withings.net/measure", data=form, headers=auth)

# Web: the OAuth callback's code exchange (#22). The caller hands in four values; the package
# builds the request (body, headers, URL) and returns the answer, body read.
response = await egress.exchange_withings_code(
    client_id=cid, client_secret=secret, code=code, redirect_uri=callback_url
)

# Web: Google sign-in (#10): Authlib's AsyncOAuth2Client, built by the package (PKCE S256,
# client_secret_post) over Google's sign-in transport. PKCE takes effect only when the caller
# passes code_verifier to both calls, as here. Nothing else may import authlib.integrations.
async with egress.google_oauth_client(
    client_id=cid, client_secret=secret, scope="openid email", redirect_uri=callback_url
) as client:
    url, state = client.create_authorization_url(GOOGLE_AUTHORIZE_URL, code_verifier=verifier)
    token = await client.fetch_token(GOOGLE_TOKEN_URL, code=code, code_verifier=verifier)
async with egress.google_async_client() as client:  # the JWKS fetch
    ...
```

**The code exchange's request.** Each value must be a non-empty string of printable ASCII without
a space, `;`, `&`, `=` or `#` (so no CR, LF or other control character), checked before anything
is encoded. The body is `urlencode` of exactly `action`, `grant_type`, `client_id`,
`client_secret`, `code`, `redirect_uri`, and must parse back to exactly those fields. The
headers are `Content-Type` (form), `Accept`, `User-Agent` and httpx's own `Host` and
`Content-Length`. The transport checks the final bytes again: method, raw path, the header names
(each once, `Host` the API host, a matching `Content-Length`), and a body of at most 4 KiB that
is the canonical encoding of its own fields, with the fixed action and grant type and no
forbidden character in any decoded value. No redirect is followed.

- Every client has `trust_env=False`, follows no redirect, refuses a redirect answer to another
  host (the code exchange refuses every redirect), verifies TLS against `certifi` and never
  writes a TLS key log (`SSLKEYLOGFILE` is ignored, `tls_client_context`).
- `total_seconds` (positive and finite) bounds a whole request with its body
  (`RequestDeadline`, a `TimeoutError`): Withings 30 s, Google 10 s, the code exchange 10 s (the
  code is valid for 30 s), the model 300 s (`MODEL_TOTAL_SECONDS`; each read 120 s). urllib
  requests get theirs from `timeout` (fallback 60 s) and `call_deadline` / `call_deadline_until`.
- `RequestBudget(hosts, limit, used)` with `request_budget(budget)` counts every request and
  redirect hop to `hosts` before it is sent; the one over the limit raises
  `RequestBudgetExhausted` and is never sent (per-job budgets, #18).
- A refusal raises `OutboundBlocked` (`host` only; a job fails `outbound_blocked`, ADR 0005) and
  logs `outbound request blocked` with the host, never the URL, a header or a body.
- `inner=` replaces the network transport in tests (an `httpx.MockTransport` or a fake); every
  check still runs.

## Mail

`Mailer(settings).send(MailMessage(to, subject, body))` over `smtplib` in a thread;
`send_logged` is the background task that sends after the response. Settings
([backend.md](backend.md)): `smtp_host` (unset: nothing is sent), `smtp_port` 587,
`smtp_sender`, `smtp_tls` `starttls` (default), `tls` or `none` (`none` only for loopback or
`mailpit`), `smtp_username`, and the secret file `smtp_password`. Locally the host is mailpit.
A failure is a `MailError` with the exception class only (no recipient, no cause, no context).
Tests use the in-process fake `tests/smtpfake.py` on loopback (trickling, stalling, STARTTLS and
implicit-TLS modes).

**Total deadline.** Mail runs inside web requests, so one delivery ends within
`SMTP_TOTAL_SECONDS` (20 s; `Mailer(settings, total_seconds=...)`, positive and finite). The
deadline is taken before the thread is scheduled, and every socket operation gets the time left
as its timeout: the connection, each `recv` of a reply, each send and the TLS handshake. A server
that trickles its replies a byte at a time, stalls after the greeting, or stops reading during
`DATA` (so the client's send blocks) ends at the deadline with
`MailError("delivery failed (deadline passed)")`. It is a module constant in `mail.py` until
`config.py` can take a setting (the Architect moves it once the shared-file lock allows).

**EHLO name.** smtplib's default is `socket.getfqdn()`: a reverse lookup outside the deadline,
and the container's host name sent to the server. `Mailer` passes a fixed `local_hostname`
instead: the host of `public_base_url`, or `localhost` when that is an IP address or has no dot
(`mail.ehlo_name`).

**Egress hook.** The SMTP client is built and used inside `egress.smtp_session()`, in
`_send_blocking`, in the thread that connects: the connection scope the hook requires (below).
Only `ownvitals.mail` may open it (the guard).

## The runtime hook (ADR 0009)

The authoritative control. `install_egress_hook(kind, settings)` (`kind`: `web`, `worker`,
`cli`) adds a PEP 578 audit hook, once per process; it cannot be removed. Installed by
`ownvitals/worker/__main__.py` (`main`) and the CLI (`run_command`); web's install and its
startup loop check are acceptance criteria of #77. The guard allows the call only in those three
modules.

| Audit event | Rule |
|---|---|
| `socket.getaddrinfo`, `socket.gethostbyname` (and `_ex`) | only a host in the process's set: web: Google sign-in's hosts and `wbsapi.withings.net`; worker (and the CLI once its probe installs it): the installed worker list; every kind: the database URLs' hosts; web and CLI: `smtp_host`. No context needed: asyncio resolves in an executor thread |
| `socket.gethostbyaddr`, `socket.getnameinfo` | always refused |
| `socket.connect` (and `connect_ex`), `sendto`, `sendmsg` | `AF_INET`/`AF_INET6`: only inside a connection scope, set by `AllowlistTransport` and the urllib opener around a send (after their allowlist check) and by `mail` around one SMTP session. `AF_UNIX`: only a configured database socket (`<host dir>/.s.PGSQL.<port>`). Any other family: refused. A send without an address is allowed |
| `socket.__new__` | only `AF_INET`, `AF_INET6` and `AF_UNIX`: a raw or packet socket is refused before it exists, and so is `socket.socket(fileno=fd)` without a family (family `-1` on Linux) |
| `subprocess.Popen`, `os.system`, `os.exec`, `os.posix_spawn`, `os.spawn`, `os.fork`, `os.forkpty`, `os.startfile`, `_winapi.CreateProcess` | always refused: no ownvitals process starts another, and a child would run without the hook. Logged as `process creation refused`. `_posixsubprocess.fork_exec` raises no event: the guard refuses it (below) |

A refusal raises `EgressRefused` (a `PermissionError`) and logs `outbound connection refused`
with `event`, `target` (host, address family or `reverse lookup`) and `process_kind`: never a
URL, a path, a query or a payload. `bind`, `listen` and `accept` are not checked. A second
install with the same kind does nothing (the first policy stays); another kind raises.

**The scope is inherited.** It is a context variable: a task created, or a `to_thread` call
made, inside a scope keeps it for as long as it runs (a `threading.Thread` or `run_in_executor`
call does not). So package code never starts concurrent work inside a scope (`create_task`,
`ensure_future`, `gather`, `to_thread`, `run_in_executor`, a `Thread`, a task group ...);
`tests/test_egress_scope.py` checks the egress package and mail for it. Work a library starts
and ends inside the send itself (anyio's happy-eyeballs tasks in httpx's connect) is part of the
send.

**The event loop.** Only the standard library's selector loop raises these events for asyncio
sockets (the probe: `tests/egress_loop_probe.py`, pinned on Linux by
`tests/test_egress_loop_probe.py`). So:

- web runs uvicorn with `loop="asyncio"` (`asyncio:SelectorEventLoop` on Windows), pinned in
  `test_main.py`; `uvicorn` is a dependency **without extras** (no uvloop, httptools,
  websockets, watchfiles or pyyaml), and `test_egress_hook.py` asserts uvloop is neither locked
  nor importable;
- `install_egress_hook` raises if `uvloop` is importable;
- `require_standard_event_loop()` refuses any other running loop (uvloop, the proactor loop); the
  worker's `_run` and the CLI's command call it first.

**Re-run the probe before any change of event loop**, of uvicorn's loop setting or of the Python
minor version: `python tests/egress_loop_probe.py` on Linux (loopback only).

Windows only: the selector loop's self-pipe is the standard library's Python `socketpair`
emulation, which connects to its own loopback listener. The hook recognises it by its code
object.

**Tests.** The hook never goes into the pytest process: `tests/conftest.py` makes
`sys.addaudithook` a no-op there. `tests/test_egress_hook.py` runs `tests/egress_hook_child.py`
once per kind in a fresh interpreter, with fake resolvers (they raise the same audit events and
answer `127.0.0.1`) and a loopback listener, so even a mutated hook reaches no network.

## The guard

`tests/egress_scan.py`, run by `tests/test_egress_guard.py` and `tests/test_egress_closure.py`
over the whole source tree: the early warning in review, behind the runtime hook (ADR 0009). It
is **closed-world** for direct imports and references: which modules can reach the network is
computed from the locked environment, and every one that `src/` uses is classified in one
reviewed table. Outside `ownvitals.egress`, `ownvitals.mail` included, a capable module that is
not classified is refused.

**Network-capable** (`tests/egress_closure.py`; nothing is imported or executed, modules are
found on `sys.path` and read with `ast`, parses are cached for the run):

- a seed: `socket`, `_socket`, `ssl`, `_ssl`, `select`;
- a module whose **module-level** imports include a capable module (`try`/`except`, `if`/`else`,
  `with`, loops, `match` cases and class bodies are followed; `if TYPE_CHECKING:` and exactly
  `if __name__ == "__main__":` are not; importing `a.b` imports `a`);
- a module in which a **function body** imports a seed (`import socket` or
  `from socket import x`). Other function-level imports are not followed (`os.popen` imports
  `subprocess` lazily; following that makes nearly everything capable). This is a recorded
  static limit: libraries that fetch lazily (numpy's `numpy.lib._datasource` behind `loadtxt`,
  minidom's `parseURI`) are not capable here, and the runtime hook refuses their connections.

**Edge cuts** (`EDGE_CUTS` in `tests/egress_classified.py`) stop propagation, each with a reason
and a test that proves it:

- `typing_extensions` imports `_socket` only to read the type of its CAPI capsule and binds no
  socket API. A chain through it (`typing_extensions._socket`) is still refused.
- `platform` imports `socket` only inside `_node()`, for `socket.gethostname()` (a local system
  call). Without this cut everything that imports `platform` would be capable through it. That
  does not make `numpy` network-free: it has lazy URL fetchers (`numpy.lib._datasource`), which
  the guard does not follow and the hook refuses at run time.

**The table** (`TABLE` in `tests/egress_classified.py`; a key `x.*` covers a package, the most
specific key wins, `only_in` scopes an entry):

| Class | Entries |
|---|---|
| denied | `starlette.testclient` |
| by design (inbound or driver code) | `fastapi`, `fastapi.responses`, `starlette.*`, `uvicorn`, `sqlalchemy.*` |
| allowed names | `asyncio`: `AbstractEventLoop`, `CancelledError`, `Event`, `FIRST_COMPLETED`, `Lock`, `SelectorEventLoop`, `Task`, `ensure_future`, `gather`, `get_running_loop`, `run`, `sleep`, `timeout`, `to_thread`, `wait`, `wait_for`; `socket`, only in `ownvitals.worker.loop`: `gethostname` (a local call); `email.message`: `EmailMessage`; `importlib.metadata`: `PackageNotFoundError`, `version`; `pydantic.fields`: `FieldInfo`; `pydantic_settings`: `BaseSettings`, `PydanticBaseSettingsSource`, `SettingsConfigDict`; `smtplib`, only in `ownvitals.mail`: `SMTP`, `SMTP_SSL` and its exception classes; `uuid`: `UUID`, `uuid4`; `scipy.stats`: `rankdata`, `scipy.signal`: `butter`, `find_peaks`, `get_window`, `iirnotch`, `sosfiltfilt`, `sosfreqz`, `tf2sos`, `scipy.ndimage`: `maximum_filter1d`, `uniform_filter1d` (capable only through scipy's docstring tooling and `pydoc`); `alembic`: none (imported for `alembic.op`); `alembic.op`: `execute` |

Any other name of an allowed module is refused wherever it is referenced: an import, a call, a
bare reference, an attribute value, a `partial` argument, an alias or a chain. A submodule is a
module of its own and is classified on its own (`alembic.op`, `asyncio.base_events`).

What else the guard refuses outside the package:

- **chains**: a dotted name is resolved to its longest module prefix and followed through what
  each module bound by an import (`asyncio.base_events.socket` is `socket`,
  `logging.handlers.socket` too); inside `ownvitals` re-exported names are followed as well
  (`mail.smtplib.SMTP`, `egress.clients.httpx`). Every capable module on the way is classified;
- **event-loop network methods on any object**, called or not: every method of
  `asyncio.AbstractEventLoop` outside `LOOP_METHODS` (`create_connection`, `sock_connect`,
  `getaddrinfo` ...) and the functions of `asyncio.streams`, read from the locked stdlib. A loop
  object cannot be told apart statically, so `retry(loop.sock_connect)` is refused too;
- **strings** that name a dotted path to a capable module or to a name outside its allowed names
  (`ext://` stripped; `"handlers.X"` is also read as `logging.handlers.X`, the `fileConfig`
  form), as `dictConfig` would import it;
- an import that does **not resolve** in the locked environment ("not in the locked
  environment"), except inside `if TYPE_CHECKING:`;
- **dynamic imports** with a computed name, a `package` or a `fromlist` (a constant name is an
  ordinary import, judged by every rule above). An importer (`importlib.import_module`,
  `importlib.__import__`, `builtins.__import__`, bare `__import__`, by any alias, `from` form
  or chain) may only be called directly with one string literal: any other reference (stored
  in a variable or a list, passed to `partial`, taken through `getattr`) is refused, so the
  name it imports is always visible. `importlib.util.find_spec` and `spec_from_*` are refused
  outright (`IMPORT_CALLS`, `SPEC_FINDERS`). `getattr(x, "name")` is the reference `x.name`;
- **module loaders**, under any alias, `from` form or chain (`LOADER_MODULES`,
  `tests/egress_importers.py`): `runpy`, `pkgutil` (the whole module), `zipimport`,
  `importlib.machinery`, `importlib.abc`, `importlib._bootstrap*`, `_frozen_importlib*`, `_imp`,
  and every `importlib` name except `import_module` (direct, literal only) and
  `importlib.metadata`. Each loads a module by a name the guard does not judge; `src` uses
  none of them, so this fails closed;
- **names in strings** (fail closed, `tests/test_egress_importers.py`): a string constant (an
  f-string part included) equal to or under a native or process module (`"ctypes"`,
  `"ctypes.util"`, `"_posixsubprocess"`, `"multiprocessing.pool"` ...), the strings
  `"__import__"`, `"import_module"` and `"__builtins__"`, and the name or attribute
  `__builtins__`. Every importer reached by name (`__builtins__["__import__"]`,
  `vars(builtins)["__import__"]`, `importlib.__dict__["import_module"]`) names one of them. A
  string nested inside an annotation expression is ordinary code;
- **string annotations** (forward references, `x: "Node"`): `typing.get_type_hints`, which
  FastAPI and pydantic call on routes and models, evaluates them. One may hold only names,
  dotted names, subscripts, tuples, lists, constants and `X | Y` (`ast.parse(s, mode="eval")`;
  a nested string is a forward reference too). Anything else, a call, a lambda, a
  comprehension, an operator or a syntax error, is refused. Each name in it is then judged as a
  name in code, through the run-time imports or, when nothing binds it, as spelled: so
  `"ctypes.c_int"` is refused as a reference to `ctypes`, even under a type-only import;
- **a module looked up by name**: any reference to `sys.modules` (a subscript, `.get(...)`, an
  `in` test, a variable holding it) and `pkgutil.resolve_name`, under any alias of `sys`, as
  `from sys import modules` and through a chain (`os.sys.modules`). `subprocess` imports
  `_posixsubprocess`, so `sys.modules["_posixsubprocess"]` would hand out a process route every
  other rule misses (`REFLECTION_NAMES`, `tests/test_egress_reflection.py`);
- **`ctypes` and `_ctypes`** (`NATIVE_MODULES`), under any alias or `from` form: they reach the
  C library's process and socket functions, which raise no audit event the hook sees;
- **assigning to** or deleting any attribute of `ownvitals.egress` or `ownvitals.mail` (also by
  `setattr`/`delattr`), and any private name of either;
- building an `Allowlist`, `AllowlistTransport`, opener or deadline handler, or handing an entry
  point a list;
- calling `install_worker_allowlist` (only `WORKER_ROLE_MODULES`, above);
- calling `install_egress_hook` outside the entry points (`ownvitals.worker.__main__`,
  `ownvitals.cli`, `ownvitals.web`), or `smtp_session` outside `ownvitals.mail`;
- star-importing anything.

An import inside `if TYPE_CHECKING:` passes every rule, `ctypes` and the process routes
included, when its names appear only in annotations (`tests/egress_typeonly.py`,
`tests/test_egress_typeonly.py`). Such an import never runs: `typing.TYPE_CHECKING` is `False`
at run time, so it only tells the type checker what a name is. A name it binds is a run-time
reference anywhere outside an annotation, and inside an annotation it resolves only through the
run-time imports, so an annotation that is evaluated cannot reach the module unless a run-time
import binds it, which the guard sees. (A string annotation is the exception: its names are
also judged as spelled, above, so `"ctypes.c_int"` is refused even then.) That holds only while
the flag is really `typing`'s, so
the block is skipped only when:

- its test is `TYPE_CHECKING` imported from `typing` or `typing_extensions` (by any alias), or
  `typing.TYPE_CHECKING` with `typing` (or `typing_extensions`) imported by any alias;
- every import that binds that name (a relative import never counts) binds it to one of
  those, and no other binding of it is recorded in any `symtable` scope of the module
  (assignment, `del`, a parameter, the walrus, a loop, `with` or `except` target; a
  `global`/`nonlocal` write is an assignment in its own scope). A 3.12 comprehension variable
  is recorded only in the comprehension, which is right: it does not rebind the module's name;
- nothing in the module writes to a name or attribute `TYPE_CHECKING`, or names it as a string
  or a keyword.

Otherwise the block is ordinary code and is scanned as such. Separately, these are refused in
`src/`:

- any write to a name or attribute `TYPE_CHECKING`: `TYPE_CHECKING = True`,
  `typing.TYPE_CHECKING = True`, `del`, a parameter or type parameter, a definition, a `match`
  capture;
- the string `"TYPE_CHECKING"` (or bytes `b"TYPE_CHECKING"`) and the keyword `TYPE_CHECKING=`
  anywhere. Every other way to
  write the flag names it like that: `globals()[...]`, `globals().update(...)`, `vars(...)`,
  `__dict__`, and `setattr`, `__setattr__` or `object.__setattr__` by any spelling. `src` only
  imports the flag and tests it, so this fails closed.

An `else:` branch runs and is scanned.

**Adding a dependency or an import.** The guard names the path to the network
(`x can reach the network (x -> y -> socket); not classified`). The PR adds a table entry with
its reason, and only the names it uses; that is a security-critical change. #10 classifies
`authlib.jose` this way (it is capable through `cryptography.x509` -> `email.utils` -> `socket`);
`authlib.oauth2` is not capable.

**Platforms.** An unclassified capable import fails everywhere. A table entry or cut that
nothing needs fails on Linux only, where CI runs; an allowed name nothing uses fails everywhere.
Modules that cannot be found on the platform are skipped and counted, and the test prints them
(platform modules and optional libraries; the number depends on what the run reached). The
two files take about 20 s locally and 10 s on Linux, 6 to 11 s of it the two reachability
subprocesses.

**Exceptions callers catch.** The package's own (`OutboundBlocked`, `RequestDeadline`,
`RequestBudgetExhausted`) come from `ownvitals.egress`. A caller that needs an exception class of
a network library (an httpx transport error, say) gets it re-exported by `ownvitals.egress` in
its own PR, with the reason in the guard test; it never imports the library. None is needed yet.

- `WORKER_PACKAGES` (`tests/egress_worker.py`): packages that run in the worker never import
  `ownvitals.mail`.
- `WORKER_ENTRYPOINTS` (`tests/egress_worker.py`): imported in a fresh interpreter, they must not load `ownvitals.mail`,
  `smtplib`, `authlib` or another client library. **Add each new worker entry module here**
  (the worker loop, the job runner, the Withings client ...).
- Tests that call `install_worker_allowlist` reset the process state afterwards with an autouse
  fixture (`urllib.request._opener` and `ownvitals.egress.core._worker_list`), as each
  `test_egress*.py` file does.

```bash
cd backend
uvx uv@0.13.6 run pytest -q tests/test_egress.py tests/test_egress_guard.py \
  tests/test_egress_closure.py tests/test_egress_runtime.py tests/test_egress_code_exchange.py tests/test_egress_deadline.py \
  tests/test_egress_budget.py tests/test_mail.py tests/test_egress_hook.py tests/test_egress_loop_probe.py \
  tests/test_egress_scope.py tests/test_egress_process.py tests/test_egress_reflection.py \
  tests/test_egress_typeonly.py tests/test_egress_importers.py
```

## Known limits

- The hook sees only Python's `socket` module. It cannot see C extensions that open sockets
  themselves (libpq, the database driver, by design: it connects only to the configured
  database; a new dependency with native networking is caught by dependency review), `ctypes`
  (refused statically in `src/`; a dependency's use is trusted code), or code that already runs
  in the process: it is a control on our code and our libraries, not
  a sandbox. A network-level egress rule stays the deployment's (M2).
- The hook refuses the process-creation routes that raise audit events. `_posixsubprocess.fork_exec`
  raises none: the `multiprocessing` `spawn` and `forkserver` methods (and uvicorn's
  `workers > 1` and `reload`) use it, and `ctypes` can start a process too. The guard refuses
  `_posixsubprocess`, `_winapi`, `multiprocessing` and `concurrent.futures.ProcessPoolExecutor`
  in `src/` under any name (`tests/test_egress_process.py`), and `ctypes`, `_ctypes` and
  `sys.modules` too (`tests/test_egress_reflection.py`); dependencies are trusted code.
  Web runs as one uvicorn process (#77).
- The inherited-scope check (`tests/test_egress_scope.py`) is lexical: a helper called inside a
  scope that starts a task itself is not seen; the four modules that open a scope are reviewed.
- Until #77 installs it in web, web relies on the guard, the clients' allowlists and the
  deployment's rule.
- The guard is static; the hook covers at run time what it cannot see, and the review gate the
  rest:
  - deliberate indirection (N9): a computed `getattr`, `vars(sys)`, `sys.__dict__`, a module
    an expression returns (`importlib.import_module("sys").modules`), `exec`/`eval`, the
    module's own `__loader__` and `__spec__.loader` (bound to its own file), and a module or
    flag name **built at run time**: `"cty" + "pes"`, `"TYPE_" + "CHECKING"`,
    `"".join(map(chr, ...))`, `"type_checking".upper()`, `b"...".decode()`, an f-string with a
    variable part. Every route that names its target as a constant is refused (above). The
    cheap partial closes are in: a rebound or named `TYPE_CHECKING`, dynamic imports and stored
    importers, module loaders, names of refused modules and importers in strings,
    `__builtins__`, `find_spec`/`spec_from_*`, `sys.modules`, `pkgutil`, `ctypes`, constant
    `getattr`, and `_socket`/`_ssl` as seeds. The `module:attr`
    string form (pydantic's `ImportString`, `"socket:create_connection"`) is not read by the
    string rule (N18): indirection of this kind, refused by the hook when it connects;
  - process execution (N10): `subprocess` is capable (it imports `select`) and so refused, as
    are `asyncio.create_subprocess_*` and `anyio`; `os.system`, `os.popen`, `os.exec*` and
    `os.spawn*` pass the guard. The image has `python`, so a child could connect: the hook
    refuses them at run time, and the guard refuses the routes without an audit event (above);
  - out of scope by design (N13): the database driver's host is a setting, like the SMTP host
    (`sqlalchemy.*` is by design); inbound listeners are not egress (`uvicorn` is by design;
    `socketserver`, `http.server` and `logging.config` are refused as unclassified);
    `xml.sax.parse(url)` goes through `urlopen`, which the worker's opener covers (and `xml.sax`
    is refused as unclassified).
- The web process installs no process-wide urllib opener yet: until #77 installs the hook, a
  third-party library calling `urlopen` in web is not stopped at run time (our own code is kept
  out by the guard); after it, its connect is refused (no scope). A deny-all opener at web start
  goes to #10, the next issue to take `web.py`.
- A DNS lookup in progress is not cut by a urllib deadline (the resolver's timeout ends it).
- SMTP: a DNS lookup in progress is not cut either, and a host name with several addresses
  gets the time left for each connection attempt.
- `account.withings.com` stays on the worker list because PLAN §2 lists it; the server has no
  known call to it (it hosts the authorize page the browser opens).
- The code exchange counts against the app's Withings rate budget once the rate limiter exists
  (#23).

## Decisions (accepted by the Architect, 2026-10-31, #6)

1. **Worker-only clients refuse outside the worker.** `withings_async_client` and
   `model_async_client` check the installed worker list when built and on every request, so the
   web process can make no Withings call but the code exchange. Every process that calls
   Withings (the worker, later the probe CLI) and the tests of such code install the worker list
   first (`install_worker_allowlist`), the tests with a reset fixture.
2. **The model host is gated through the installed list.** `model_async_client` builds only in a
   worker installed with `ai_enabled=True` (`ai_insights`); `ai_enabled` must be a real `bool`.
3. **Google sign-in has a host list in web**: the token endpoint and JWKS hosts, not
   `accounts.google.com` (the browser's authorize page). *Revised in review:* the package builds
   Authlib's client itself (`google_oauth_client`); outside it nothing imports
   `authlib.integrations`, so sign-in needs no guard exemption. This supersedes #10's
   `transport=` wording. Authlib 1.9 runs its client on `httpx2` when that is installed; the
   helper then refuses to build it (`TypeError`), since the transport is httpx's.
4. **The code-exchange form is strict**: unknown or repeated fields are refused, not ignored
   (a PHP-style server folds `grant.type` and `grant_type[]` into `grant_type`). Allowed:
   `action`, `grant_type`, `client_id`, `client_secret`, `code`, `redirect_uri`. Withings'
   signature/nonce variant would widen it here, under review. *Revised in review:* the caller
   passes values, never a body or headers (`exchange_withings_code`).
5. **Mail errors carry no exception context**: `MailError` is raised after the `except` block,
   with the exception class only.
