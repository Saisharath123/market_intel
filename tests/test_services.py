import logging

import pytest
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


def test_metrics_do_not_label_arbitrary_paths_or_queries():
    api = TestClient(market_app)
    api.get('/unknown-sensitive-value?token=private-value')
    metrics = api.get('/metrics')
    assert metrics.status_code == 200
    assert 'route="unmatched",service="market",status="404"' in metrics.text
    assert 'unknown-sensitive-value' not in metrics.text
    assert 'private-value' not in metrics.text


@pytest.mark.parametrize('service_app', [market_app, app])
def test_internal_api_has_security_headers_and_no_schema_ui(service_app):
    api = TestClient(service_app)
    for path in ('/docs', '/redoc', '/openapi.json'):
        assert api.get(path).status_code == 404

    response = api.get('/health')
    assert response.status_code == 200
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['X-Frame-Options'] == 'DENY'
    assert response.headers['Referrer-Policy'] == 'no-referrer'


def test_api_access_log_uses_route_template_without_request_data(caplog):
    logger = logging.getLogger('uvicorn.error')
    api = TestClient(market_app)
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger=logger.name):
            api.get('/unknown-sensitive-value?token=private-value')
    finally:
        logger.removeHandler(caplog.handler)

    event, = [record.getMessage() for record in caplog.records if record.name == 'uvicorn.error']
    assert 'api_request utc=' in event
    assert 'service=market route=unmatched status=404' in event
    assert 'unknown-sensitive-value' not in event
    assert 'private-value' not in event
