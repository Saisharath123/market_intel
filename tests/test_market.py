import pytest
import requests
from pathlib import Path
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest
from market.api import app
from market import service


def test_percent_change():
    assert service.percent_change(110, 100) == 10
    assert service.percent_change(90, 100) == -10
    assert service.percent_change(10, 0) is None


def test_rss_dedup_and_reject_external_links():
    xml = b'''<rss><channel>
    <item><title>A &amp; B</title><link>https://www.moneycontrol.com/news/1</link></item>
    <item><title>Duplicate</title><link>https://www.moneycontrol.com/news/1</link></item>
    <item><title>External</title><link>https://evil.example/</link></item>
    </channel></rss>'''
    assert len(service.parse_rss(xml)) == 1
    assert service.parse_rss(xml)[0]['title'] == 'A & B'
    with pytest.raises(ValueError):
        service.parse_rss(b'<html/>')


@pytest.fixture
def rss_transport(monkeypatch):
    calls, responses = [], []

    class Body:
        def __init__(self, chunks):
            self.chunks = chunks
            self.reads = 0
            self.closed = False

        def stream(self, chunk_size, *, decode_content):
            assert chunk_size == 65_536
            assert decode_content
            for chunk in self.chunks:
                self.reads += 1
                yield chunk

        def close(self):
            self.closed = True

    def queue(status, chunks, location=None):
        response = requests.Response()
        response.status_code = status
        response.raw = Body(chunks)
        if location is not None:
            response.headers['Location'] = location
        responses.append(response)
        return response.raw

    def send(_adapter, request, *, stream, timeout, **_settings):
        assert stream and timeout == 10
        calls.append(request.url)
        response = responses.pop(0)
        response.url = request.url
        response.request = request
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    return calls, queue


@pytest.mark.parametrize('mode, host', [('moneycontrol', 'moneycontrol.com'), ('cnbctv18', 'cnbctv18.com')])
def test_news_follows_approved_https_redirect(monkeypatch, rss_transport, mode, host):
    monkeypatch.setenv('NEWS_MODE', mode)
    calls, queue = rss_transport
    redirect = queue(302, [b'Redirect body'], '/rss/new.xml')
    queue(200, [f'<rss><channel><item><title>Market news</title><link>https://www.{host}/news/1</link>'
                '</item></channel></rss>'.encode()])

    result = service.news.__wrapped__()

    assert calls == [service.NEWS_FEEDS[mode][1], f'https://www.{host}/rss/new.xml']
    assert result['items'][0]['title'] == 'Market news'
    assert redirect.reads == 0 and redirect.closed


@pytest.mark.parametrize('location', [
    'http://www.moneycontrol.com/rss/new.xml',
    'https://www.moneycontrol.com.evil.example/rss.xml',
    'https://www.moneycontrol.com:8443/rss.xml',
    'https://user@www.moneycontrol.com/rss.xml',
    'https://evil.example\\@www.moneycontrol.com/rss.xml',
    '//127.0.0.1/rss.xml',
])
def test_news_rejects_unsafe_redirect_without_reading_body(monkeypatch, rss_transport, location):
    monkeypatch.setenv('NEWS_MODE', 'moneycontrol')
    calls, queue = rss_transport
    redirect = queue(302, [b'Redirect body'], location)

    result = service.news.__wrapped__()

    assert calls == [service.NEWS_URL]
    assert result['status'] == 'unavailable' and result['items'] == []
    assert redirect.reads == 0 and redirect.closed


def test_news_bounds_redirect_hops(monkeypatch, rss_transport):
    monkeypatch.setenv('NEWS_MODE', 'moneycontrol')
    calls, queue = rss_transport
    for _ in range(4):
        queue(302, [], '/rss/new.xml')

    result = service.news.__wrapped__()

    assert len(calls) == 4
    assert result['status'] == 'unavailable' and result['items'] == []


def test_news_stops_reading_when_decoded_limit_is_exceeded(monkeypatch, rss_transport):
    monkeypatch.setenv('NEWS_MODE', 'moneycontrol')
    monkeypatch.setattr(service, 'MAX_RSS_BYTES', 9)
    _, queue = rss_transport
    payload = queue(200, [b'12345', b'67890', b'Never consumed'])

    result = service.news.__wrapped__()

    assert result['status'] == 'unavailable' and result['items'] == []
    assert payload.reads == 2 and payload.closed


def test_missing_quotes_and_zero_close():
    data = {'NSE:TCS': {'last_price': 100, 'ohlc': {'close': 0}}}
    assert service.parse_quotes(data)[0]['change_pct'] is None
    assert service.parse_quotes({}) == []


def test_stale_news_is_flagged():
    items = [{'published_at': 'Tue, 23 Apr 2024 15:46:31 +0530'}]
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    assert service.news_freshness(items, now)[0] == 'stale'
    assert service.news_freshness([{'published_at': None}], now)[0] == 'unknown'


def test_live_failure_never_returns_demo(monkeypatch):
    monkeypatch.setenv('MARKET_MODE', 'kite')
    monkeypatch.setenv('KITE_API_KEY', 'test')
    monkeypatch.setenv('KITE_ACCESS_TOKEN', 'test')

    def fail(*args, **kwargs):
        raise requests.Timeout()

    monkeypatch.setattr(service.requests, 'get', fail)
    response = service.quotes.__wrapped__()
    assert response['status'] == 'unavailable'
    assert response['items'] == []
    assert response['mode'] == 'kite'


def test_api_and_insight_scope(monkeypatch):
    monkeypatch.setenv('MARKET_MODE', 'demo')
    monkeypatch.setenv('NEWS_MODE', 'demo')
    client = TestClient(app)
    assert client.get('/health').status_code == 200
    assert client.get('/api/v1/quotes').json()['mode'] == 'demo'
    assert client.get('/api/v1/news').json()['mode'] == 'demo'
    result = client.get('/api/v1/insights').json()
    assert result['advances'] + result['declines'] + result['unchanged'] == 4
    assert 'not exchange-wide' in result['scope']


def test_streamlit_demo(monkeypatch):
    monkeypatch.setenv('MARKET_MODE', 'demo')
    monkeypatch.setenv('NEWS_MODE', 'demo')
    view = AppTest.from_file(Path(__file__).resolve().parents[1] / 'app.py').run(timeout=30)
    assert not view.exception
    assert len(view.metric) == 6
    assert 'DEMO QUOTES' in view.warning[0].value
