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
