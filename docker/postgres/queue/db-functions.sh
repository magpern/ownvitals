#!/bin/sh
# Entrypoint of the one-shot compose service `db-functions` (ADR 0005 section 3): run
# 20-queue.sql as the superuser, in one transaction, after the migrations and before web and the
# worker start.
#
# It runs in the application image, which carries this file and the SQL under /app/db-functions
# (Dockerfile): the functions installed are always those of the release the image belongs to,
# never a file on the host (docs/dev/deploy.md, "Database functions").
#
# The superuser password is read from the postgres_password secret file and written to a
# PGPASSFILE on a tmpfs (/run/pgpass, mode 0700), never to the environment or a command line, so
# it never shows in `docker inspect` and never lands in the container's writable layer.
set -eu

secret=/run/secrets/postgres_password
pgpass=/run/pgpass/pgpass
if [ ! -s "$secret" ]; then
  echo "db-functions: the postgres_password secret is missing or empty" >&2
  exit 1
fi

umask 077
# Like the postgres image's POSTGRES_PASSWORD_FILE: the file's content without trailing newlines.
# In a .pgpass field, backslash and colon are escaped with a backslash: backslashes first, so the
# backslash added before a colon is not doubled (tests/test_db_functions_script.py).
password="$(cat "$secret")"
if [ -z "$password" ]; then
  echo "db-functions: the postgres_password secret is empty" >&2
  exit 1
fi
cr="$(printf '\r')"
case $password in
  *"$cr"* | *'
'*)
    # A .pgpass line cannot hold a line break, and libpq drops a trailing CR.
    echo "db-functions: the postgres_password secret contains a line break" >&2
    exit 1
    ;;
esac
escaped="$(printf '%s' "$password" | sed -e 's/\\/\\\\/g' -e 's/:/\\:/g')"
unset password
printf 'db:5432:ownvitals:postgres:%s\n' "$escaped" > "$pgpass"
unset escaped
export PGPASSFILE="$pgpass"

exec python -m ownvitals.db.install_functions \
  "host=db port=5432 dbname=ownvitals user=postgres" /app/db-functions/20-queue.sql
