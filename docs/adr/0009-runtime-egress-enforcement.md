# ADR 0009: Runtime egress enforcement with an audit hook

Status: Accepted (2026-10-31, #6). Decided by the Architect after the third `FAIL` on finding B1
of PR #55, and amended before implementation on 2026-10-31 after the event-loop probe
(PR #55, comment 6550033885). Implemented in #6 (PR #55); web's install is an acceptance
criterion of #77. Amended on 2026-11-01 by the Architect after `Agent review (Opus): FAIL
e1feee9` (findings N3, N4 and N5): process creation and sockets of other families are refused,
and the inherited scope is stated (see "Amendment, 2026-11-01").

## Context

PLAN-operations §10.3 and AGENTS.md ("Security rules") require that every outbound HTTP call
goes through `ownvitals.egress`, that SMTP is built only in `ownvitals.mail`, and that the worker
reaches only its own hosts. Until this ADR the control was a static guard
(`backend/tests/egress_scan.py`): it parses `src/`, follows the import closure of every library
`src/` uses and refuses any network-capable module the reviewed table does not classify.

Finding B1 survived three review cycles (`471a44a`, `efae7bb`, `3f1b007`). The root cause: a
static scan cannot prove that a library never reaches the network. Libraries fetch lazily,
through imports inside functions: numpy's `DataSource` behind `loadtxt`, `genfromtxt` and
`fromregex` (`numpy.lib._datasource`), `xml.dom.minidom`'s `parseURI`. Following every
function-level import makes `os`, `json` and `logging` capable, so the closure is either
unsound or useless. Each round moved the gap without closing it.

PEP 578 audit hooks see the operations themselves. CPython raises an audit event for every
lookup and connection made through the `socket` module:

| Event | Arguments | Also raised by |
|---|---|---|
| `socket.getaddrinfo` | host, port, family, type, proto | |
| `socket.gethostbyname` | host | `gethostbyname_ex` |
| `socket.gethostbyaddr` | address | `getfqdn` |
| `socket.getnameinfo` | sockaddr (no flags) | |
| `socket.connect` | socket, address | `connect_ex` |
| `socket.sendto`, `socket.sendmsg` | socket, address | |

An audit hook only helps for code whose sockets go through that module. Before deciding, the
Developer probed what the event loops actually do. The probe ran on Linux
(`python:3.12-slim-bookworm`, Python 3.12.16, the locked environment of the time, which had
uvicorn 0.54.0 and uvloop 0.23.0), against a loopback listener only:

| Operation | Standard selector loop | uvloop 0.23.0 |
|---|---|---|
| `asyncio.open_connection`, httpx (anyio), psycopg async: the lookup | `socket.getaddrinfo` in an executor thread (`asyncio_0`); the caller's context **not** visible | no event |
| the same: the connect | `socket.connect` in the task's thread; the caller's context visible | no event |
| `asyncio.to_thread(getaddrinfo)` | event in a worker thread; context visible | (not a loop operation: as standard) |
| `loop.run_in_executor(None, getaddrinfo)` | event in a worker thread; context not visible | (as standard) |
| blocking `urllib.request.urlopen`, `np.loadtxt(url)`, minidom `parseURI`, smtplib | `getaddrinfo` and `connect` in the calling thread | the same: blocking sockets are audited under any loop |
| `socket.getfqdn()` | `socket.gethostbyaddr` | the same |

Two more observations, on Windows (the development machine, not a deployment target): the
proactor loop connects with `ConnectEx` and raises no `socket.connect`, and the selector loop's
self-pipe is the standard library's Python emulation of `socket.socketpair`, which raises one
`socket.connect` to its own loopback listener when a loop is created. uvicorn's `loop="auto"`
picked uvloop when installed (`uvicorn[standard]` installed it), and its `loop="asyncio"` is the
proactor loop on Windows.

`backend/tests/egress_loop_probe.py` is the probe; `test_egress_loop_probe.py` runs it on Linux
in CI and pins the standard loop's rows.

## Decision

1. **The hook.** `ownvitals.egress.install_egress_hook(kind, settings)`, with `kind` one of
   `web`, `worker` and `cli`, adds one audit hook per process. A second call with the same kind
   does nothing; another kind raises. It is called first thing in each entry point, before the
   app's other modules can connect: `ownvitals/worker/__main__.py`, the CLI's `run_command`
   (`ownvitals/cli.py`), and web's startup (#77). An audit hook cannot be removed. It returns at
   once for an event that does not start with `socket.`, so its cost elsewhere is one prefix
   check.
2. **Resolution** (`socket.getaddrinfo`, `socket.gethostbyname`): allowed only for a host in the
   process kind's resolvable set, else `EgressRefused`:
   - web: Google sign-in's hosts and `wbsapi.withings.net` (the code exchange, roadmap
     Amendment 1);
   - worker: its list, once `install_worker_allowlist` installed it (Withings, and the model
     host only with AI enabled); the CLI too, once its probe command (#29) installs it;
   - every kind: the configured database hosts (the host and the query's `host` of each
     database URL);
   - web and the CLI: the configured SMTP host.

   No context is required: asyncio resolves in an executor thread, where context variables do
   not reach (probe table). A lookup without a host (`None`, a local address) is allowed.
3. **Reverse lookups** (`socket.gethostbyaddr`, `socket.getnameinfo`): always refused.
   `getnameinfo`'s event carries no flags, so a numeric call cannot be told from a lookup; a
   legitimate caller goes back to the Architect. `Mailer` passes smtplib a fixed
   `local_hostname` (the public base URL's host, else `localhost`), so `getfqdn()` is never
   called: no reverse lookup outside the send deadline, and the machine's name is not sent in
   `EHLO` (finding N19).
4. **Connections** (`socket.connect`, `connect_ex`, `sendto`, `sendmsg`):
   - `AF_INET` and `AF_INET6` only while a connection scope (a context variable) is set. Only
     the package's transports set it, around the send and after their allowlist check
     (`AllowlistTransport`, the urllib opener), and `ownvitals.mail` around one SMTP session,
     inside `_send_blocking`, in the thread that connects. A connect outside a scope is refused
     even to an address an allowed name resolved to.
   - `AF_UNIX` only to a configured database socket (`<dir>/.s.PGSQL.<port>` from a URL's
     `host=/dir`). Every other family is refused.
   - A send on a connected socket (no address) is allowed: its connect was checked.
   - `bind`, `listen` and `accept` are not checked (uvicorn listens).
   - The scope is a context variable, so work started inside it inherits it: a task created,
     or a `to_thread` call made, inside a scope keeps the scope for as long as it runs. A
     `threading.Thread` or a `run_in_executor` call does not. Package code never starts
     concurrent work inside a scope: `tests/test_egress_scope.py` checks the egress package and
     mail for it. Work that a library starts inside the send and ends there (anyio's
     happy-eyeballs tasks inside httpx's connect) is the send itself.
   - Windows only: the standard library's `socketpair` emulation connects to its own loopback
     listener, recognised by its code object (`sys._getframe`), so a selector loop can start.
5. **Refusals.** `EgressRefused` subclasses `PermissionError`. A refusal logs one line,
   `outbound connection refused`, with the event, the host (or the address family, or
   `reverse lookup`) and the process kind: never a URL, a path, a query or a payload.
6. **Only the standard selector loop.** The hook is blind to uvloop and the proactor loop, so:
   - web runs uvicorn with `loop="asyncio"` (`asyncio:SelectorEventLoop` on Windows), pinned by
     a test;
   - `backend/pyproject.toml` depends on `uvicorn` without `[standard]` and without any extra:
     the app uses none of them (`h11`, a core dependency, serves HTTP; no websockets, no reload,
     no YAML or dotenv config through uvicorn). The lock lost uvloop, httptools, websockets,
     watchfiles and pyyaml. A test asserts that `uvloop` is not in `uv.lock` and is not
     importable;
   - `install_egress_hook` raises, for every kind, if `uvloop` is importable;
   - `require_standard_event_loop()` refuses a running loop whose class is not from the
     standard library's `asyncio` or not a selector loop. The worker and the CLI call it as
     their first await; web's startup calls it in #77.

   **Any future change of event loop, of uvicorn's loop setting or of the asyncio
   implementation re-runs the probe first** and updates this ADR.
7. **What the hook cannot see**, stated plainly:
   - C extensions that open sockets without Python's `socket` module. libpq (psycopg's binary
     driver) is one, by design: it connects only to the configured database. A new dependency
     with native networking is caught by dependency review (the locked `pyproject.toml`), not by
     the hook.
   - `ctypes`, and an attacker who already runs code in the process. The hook is a control on
     our code and our libraries, not a sandbox.
   - The network-level egress rule stays the deployment's (owner side, M2).

   Two things it would not see are refused outright instead (amendment of 2026-11-01):
   - **a child process**, which would run without the hook: the audit events
     `subprocess.Popen`, `os.system`, `os.exec`, `os.posix_spawn`, `os.spawn`, `os.fork`,
     `os.forkpty` and `os.startfile` are refused in every kind, since no ownvitals process starts
     another. A refusal logs `process creation refused`;
   - **a socket of another family** (a raw `AF_PACKET` socket's sends raise no audit event): the
     `socket.__new__` event is refused for any family but `AF_INET`, `AF_INET6` and `AF_UNIX`,
     before the socket exists.
8. **The static guard's role.** It stays: closed-world for **direct** imports (a capable module
   `src/` imports or references must be classified), the per-module rules (only `mail` uses
   smtplib; only the entry points install the worker list or the hook; only `mail` opens an
   SMTP session), and the early warning in review. Function-level imports of non-seed modules
   (lazy library fetchers such as numpy's `_datasource`) are a recorded static limit that the
   hook covers in every process.

## Alternatives

- **Keep extending the static closure** to follow every function-level import: makes `os`,
  `json` and `logging` capable, so every module is flagged and the table becomes a rubber stamp.
  Not following them leaves the lazy fetchers open (three review cycles).
- **Monkeypatch `socket.socket`, `getaddrinfo` and friends**: reversible by any code that holds
  the original, and does not cover `_socket` or modules that bound the functions before the
  patch. The audit hook cannot be removed and sees every caller.
- **Require the connection scope for lookups too**: breaks every asyncio lookup, which runs in
  an executor thread without the caller's context (probe table). Checking the host instead
  keeps the lookup rule exact.
- **Keep uvloop and hook it elsewhere**: uvloop's sockets live in libuv; there is no Python
  event to hook. Network-namespace or seccomp filtering is the deployment's layer, not a
  replacement for an in-process rule that names hosts.
- **A per-process network sandbox only (firewall, namespace)**: kept as the deployment's
  second layer (M2), but it cannot express "web may reach Google, the worker may not".

## Consequences

- The egress rules are enforced at run time in every process kind, including libraries the
  static guard cannot follow; a refusal is a `PermissionError` with one redacted log line.
- Web loses uvloop's throughput, negligible for a household-scale app. uvicorn serves HTTP with
  h11 and has no websocket support installed; a feature that needs one adds the extra by name
  and keeps uvloop out (the lock test).
- Tests never install the hook in the pytest process (it cannot be removed): an autouse fixture
  makes `sys.addaudithook` a no-op there, and `test_egress_hook.py` runs each kind in a fresh
  interpreter with fake resolvers and a loopback listener.
- The hook depends on the probe's findings. A change of event loop, of uvicorn's loop setting,
  of Python's minor version or of a library that moves its sockets into C re-runs
  `egress_loop_probe.py` first.
- Web's install and its startup loop check land with #77 (web's startup belongs to #82 and #77);
  until then web relies on the static guard, the egress clients' allowlists and the deployment's
  network rule.
- `docs/dev/egress.md` documents the hook, the guard's role and the known limits.

## Amendment, 2026-11-01 (Architect, after `Agent review (Opus): FAIL e1feee9`)

- **N3:** process creation is refused in every kind (Decision item 7). Nothing in `src/` starts
  a process. A child-process test imports the three entry points and builds web's app with the
  hook installed, and nothing at start needs one. A future need goes to the Architect first.
- **N5:** a socket of a family other than `AF_INET`, `AF_INET6` and `AF_UNIX` cannot be created
  (Decision item 7).
- **N4:** the inherited scope is stated in Decision item 4, and a static test keeps package
  code from starting concurrent work inside a scope.
- **N2:** a second install with the same kind is a no-op that keeps the first policy, as before;
  it is now tested.
