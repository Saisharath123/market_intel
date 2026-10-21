"""Dashboard transport. EKS uses internal HTTP services; local mode remains supported."""
import os

import requests

from market import service


def fetch(base_url, path, fallback):
    if not base_url:
        return fallback()
    try:
        response = requests.get(base_url.rstrip('/') + path, timeout=30)
        response.raise_for_status()
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
    return fetch(os.getenv('NEWS_API_URL'), '/api/v1/news', service.news)
