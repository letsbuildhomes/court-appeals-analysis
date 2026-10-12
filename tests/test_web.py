"""The web layer: host checks and the site-wide password."""
import http.client
from http.server import ThreadingHTTPServer
import threading
import time
import unittest

from court.web import COOKIE, Password, handler


class Served:
    def __init__(self, password=None):
        routes = {'/api/ping': lambda: {'ok': True}}
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(routes, {}, (), password))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        connection.request(method, path, body, {'Host': f'127.0.0.1:{self.port}', **(headers or {})})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class PasswordTests(unittest.TestCase):
    def setUp(self):
        self.password = Password('correct horse')
        self.site = Served(self.password)

    def tearDown(self):
        self.site.close()

    def sign_in(self, password):
        return self.site.request('POST', '/login', f'password={password}',
                                 {'Content-Type': 'application/x-www-form-urlencoded'})

    def test_everything_waits_for_the_password(self):
        status, _, body = self.site.request('GET', '/runs')
        self.assertEqual(status, 200)
        self.assertIn(b'Please enter the password', body)
        self.assertEqual(self.site.request('GET', '/api/ping')[0], 401)
        self.assertEqual(self.site.request('GET', '/login.css')[0], 200)

    def test_signing_in_sets_a_cookie_that_unlocks_the_site(self):
        status, _, body = self.sign_in('wrong')
        self.assertEqual(status, 401)
        self.assertIn(b'not right', body)
        status, headers, _ = self.sign_in('correct%20horse')
        self.assertEqual((status, headers['Location']), (303, '/'))
        cookie = headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        session = cookie.split(';')[0]
        status, _, body = self.site.request('GET', '/api/ping', headers={'Cookie': session})
        self.assertEqual((status, body), (200, b'{"ok": true}'))
        tampered = session[:-1] + ('0' if session[-1] != '0' else '1')
        self.assertEqual(self.site.request('GET', '/api/ping', headers={'Cookie': tampered})[0], 401)
        status, headers, _ = self.site.request('GET', '/logout', headers={'Cookie': session})
        self.assertIn('Max-Age=0', headers['Set-Cookie'])

    def test_sessions_expire_and_depend_on_the_password(self):
        old = self.password.session(now=time.time() - 40*24*3600)
        self.assertFalse(self.password.valid(f'{COOKIE}={old}'))
        current = self.password.session()
        self.assertTrue(self.password.valid(f'{COOKIE}={current}'))
        self.assertFalse(Password('another').valid(f'{COOKIE}={current}'))

    def test_failed_attempts_slow_down_and_cross_site_posts_are_refused(self):
        self.assertEqual(self.password.delay('10.0.0.1'), 0)
        self.password.failed('10.0.0.1')
        self.password.failed('10.0.0.1')
        self.assertEqual(self.password.delay('10.0.0.1'), 2.0)
        status, _, _ = self.site.request('POST', '/login', 'password=correct%20horse',
                                         {'Origin': 'http://evil.example', 'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(status, 403)


class OpenTests(unittest.TestCase):
    def test_without_a_password_the_site_is_open_but_host_checked(self):
        site = Served()
        try:
            self.assertEqual(site.request('GET', '/api/ping')[0], 200)
            self.assertEqual(site.request('GET', '/api/ping', headers={'Host': 'evil.example:80'})[0], 403)
        finally:
            site.close()


if __name__ == '__main__':
    unittest.main()
