# Runbook: the Withings probe (M2, the owner's step)

PLAN-operations §10.4, issue #29. The probe is how M1 becomes **INTEGRATION_VERIFIED**: you run it
on the home server against your own connected Withings account and paste its output into the
issue. Only you run it. Agents never run it against a real account, never ask for real values and
never set INTEGRATION_VERIFIED.

## What it does

`python -m ownvitals probe --connection <id>` opens the stored grant of that connection and calls
the public Withings API the way the worker does: the devices and goals, every measure group (up
to 20 pages of 100), the ECG list (up to 10 pages) and the newest three ECG signals, one call
for each other stream over the last 30 days (sleep series and intraday: the last 24 hours), and
the subscription list. Each call takes a token from the app's rate limiter, so the probe and
the worker share the 100-a-minute budget. If Withings refuses the access token, the probe
refreshes it once through the same crash-safe path as the worker; that is the only thing it
writes.

It prints **shapes only**:

| Line | Meaning |
|---|---|
| `connection <id>` | the id you passed (the only id in the output) |
| `grant stored`, `refreshed`, `current`, `needs_reconnect` … | how the access token was obtained |
| `outcome complete` or `outcome stopped <reason>` | `token_invalid` or `rate_limited` stop the probe |
| `call <endpoint> <result>` | `ok`, `skipped (no scope)`, or an error category with the API status number |
| `capability <stream> <state>` | `supported`, `unsupported`, `not_authorized`, `not_available` or `unknown` |
| `measures meastype <type> count <n>` | which measure types the account has, and how many |
| `measures attrib`, `category`, `position` | the codes seen, with counts; a code outside the documented list is `other` |
| `measures group_fields`, `measure_fields` | which documented field names the answers carry (no values); `other` means at least one undocumented key was sent |
| `ecg listed`, `fetched` | recordings listed, signals fetched |
| `ecg arrays <n> count <k>` | `k` signals carried `n` arrays (more than one would be extra leads) |
| `ecg lengths <samples> count <k>` | array lengths |
| `ecg sampling_rate`, `ecg wearposition` | the codes seen, with counts (out-of-bound values are `other`) |
| `notify appli`, `notify fields` | your subscriptions' appli numbers and field names |

It never prints a measurement value, an ECG sample, a timestamp, a date, a time zone, a device
or Withings user id, a callback URL, a comment or a token. Only documented field names are
printed: any other key (it could be an id or a timestamp) shows as `other`, and so does a code
outside its documented list or bound. A failure prints the error's type, never its text.

The report goes to stdout; log lines (for example `withings call failed`) and errors go to
stderr, so with `--json` stdout is a single JSON document.

## Before you run it

1. M2 is deployed on the home server, and the worker service has the connection key
   (`connection_kek`) and the worker's key files, as it needs them to sync at all.
2. You signed in and connected your Withings account in the app.
3. Find the connection id: while signed in, open `https://withings.pernemark.se/api/connections`
   in the same browser. Copy the `id` of the row whose `provider` is `withings` and whose
   `status` is `connected`.

## Run it

On the home server (192.168.1.222), in the worker container, as the app role:

```bash
cd /opt/ownvitals
docker compose -f compose.yaml -f /var/lib/ownvitals-deploy/compose.image.yaml \
  exec worker python -m ownvitals probe --connection <id>
```

For the same content as JSON add `--json`. `--ecg-signals N` (0 to 10, default 3) changes how
many of the newest ECG signals it fetches.

Exit code 0: complete. Exit code 1: it stopped or was refused; the output says why
(`outcome stopped rate_limited`: wait a minute and run it again; `token_invalid` with
`grant needs_reconnect`: reconnect Withings in the app, then run it again;
`error: no live Withings connection has this id`: check the id).

## What to paste

1. Read the output once yourself. If any line shows a number that looks like a measurement, a
   date or a timestamp, or any text you recognise as yours, **do not paste it**: open an issue
   that says only which line kind it was.
2. Otherwise paste the whole plain-text report (stdout only, from the `# ownvitals probe` line
   to the last line; with `--json`, the one JSON document on stdout) into a comment on issue #29, or on the issue the Architect names for M1
   INTEGRATION_VERIFIED, in a fenced code block. Add which devices you own (for example
   "ScanWatch, Body Smart"); the probe does not print device names.
3. The Architect compares it with the documented assumptions in `docs/status.md` (attrib values,
   ECG array count and length, sampling rate, wear position, which statuses come back) and
   proposes the ADR. Setting INTEGRATION_VERIFIED is yours.
