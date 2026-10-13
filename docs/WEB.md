# Web app

One read-only web app shows everything: runs as they happen, the lab, every
dataset with a viewer for its kind, and pipelines.

```sh
echo 'a long password' > .web-password    # once; the file is ignored by git
make web                                  # python -m court web; http://SERVER:8771/
```

`SERVER` is this machine's private-network address; the app listens on all
interfaces and prints its URL. It changes nothing and needs no model
credentials.

## Password

The whole site is behind one password. It comes from `COURT_WEB_PASSWORD`,
or the first line of `.web-password` (`--password-file` names another file);
without one, the app refuses to start unless given `--no-password`. Signed
out, every page shows a sign-in form and the API answers 401. Signing in sets
a cookie for 30 days: HttpOnly, SameSite=Strict, and signed with a key derived
from the password, so changing the password signs everyone out while
restarting the app does not. Repeated wrong passwords from one address are
answered more and more slowly. **Sign out** is in the header.

The app itself speaks plain HTTP. On the private network the password keeps
casual visitors out, but anyone who can watch that network's traffic could
read it; to reach the app from anywhere else, put it behind a reverse proxy
that serves HTTPS.

## Behind a reverse proxy

A web server elsewhere, such as a gateway reached over a VPN, can serve the
app at a public address with its own TLS certificate. Tell the app that
address with `--public-url` (or `COURT_WEB_PUBLIC_URL`), and bind it to the
address the proxy reaches it on:

```sh
make web LISTEN_HOST=10.0.0.2 ARGS='--public-url https://reviews.example.org/'
```

Requests for the public host must then come from pages on that site, and the
session cookie they get is marked Secure and limited to the public path.
Requests made straight to the app's own address work as before. The proxy
must pass the browser's `Host` header through unchanged; Caddy does by
default, and nginx needs `proxy_set_header Host $http_host`.

The app can have a subdomain to itself or live under a path. Its pages use
relative links, so under a path the proxy removes the prefix before passing
requests on, and redirects the bare prefix to the same path with a trailing
slash.

**A subdomain of its own** (`--public-url https://reviews.example.org/`):

```caddy
reviews.example.org {
	reverse_proxy 10.0.0.2:8771
}
```

```nginx
server {
    listen 443 ssl;
    server_name reviews.example.org;
    # ssl_certificate and ssl_certificate_key as for the server's other sites
    location / {
        proxy_pass http://10.0.0.2:8771;
        proxy_set_header Host $http_host;
    }
}
```

**Under a path** (`--public-url https://example.org/review/`):

```caddy
example.org {
	redir /review /review/
	handle_path /review/* {
		reverse_proxy 10.0.0.2:8771
	}
}
```

```nginx
location = /review { return 301 /review/; }
location /review/ {
    proxy_pass http://10.0.0.2:8771/;   # the trailing slash removes the prefix
    proxy_set_header Host $http_host;
}
```

Sign in once through the public address; a session from the app's own
address doesn't carry over, since the browser treats them as different sites.

## Keeping it running

`deploy/web/court-web.service` runs `make web` as a systemd user service, so
the app starts at boot and comes back within seconds if it stops. The
password still comes from `.web-password`, and this deployment's settings come
from `.web-env` in the checkout, which git ignores too:

```sh
LISTEN_HOST=10.0.0.2
WEB_PORT=8771
COURT_WEB_PUBLIC_URL=https://reviews.example.org/
```

`LISTEN_HOST` is the address the proxy reaches the app on, `WEB_PORT` its port
(`make web` takes it too) and `COURT_WEB_PUBLIC_URL` the public address from
above. Comments go on lines of their own; systemd keeps anything after a
value as part of it. Without `.web-env`, the service serves like a plain
`make web`: every address, port 8771, no public URL. After editing it,
restart the service.

```sh
systemctl --user enable --now "$PWD/deploy/web/court-web.service"   # from the checkout; once
systemctl --user status court-web
journalctl --user -u court-web -f            # its output
systemctl --user restart court-web           # after pulling or switching branches
systemctl --user disable --now court-web     # stop serving
```

Enabling links the unit to the file in the checkout; after editing the unit file,
run `systemctl --user daemon-reload` and restart. The service serves whatever
is checked out when it starts, and keeps running after you log out only if
lingering is on for your account (`loginctl enable-linger`). If you change the
port, change the reverse proxy to match, and stop any `make web` already using
it.

## Sections

| Section | What it shows |
| --- | --- |
| **Overview** | Running and recent runs, production's numbers, experiments, named datasets |
| **Runs** | Every run, full or an experiment's subset, newest first; running ones refresh every two seconds |
| **Lab** | Experiment rounds (verdicts, quality measures, what changed, cost) and production's quality profile |
| **Data** | Every dataset by kind, with its report (grouping report, text audit, results profile) and items |
| **Reference** | The hand-labeled reference cases: one table of what each label says (in scope first, then by how much is known), the open questions, and each case's answers beside the text they cite |
| **Pipelines** | Resolved pipelines, their step trees, prompts and settings, tag history, and diffs between any two |

## Watching a run

A run's page shows, while it runs:

- **Now:** elapsed time, items, charges against the cap (and money reserved
  by calls still in flight), the value of saved calls reused, calls sent and
  failed, and tokens in (cached) and out.
- **Spending over time:** each delivered call's charge, cumulative, scaled to
  the charges; the cap's line appears once spending nears it.
- **Steps:** the pipeline's steps from where the run starts: waiting, running
  or done; items in and out, failures by kind, how many items the running step
  has touched with an estimate of the time left; and each step's calls,
  spending and tokens.
- **In flight:** each model call sent and not yet returned: its step, cases,
  time out and reservation.
- **Items:** where each case is, or how it ended; select one for its trace:
  every call (reused or new, with batch partners), warning and rejection.

The runner writes `data/runs/NAME/run.json` when a run starts,
`progress.json` once a second while it runs, and every call's reservation
and charge to `ledger.jsonl` as it happens; the page reads only these. A run
whose process ended while running shows as interrupted; running the same name
again continues it. Experiment rounds are ordinary runs labeled with their
experiment, round and side.

Live pages refresh every two seconds by patching the page in place with
[idiomorph](https://github.com/bigskysoftware/idiomorph) 0.8.0, saved
unmodified as `court/ui/static/idiomorph.js` with its license (0BSD) beside
it. Patching keeps the reader's place, hover and open trace; the browser holds
what they're reading still when a section above it changes height.

## Stage viewers

From **Data**, each kind of item has its own view:

- **Documents:** listing entries and the PDF's pages beside their text.
- **Pages:** the PDF page beside its selected text, with status, source,
  reason and OCR confidence; step through the document.
- **Cases:** each document's caption and listing dockets, basis and flags,
  and its pages beside the case text the model receives. This is where
  grouping flags are judged.
- **Results:** the disposition, every record's facts, and the evidence for
  each, highlighted in the case text.
