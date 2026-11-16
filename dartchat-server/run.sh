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
RESEND_KEY="$(opt resend_api_key)"
INVITE_FROM="$(opt invite_email_from)"
APP_LINK="$(opt app_link)"
TIMEZONE="$(opt timezone)"
REPORT_EMAIL="$(opt storage_report_email)"
REPORT_TIME="$(opt storage_report_time)"

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
# The chat server itself listens only inside this add-on. The front door on
# port 6167 (further down) passes requests on to it.
export CONTINUWUITY_ADDRESS=127.0.0.1
export CONTINUWUITY_PORT=6166
export CONTINUWUITY_MAX_REQUEST_SIZE="$((${MAX_UPLOAD_MB:-20} * 1024 * 1024))"
export CONTINUWUITY_LOG="${LOG_LEVEL:-info}"

# Dart Chat is a closed server: it does not talk to other Matrix servers.
export CONTINUWUITY_ALLOW_FEDERATION=false
export CONTINUWUITY_TRUSTED_SERVERS='[]'
export CONTINUWUITY_ALLOW_CHECK_FOR_UPDATES=false
export CONTINUWUITY_NEW_USER_DISPLAYNAME_SUFFIX=""
# Only the owner may list a chat publicly, so circles cannot meet that way.
export CONTINUWUITY_LOCKDOWN_PUBLIC_ROOM_DIRECTORY=true
export CONTINUWUITY_ALLOW_PUBLIC_ROOM_DIRECTORY_WITHOUT_AUTH=false

# Accounts can only be created with a registration token. Friends never see
# it: they use a personal invitation code, and the invitation service on this
# machine registers them with a random token that is generated here once.
# Setting 'registration_token' by hand replaces it with a code you can share.
TOKEN_FILE=/data/internal_registration_token
if [ ! -s "$TOKEN_FILE" ]; then
  (umask 077 && head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$TOKEN_FILE")
fi
SHARED_TOKEN=0
if [ -n "$REGISTRATION_TOKEN" ]; then
  SHARED_TOKEN=1
  echo "[dartchat] Sign-up is open to invited people and to anyone with the registration token"
else
  REGISTRATION_TOKEN="$(cat "$TOKEN_FILE")"
  echo "[dartchat] Sign-up is by invitation only"
fi
export CONTINUWUITY_ALLOW_REGISTRATION=true
export CONTINUWUITY_REGISTRATION_TOKEN="$REGISTRATION_TOKEN"

# Runs a helper and starts it again if it stops. Exit code 78 means its
# settings are wrong, so there is no point in trying again.
keep_running() {
  name="$1"
  shift
  while true; do
    status=0
    "$@" || status=$?
    [ "$status" -eq 78 ] && exit 0
    echo "[dartchat] $name stopped (code $status), restarting in 5 seconds"
    sleep 5
  done
}

# Invitations: friends create accounts with a personal code, and the owner
# manages invitations and accounts from the app.
(
  export DARTCHAT_SERVER_NAME="$SERVER_NAME"
  export DARTCHAT_REGISTRATION_TOKEN="$REGISTRATION_TOKEN"
  export DARTCHAT_SHARED_TOKEN="$SHARED_TOKEN"
  export DARTCHAT_RESEND_KEY="$RESEND_KEY"
  export DARTCHAT_INVITE_FROM="$INVITE_FROM"
  export DARTCHAT_APP_LINK="$APP_LINK"
  export DARTCHAT_TIMEZONE="$TIMEZONE"
  keep_running "Invitation service" python3 /invite_service.py
) &
# Daily storage email: how much space the chat uses, how fast it grows, and
# how many messages were sent, mailed to the owner once a day.
(
  export DARTCHAT_SERVER_NAME="$SERVER_NAME"
  export DARTCHAT_RESEND_KEY="$RESEND_KEY"
  export DARTCHAT_INVITE_FROM="$INVITE_FROM"
  export DARTCHAT_TIMEZONE="$TIMEZONE"
  export DARTCHAT_STORAGE_REPORT_EMAIL="$REPORT_EMAIL"
  export DARTCHAT_STORAGE_REPORT_TIME="$REPORT_TIME"
  keep_running "Storage report" python3 /storage_report.py
) &
# Group rules: a bot account that applies the owner's rules (repost check
# and the rest) in the groups the owner switches on in the app.
(
  export DARTCHAT_SERVER_NAME="$SERVER_NAME"
  export DARTCHAT_REGISTRATION_TOKEN="$REGISTRATION_TOKEN"
  export PYTHONPATH=/opt/group_bot/lib:/opt/group_bot
  keep_running "Group bot" python3 -m group_bot
) &
unset RESEND_KEY REGISTRATION_TOKEN

# The web version runs on its own address (web.dartchat.trade), so the
# browser asks first whether it may call this server. The chat server answers
# that itself; the Dart Chat services behind the front door get the same
# answer here. Sign-in is by token, never by cookie, so any site may ask.
cat > /tmp/cors.conf <<'EOF'
add_header Access-Control-Allow-Origin "*" always;
add_header Access-Control-Allow-Methods "GET, POST, PUT, DELETE, OPTIONS" always;
add_header Access-Control-Allow-Headers "Authorization, Content-Type, Accept, X-Requested-With" always;
EOF
cat > /tmp/cors-proxy.conf <<'EOF'
if ($request_method = OPTIONS) {
  include /tmp/cors.conf;
  add_header Access-Control-Max-Age 86400 always;
  return 204;
}
include /tmp/cors.conf;
proxy_hide_header Access-Control-Allow-Origin;
proxy_hide_header Access-Control-Allow-Methods;
proxy_hide_header Access-Control-Allow-Headers;
EOF

# Front door: one port for the apps. Chat traffic goes to the chat server and
# /_dartchat/ goes to the invitation service.
cat > /tmp/nginx.conf <<EOF
user nginx;
worker_processes 1;
daemon off;
error_log stderr warn;
pid /tmp/nginx.pid;
events { worker_connections 1024; }
http {
  access_log off;
  server_tokens off;
  client_max_body_size ${MAX_UPLOAD_MB:-20}m;
  proxy_http_version 1.1;
  proxy_set_header Connection "";
  proxy_set_header Host \$host;
  proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
  server {
    listen 6167;
    location = /_dartchat/v1/webpush/key {
      include /tmp/cors-proxy.conf;
      proxy_pass http://127.0.0.1:5000;
    }
    location /_dartchat/v1/bot/ {
      include /tmp/cors-proxy.conf;
      proxy_pass http://127.0.0.1:6169;
    }
    location /_dartchat/ {
      include /tmp/cors-proxy.conf;
      proxy_pass http://127.0.0.1:6168;
    }
    # Requests that could reveal or contact another account are checked
    # against the owner's circles before they reach the chat server.
    location ~ ^/_matrix/client/[^/]+/([^/]+/)?(user_directory/search|profile/|presence/|createRoom|rooms/[^/]+/invite|rooms/[^/]+/state/m\\.room\\.member/|keys/query|keys/claim|login|register|rooms/[^/]+/send/) {
      include /tmp/cors-proxy.conf;
      proxy_pass http://127.0.0.1:6168;
      proxy_read_timeout 120s;
    }
    # This server does not talk to other chat servers.
    location /_matrix/federation/ {
      return 404;
    }
    location / {
      proxy_pass http://127.0.0.1:6166;
      proxy_read_timeout 600s;
      proxy_send_timeout 600s;
      proxy_buffering off;
      proxy_request_buffering off;
    }
  }
}
EOF
( keep_running "Front door" nginx -c /tmp/nginx.conf ) &

# Notifications: a small gateway on this machine asks Firebase to wake phones
# and browsers' push services to wake the web version.
# The chat server normally refuses to call local addresses, so the block list
# below is the default one minus this machine's own address (127.0.0.0/8).
export CONTINUWUITY_IP_RANGE_DENYLIST='["10.0.0.0/8","172.16.0.0/12","192.168.0.0/16","100.64.0.0/10","192.0.0.0/24","169.254.0.0/16","192.88.99.0/24","198.18.0.0/15","192.0.2.0/24","198.51.100.0/24","203.0.113.0/24","224.0.0.0/4","::1/128","fe80::/10","fc00::/7","2001:db8::/32","ff00::/8","fec0::/10"]'
# Browsers (the web version) are woken through their own push service; that
# needs no Firebase, so the gateway always runs.
if [ -z "$FIREBASE_ACCOUNT" ]; then
  echo "[dartchat] Phone notifications are off (no Firebase service account set)"
fi
(
  export DARTCHAT_FIREBASE_ACCOUNT="$FIREBASE_ACCOUNT"
  export DARTCHAT_SERVER_NAME="$SERVER_NAME"
  keep_running "Push gateway" python3 /push_gateway.py
) &
unset FIREBASE_ACCOUNT

echo "[dartchat] Starting chat server for $SERVER_NAME on port 6167"
exec /opt/continuwuity/lib/ld-linux-x86-64.so.2 \
  --library-path /opt/continuwuity/lib \
  /opt/continuwuity/conduwuit
