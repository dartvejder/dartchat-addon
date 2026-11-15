# Changelog

## 0.12.1

- Group rules: the owner can give the bot a profile picture, or remove it, from the app (Group rules, "Bot picture").

## 0.12.0

- Link previews for the web version: a browser may not read other websites, so the web app asks the server to fetch a link's page and picture. Only signed-in people can ask, only public websites are fetched (never the home network or this server), and only pages and pictures up to 1 MB. The website sees the server's address instead of the sender's, and the server sees which links are previewed from the web. The preview still travels inside the encrypted message. The phone apps fetch previews themselves as before.

## 0.11.1

- Group rules: after the server app restarted, the bot no longer knew which groups it was in. Changing a group's rules then failed with "Only a group admin can add the group bot", and the bot stayed silent in its groups until someone joined or left. It now reloads its groups on every start and asks the chat server which groups it is in.

## 0.11.0

- When someone changes their display name, the owner's phones get a notice. Like every notice it names nobody; the owner's accounts list in the app shows the new name and the one before it. A person's first name and the owner's own changes are not reported.

## 0.10.1

- Ready for the Dart Chat web version: the server now answers browsers on another address (such as web.dartchat.trade) for sign-in, sign-up, invitations, circles, groups and the bot, as it already did for plain chat traffic.

## 0.10.0

- Timed bans and mutes in groups, for the owner only. A ban removes someone from a group for a chosen time; the server lifts it by itself and invites the person back. A mute leaves them in the group, able to read but not to write, until the time is up. Group admins and members cannot use either.
- The group bot takes the owner's commands `!ban @name 1d`, `!mute @name 2h`, `!unban @name` and `!unmute @name` (m, h, d or w for the length) in groups where it is switched on. From anyone else they are refused.
- A banned or muted person's app can ask when it ends.

## 0.9.0

- Scheduled messages: a phone can hand over a message to be sent later, at a time the sender chose. The phone encrypts it first, so the server holds only ciphertext and never sees the text. The server sends it at the right time even if the phone is off. The sender can list and cancel what is waiting.

## 0.8.2

- The storage email always shows the Messages section, with zeros and the day counting began while nothing has been counted yet.

## 0.8.1

- Fix: updating to 0.8.0 with `storage_report_email` left empty was refused by Home Assistant. An empty address now simply turns the email off.

## 0.8.0

- Daily storage email: set `storage_report_email` and the server emails you every day (at `storage_report_time`, default 08:00) how much space Dart Chat uses (photos and files, database, other), how much is free on the disk, how much it grew per day, month and year, and how many messages were sent in each group and in private chats overall. See "Daily storage email" in the documentation.

## 0.7.0

- The owner's phone gets a "Someone you invited has joined" notification when an invited friend creates their account. The notification names nobody; the app shows who it was under Invitations and accounts. It reaches the phones the owner has opened the invitation screen on.
- Groups are now the owner's business: only the owner can create a group or add people to one. Everyone else can still start a chat with one person (inside their circle) and accept group invitations. The server enforces this, so it also holds for other Matrix apps. Groups that already exist stay as they are, but their members can no longer add people.

## 0.6.0

- Notifications for the iPhone app: iPhones get a fixed "New message" notice with the unread count on the app icon, and the counter clears when everything is read on another device. The notice never contains the message, the sender or the chat name. Android phones work as before.

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
