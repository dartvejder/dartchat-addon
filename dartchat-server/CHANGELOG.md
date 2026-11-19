# Changelog

## 0.22.0

- The invitation email shows the Dart Chat poster under its first line, linked to the invitation, with a small "*neke stvari su malo preuveličane 😄" under it. The picture comes from `<web_link>/invite-poster.jpg`, so it shows only when `web_link` is set; nothing is attached to the email.

## 0.21.0

- Looks from the server. The add-on now carries the app's themes, colour schemes, backgrounds, message marks and reply looks as data, plus new ones the app never had (themes Sveska, Svemir, Arkada and Strip, colour schemes Mint staklo, Beton and Oblak, picture backgrounds Doodle, Sveska and Zvezde, the Breskva gradient, marks Pivo, Pikado, Kafa and Semafor), three fonts (Comic Neue, Patrick Hand, Press Start 2P, all under the SIL Open Font License), a sticker pack and a GIF pack. New looks need only an add-on update from now on; apps from the next Dart Chat version on pick them up by themselves.
- `GET /_dartchat/v1/looks` is the catalog (about 13 KB, 4 KB gzipped, asked again with ETag, so an unchanged catalog costs a 304 of a few hundred bytes). Pictures and fonts are under `/_dartchat/v1/looks/files/`, named by their SHA-256 and cached for a year. Both are plain files served by nginx, with no sign-in.
- Packs of your own: put them in `/share/dartchat/looks` in the same layout as `looks/` in this repository (an item with the same id replaces the built-in one) and restart the add-on. The log lists anything left out and why.
- New options: `features` (for example `stickers=off, gifs=off`; the switches are stickers, gifs, link_previews, voice_to_text, scheduled_send, polls and location, all on by default), `banner_text` (a notice at the top of everyone's chat list), `quick_reactions` (the reaction row for people who never changed theirs, comma separated), `invite_share_text` (the message the app shares on WhatsApp, Viber or SMS; `{link}` and `{code}` are filled in), `invite_subject` and `invite_intro` (the invitation email's subject and first line; `{inviter}` is filled in).
- Older apps ignore all of this and keep working as before.

## 0.20.0

- Invitations for every device. The inviter picks the friend's device (Android, iPhone, web, computer, or none for all of them) and the email shows a button for each: Google Play (`app_link`), TestFlight (new `iphone_link`, default the public Dart Chat TestFlight link), the web app and the Windows and Linux downloads (new `web_link`). When only phones are picked, the email still mentions the Windows, Linux and web versions in a short note. The email also carries one smart link, `<web_link>/join.html#CODE`, which picks the right steps for the phone that opens it.
- An invitation can go out on several channels at once: `channels` is any of `email`, `link` (the app shares the smart link through Viber, WhatsApp or SMS) and `qr` (the app shows it as a QR code). Without email a name is enough; older apps that send only an address keep working (email).
- `POST /_dartchat/v1/admin/invites/<id>/resend` sends an invitation again on channels picked anew. A waiting invitation keeps its code; an expired or cancelled one gets a new code.
- New invitations stop working after 7 days (`DARTCHAT_INVITE_EXPIRED`). Invitations made before this version do not expire.
- The invitation list shows what became of each email (delivered, bounced, ...) as the email service reports it, when and how it was last sent, and whether an address is on the Play Store tester list, which the owner marks with `PUT /_dartchat/v1/admin/play-testers`.
- Creating an account answers with `inviter`, so the app can open a chat with the person who sent the invitation.

## 0.19.0

- Friends can make groups now, not just the owner: a group (or an invitation into one) is allowed when everyone in it, apart from the owners and the group bot, is in one circle together with the person making it. Mixing two circles, public groups and groups from an account in no circle are refused with `DARTCHAT_NOT_ONE_CIRCLE`. The owner may still make any group.
- New `GET /_dartchat/v1/people`: the people the signed-in account can start a chat with (the owner: every account; a friend: their circles and the owners; while no circle exists: everyone), whether they may make groups, and their own circles, for the app's pick lists.

## 0.18.0

- "Show when I am online" switched off in the app now really hides it: friends asking the server get "offline" with no "last seen" time. The app sends `dartchat_hidden` with its own presence update; the front door keeps the list in `/data/hidden_presence.json` and takes the field off before the chat server sees it.
- Taking someone out of a circle (or deleting a circle) also closes the private chats between people who no longer share a circle: both leave it, through the owner's admin commands, in the background. Groups stay, since the owner decides who is in those. A chat where the other person never accepted the invitation is not closed.

## 0.17.0

- Voice messages to text. The app sends a voice message it has decrypted to `/_dartchat/v1/transcribe`; the server turns it into 16 kHz sound with ffmpeg and asks the Whisper add-on on this Home Assistant for the words (Wyoming protocol, `whisper_host`, `whisper_port`, `whisper_language`, default `core-whisper`, `10300`, `sr`). The phone gets a job id and asks for the text until it is ready, so long messages do not run into the Cloudflare Tunnel's time limit. One message at a time; nothing is written to disk and the text is forgotten once the phone has it. Cyrillic is written in Latin letters (`whisper_latin`, on by default).

## 0.16.2

- iPhone: "New message" notices are marked so the app's notification helper may change them before they are shown. With an app that has the helper, the notice then shows the chat's name (the person or the group), which the phone asks the chat server for itself; the push through Apple still carries no name or text. Older iPhone apps keep showing "New message".

## 0.16.1

- Group bot: when a phone cannot read the key the bot sent it (its secure channel to the bot broke, so it opens a new one), the bot now sends the key again over the new channel, so its later messages are readable on that phone. It logs when it does.
- The e2e test checks that the bot answers a phone asking for the key of an earlier bot message.

## 0.16.0

- Sign in on a computer with your phone. The computer shows a QR code; the phone scans it (menu, "Link a computer"), asks you to confirm, and the computer is signed in to the same account with access to your earlier messages. The phone sends the message keys encrypted for that computer only, so the server just passes on a sealed box it cannot open. A code lasts 150 seconds and works once. The server signs the computer in through a small built-in service (`dartchat-link`) that it registers with the chat server on first use, using the owner's session; its details are kept in `/data/link_appservice.json`.

## 0.15.0

- Notifications for the web version, also when its window or tab is closed. Browsers are woken through their own push service (Web Push) instead of Firebase. What the browser gets is encrypted for that browser and says only that something is new, in which chat, and how many are unread; the browser shows "New message". The key the server signs these with is made on first start and kept in `/data/webpush_vapid.pem`. The push gateway now also runs without a Firebase service account; then only browsers get notifications.

## 0.14.2

- Group bot: phones no longer show "Waiting for this message" for the bot's messages. When handing out the key to its messages failed for some phones (for example while the chat server was restarting), the bot thought it was done; phones signed in later never got the key either. Before each message it now checks that every phone in the group has the key and hands it out again where it is missing. It also answers a phone that asks for the key of one of its earlier messages.

## 0.14.1

- Bridge to Signal: edits pass both ways. Editing a message in the Dart Chat group edits the copy in Signal, and an edit in Signal edits the copy in Dart Chat. An edit of a message sent before the last restart is posted as a new message marked ✏️.

## 0.14.0

- Group rules: a new rule, **Bridge to Signal**. With `signal_bridge_token` set here and in the Signal bot add-on, the group bot passes text and photos between a Dart Chat group and the Signal bot's group, both ways, as "Name: message". Switching the rule off in the app stops both directions at once.

## 0.13.0

- Group rules: a new rule, **Birthdays**. The owner enters the birthdays in the app (Admin, Accounts; day and month only, no year), for every member and for themselves. Members cannot change them. On that day, at `birthday_time`, the bot posts `birthday_text` in every group that has the rule on and that the person is a member of. Birthdays are kept on the server in `/data/group_bot/birthdays.json`; they are not end-to-end encrypted, and the app says so.

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
