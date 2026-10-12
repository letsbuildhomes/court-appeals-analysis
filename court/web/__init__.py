"""Small shared pieces of the read-only web tools: listening, host checks, a password and responses.

Tools listen on all interfaces by default, because this checkout runs on a
server reached over a private network. Host checks admit loopback, IP literals
and explicitly allowed names, which keeps DNS rebinding out.

With a password, every page shows a sign-in form and the API answers 401 until
the password is entered. Signing in sets a session cookie: an expiry signed
with a key derived from the password, so changing the password signs everyone
out, and restarting the server does not. The cookie is HttpOnly and
SameSite=Strict; failed attempts are slowed down.

Behind a reverse proxy that terminates HTTPS, a ``PublicUrl`` names the
address browsers use. Requests for that host must come from that origin, and
their cookie is Secure and scoped to its path. Pages use relative URLs, so the
proxy may serve the site under a path by removing that prefix before passing
requests on.
"""
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
import ipaddress
import json
from pathlib import Path
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

DEFAULT_HOST = '0.0.0.0'
TYPES = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png'}
HERE = Path(__file__).parent
COOKIE = 'court_session'
SESSION_SECONDS = 30*24*3600


class Password:
    """A site-wide password and the session cookies it signs."""

    def __init__(self, password):
        if not password:
            raise ValueError('The password is empty')
        self.digest = hashlib.sha256(password.encode()).digest()
        self.key = hmac.new(password.encode(), b'court-web-session', hashlib.sha256).digest()
        self.failures, self.lock = {}, threading.Lock()

    def matches(self, given):
        return hmac.compare_digest(hashlib.sha256(given.encode()).digest(), self.digest)

    def session(self, now=None):
        expires = int((now or time.time()) + SESSION_SECONDS)
        return f'{expires}.{hmac.new(self.key, str(expires).encode(), hashlib.sha256).hexdigest()}'

    def valid(self, cookie_header, now=None):
        try:
            value = SimpleCookie(cookie_header or '')[COOKIE].value
            expires, signature = value.split('.', 1)
            expected = hmac.new(self.key, expires.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(signature, expected) and int(expires) > (now or time.time())
        except (KeyError, ValueError):
            return False

    def delay(self, client):
        """Seconds to wait before answering another attempt from this client."""
        with self.lock:
            count = self.failures.get(client, 0)
        return min(8, 0.5 * 2 ** count) if count else 0

    def failed(self, client):
        with self.lock:
            self.failures[client] = self.failures.get(client, 0) + 1

    def succeeded(self, client):
        with self.lock:
            self.failures.pop(client, None)


class PublicUrl:
    """The address browsers use when a reverse proxy serves the site, such as ``https://reviews.example.org/app/``."""

    def __init__(self, url):
        parts = urlsplit(url.strip())
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            raise ValueError(f'The public URL needs http:// or https:// and a host: {url}')
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError(f'The public URL takes only a scheme, host, port and path: {url}')
        default = 443 if parts.scheme == 'https' else 80
        self.scheme, self.hostname, self.port = parts.scheme, parts.hostname, parts.port or default
        self.path = parts.path.rstrip('/') + '/'
        name = f'[{self.hostname}]' if ':' in self.hostname else self.hostname
        self.origin = f'{self.scheme}://{name}' + ('' if self.port == default else f':{self.port}')
        self.url = self.origin + self.path

    def serves(self, hostname, port):
        """True when a request's Host names this address."""
        return hostname == self.hostname and (port or (443 if self.scheme == 'https' else 80)) == self.port


def login_page(error=''):
    page = (HERE/'login.html').read_text()
    if error:
        page = page.replace('<!--error-->', f'<p class="error">{error}</p>')
    return page.encode()


def display_url(host, port, path=''):
    """A URL to share; a wildcard bind shows the host's outbound LAN address."""
    if host in ('', '0.0.0.0'):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(('192.0.2.1', 9))  # Selects a route; UDP connect sends nothing.
                host = probe.getsockname()[0]
        except OSError:
            host = '127.0.0.1'
    return f'http://{host}:{port}{path}'


def trusted_hostname(hostname, allowed_hosts=()):
    """DNS rebinding needs a name, so IP literals are safe; names must be listed."""
    if hostname == 'localhost' or hostname in allowed_hosts:
        return True
    try:
        ipaddress.ip_address(hostname or '')
    except ValueError:
        return False
    return True


def handler(routes, static, allowed_hosts=(), password=None, public=None):
    """A read-only handler: ``routes`` maps paths to functions of query arguments; ``static`` maps paths to files.

    With ``password`` (a ``Password``), everything but the sign-in form needs a session cookie. With
    ``public`` (a ``PublicUrl``), requests may also arrive through a reverse proxy at that address.
    """
    allowed = {name.lower() for name in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        def checked(self):
            """Refuse requests for another host or from another site; True when the request may proceed."""
            authority = self.headers.get('Host', '').lower()
            host = urlsplit('http://' + authority)
            self.public = bool(public and not host.path and public.serves(host.hostname, host.port))
            if self.public:
                expected = public.origin
            elif trusted_hostname(host.hostname, allowed) and host.port == self.server.server_address[1] and not host.path:
                expected = f'http://{authority}'
            else:
                self.respond(403, {'error': 'Invalid Host'})
                return False
            origin = self.headers.get('Origin')
            if origin and origin.lower() != expected:
                self.respond(403, {'error': 'Invalid Origin'})
                return False
            if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                self.respond(403, {'error': 'Cross-site request forbidden'})
                return False
            return True

        def cookie(self, value, seconds):
            """A session cookie; through the public address it is scoped to its path, and Secure over HTTPS."""
            path = public.path if self.public else '/'
            secure = '; Secure' if self.public and public.scheme == 'https' else ''
            return f'{COOKIE}={value}; Path={path}; HttpOnly; SameSite=Strict; Max-Age={seconds}{secure}'

        def do_POST(self):
            if not self.checked():
                return
            if urlsplit(self.path).path != '/login' or not password:
                return self.respond(404, {'error': 'Unknown endpoint'})
            length = int(self.headers.get('Content-Length') or 0)
            if not 0 < length <= 4096:
                return self.respond(400, login_page('Please enter the password.'), 'text/html')
            form = parse_qs(self.rfile.read(length).decode('utf-8', 'replace'))
            client = self.client_address[0]
            time.sleep(password.delay(client))
            if not password.matches((form.get('password') or [''])[0]):
                password.failed(client)
                return self.respond(401, login_page('That password is not right.'), 'text/html')
            password.succeeded(client)
            self.respond(303, b'', 'text/plain', {'Location': './', 'Set-Cookie': self.cookie(password.session(), SESSION_SECONDS)})

        def do_GET(self):
            try:
                if not self.checked():
                    return
                url = urlsplit(self.path)
                if url.path == '/logout':
                    return self.respond(303, b'', 'text/plain', {'Location': './', 'Set-Cookie': self.cookie('', 0)})
                if password:
                    if url.path == '/login.css':
                        return self.respond(200, (HERE/'login.css').read_bytes(), 'text/css')
                    if not password.valid(self.headers.get('Cookie')):
                        if url.path.startswith('/api/'):
                            return self.respond(401, {'error': 'Sign in required'})
                        return self.respond(200, login_page(), 'text/html')
                query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True) if url.query else {}
                if any(len(v) != 1 for v in query.values()):
                    raise ValueError('Repeated query argument')
                args = {k: v[0] for k, v in query.items()}
                if url.path in routes:
                    value = routes[url.path](**args)
                    return self.respond(200, value, 'image/png' if isinstance(value, bytes) else 'application/json')
                if url.path in static and not args:
                    file = Path(static[url.path])
                    return self.respond(200, file.read_bytes(), TYPES[file.suffix])
                self.respond(404, {'error': 'Unknown endpoint'})
            except (ValueError, TypeError, UnicodeError) as exc:
                self.respond(400, {'error': str(exc) if not isinstance(exc, TypeError) else 'Invalid query arguments'})
            except (KeyError, FileNotFoundError) as exc:
                self.respond(404, {'error': f'Not found: {exc}'})
            except OSError:
                self.respond(400, {'error': 'Could not be read'})

        def respond(self, status, value, content_type='application/json', headers=None):
            raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            for name, header in (headers or {}).items():
                self.send_header(name, header)
            self.send_header('Content-Type', content_type + ('' if content_type.startswith('image/') else '; charset=utf-8'))
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy',
                             "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format, *args):
            pass
    return Handler


def serve(name, routes, static, host=DEFAULT_HOST, port=8771, path='/', allowed_hosts=(), password=None, public=None):
    if not 1 <= port <= 65535:
        raise ValueError('Port must be between 1 and 65535')
    server = ThreadingHTTPServer((host, port), handler(routes, static, allowed_hosts, password, public))
    print(f'{name}: {display_url(host, port, path)}' + (f' and {public.url}' if public else '')
          + ('' if password else ' (no password)'), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
