# The web app uses plain HTTP

- Status: done
- Opened: 2026-10-12
- Needed: only if the app is exposed beyond the private network

**What:** The password-protected web app serves plain HTTP. That's fine on
the private network, but anywhere else the password could be read off the
network.

**Recommendation:** Put HTTPS in front of it, such as a reverse proxy, before
exposing it.

**Outcome:** HTTPS ends at a reverse proxy in front of the app. The app takes
the proxy's address with `--public-url`, which admits requests for that host
and gives them a Secure cookie limited to its path, and its pages use
relative links so it can be served under a path. `docs/WEB.md` has the setup
for a subdomain and for a path, with Caddy and nginx.
