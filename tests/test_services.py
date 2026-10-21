import requests
from fastapi.testclient import TestClient

from market import client
from market.news_api import app
from market.market_api import app as market_app


def test_news_service(monkeypatch):
    monkeypatch.setenv('NEWS_MODE', 'demo')
    api = TestClient(app)
    assert api.get('/health').json()['service'] == 'news'
    assert api.get('/api/v1/news').json()['mode'] == 'demo'


def test_market_service_boundary(monkeypatch):
    monkeypatch.setenv('MARKET_MODE', 'demo')
    api = TestClient(market_app)
    assert api.get('/health').json()['service'] == 'market'
    assert api.get('/api/v1/quotes').json()['mode'] == 'demo'
    assert api.get('/api/v1/news').status_code == 404


def test_remote_outage_does_not_fall_back_to_local(monkeypatch):
    def fail(*args, **kwargs):
        raise requests.Timeout()

    monkeypatch.setattr(client.requests, 'get', fail)

    def forbidden_fallback():
        raise AssertionError('Remote outages must never fall back silently')

    result = client.fetch('http://market-api:8000', '/api/v1/quotes', forbidden_fallback)
    assert result['status'] == 'unavailable'
    assert result['items'] == []


def test_remote_service_response(monkeypatch):
    payload = {'items': [], 'mode': 'yahoo', 'source': 'Yahoo Finance',
               'status': 'ok', 'fetched_at': '2026-10-21T00:00:00Z', 'error': None}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    monkeypatch.setattr(client.requests, 'get', lambda *args, **kwargs: Response())
    assert client.fetch('http://market-api:8000', '/api/v1/quotes', None) == payload
