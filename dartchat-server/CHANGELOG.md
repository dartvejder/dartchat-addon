# Changelog

## 0.5.0

- Group rules: a bot account ("Dart Bot") that brings the Signal link bot's rules to Dart Chat groups: repost check, same news from another site, a watched-name counter, `!lista`/`!stats`/`!top` commands, a midnight report and Home Assistant sensors. The owner switches each rule on or off per group in the app; the bot joins only those groups.
- New options for the bot's texts, see the Group rules section of the documentation.
- The add-on can now publish sensors to Home Assistant (`homeassistant_api`).

## 0.4.0

- Circles: the owner can sort accounts into circles from the app. Two people can find, see and message each other only if they share a circle; to everyone else the username looks like it does not exist. The owner sees everyone and everyone sees the owner. While no circle exists, nothing changes.
- The server enforces this itself (search, profile lookup, new chats, group invitations, encryption key lookup), so it also holds for other Matrix apps.
- Strangers can no longer test which usernames exist: sign-in gives the same answer for a wrong password and an unknown username, and direct sign-up is closed once the first account exists (unless `registration_token` is set).

## 0.3.0

- Invitations: the owner invites friends by email from inside the app. Every invitation has its own code that works once. Paste a Resend API key into the new `resend_api_key` option to have the emails sent; without it the app shows the code so you can pass it on yourself.
- Owner tools for the app: revoke an unused invitation, list accounts, deactivate an account.
- Sign-up no longer needs `registration_token`. Leave it empty for invitation-only sign-up.
- The apps still connect to port 6167. Inside the add-on a small front door now shares that port between the chat server and the invitation service.

## 0.2.0

- Notifications: a built-in push gateway asks Firebase to wake phones when there is something new. The wake-up never contains the message, the sender or the chat name. Turn it on by pasting a Firebase service account key into the new `firebase_service_account` option.

## 0.1.0

- First version: continuwuity 26.9.4 running as a closed, non-federated chat server.
