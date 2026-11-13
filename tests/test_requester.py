"""Unit tests for core.requester outcome classification and retry budget."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import requests

from core import requester
from core.requester import (
    requester as do_request,
    SUCCESS, RESTRICTED, TRANSIENT, DEFAULT_MAX_RETRIES,
    CrawlResult,
)


class FakeResponse:
    def __init__(self, status_code=200, content_type='text/html',
                 text='<html></html>', url=None):
        self.status_code = status_code
        if content_type is None:
            self.headers = {}
        else:
            self.headers = {'Content-Type': content_type}
        self.text = text
        self.url = url
        self.closed = 0

    def close(self):
        self.closed += 1


def call(url='http://example.com/', **overrides):
    """Invoke requester with safe defaults and no real sleeping/network."""
    kwargs = dict(
        main_url='http://example.com',
        delay=0,
        cook=None,
        headers=None,
        timeout=5,
        host='example.com',
        proxies=[None],
        user_agents=['test-agent'],
        failed=set(),
        processed=set(['dummy']),
        transient=set(),
        attempts={},
    )
    kwargs.update(overrides)
    return do_request(url, **kwargs), kwargs


class RequesterTests(unittest.TestCase):

    def setUp(self):
        # requester.time.sleep is used for both the crawl delay (default 0)
        # and retry backoff (always > 0). Patch it to no-op and record only
        # the non-zero backoff sleeps.
        self.backoff_calls = []

        def fake_sleep(secs):
            if secs:
                self.backoff_calls.append(secs)

        sleep_patch = mock.patch.object(requester.time, 'sleep',
                                        side_effect=fake_sleep)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def patch_get(self, *args, **kwargs):
        """Patch SESSION.get and return the mock (use it for call_count)."""
        patcher = mock.patch.object(requester.SESSION, 'get',
                                    *args, **kwargs)
        get_mock = patcher.start()
        self.addCleanup(patcher.stop)
        return get_mock

    # ------------------------------------------------------------------ #
    # Success
    # ------------------------------------------------------------------ #
    def test_success_advances_processed_and_returns_body(self):
        fake = FakeResponse(text='<p>hello</p>')
        get_mock = self.patch_get(return_value=fake)
        result, state = call()
        self.assertIsInstance(result, CrawlResult)
        self.assertEqual(result.status, SUCCESS)
        self.assertTrue(result.ok)
        self.assertEqual(result.body, '<p>hello</p>')
        self.assertIn('http://example.com/', state['processed'])
        self.assertEqual(state['failed'], set())
        self.assertEqual(state['transient'], set())
        self.assertEqual(state['attempts']['http://example.com/'], 1)
        # No backoff sleep on the fast path (the delay=0 sleep is ignored)
        self.assertEqual(self.backoff_calls, [])
        self.assertEqual(get_mock.call_count, 1)

    # ------------------------------------------------------------------ #
    # First hop failure: connection error must be retried, not fatal
    # ------------------------------------------------------------------ #
    def test_connection_failure_retries_then_succeeds(self):
        fail = requests.exceptions.ConnectionError('refused')
        ok = FakeResponse(text='recovered')
        get_mock = self.patch_get(side_effect=[fail, ok])
        result, state = call()
        self.assertEqual(result.status, SUCCESS)
        self.assertEqual(result.body, 'recovered')
        self.assertIn('http://example.com/', state['processed'])
        self.assertEqual(state['failed'], set())
        # One backoff sleep between the two attempts
        self.assertEqual(len(self.backoff_calls), 1)
        self.assertEqual(state['attempts']['http://example.com/'], 2)
        self.assertEqual(get_mock.call_count, 2)

    def test_connection_failure_exhausting_budget_is_transient_and_failed(self):
        fail = requests.exceptions.Timeout('timed out')
        get_mock = self.patch_get(side_effect=fail)
        result, state = call()
        self.assertEqual(result.status, TRANSIENT)
        self.assertFalse(result.ok)
        self.assertEqual(result.body, 'dummy')
        # Budget attempts, budget-1 backoff sleeps
        self.assertEqual(get_mock.call_count, DEFAULT_MAX_RETRIES)
        self.assertEqual(len(self.backoff_calls), DEFAULT_MAX_RETRIES - 1)
        self.assertNotIn('http://example.com/', state['processed'])
        self.assertIn('http://example.com/', state['failed'])
        self.assertEqual(state['transient'], set())

    def test_url_not_marked_processed_before_request(self):
        fail = requests.exceptions.ConnectionError('boom')
        self.patch_get(side_effect=fail)
        _, state = call()
        self.assertEqual(state['processed'], set(['dummy']))

    # ------------------------------------------------------------------ #
    # 404: restricted, single attempt, integer status comparison
    # ------------------------------------------------------------------ #
    def test_404_is_restricted_and_not_retried(self):
        fake = FakeResponse(status_code=404)
        get_mock = self.patch_get(return_value=fake)
        result, state = call()
        self.assertEqual(result.status, RESTRICTED)
        self.assertEqual(result.status_code, 404)
        self.assertEqual(get_mock.call_count, 1)
        self.assertEqual(self.backoff_calls, [])
        self.assertIn('http://example.com/', state['failed'])
        self.assertNotIn('http://example.com/', state['processed'])
        self.assertEqual(fake.closed, 1)

    def test_other_4xx_is_restricted(self):
        fake = FakeResponse(status_code=403)
        get_mock = self.patch_get(return_value=fake)
        result, _ = call()
        self.assertEqual(result.status, RESTRICTED)
        self.assertEqual(get_mock.call_count, 1)

    # ------------------------------------------------------------------ #
    # 429: transient, honored retry budget, respects Retry-After
    # ------------------------------------------------------------------ #
    def test_429_retries_then_succeeds(self):
        limited = FakeResponse(status_code=429, content_type=None, text='')
        limited.headers['Retry-After'] = '0'
        ok = FakeResponse(text='ok')
        get_mock = self.patch_get(side_effect=[limited, ok])
        result, state = call()
        self.assertEqual(result.status, SUCCESS)
        self.assertEqual(get_mock.call_count, 2)
        # Retry-After: 0 yields a zero wait, so no non-zero backoff recorded
        self.assertEqual(self.backoff_calls, [])
        self.assertIn('http://example.com/', state['processed'])

    def test_429_exhausted_is_transient_and_failed(self):
        limited = FakeResponse(status_code=429, content_type=None, text='')
        get_mock = self.patch_get(return_value=limited)
        result, state = call()
        self.assertEqual(result.status, TRANSIENT)
        self.assertEqual(get_mock.call_count, DEFAULT_MAX_RETRIES)
        self.assertIn('http://example.com/', state['failed'])
        self.assertNotIn('http://example.com/', state['processed'])

    def test_5xx_is_transient(self):
        server_error = FakeResponse(status_code=503, content_type=None)
        ok = FakeResponse(text='back')
        get_mock = self.patch_get(side_effect=[server_error, ok])
        result, _ = call()
        self.assertEqual(result.status, SUCCESS)
        self.assertEqual(get_mock.call_count, 2)

    # ------------------------------------------------------------------ #
    # Bad / missing Content-Type
    # ------------------------------------------------------------------ #
    def test_bad_content_type_is_restricted(self):
        fake = FakeResponse(status_code=200, content_type='application/pdf')
        get_mock = self.patch_get(return_value=fake)
        result, state = call()
        self.assertEqual(result.status, RESTRICTED)
        self.assertIn('unsupported Content-Type', result.error)
        self.assertEqual(get_mock.call_count, 1)
        self.assertIn('http://example.com/', state['failed'])
        self.assertEqual(fake.closed, 1)

    def test_missing_content_type_is_transient_then_recovers(self):
        no_type = FakeResponse(status_code=200, content_type=None)
        ok = FakeResponse(text='fine')
        get_mock = self.patch_get(side_effect=[no_type, ok])
        result, state = call()
        self.assertEqual(result.status, SUCCESS)
        self.assertEqual(result.body, 'fine')
        self.assertEqual(get_mock.call_count, 2)
        self.assertIn('http://example.com/', state['processed'])

    def test_missing_content_type_exhausted_is_failed(self):
        no_type = FakeResponse(status_code=200, content_type=None)
        get_mock = self.patch_get(return_value=no_type)
        result, state = call()
        self.assertEqual(result.status, TRANSIENT)
        self.assertIn('http://example.com/', state['failed'])
        self.assertNotIn('http://example.com/', state['processed'])
        self.assertEqual(get_mock.call_count, DEFAULT_MAX_RETRIES)

    # ------------------------------------------------------------------ #
    # Redirects: identity of the requested URL is preserved
    # ------------------------------------------------------------------ #
    def test_redirect_keeps_original_url_identity(self):
        fake = FakeResponse(text='<html>moved</html>',
                            url='http://example.com/new-place')
        get_mock = self.patch_get(return_value=fake)
        result, state = call(url='http://example.com/old-place')
        self.assertEqual(result.status, SUCCESS)
        self.assertEqual(get_mock.call_count, 1)
        # The processed entry is the originally requested URL, not the target
        self.assertIn('http://example.com/old-place', state['processed'])
        self.assertNotIn('http://example.com/new-place', state['processed'])
        self.assertEqual(result.url, 'http://example.com/old-place')

    def test_redirect_loop_is_restricted(self):
        err = requests.exceptions.TooManyRedirects('loop')
        get_mock = self.patch_get(side_effect=err)
        result, state = call()
        self.assertEqual(result.status, RESTRICTED)
        self.assertEqual(get_mock.call_count, 1)
        self.assertIn('http://example.com/', state['failed'])
        self.assertNotIn('http://example.com/', state['processed'])

    # ------------------------------------------------------------------ #
    # Same page (duplicate submission): shared budget and processed set
    # ------------------------------------------------------------------ #
    def test_duplicate_submission_shares_budget_and_processed(self):
        url = 'http://example.com/same'
        attempts = {}
        failed, processed, transient = set(), set(['dummy']), set()
        flaky = FakeResponse(status_code=500, content_type=None)
        ok = FakeResponse(text='body')
        # First call: flaky then ok (budget spent at attempt 2). Second call
        # happens to fetch once more because the shared budget is per-URL and
        # the first call already recorded a success; provide a spare response.
        get_mock = self.patch_get(side_effect=[flaky, ok, ok])
        first, _ = call(url, attempts=attempts, failed=failed,
                        processed=processed, transient=transient)
        second, _ = call(url, attempts=attempts, failed=failed,
                         processed=processed, transient=transient)
        self.assertEqual(first.status, SUCCESS)
        self.assertEqual(second.status, SUCCESS)
        self.assertEqual(attempts[url], 3)  # shared counter keeps growing
        self.assertEqual(len(processed), 2)  # dummy + url, no duplicates
        self.assertEqual(failed, set())
        self.assertEqual(get_mock.call_count, 3)

    def test_budget_is_shared_across_calls_and_caps_retries(self):
        url = 'http://example.com/flaky'
        attempts = {}
        failed, processed, transient = set(), set(['dummy']), set()
        err = requests.exceptions.ConnectionError('nope')
        get_mock = self.patch_get(side_effect=err)
        first, _ = call(url, attempts=attempts, failed=failed,
                        processed=processed, transient=transient,
                        max_retries=2)
        second, _ = call(url, attempts=attempts, failed=failed,
                         processed=processed, transient=transient,
                         max_retries=2)
        # First call spends the whole budget of two; second makes no new fetch
        self.assertEqual(get_mock.call_count, 2)
        self.assertEqual(attempts[url], 2)
        self.assertEqual(first.status, TRANSIENT)
        self.assertEqual(second.status, TRANSIENT)
        self.assertIn(url, failed)
        self.assertNotIn(url, processed)


if __name__ == '__main__':
    unittest.main()
