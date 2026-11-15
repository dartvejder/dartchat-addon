# Dart Chat Server

This add-on runs the Dart Chat server on your Home Assistant machine. Messages
are end-to-end encrypted by the apps, so the server stores only encrypted
content.

## Configuration

| Option | What it does |
| --- | --- |
| `server_name` | The address friends connect to, for example `chat.example.com`. Required. |
| `registration_token` | A secret that lets someone create an account. Leave empty to close sign-up. |
| `firebase_service_account` | Turns on phone notifications. Paste the whole contents of the Firebase service account key file (JSON). Leave empty for no notifications. |
| `max_upload_mb` | Largest photo or file upload, in megabytes. Default 20. |
| `log_level` | How much the server logs: `error`, `warn`, `info` or `debug`. |

**Choose `server_name` carefully.** It becomes part of every account name
(`@anna:chat.example.com`) and cannot be changed later. The add-on refuses to
start if you change it after the first start. Use the exact host name you
will route through your Cloudflare Tunnel.

## First start

1. Set `server_name` and start the add-on.
2. Open the **Log** tab. On the very first start the server prints a welcome
   message with a one-time registration token.
3. Create the first account with that token. The first account becomes the
   server administrator. Until the Dart Chat app is available, any Matrix app
   (for example Element) can do this: choose "other homeserver", enter your
   server address and register.
4. After the first account exists, the one-time token stops working and the
   `registration_token` option takes over.

## Connecting it to the internet

The server listens on port 6167 without encryption, which is what a
Cloudflare Tunnel expects. In the Cloudflared add-on, add a host entry that
sends your chat host name to `http://<Home Assistant IP>:6167`. Do not forward
port 6167 on your router: the tunnel provides HTTPS, and plain port 6167
should stay inside your home network.

To check that it works, open `https://<server_name>/_matrix/client/versions`
in a browser. You should see a short block of text starting with `versions`.

## What is switched off

- Federation: the server never talks to other Matrix servers.
- Open sign-up: an account needs a registration token.
- Update checks and other calls to outside services.

## Backups

All data lives in the add-on's data folder and is included in Home Assistant
backups. The add-on is stopped briefly while a backup is taken so the database
is copied in a consistent state. Keep at least one backup copy off this
machine: it holds the accounts and the encrypted message history.

## Hardware

This add-on supports 64-bit Intel and AMD machines (amd64).

## Notifications

Phones are woken through Firebase Cloud Messaging. The server only ever sends
Firebase "something new in this chat" with an unread count. The message text,
the sender and the chat name never leave your server unencrypted.

To turn notifications on:

1. In the Firebase console open **Project settings**, then **Service accounts**,
   and click **Generate new private key**. A JSON file downloads.
2. Open that file in a text editor, copy everything, and paste it into the
   `firebase_service_account` option. Save and restart the app.
3. The log shows `[dartchat-push] Ready` when it works.

That key is a secret. Keep it out of GitHub and chats. If it leaks, delete it
in the Firebase console and generate a new one.
