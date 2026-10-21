"""Shared read-only data adapters. Never silently substitute demo data."""
import os
import math
import time
from datetime import datetime, timezone
from functools import wraps
from email.utils import parsedate_to_datetime
from threading import Lock
from urllib.parse import urlparse

import requests
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from dotenv import load_dotenv

load_dotenv()
WATCHLIST = ('NSE:NIFTY 50', 'NSE:NIFTY BANK', 'NSE:RELIANCE', 'NSE:TCS', 'NSE:HDFCBANK', 'NSE:INFY')
NEWS_URL = 'https://www.moneycontrol.com/rss/marketreports.xml'
NEWS_FEEDS = {
    'moneycontrol': ('Moneycontrol', NEWS_URL, 'moneycontrol.com'),
    'cnbctv18': ('CNBC TV18', 'https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml', 'cnbctv18.com'),
}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def cached(seconds):
    """Per-process bounded TTL cache; serialize misses to limit upstream requests."""
    def decorate(fn):
        lock, state = Lock(), {}

        @wraps(fn)
        def wrapped():
            with lock:
                if time.monotonic() >= state.get('expires', 0):
                    state['value'] = fn()
                    state['expires'] = time.monotonic() + seconds
                return state['value']
        return wrapped
    return decorate


def percent_change(price, previous_close):
    return round((price / previous_close - 1) * 100, 2) if previous_close > 0 else None


def parse_quotes(data):
    rows = []
    for symbol in WATCHLIST:
        item = data.get(symbol)
        if not item:
            continue
        price, close = float(item['last_price']), float(item['ohlc']['close'])
        if not all(math.isfinite(v) and v >= 0 for v in (price, close)):
            raise ValueError('Invalid quote')
        rows.append(dict(symbol=symbol, price=price, previous_close=close,
                         change_pct=percent_change(price, close),
                         quote_time=item.get('timestamp'), volume=item.get('volume')))
    return rows


def parse_rss(payload, source='Moneycontrol', allowed_host='moneycontrol.com'):
    root = ElementTree.fromstring(payload)
    if root.tag != 'rss':
        raise ValueError('Expected RSS XML')
    rows, seen = [], set()
    for item in root.findall('./channel/item'):
        title = (item.findtext('title') or '').strip()
        link = (item.findtext('link') or '').strip()
        host = (urlparse(link).hostname or '').lower()
        if not title or urlparse(link).scheme not in ('https', 'http'):
            continue
        if host != allowed_host and not host.endswith('.' + allowed_host):
            continue
        if link in seen:
            continue
        seen.add(link)
        rows.append(dict(title=title, url=link, published_at=item.findtext('pubDate'), source=source))
    if not rows:
        raise ValueError('Feed contained no usable headlines')
    return rows[:30]


def news_freshness(items, now=None):
    now = now or datetime.now(timezone.utc)
    dates = []
    for item in items:
        try:
            date = parsedate_to_datetime(item['published_at'])
            if date.tzinfo is not None:
                dates.append(date)
        except (TypeError, ValueError, KeyError):
            continue
    if not dates:
        return 'unknown', 'Publication dates unavailable; news freshness cannot be verified.'
    if (now - max(dates)).total_seconds() > 72 * 3600:
        return 'stale', 'Latest feed headline is over 72 hours old. This feed is not current market news.'
    if max(dates) > now:
        return 'unknown', 'Feed has a future publication date; verify the source timestamp.'
    return 'ok', None


@cached(300)
def quotes():
    mode = os.getenv('MARKET_MODE', 'yahoo')
    result = dict(mode=mode, source={'kite': 'Kite', 'yahoo': 'Yahoo Finance via yfinance', 'demo': 'Demo'}.get(mode, mode), fetched_at=utcnow(),
                  status='ok', error=None, items=[], missing_symbols=[])
    if mode == 'yahoo':
        from market.yahoo import fetch_quotes
        result['items'] = fetch_quotes()
        present = {row['symbol'] for row in result['items']}
        result['missing_symbols'] = [s for s in WATCHLIST if s not in present]
        if result['missing_symbols']:
            result.update(status='partial' if present else 'unavailable',
                          error='Some Yahoo quotes could not load. The free source may be rate-limited or unavailable; retry after five minutes.')
        elif any(row['age_hours'] > 36 for row in result['items']):
            result.update(status='stale', error='Some quotes are over 36 hours old. Check exchange timestamps; markets may be closed or the source delayed.')
        return result
    if mode == 'demo':
        prices = [22450, 48100, 1420, 3650, 1700, 1550]
        closes = [22300, 48300, 1400, 3690, 1680, 1540]
        result['items'] = [dict(symbol=s, price=p, previous_close=c, change_pct=percent_change(p, c),
                                quote_time=None, volume=None) for s, p, c in zip(WATCHLIST, prices, closes)]
        return result
    key, token = os.getenv('KITE_API_KEY'), os.getenv('KITE_ACCESS_TOKEN')
    if mode != 'kite' or not key or not token:
        return dict(result, status='unavailable', error='Set MARKET_MODE=kite and configure Kite credentials on the server.')
    try:
        response = requests.get('https://api.kite.trade/quote', params=[('i', s) for s in WATCHLIST],
                                headers={'X-Kite-Version': '3', 'Authorization': f'token {key}:{token}'}, timeout=10)
        response.raise_for_status()
        payload = response.json()
        if payload.get('status') != 'success':
            raise ValueError('Provider rejected request')
        result['items'] = parse_quotes(payload['data'])
        present = {q['symbol'] for q in result['items']}
        result['missing_symbols'] = [s for s in WATCHLIST if s not in present]
        if result['missing_symbols']:
            result['status'] = 'partial' if present else 'unavailable'
            result['error'] = 'Some requested quotes are unavailable.'
    except (requests.RequestException, ValueError, KeyError, TypeError):
        result.update(status='unavailable', error='Kite request failed. Check connectivity, API entitlement and token expiry.')
    return result


@cached(300)
def news():
    mode = os.getenv('NEWS_MODE', 'cnbctv18')
    source, url, host = NEWS_FEEDS.get(mode, ('Demo', '', ''))
    result = dict(mode=mode, source=source,
                  fetched_at=utcnow(), status='ok', error=None, items=[])
    if mode == 'demo':
        result['items'] = [dict(title='Demo: earnings season and policy updates in focus',
                                url=None, published_at=None, source='Synthetic example')]
        return result
    if mode not in NEWS_FEEDS:
        return dict(result, status='unavailable', error='NEWS_MODE must be demo, cnbctv18 or moneycontrol.')
    try:
        response = requests.get(url, timeout=10, headers={'User-Agent': 'IndianMarketMVP/0.1 RSS reader'})
        response.raise_for_status()
        if len(response.content) > 2_000_000:
            raise ValueError('Feed exceeds size limit')
        result['items'] = parse_rss(response.content, source, host)
        result['status'], result['error'] = news_freshness(result['items'])
    except (requests.RequestException, ValueError, ElementTree.ParseError, DefusedXmlException):
        result.update(status='unavailable', error=f'{source} RSS is unavailable or changed format. Retry later.')
    return result


def insights(snapshot):
    equities = [q for q in snapshot['items'] if q['symbol'] not in WATCHLIST[:2] and q['change_pct'] is not None]
    return dict(scope='Four-stock demo watchlist; not exchange-wide breadth' if snapshot['mode'] == 'demo'
                else 'Four-stock watchlist; not exchange-wide breadth',
                advances=sum(q['change_pct'] > 0 for q in equities),
                declines=sum(q['change_pct'] < 0 for q in equities),
                unchanged=sum(q['change_pct'] == 0 for q in equities),
                leaders=sorted(equities, key=lambda q: q['change_pct'], reverse=True))
