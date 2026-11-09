"""Exercise running service contracts; HTTP 200 alone is insufficient."""
import argparse
from datetime import datetime, timezone
from functools import partial
import json
import math
import ssl
import time
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, urlopen


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def fetch(url, as_json=True, *, open_url=None):
    with (open_url or urlopen)(url, timeout=10) as response:
        if response.status != 200:
            raise ValueError(f'Unexpected HTTP status from {url}')
        payload = response.read(2_000_001)
    if len(payload) > 2_000_000:
        raise ValueError('Response exceeds contract size limit')
    return json.loads(payload) if as_json else payload.decode('utf-8').strip()


def check_snapshot(payload, expect_demo, expected_mode=None):
    if payload.get('status') != 'ok' or not payload.get('items'):
        raise ValueError('Provider returned an empty, unavailable, partial or stale snapshot')
    fetched_at = datetime.fromisoformat(payload['fetched_at'].replace('Z', '+00:00'))
    if fetched_at.tzinfo is None or not -30 <= (datetime.now(timezone.utc) - fetched_at).total_seconds() <= 360:
        raise ValueError('Missing or stale response fetch timestamp')
    if expect_demo and payload.get('mode') != 'demo':
        raise ValueError('Staging must use the explicit deterministic demo adapter')
    if expected_mode and payload.get('mode') != expected_mode:
        raise ValueError('Response provider mode does not match the reviewed environment')


def check_services(market_url, news_url, dashboard_url, expect_demo,
                   market_traffic_url=None, market_mode=None, news_mode=None, *, open_url=None):
    get = partial(fetch, open_url=open_url) if open_url is not None else fetch
    if get(market_url + '/health').get('service') != 'market':
        raise ValueError('Market service boundary changed')
    if get(news_url + '/health').get('service') != 'news':
        raise ValueError('News service boundary changed')
    # Probe process/metrics directly, and user-facing API contracts through the
    # weighted production route. The ALB intentionally routes only /api/v1.
    traffic_url = (market_traffic_url or market_url).rstrip('/')
    quotes = get(traffic_url + '/api/v1/quotes')
    headlines = get(news_url + '/api/v1/news')
    check_snapshot(quotes, expect_demo, market_mode)
    check_snapshot(headlines, expect_demo, news_mode)
    if expect_demo and len(quotes['items']) != 6:
        raise ValueError('Expected the six-symbol deterministic watchlist')
    for quote in quotes['items']:
        price = quote.get('price')
        if not quote.get('symbol') or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            raise ValueError('Invalid quote contract')
    if not all(item.get('title') for item in headlines['items']):
        raise ValueError('Invalid news contract')
    insights = get(traffic_url + '/api/v1/insights')
    if not all(key in insights for key in ('advances', 'declines', 'unchanged', 'leaders')):
        raise ValueError('Invalid insight contract')
    if get(dashboard_url + '/_stcore/health', as_json=False) != 'ok':
        raise ValueError('Dashboard failed its health contract')
    metrics = get(market_url + '/metrics', as_json=False)
    if 'market_intel_http_requests_total' not in metrics:
        raise ValueError('API instrumentation is missing')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--market-url', default='http://market-api:8000')
    parser.add_argument('--news-url', default='http://news-api:8000')
    parser.add_argument('--dashboard-url', default='http://dashboard:8501')
    parser.add_argument('--market-traffic-url', help='Weighted API origin; process and metrics checks remain direct')
    parser.add_argument('--market-mode')
    parser.add_argument('--news-mode')
    parser.add_argument('--tls-ca-file', help='Trust this CA for HTTPS APIs and refuse redirects')
    parser.add_argument('--expect-demo', action='store_true')
    parser.add_argument('--attempts', type=int, default=6)
    args = parser.parse_args()
    if args.attempts < 1:
        parser.error('--attempts must be positive; every smoke run must check services')
    tls = {}
    if args.tls_ca_file:
        urls = (args.market_url, args.news_url, args.market_traffic_url or args.market_url)
        if any(urlsplit(url).scheme != 'https' for url in urls):
            parser.error('--tls-ca-file requires HTTPS market, news and market traffic URLs')
        context = ssl.create_default_context(cafile=args.tls_ca_file)
        tls['open_url'] = build_opener(ProxyHandler({}), HTTPSHandler(context=context), _NoRedirect()).open
    for attempt in range(args.attempts):
        try:
            check_services(args.market_url, args.news_url, args.dashboard_url, args.expect_demo,
                           args.market_traffic_url, args.market_mode, args.news_mode, **tls)
            print('Dashboard, market/news data contracts, freshness and metrics passed')
            return
        except (OSError, ValueError, KeyError) as error:
            if attempt + 1 == args.attempts:
                raise SystemExit(f'Service smoke test failed: {error}') from error
            time.sleep(5)


if __name__ == '__main__':
    main()
