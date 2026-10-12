# The web app uses plain HTTP

- Status: open
- Opened: 2026-10-12
- Needed: only if the app is exposed beyond the private network

**What:** The password-protected web app serves plain HTTP. That's fine on
the private network, but anywhere else the password could be read off the
network.

**Recommendation:** Put HTTPS in front of it, such as a reverse proxy, before
exposing it.
