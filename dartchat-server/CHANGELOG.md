# Changelog

## 0.3.0

- Invitations: the owner invites friends by email from inside the app. Every invitation has its own code that works once. Paste a Resend API key into the new `resend_api_key` option to have the emails sent; without it the app shows the code so you can pass it on yourself.
- Owner tools for the app: revoke an unused invitation, list accounts, deactivate an account.
- Sign-up no longer needs `registration_token`. Leave it empty for invitation-only sign-up.
- The apps still connect to port 6167. Inside the add-on a small front door now shares that port between the chat server and the invitation service.

## 0.2.0

- Notifications: a built-in push gateway asks Firebase to wake phones when there is something new. The wake-up never contains the message, the sender or the chat name. Turn it on by pasting a Firebase service account key into the new `firebase_service_account` option.

## 0.1.0

- First version: continuwuity 26.9.4 running as a closed, non-federated chat server.
