#!/bin/sh
# Starts the Dart Chat server (continuwuity) using the add-on options that
# Home Assistant writes to /data/options.json.
set -eu

OPTIONS=/data/options.json
opt() { jq -r --arg k "$1" '.[$k] // empty' "$OPTIONS"; }

SERVER_NAME="$(opt server_name)"
REGISTRATION_TOKEN="$(opt registration_token)"
MAX_UPLOAD_MB="$(opt max_upload_mb)"
LOG_LEVEL="$(opt log_level)"
FIREBASE_ACCOUNT="$(opt firebase_service_account)"

if [ -z "$SERVER_NAME" ]; then
  echo "[dartchat] FATAL: set 'server_name' in the add-on configuration" >&2
  echo "[dartchat] It is the address friends will use, for example chat.example.com." >&2
  exit 1
fi

case "$SERVER_NAME" in
  *://* | */* | *" "*)
    echo "[dartchat] FATAL: server_name must be a bare host name such as chat.example.com" >&2
    exit 1
    ;;
esac

# The server name becomes part of every account (@name:server_name) and can
# never be changed once the database exists. Refuse to start if it differs.
mkdir -p /data/db
NAME_FILE=/data/server_name
if [ -f "$NAME_FILE" ]; then
  SAVED="$(cat "$NAME_FILE")"
  if [ "$SAVED" != "$SERVER_NAME" ]; then
    echo "[dartchat] FATAL: server_name was '$SAVED' when this server was created" >&2
    echo "[dartchat] and cannot be changed to '$SERVER_NAME'. Set it back." >&2
    exit 1
  fi
else
  printf '%s' "$SERVER_NAME" > "$NAME_FILE"
fi

export CONTINUWUITY_SERVER_NAME="$SERVER_NAME"
export CONTINUWUITY_DATABASE_PATH=/data/db
export CONTINUWUITY_ADDRESS=0.0.0.0
export CONTINUWUITY_PORT=6167
export CONTINUWUITY_MAX_REQUEST_SIZE="$((${MAX_UPLOAD_MB:-20} * 1024 * 1024))"
export CONTINUWUITY_LOG="${LOG_LEVEL:-info}"

# Dart Chat is a closed server: it does not talk to other Matrix servers.
export CONTINUWUITY_ALLOW_FEDERATION=false
export CONTINUWUITY_TRUSTED_SERVERS='[]'
export CONTINUWUITY_ALLOW_CHECK_FOR_UPDATES=false
export CONTINUWUITY_NEW_USER_DISPLAYNAME_SUFFIX=""

# Accounts can only be created by someone who knows the registration token.
# With no token set, nobody can sign up.
if [ -n "$REGISTRATION_TOKEN" ]; then
  export CONTINUWUITY_ALLOW_REGISTRATION=true
  export CONTINUWUITY_REGISTRATION_TOKEN="$REGISTRATION_TOKEN"
  echo "[dartchat] Sign-up is open to people who have the registration token"
else
  export CONTINUWUITY_ALLOW_REGISTRATION=false
  echo "[dartchat] Sign-up is closed (no registration token set)"
fi

# Notifications: a small gateway on this machine asks Firebase to wake phones.
# The chat server normally refuses to call local addresses, so the block list
# below is the default one minus this machine's own address (127.0.0.0/8).
export CONTINUWUITY_IP_RANGE_DENYLIST='["10.0.0.0/8","172.16.0.0/12","192.168.0.0/16","100.64.0.0/10","192.0.0.0/24","169.254.0.0/16","192.88.99.0/24","198.18.0.0/15","192.0.2.0/24","198.51.100.0/24","203.0.113.0/24","224.0.0.0/4","::1/128","fe80::/10","fc00::/7","2001:db8::/32","ff00::/8","fec0::/10"]'
if [ -n "$FIREBASE_ACCOUNT" ]; then
  (
    export DARTCHAT_FIREBASE_ACCOUNT="$FIREBASE_ACCOUNT"
    while true; do
      status=0
      python3 /push_gateway.py || status=$?
      [ "$status" -eq 78 ] && exit 0
      echo "[dartchat] Push gateway stopped (code $status), restarting in 5 seconds"
      sleep 5
    done
  ) &
else
  echo "[dartchat] Notifications are off (no Firebase service account set)"
fi
unset FIREBASE_ACCOUNT

echo "[dartchat] Starting chat server for $SERVER_NAME on port 6167"
exec /opt/continuwuity/lib/ld-linux-x86-64.so.2 \
  --library-path /opt/continuwuity/lib \
  /opt/continuwuity/conduwuit
