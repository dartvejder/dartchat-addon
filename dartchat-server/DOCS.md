# Dart Chat Server

This add-on runs the Dart Chat server on your Home Assistant machine. Messages
are end-to-end encrypted by the apps, so the server stores only encrypted
content.

## Configuration

| Option | What it does |
| --- | --- |
| `server_name` | The address friends connect to, for example `chat.example.com`. Required. |
| `registration_token` | Normally empty: friends join with a personal invitation code. If you set it, anyone who knows this secret can also create an account. |
| `firebase_service_account` | Turns on phone notifications. Paste the whole contents of the Firebase service account key file (JSON). Leave empty for no notifications. |
| `resend_api_key` | Lets the server send invitation emails through Resend. Leave empty to pass invitation codes on yourself. |
| `invite_email_from` | Sender of invitation emails, for example `Dart Chat <invites@example.com>`. Empty uses `invites@` your domain. |
| `app_link` | Optional link, put in invitation emails, to where Android friends install the app (Google Play). |
| `iphone_link` | Where iPhone friends get the app, like a public TestFlight link. |
| `web_link` | Address of the web app, like `https://web.dartchat.trade`. Invitations then get a web button, Windows and Linux buttons, and one smart link (`<web_link>/join.html#CODE`) that shows each phone the right steps. |
| `max_upload_mb` | Largest photo or file upload, in megabytes. Default 20. |
| `log_level` | How much the server logs: `error`, `warn`, `info` or `debug`. |
| `storage_report_email` | Where to send the daily storage email. Empty turns the email off. Needs `resend_api_key`. |
| `storage_report_time` | When the daily storage email goes out, like `08:00`, in the `timezone` option's time zone (default Europe/Belgrade). |

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
4. After the first account exists, the one-time token stops working. From
   then on people join with an invitation code (see Invitations below).

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
- Open sign-up: an account needs an invitation code.
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

The owner also gets a "Someone you invited has joined" notification when an invited friend creates their account. It names nobody; the app shows who it was under Invitations and accounts. It reaches the phones on which the owner has opened that screen.

The same phones get a "Someone changed their name" notification when a person changes their display name. It names nobody either; the accounts list shows the new name and the one before it. The first name a person sets in place of their username is not reported.

To turn notifications on:

1. In the Firebase console open **Project settings**, then **Service accounts**,
   and click **Generate new private key**. A JSON file downloads.
2. Open that file in a text editor, copy everything, and paste it into the
   `firebase_service_account` option. Save and restart the app.
3. The log shows `[dartchat-push] Ready` when it works.

That key is a secret. Keep it out of GitHub and chats. If it leaks, delete it
in the Firebase console and generate a new one.

## Invitations

The owner (the first account, or anyone made a server admin) invites friends
from the Dart Chat app: **Invitations and accounts** in the menu. Each
invitation is for one email address and holds one code that works once. An
unused invitation can be revoked, and inviting the same address again replaces
the earlier code.

To have the server send the invitation emails:

1. Create an API key at [resend.com](https://resend.com) for a domain you have
   verified there. "Sending access" is enough.
2. Paste the key into the `resend_api_key` option. Save and restart the app.
3. The log shows `[dartchat-invite] Ready. Invitation emails are sent from ...`.

Without a key everything else still works: the app shows each code and you
pass it on yourself.

That key is a secret too. Keep it out of GitHub and chats. If it leaks, delete
it at Resend and create a new one.

The same screen lists the accounts on the server and can deactivate one. A
deactivated account is signed out everywhere and cannot sign in again. This
cannot be undone.

## Circles

If you have separate groups of friends who should not see each other, create
circles in the app: **Invitations and accounts**, then **Circles**. Put each
account in one or more circles, and pick the circle when you invite someone.

- Two people can find, see and message each other only if they share a
  circle. To everyone else the username looks like it does not exist.
- You, the owner, see everyone and everyone sees you.
- Once at least one circle exists, an account in no circle sees only you.
- While there are no circles, everyone can reach everyone.

The server enforces this, not just the app. One thing circles do not hide: if
you put people from different circles into the same group chat, they see each
other in that chat.

## Who can make groups

You, the owner, can create any group and add anyone to it. A friend can
create a group too, and add people to one, as long as the whole group stays
inside one of their circles: everyone in it (apart from you and the group bot)
must be in that one circle together. Someone in two circles cannot mix them
in one group. An account in no circle makes no groups. Everyone can still
start a chat with one person inside their circle. The server enforces this,
not just the app.

## Group rules

The group bot brings the rules of a Signal link bot to Dart Chat groups. You
choose, per group, which rules apply: in the app open the group, tap its name,
and use **Group rules** (only the server owner sees it, and you need to be an
admin of that group). Switching any rule on adds the bot account ("Dart Bot")
to the group; switching all of them off makes it leave.

The bot's name is the `group_bot_name` option. Its picture is set in the app:
**Group rules**, then **Bot picture**. Like every profile picture it is not
end-to-end encrypted.

| Rule | What it does |
| --- | --- |
| Repost check | When a link is posted again, it replies quoting the first post (`repost_reply_text`, or `self_repost_reply_text` for your own link) and adds a score line (`repost_count_text`). It knows the same Instagram, X, YouTube, TikTok, Reddit, Facebook, Bluesky and Spotify post under different addresses, ignores tracking junk and follows short links. |
| Same news from another site | Compares headlines (Latin and Cyrillic, word endings) with links from the last `news_window_days` days and replies `news_reply_text`. Counts as a repost. `news_threshold` is how much of the headline must match. |
| Watched name | Links whose headline or address mention one of `keyword_words` get `keyword_reply_text` and their own score. Shown only once `keyword_words` is set; `keyword_rule_name` is its name in the app and in the reports. |
| Commands | Answers `list_commands` (repeaters list), `stats_commands` (today's numbers), `top_commands` (today's top list) and `keyword_commands`. |
| Midnight report | Right after midnight (`timezone`), posts the day's statistics and top list. |
| Home Assistant sensors | Shows the group's numbers as sensors named after the group, for example `sensor.dartchat_drustvo_poruke_danas`. |
| Birthdays | At `birthday_time` on a member's birthday, posts `birthday_text` (`{name}` is the person). Only the owner enters birthdays, day and month only, in the app under Admin, Accounts: for every member and for themselves. Someone born on 29 February is congratulated on 28 February in other years. |
| Bridge to Signal | Passes every message and photo between this group and the Signal group of the owner's Signal bot add-on, both ways, as "Name: message". Shown only once `signal_bridge_token` is set. Switch it off to stop both directions at once. See below. |

In texts, `|` separates options picked at random, `{name}` and `{date}` are
who posted the link first and when, and in score lines `{count}` and `{puta}`
(put/puta) are filled in. `group_bot_name` is the bot's name in groups.

Messages are end-to-end encrypted, so the bot reads a group as a member of it,
from the moment it joins. It keeps only links, headlines and counts in
`/data/group_bot`, never message text. If you delete a message, its links stop
counting as the first post.

## Bridge to Signal

The group bot can pass messages between a Dart Chat group and one Signal
group, both ways, through the Signal bot add-on (Signal duplicate-link bot)
running on the same Home Assistant.

1. Pick a long random password and put it in `signal_bridge_token` here and in
   the Signal bot's `dartchat_bridge_token`. Restart this add-on.
2. In the Signal bot's options switch on `dartchat_bridge`. It forwards the
   group in its `allowed_groups` (or `dartchat_bridge_group`).
3. In the app, open the Dart Chat group, **Group rules**, and switch on
   **Bridge to Signal**.

Text and photos pass both ways. A message from Signal is posted in the group
by the group bot as "Name: message"; a message from Dart Chat is posted in the
Signal group by the Signal bot the same way. Replies show the message they
answer above them, and edits are passed on both ways. Reactions and deletions
are not passed on.

To stop it quickly, switch **Bridge to Signal** off in the app, or switch
`dartchat_bridge` off in the Signal bot. The bridge listens on port 6170 only
inside Home Assistant (no port is published) and answers only with the
password. Messages that pass the bridge leave end-to-end encryption: the two
bots read them in order to pass them on, and Signal's servers carry them on
the Signal side.

## Timed bans and mutes

The owner can ban or mute someone in a group for a while, from the app or by
telling the group bot (`!ban @name 1d`, `!mute @name 2h`, `!unban @name`,
`!unmute @name`; the length is a number with m, h, d or w). Nobody else can.

- A mute is kept by this server: while it lasts, that person's messages in
  that group are refused. They can still read.
- A ban is an ordinary ban on the chat server. When the time is up, this
  server lifts it and invites the person back.
- To lift a ban while the owner's phone is off, the server keeps the owner's
  most recent sign-in in `/data/moderation.json`, which is part of Home
  Assistant backups. If the owner has signed out everywhere when the time
  comes, the ban stays until the owner lifts it in the app.
- The owner and the bot cannot be banned or muted this way.

## Scheduled messages

Someone can write a message now and have it sent later. Their phone encrypts
the message straight away and hands it to this server, which sends it at the
chosen time, also when the phone is switched off by then.

- The server holds only the encrypted message and never sees the text.
- To send in the sender's name, the server keeps that phone's sign-in next to
  the waiting message, in `/data/scheduled.json`, and forgets it once the
  message is sent or cancelled. This file is part of Home Assistant backups.
- If the sender signs out on that phone, or leaves the chat, before the time
  comes, the message is not sent and shows as failed in their app.
- People or phones that join the chat after the message was written cannot
  read it.
- Each account can have 50 messages waiting, up to a year ahead.

## Web version and link previews

The web version (https://web.dartchat.trade) talks to this server from another address, so the front door answers the browser's permission checks (CORS) for every path.

A browser may not read other websites, so when someone sends a link from the web version, the app asks this server to fetch the page and its picture (`POST /_dartchat/v1/fetch`). Only signed-in people can ask; only public websites are fetched, never the home network or the server itself; only pages and pictures, at most 1 MB. The website sees this server's address, and the server sees which links are previewed from the web. Nothing is stored. The phone apps fetch previews themselves.

## Daily storage email

Photos, files and messages stay on the Home Assistant disk and grow over time.
Set `storage_report_email` and the server emails you once a day, at
`storage_report_time`, with:

- how much space Dart Chat uses, split into photos and files, the message
  database, and the rest of its data;
- how much free space is left on the disk, and roughly when it would be full
  at the current pace;
- how much it grew over the last day, 7 days, 30 days and 12 months, and per
  day, per month and per year;
- how many messages were sent yesterday, this month, this year and since
  counting began: each group by name, and private chats as one total (nobody's
  private chats are listed by person). Reactions are counted on their own,
  edits are not counted, and Dart Bot's posts are left out.

The first email is sent right after the add-on starts with the option set.
Sizes are measured once a day and kept in `/data/storage_history.json`, so the
growth figures fill in as days go by. Messages are counted from the moment
this version is installed, in `/data/message_counts.json`; the server only
counts them and never sees what they say.

## Server stats

The owner screen in the app has a "Server stats" tab: how many people were active now, today and this week, each person's last time online and how many messages they sent (today, 7 and 30 days, all chats together), messages per day for two weeks, the busiest groups, and how much room the server uses and has left. Only owners can see it (`GET /_dartchat/v1/admin/stats`). Message contents are never read; they are end-to-end encrypted. The owner sees everyone's last-seen time here, also of people who switched off "Show when I am online" for their friends.

## Voice messages to text

Swipe a voice message to the right (or press and hold it and pick "Turn into text") and the app shows the words under it. The phone decrypts the voice message itself and sends the sound only to this server, which hands it to the **Whisper** add-on on the same Home Assistant. Nothing goes to an outside company, the sound is never saved, and the text is forgotten by the server as soon as the phone has it; the phone keeps it with the message.

Set up once:

1. Settings, Add-ons, Add-on store: install **Whisper** (the official one, by Home Assistant) and start it.
2. In its configuration pick a model. `small-int8` is quick; `medium-int8` understands Serbian noticeably better but is slower. Set the language to `sr`.
3. Nothing else is needed here: `whisper_host` (`core-whisper`), `whisper_port` (`10300`) and `whisper_language` (`sr`) already point at it. Use `auto` as the language to let Whisper guess. Whisper often writes Serbian in Cyrillic; with `whisper_latin` on (the default) the server turns it into Latin letters.

One voice message is turned into text at a time; a phone waits its turn. Without the Whisper add-on the app says that turning voice into text is not set up.

## Looks, stickers and switches from the server

The app's themes, colour schemes, backgrounds, message marks, reply looks, fonts, stickers and GIFs come from this add-on. A new look needs only an add-on update; the apps (from the version released with this add-on on) see it in Settings the next time they start or within an hour. Each phone downloads a picture or font once, only when someone picks it, and keeps it for offline use. Without the server the app keeps the looks built into it.

Your own packs go in `/share/dartchat/looks` (the `share` folder in Home Assistant), in the same layout as the add-on's own `looks` folder: `themes.json`, `schemes.json`, `backgrounds.json`, `statuses.json`, `fonts.json`, and folders `stickers/<pack>/pack.json` and `gifs/<pack>/pack.json` with their pictures. Restart the add-on after changing them. PNG, WebP, GIF, JPEG and TTF/OTF files up to 2 MB each are accepted; the log says what was left out and why.

Options:

- `features`: switch parts of the app off for everyone, for example `stickers=off, gifs=off`. Switches: `stickers`, `gifs`, `link_previews`, `voice_to_text`, `scheduled_send`, `polls`, `location`. Everything is on by default.
- `banner_text`: a short notice shown at the top of everyone's chat list until they close it. A new text shows again for everyone.
- `quick_reactions`: the reactions offered first, comma separated, for people who never changed their own.
- `invite_share_text`: the message the app shares through WhatsApp, Viber or SMS. `{link}` and `{code}` are filled in.
- `invite_subject`, `invite_intro`: the subject and first line of the invitation email. `{inviter}` is filled in.

## Quiz invitations

Dart Chat can invite friends into a game of the Kviz app. Make up a long
random key (for example 40 letters and digits) and put it in two places: the
add-on option `quiz_api_key`, and the quiz server's secret `KVIZ_KLJUC`. The
app then gets a one-hour Dart Chat token from the add-on and passes it to the
quiz, and the quiz server checks it at `/_dartchat/v1/quiz/whoami` with that
key. The quiz learns only the account name and display name. Leave the option
empty to switch quiz invitations off.
