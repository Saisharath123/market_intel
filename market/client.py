"""Dashboard transport for private API routes and the local demo."""
import os
from urllib.parse import urlsplit

import requests

from market import service


def fetch(base_url, path, fallback, *, client_cert=None):
    if not base_url and client_cert is None:
        return fallback()
    try:
        options = {}
        if client_cert is not None:
            endpoint = urlsplit(base_url or '')
            if not all(client_cert) or endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username is not None:
                raise ValueError('Client authentication requires a complete certificate pair and an HTTPS API')
            options.update(cert=client_cert, allow_redirects=False)
        response = requests.get(base_url.rstrip('/') + path, timeout=30, **options)
        response.raise_for_status()
        if client_cert is not None and response.status_code != 200:
            raise ValueError('Unexpected authenticated API response status')
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
            raise ValueError('Invalid service response')
        for key in ('mode', 'source', 'status', 'fetched_at', 'error'):
            if key not in payload:
                raise ValueError('Missing service metadata')
        return payload
    except (requests.RequestException, ValueError):
        return dict(mode='remote', source='Internal service', status='unavailable',
                    fetched_at=service.utcnow(), error='Data service is unavailable. Please retry shortly.',
                    items=[], missing_symbols=[])


def quotes():
    return fetch(os.getenv('MARKET_API_URL'), '/api/v1/quotes', service.quotes)


def news():
    cert = os.getenv('NEWS_API_CLIENT_CERT_FILE')
    key = os.getenv('NEWS_API_CLIENT_KEY_FILE')
    client_cert = (cert, key) if cert is not None or key is not None else None
    return fetch(os.getenv('NEWS_API_URL'), '/api/v1/news', service.news, client_cert=client_cert)
