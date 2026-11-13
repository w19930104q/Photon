"""HTTP requesting with explicit outcome classification and bounded retries."""
import random
import threading
import time

import requests
from requests.exceptions import RequestException, TooManyRedirects


SESSION = requests.Session()
SESSION.max_redirects = 3

# Outcome categories -------------------------------------------------------
SUCCESS = 'success'        # Response fetched and parsed, processed may advance
RESTRICTED = 'restricted'  # Deterministic refusal (404/other 4xx, bad type,
                           # redirect loop): retrying within the run is useless
TRANSIENT = 'transient'    # Temporary failure (connection error, timeout, 429,
                           # 5xx, missing Content-Type): retryable on a budget

# Total fetch attempts allowed for a single URL during one run
DEFAULT_MAX_RETRIES = 3
# Exponential backoff bounds for retries after a transient failure
BACKOFF_BASE = 0.5
BACKOFF_CAP = 30.0

# Shared crawl state (processed/failed/...) is mutated from the thread pool.
_STATE_LOCK = threading.Lock()


class CrawlResult:
    """The outcome of fetching a URL.

    Attributes:
        status: one of SUCCESS, RESTRICTED, TRANSIENT
        body: response text for SUCCESS, otherwise the sentinel ``'dummy'``
        url: the requested URL (redirect identity is intentionally preserved)
        status_code: HTTP status code when a response was received
        error: exception or human readable reason for failures
    """

    __slots__ = ('status', 'body', 'url', 'status_code', 'error')

    def __init__(self, status, body, url, status_code=None, error=None):
        self.status = status
        self.body = body
        self.url = url
        self.status_code = status_code
        self.error = error

    @property
    def ok(self):
        """Whether the URL was fetched and parsed successfully."""
        return self.status == SUCCESS


def _backoff(attempt, response=None):
    """Return the seconds to sleep before attempt ``attempt + 1``.

    Honours a numeric ``Retry-After`` header (capped), otherwise falls back to
    exponential backoff with jitter.
    """
    if response is not None:
        retry_after = response.headers.get('Retry-After')
        if retry_after:
            try:
                return min(float(retry_after), BACKOFF_CAP)
            except ValueError:
                pass
    delay = min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_CAP)
    return delay + random.uniform(0, delay / 2.0)


def _mark_restricted(url, failed, transient, status_code=None, error=None):
    """Record a deterministic, non-retryable failure."""
    with _STATE_LOCK:
        transient.discard(url)
        failed.add(url)
    return CrawlResult(RESTRICTED, 'dummy', url, status_code, error)


def requester(
        url,
        main_url=None,
        delay=0,
        cook=None,
        headers=None,
        timeout=10,
        host=None,
        proxies=[None],
        user_agents=[None],
        failed=None,
        processed=None,
        transient=None,
        attempts=None,
        max_retries=DEFAULT_MAX_RETRIES
    ):
    """Fetch ``url`` and classify what happened.

    Only a successfully parsed response advances ``processed``. Restricted
    outcomes land in ``failed`` immediately and are never retried. Transient
    outcomes are retried with backoff until the per-URL budget (shared across
    the whole run through ``attempts``) is exhausted, at which point the URL
    also lands in ``failed``.
    """
    cook = cook or set()
    headers = headers or set()
    user_agents = user_agents or ['Photon']
    failed = failed if failed is not None else set()
    processed = processed if processed is not None else set()
    transient = transient if transient is not None else set()
    attempts = attempts if attempts is not None else {}
    budget = max(1, max_retries)

    # Pause/sleep the program for specified time
    time.sleep(delay)
    last_error = None

    while attempts.get(url, 0) < budget:
        final_headers = headers or {
            'Host': host,
            # Selecting a random user-agent
            'User-Agent': random.choice(user_agents),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip',
            'DNT': '1',
            'Connection': 'close',
        }
        with _STATE_LOCK:
            attempts[url] = attempts.get(url, 0) + 1
            attempt_no = attempts[url]

        try:
            response = SESSION.get(
                url,
                cookies=cook,
                headers=final_headers,
                verify=False,
                timeout=timeout,
                stream=True,
                proxies=random.choice(proxies)
            )
        except TooManyRedirects as err:
            # A redirect loop is deterministic: do not spend the retry budget
            return _mark_restricted(url, failed, transient, error=err)
        except RequestException as err:
            # Connection failure, timeout, DNS error, ...
            last_error = err
            response = None
        else:
            status_code = response.status_code
            content_type = response.headers.get('Content-Type', '')

            if status_code == 404 or (
                    400 <= status_code < 500 and status_code != 429):
                # 404 and other client errors are permanent for this run.
                # status_code is an int; compare it as one.
                response.close()
                return _mark_restricted(url, failed, transient, status_code)

            if status_code == 429 or 500 <= status_code < 600:
                # Rate limited / server error: worth retrying later
                response.close()
                last_error = 'HTTP %s' % status_code
            elif not content_type:
                # A missing Content-Type is often a transient server glitch;
                # only give up once the retry budget is spent.
                response.close()
                last_error = 'missing Content-Type'
            elif 'text/html' in content_type or \
                    'text/plain' in content_type:
                body = response.text
                # Success is the only outcome that advances the processed set
                with _STATE_LOCK:
                    processed.add(url)
                    transient.discard(url)
                    failed.discard(url)
                return CrawlResult(SUCCESS, body, url, status_code)
            else:
                # A present but unusable Content-Type (image, pdf, ...) is a
                # deterministic property of the resource: no retries.
                response.close()
                return _mark_restricted(
                    url, failed, transient, status_code,
                    'unsupported Content-Type: %s' % content_type)

        # Reachable only for transient failures.
        with _STATE_LOCK:
            transient.add(url)
        if attempts.get(url, 0) < budget:
            time.sleep(_backoff(attempt_no, response))

    # Retry budget exhausted: the URL stays recorded as a failed crawl.
    with _STATE_LOCK:
        transient.discard(url)
        failed.add(url)
    return CrawlResult(TRANSIENT, 'dummy', url, error=last_error)
