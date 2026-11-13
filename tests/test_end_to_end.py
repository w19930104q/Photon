"""End-to-end crawl test against a local, fully controlled HTTP server.

Covers the required scenarios without touching the network:
  * first hop failure (503 on the first attempt, then recovery), 404, 429,
    bad Content-Type, redirect, same-name (duplicate) pages
and asserts that retry/processed bookkeeping, level crawling, de-duplication,
robot/sitemap seeding, mirroring and atomic text/CSV/JSON exports behave.
Real connection-level failures are exercised deterministically in
tests.test_requester.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


class ScenarioHandler(BaseHTTPRequestHandler):
    # Shared, class-level state because a new handler is created per request
    flaky_hits = 0
    always_429_hits = 0
    no_type_hits = 0
    request_log = []

    def log_message(self, *args):  # silence the test server
        pass

    def _send(self, body, status=200, content_type='text/html',
              extra_headers=None):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(status)
        if content_type is not None:
            self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).request_log.append(self.path)
        path = self.path.split('?')[0]

        if path == '/robots.txt':
            return self._send(
                'User-agent: *\nDisallow: /forbidden\n',
                content_type='text/plain')
        if path == '/sitemap.xml':
            return self._send(
                '<?xml version="1.0"?>'
                '<urlset><url><loc>http://%s/from-sitemap</loc></url>'
                '</urlset>' % self.headers['Host'],
                content_type='application/xml')
        if path == '/':
            # One anchor per line: Photon's href regex is line-based, so
            # anchors sharing a line would collapse into one match (original
            # crawler behavior kept untouched by this change).
            anchors = [
                '/flaky-child', '/missing', '/rate-limited', '/logo.png',
                '/no-type', '/redirect', '/same-name?id=1',
                '/same-name?id=2', '/forbidden',
            ]
            page = '<html><body>\n' + '\n'.join(
                '<a href="%s">%s</a>' % (a, a) for a in anchors
            ) + '\n</body></html>'
            return self._send(page)
        if path == '/flaky-child':
            # Deterministic first-hop temporary failure: the first request
            # gets a 503 (transient, retryable), subsequent ones succeed.
            type(self).flaky_hits += 1
            if type(self).flaky_hits == 1:
                return self._send(
                    'temporarily unavailable', status=503,
                    content_type='text/plain')
            return self._send(
                '<html><body><a href="/deep">deep</a></body></html>')
        if path == '/deep':
            return self._send('<html><body>deep page</body></html>')
        if path == '/missing':
            return self._send('<html>not found</html>', status=404)
        if path == '/rate-limited':
            # Always 429 with a zero Retry-After: budget must exhaust
            type(self).always_429_hits += 1
            return self._send(
                'slow down', status=429, content_type=None,
                extra_headers={'Retry-After': '0'})
        if path == '/logo.png':
            return self._send(b'\x89PNG\r\n\x1a\n',
                              content_type='image/png')
        if path == '/no-type':
            # Missing Content-Type every time -> transient until exhausted
            type(self).no_type_hits += 1
            return self._send('mystery', content_type=None)
        if path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/redirect-target')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if path == '/redirect-target':
            # Contains an in-scope link so the target itself becomes an
            # internal URL once the redirect is followed
            return self._send(
                '<html><body>\n'
                '<a href="/redirect-landed">landed</a>\n'
                '</body></html>')
        if path == '/redirect-landed':
            return self._send('<html><body>redirected here</body></html>')
        if path == '/same-name':
            return self._send('<html><body>same name page</body></html>')
        if path == '/forbidden':
            return self._send('<html>forbidden</html>', status=403)
        if path == '/from-sitemap':
            return self._send('<html><body>sitemap seed</body></html>')
        return self._send('<html>unhandled</html>', status=404)


class EndToEndCrawlTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), ScenarioHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.workdir = tempfile.mkdtemp(prefix='photon-e2e-')
        cls.base = 'http://127.0.0.1:%d' % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        shutil.rmtree(cls.workdir, ignore_errors=True)

    def setUp(self):
        ScenarioHandler.flaky_hits = 0
        ScenarioHandler.always_429_hits = 0
        ScenarioHandler.no_type_hits = 0
        ScenarioHandler.request_log = []

    def run_photon(self, export_fmt):
        out_dir = os.path.join(self.workdir, export_fmt or 'txt')
        cmd = [
            sys.executable, os.path.join(REPO_ROOT, 'photon.py'),
            '-u', self.base,
            '-l', '3',
            '-t', '4',
            '-o', out_dir,
        ]
        if export_fmt:
            cmd += ['-e', export_fmt]
        env = dict(os.environ)
        env['PYTHONPATH'] = REPO_ROOT
        # Run with the repo root as cwd so the `core`/`plugins` packages
        # resolve; photon.py is trusted project code we are testing.
        proc = subprocess.run(cmd, cwd=REPO_ROOT, env=env,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=120)
        self.assertEqual(
            proc.returncode, 0,
            msg='photon failed\nstdout:\n%s\nstderr:\n%s' % (
                proc.stdout.decode(errors='replace'),
                proc.stderr.decode(errors='replace')))
        return out_dir, proc.stdout.decode(errors='replace')

    def _read_dataset(self, out_dir, name):
        path = os.path.join(out_dir, name + '.txt')
        if not os.path.exists(path):
            return set()
        with open(path) as fh:
            return set(line.strip() for line in fh if line.strip())

    def test_full_crawl_all_scenarios_txt(self):
        out_dir, stdout = self.run_photon(None)

        internal = self._read_dataset(out_dir, 'internal')
        failed = self._read_dataset(out_dir, 'failed')
        files = self._read_dataset(out_dir, 'files')
        robots = self._read_dataset(out_dir, 'robots')

        # First hop failure recovered: the flaky page and its deep link are
        # present in internal and were actually fetched
        self.assertIn(self.base + '/flaky-child', internal)
        self.assertIn(self.base + '/deep', internal)
        self.assertGreaterEqual(ScenarioHandler.flaky_hits, 2)
        self.assertNotIn(self.base + '/flaky-child', failed)

        # 404 is a permanent restricted outcome recorded in failed
        self.assertIn(self.base + '/missing', failed)

        # 429 exhausted the budget: failed, and retried more than once
        self.assertIn(self.base + '/rate-limited', failed)
        self.assertEqual(ScenarioHandler.always_429_hits, 3)

        # Bad Content-Type is a deterministic restricted outcome. Photon
        # records file-type links in `files` using their original (relative)
        # form and never enqueues them as crawlable pages.
        self.assertTrue(
            any(f.endswith('/logo.png') for f in files), files)
        self.assertFalse(
            any('logo.png' in u for u in internal))

        # Missing Content-Type is transient: three attempts, then failed
        self.assertIn(self.base + '/no-type', failed)
        self.assertEqual(ScenarioHandler.no_type_hits, 3)

        # Redirect is followed: the page reached after the 302 contributes
        # its own links, while the requested URL /redirect is the identity
        # recorded as processed
        self.assertIn(self.base + '/redirect', internal)
        self.assertIn(self.base + '/redirect-landed', internal)
        redirect_landed_hits = ScenarioHandler.request_log.count(
            '/redirect-landed')
        self.assertEqual(redirect_landed_hits, 1)

        # Same-name pages: distinct query URLs both kept, page fetched once
        # per distinct URL
        self.assertIn(self.base + '/same-name?id=1', internal)
        self.assertIn(self.base + '/same-name?id=2', internal)
        same_name_hits = ScenarioHandler.request_log.count('/same-name?id=1')
        self.assertEqual(same_name_hits, 1)

        # robots.txt / sitemap.xml seeding preserved
        self.assertIn(self.base + '/forbidden', robots)
        self.assertIn(self.base + '/from-sitemap', internal)

        # Forbidden page is restricted
        self.assertIn(self.base + '/forbidden', failed)

        # No leftover temp files; every output published atomically
        leftovers = [n for n in os.listdir(out_dir)
                     if n.startswith('.') or n.endswith('.tmp')]
        self.assertEqual(leftovers, [])

        # Retry attempts show up in the request accounting
        self.assertIn('Total requests made:', stdout)

    def _assert_export_file(self, out_dir, name):
        path = os.path.join(out_dir, name)
        self.assertTrue(os.path.exists(path))
        # No temp siblings and the file is fully written (not truncated)
        self.assertFalse(
            any(n.startswith('.') for n in os.listdir(out_dir)))
        return path

    def test_json_export_is_atomic_and_complete(self):
        out_dir, _ = self.run_photon('json')
        path = self._assert_export_file(out_dir, 'exported.json')
        with open(path) as fh:
            data = json.load(fh)
        self.assertIn('internal', data)
        self.assertIn(self.base + '/flaky-child', data['internal'])
        self.assertIn(self.base + '/rate-limited', data['failed'])
        # Per-dataset txt outputs coexist and were not overwritten
        self.assertTrue(os.path.exists(os.path.join(out_dir, 'failed.txt')))

    def test_csv_export_is_atomic_and_complete(self):
        out_dir, _ = self.run_photon('csv')
        path = self._assert_export_file(out_dir, 'exported.csv')
        with open(path) as fh:
            content = fh.read()
        self.assertIn('internal', content)
        self.assertIn(self.base + '/flaky-child', content)
        self.assertIn('failed', content)
        self.assertIn(self.base + '/missing', content)
        self.assertTrue(os.path.exists(os.path.join(out_dir, 'internal.txt')))


if __name__ == '__main__':
    unittest.main()
