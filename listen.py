"""Listen settings for browser tools served from this remote host."""
import socket

# The project runs on a server reached over a private network, not on a laptop.
DEFAULT_HOST = '0.0.0.0'


def display_url(host, port, path=''):
    """Return a URL to share; a wildcard bind shows the host's outbound LAN address."""
    if host in ('', '0.0.0.0'):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(('192.0.2.1', 9))  # Selects a route; UDP connect sends nothing.
                host = probe.getsockname()[0]
        except OSError:
            host = '127.0.0.1'
    return f'http://{host}:{port}{path}'
