#!/bin/sh
# Turn the platform's environment variables into the files the CLI expects, then serve.
#
# `set -e` and `set -u` only. Deliberately never `set -x`: this script handles a keystore and
# its password, and a trace would print both into the platform's log, where they would be
# retained, searchable, and visible to anyone with read access to the project.
set -eu

KEYS=/run/wrasse
SERVICE_USER=wrasse
umask 077

# --- the volume, which the platform hands over owned by root ------------------------------
#
# The mount replaces whatever the image had at this path, so nothing in the Dockerfile can
# prepare it and the ownership has to be taken here, while this script is still root. Only the
# directories the service writes; the seeded source is left exactly as it was uploaded.
# Directories are taken directly. An earlier version derived them by appending a sentinel to
# each variable and taking `dirname`, which turned an unset `WRASSE_SESSION_ROOT` into `/x`,
# whose dirname is `/`, and so an omitted setting became `chown wrasse:wrasse /`. A variable
# that is not set must mean "nothing to take", never "take the filesystem root".
take () {
    directory=$1
    case "$directory" in
        ""|"/"|"."|".."|"//") 
            echo "refusing to take ownership of '$directory'" >&2
            exit 1 ;;
    esac
    mkdir -p "$directory"
    chown "$SERVICE_USER:$SERVICE_USER" "$directory"
}

take_volume () {
    for path in "${WRASSE_TX_DB:-}" "${WRASSE_LIABILITY_DB:-}" "${WRASSE_BUYER_MEMORY_PATH:-}" \
                "${WRASSE_PROVIDER_MEMORY_PATH:-}" "${WRASSE_COLD_BUYER_MEMORY_PATH:-}" \
                "${WRASSE_COLD_PROVIDER_MEMORY_PATH:-}"; do
        [ -n "$path" ] || continue
        take "$(dirname "$path")"
    done
    # The session root is a directory already, so it is taken as one rather than having a
    # sentinel appended to make it look like a file path.
    [ -n "${WRASSE_SESSION_ROOT:-}" ] && take "$WRASSE_SESSION_ROOT"
    return 0
}

if [ "$(id -u)" = "0" ]; then
    take_volume
fi

require () {
    eval "value=\${$1:-}"
    if [ -z "$value" ]; then
        echo "$1 is not set. $2" >&2
        exit 1
    fi
}

# --- secrets, from the platform's store into files the CLI can read -----------------------
#
# `load_signer` takes paths because that is what a Foundry keystore is, and putting a key in
# an environment variable it reads directly would put the decrypted material one `os.environ`
# dump away from every subprocess. These three files are 0600, live outside the volume, and
# die with the container.
# --- secrets, only when this deployment is the one that signs -----------------------------
#
# Guarded on the execution flag rather than on the variables being present. `/api/health`
# derives `holds_keys` from that flag alone, so materialising a keystore on a read-only
# deployment that still carries the secret variables would make that answer false: nothing
# would sign, which is right, and the file would be on disk, which the health answer denies.
if [ "${WRASSE_ENABLE_EXECUTION:-}" != "1" ]; then
    unset WRASSE_BUYER_KEYSTORE_JSON WRASSE_PROVIDER_KEYSTORE_JSON WRASSE_KEYSTORE_PASSWORD \
        2>/dev/null || true
fi

if [ -n "${WRASSE_BUYER_KEYSTORE_JSON:-}" ]; then
    printf '%s' "$WRASSE_BUYER_KEYSTORE_JSON" > "$KEYS/buyer-keystore.json"
    WRASSE_KEYSTORE="$KEYS/buyer-keystore.json"
    export WRASSE_KEYSTORE
fi
if [ -n "${WRASSE_PROVIDER_KEYSTORE_JSON:-}" ]; then
    printf '%s' "$WRASSE_PROVIDER_KEYSTORE_JSON" > "$KEYS/provider-keystore.json"
    WRASSE_PROVIDER_A_KEYSTORE="$KEYS/provider-keystore.json"
    export WRASSE_PROVIDER_A_KEYSTORE
fi
if [ -n "${WRASSE_KEYSTORE_PASSWORD:-}" ]; then
    printf '%s' "$WRASSE_KEYSTORE_PASSWORD" > "$KEYS/keystore.password"
    WRASSE_KEYSTORE_PASSWORD_FILE="$KEYS/keystore.password"
    export WRASSE_KEYSTORE_PASSWORD_FILE
    # The plaintext is not left in the environment for every subprocess to inherit.
    unset WRASSE_KEYSTORE_PASSWORD
fi
unset WRASSE_BUYER_KEYSTORE_JSON WRASSE_PROVIDER_KEYSTORE_JSON 2>/dev/null || true

# --- the settings without which the service would answer something untrue ------------------
require WRASSE_ESCROW_ADDRESS "The service quotes and settles against one deployment."
require WRASSE_BUYER_ADDRESS  "Both sides of the negotiation are named in every commitment."
require WRASSE_PROVIDER_A_ADDRESS "Both sides of the negotiation are named in every commitment."
require BASE_SEPOLIA_RPC_URL  "Quoting works without it; nothing else does."

if [ "${WRASSE_ENABLE_EXECUTION:-}" = "1" ]; then
    require WRASSE_KEYSTORE "Execution is enabled, so this deployment signs and needs the key."
    require WRASSE_PROVIDER_A_KEYSTORE "Execution is enabled and the provider acts too."
    require WRASSE_KEYSTORE_PASSWORD_FILE "A keystore without its password decrypts nothing."
fi

# --- the memories, which must be supplied rather than created ------------------------------
#
# `prepare_working_copies` already refuses to serve a configured-but-empty source, and the
# reason is worth repeating here: an empty store and a missing mount both quote as a cold
# start, so a service that fell back to creating one would answer "this system remembers
# nothing" in a voice indistinguishable from the truth.
if [ -n "${WRASSE_MEMORY_SOURCE_DIR:-}" ] && [ ! -f "${WRASSE_MEMORY_SOURCE_DIR}/buyer-memory.db" ]; then
    echo "${WRASSE_MEMORY_SOURCE_DIR}/buyer-memory.db is missing." >&2
    echo "Seed the volume before the first boot; see docs/DEPLOY.md." >&2
    exit 1
fi

# The keystores are written above with umask 077, so they are root-owned and unreadable to the
# service until this hands them over. Ownership rather than a looser mode: 0600 owned by the
# user that reads them is the narrowest thing that works.
if [ "$(id -u)" = "0" ]; then
    chown -R "$SERVICE_USER:$SERVICE_USER" "$KEYS"
    exec gosu "$SERVICE_USER" uvicorn wrasse.service:app --host 0.0.0.0 --port "${PORT:-8000}"
fi

exec uvicorn wrasse.service:app --host 0.0.0.0 --port "${PORT:-8000}"
