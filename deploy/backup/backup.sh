#!/bin/sh
# deploy/backup/backup.sh — operator backup for a K2P execution-plane deployment
# (ADR-0011; plan task C1). POSIX sh, no bashisms, set -eu.
#
# Produces ONE timestamped .tar.gz at a caller-supplied destination containing:
#   postgres/   custom-format pg_dump, taken through the running `db` compose service
#   objects/    mirror of the deployment's object bucket (server-side `mc mirror`)
#   config/     the form registry (backend/forms/) + the non-secret deployment env
#   MANIFEST.txt  member sizes + SHA-256, and the credential-path NAMES
#
# Credential material is NEVER included: the manifest lists credential
# paths by name only (files and directories under backend/var/ in a checkout,
# or deploy/var/credentials/ on the shipped compose deployment), with a
# pointer to the runbook's operator steps
# (docs/operations/backup-restore-runbook.md). On the shipped compose the
# backend's /app/var is bind-mounted from the host custody directory, so the
# credential files live on the host after all — escrow of that directory is
# the operator step the runbook documents; restoring the credential is an
# operator step either way.
#
# Prerequisites (documented in the runbook; the script refuses loudly, it does
# not half-run):
#   docker + the compose plugin  — the deployment's services must be up
#   mc (MinIO client)            — with an alias for the deployment's store
#                                  (name via MC_ALIAS, default "aics")
# Everything else comes from the operator's environment or the compose file;
# the script holds no credentials of its own and never runs inside the app.
#
# Usage:
#   deploy/backup/backup.sh --dest /srv/backups [--label pre-upgrade] [--dry-run]

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
COMPOSE_FILE="$ROOT/deploy/docker-compose.yml"

DEST=""
LABEL=""
DRY_RUN=0

fail() {
    printf 'backup: ERROR: %s\n' "$*" >&2
    exit 1
}

note() {
    printf 'backup: %s\n' "$*"
}

step() {
    # step <description> <command...> — print the plan, execute unless dry-run.
    _desc=$1
    shift
    if [ "$DRY_RUN" -eq 1 ]; then
        printf 'DRY-RUN  %s\n' "$_desc"
        printf 'DRY-RUN    ->'
        for _a in "$@"; do printf ' %s' "$_a"; done
        printf '\n'
    else
        note "$_desc"
        "$@"
    fi
}

usage() {
    printf 'usage: %s --dest DIR [--label NAME] [--dry-run]\n' "$0" >&2
    exit 2
}

while [ $# -gt 0 ]; do
    case $1 in
        --dest)
            [ $# -ge 2 ] || usage
            DEST=$2
            shift 2
            ;;
        --label)
            [ $# -ge 2 ] || usage
            LABEL=$2
            shift 2
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        *)
            usage
            ;;
    esac
done

[ -n "$DEST" ] || fail "missing --dest DIR"
[ -d "$DEST" ] || fail "destination $DEST is not a directory"
[ -w "$DEST" ] || fail "destination $DEST is not writable"
[ -f "$COMPOSE_FILE" ] || fail "compose file not found: $COMPOSE_FILE"

# The postgres service must be UP before any work happens — a pg_dump into an
# unwritable or half-live deployment is the half-run this script refuses to
# do. The check needs docker, so in dry-run (where docker may be missing) it
# is printed as a planned step instead of executed.
if [ "$DRY_RUN" -eq 1 ]; then
    printf 'DRY-RUN  check: compose service %s\n' '"db" is running (docker compose ps --status running)'
elif command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    docker compose -f "$COMPOSE_FILE" ps --services --status running 2>/dev/null | grep -qx db \
        || fail "the compose service 'db' is not running — start the deployment before backing up"
fi

# Names come from deploy/docker-compose.yml, not from this script: the default
# of "${VAR:-default}" is read from the compose file; an operator-exported
# variable of the same name still wins.
compose_default() {
    sed -n "s/^.*\\\${$1:-\([^}]*\)}.*\$/\1/p" "$COMPOSE_FILE" | sed -n '1p'
}

PG_USER=${POSTGRES_USER:-$(compose_default POSTGRES_USER)}
PG_DB=${POSTGRES_DB:-$(compose_default POSTGRES_DB)}
BUCKET=${AICS_S3_BUCKET:-$(compose_default AICS_S3_BUCKET)}
MC_ALIAS=${MC_ALIAS:-aics}

[ -n "$PG_USER" ] || fail "could not read POSTGRES_USER from $COMPOSE_FILE"
[ -n "$PG_DB" ] || fail "could not read POSTGRES_DB from $COMPOSE_FILE"
[ -n "$BUCKET" ] || fail "could not read AICS_S3_BUCKET from $COMPOSE_FILE"

# ── Prerequisites ─────────────────────────────────────────────────────────────
# In dry-run a missing prerequisite is reported, not fatal: the point of a dry
# run is to see the whole plan on a machine that may not be the deployment host.
missing=0
if ! command -v docker >/dev/null 2>&1; then
    note "prerequisite MISSING: docker (the deployment host needs docker + the compose plugin)"
    missing=1
elif ! docker compose version >/dev/null 2>&1; then
    note "prerequisite MISSING: the docker compose plugin"
    missing=1
fi
if ! command -v mc >/dev/null 2>&1; then
    note "prerequisite MISSING: mc — the MinIO client (https://min.io/docs/minio/linux/reference/minio-mc.html)"
    missing=1
elif ! mc alias list "$MC_ALIAS" >/dev/null 2>&1; then
    note "prerequisite MISSING: mc alias '$MC_ALIAS' (configure: mc alias set $MC_ALIAS <endpoint> <access-key> <secret-key>)"
    missing=1
fi
if [ "$missing" -eq 1 ] && [ "$DRY_RUN" -eq 0 ]; then
    fail "missing prerequisites (see above); refusing to half-run"
fi

TS=$(date -u +%Y%m%dT%H%M%SZ)
ARCHIVE="aics-backup-$TS"
if [ -n "$LABEL" ]; then
    case $LABEL in
        *[!A-Za-z0-9._-]*) fail "--label must be [A-Za-z0-9._-] (got: $LABEL)" ;;
    esac
    ARCHIVE="$ARCHIVE-$LABEL"
fi
ARCHIVE="$ARCHIVE.tar.gz"

# ── Plan ──────────────────────────────────────────────────────────────────────
note "plan: archive $ARCHIVE in $DEST"
note "plan: source compose $COMPOSE_FILE (services db + minio, bucket $BUCKET, pg user/db $PG_USER/$PG_DB)"
note "plan: members: postgres/ (pg_dump -Fc), objects/ (mc mirror of $MC_ALIAS/$BUCKET), config/ (forms + non-secret env)"
note "plan: credential paths are listed by NAME ONLY in the manifest; their contents are never read"

if [ "$DRY_RUN" -eq 1 ]; then
    STAGE="${TMPDIR:-/tmp}/aics-backup.<staging>"
else
    STAGE=$(mktemp -d "${TMPDIR:-/tmp}/aics-backup.XXXXXX")
    trap 'rm -rf "$STAGE"' EXIT HUP INT TERM
    mkdir -p "$STAGE/postgres" "$STAGE/objects" "$STAGE/config"
fi

# ── 1. PostgreSQL custom-format dump, through the running compose service ─────
if [ "$DRY_RUN" -eq 1 ]; then
    printf 'DRY-RUN  1. pg_dump through the db service\n'
    printf 'DRY-RUN    -> docker compose -f %s exec -T db pg_dump -U %s -d %s -Fc > %s/postgres/custom.dump\n' \
        "$COMPOSE_FILE" "$PG_USER" "$PG_DB" "$STAGE"
else
    note "1. pg_dump through the db service (user=$PG_USER db=$PG_DB)"
    docker compose -f "$COMPOSE_FILE" exec -T db \
        pg_dump -U "$PG_USER" -d "$PG_DB" -Fc > "$STAGE/postgres/custom.dump"
fi

# ── 2. Object-data mirror of the deployment's bucket (server-side) ────────────
step "2. mc mirror of bucket $BUCKET (alias $MC_ALIAS)" \
    mc mirror "$MC_ALIAS/$BUCKET" "$STAGE/objects/"

# ── 3. Config bundle: form registry + non-secret deployment env ───────────────
if [ ! -d "$ROOT/backend/forms" ]; then
    fail "form registry not found at $ROOT/backend/forms (run from a checkout of the deployment's revision)"
fi
step "3a. form registry backend/forms/" cp -R "$ROOT/backend/forms" "$STAGE/config/forms"

# The deployment's non-secret env: .env.example verbatim, plus a filtered
# copy of .env in which any line whose KEY matches the credential pattern
# (PASSWORD|SECRET|KEY|TOKEN) is replaced. The filter is a key-name
# heuristic: it does not catch a secret embedded in another variable's
# value, nor a commented-out secret line — non-matching lines pass through
# verbatim. Treat env.redacted as deployment-sensitive and encrypt it with
# the archive anyway (the runbook says so).
step "3b. non-secret deployment env" cp "$ROOT/.env.example" "$STAGE/config/env.example"
if [ -f "$ROOT/.env" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        printf 'DRY-RUN  3c. filtered .env (lines whose KEY matches PASSWORD|SECRET|KEY|TOKEN replaced)\n'
    else
        note "3c. filtered .env (lines whose KEY matches PASSWORD|SECRET|KEY|TOKEN replaced)"
        {
            printf '# Filtered copy of the deployment .env: lines whose KEY matches\n'
            printf '# PASSWORD|SECRET|KEY|TOKEN were replaced with <redacted>. The filter\n'
            printf '# is a key-name heuristic — value-embedded secrets and commented-out\n'
            printf '# secret lines are NOT caught, so treat this file as deployment-\n'
            printf '# sensitive: encrypt it with the archive before any off-box copy.\n'
            printf '# Restore the real values by hand (the runbook carries the steps).\n'
            sed -E 's/^([A-Za-z0-9_]*(PASSWORD|SECRET|KEY|TOKEN)[A-Za-z0-9_]*)=.*/\1=<redacted>/' "$ROOT/.env"
        } > "$STAGE/config/env.redacted"
    fi
else
    note "3c. no $ROOT/.env — skipped"
fi

# ── 4. Manifest: member sizes + SHA-256, credential path names, never contents ─
if [ "$DRY_RUN" -eq 1 ]; then
    printf 'DRY-RUN  4. manifest with member sizes + SHA-256; credential paths by name only\n'
else
    note "4. writing manifest"
    {
        printf '# AICS backup manifest\n'
        printf 'created_utc: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf 'archive: %s\n' "$ARCHIVE"
        printf 'label: %s\n' "${LABEL:-none}"
        printf 'compose: %s\n' "$COMPOSE_FILE"
        printf 'bucket: %s (mc alias %s)\n' "$BUCKET" "$MC_ALIAS"
        printf 'postgres: user=%s db=%s format=custom\n' "$PG_USER" "$PG_DB"
        printf 'members_sha256:\n'
        # rows: sha256, size (bytes), path — paths in this tree never contain
        # spaces. MANIFEST.txt is excluded: it is being written while the
        # find runs, so it could only list itself truncated.
        find "$STAGE" -type f ! -name MANIFEST.txt -print | sort | while IFS= read -r f; do
            size=$(wc -c < "$f")
            hash=$(sha256sum "$f" | cut -d' ' -f1)
            printf '%s  %s  %s\n' "$hash" "$size" "${f#"$STAGE"/}"
        done
        printf '# Credential material is NEVER included in a backup archive.\n'
        printf '# The backend credential layer (mailbox credential, activation token,\n'
        printf '# tenant key) lives under backend/var/ in a checkout, or in the host\n'
        printf '# custody directory (deploy/var/credentials/, bind-mounted to the\n'
        printf "# container's /app/var) on the shipped compose deployment. Escrow of\n"
        printf '# that directory — the tenant key above all — is the operator step\n'
        printf '# in docs/operations/backup-restore-runbook.md; the paths below are\n'
        printf '# names only (whatever exists in this checkout); contents are never read.\n'
        printf 'credential_paths_names_only:\n'
        if [ -d "$ROOT/backend/var" ]; then
            find "$ROOT/backend/var" -mindepth 1 -maxdepth 1 \( -type f -o -type d \) -print | sort | while IFS= read -r d; do
                printf '  - %s\n' "${d#"$ROOT"/}"
            done
        fi
        if [ -d "$ROOT/deploy/var/credentials" ]; then
            find "$ROOT/deploy/var/credentials" -mindepth 1 -maxdepth 1 \( -type f -o -type d \) -print | sort | while IFS= read -r d; do
                printf '  - %s\n' "${d#"$ROOT"/}"
            done
        fi
    } > "$STAGE/MANIFEST.txt"
fi

# ── 5. One timestamped .tar.gz at the destination ─────────────────────────────
if [ "$DRY_RUN" -eq 1 ]; then
    printf 'DRY-RUN  5. tar -C %s -czf %s/%s .\n' "$STAGE" "$DEST" "$ARCHIVE"
    printf 'DRY-RUN  5b. copy MANIFEST.txt next to the archive\n'
    printf 'DRY-RUN  done — nothing was executed\n'
else
    note "5. writing $DEST/$ARCHIVE"
    tar -C "$STAGE" -czf "$DEST/$ARCHIVE" .
    cp "$STAGE/MANIFEST.txt" "$DEST/MANIFEST-$TS.txt"
    note "done: $DEST/$ARCHIVE (+ $DEST/MANIFEST-$TS.txt)"
    note "next (operator steps, runbook docs/operations/backup-restore-runbook.md):"
    note "  1. verify the manifest: extract the archive and compare each member's"
    note "     size and SHA-256 against MANIFEST.txt"
    note "  2. encrypt before any off-box copy (age or gpg)"
    note "  3. copy the encrypted archive off-box"
fi
