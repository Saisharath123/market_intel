"""Observe internal API traffic without recording raw paths or query strings."""
import logging
import time
from datetime import datetime, timezone

from fastapi import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

REQUESTS = Counter('market_intel_http_requests_total', 'API responses', ['service', 'route', 'status'])
LATENCY = Histogram('market_intel_http_request_seconds', 'API latency', ['service', 'route'])
ACCESS_LOG = logging.getLogger('uvicorn.error')


def instrument(app, service):
    @app.middleware('http')
    async def observe(request, call_next):
        started = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['X-Frame-Options'] = 'DENY'
            response.headers['Referrer-Policy'] = 'no-referrer'
            return response
        finally:
            route = getattr(request.scope.get('route'), 'path', 'unmatched')
            if route not in ('/metrics', '/health'):
                elapsed = time.monotonic() - started
                REQUESTS.labels(service, route, str(status)).inc()
                LATENCY.labels(service, route).observe(elapsed)
                ACCESS_LOG.info(
                    'api_request utc=%s service=%s route=%s status=%d duration_ms=%.1f',
                    datetime.now(timezone.utc).isoformat(timespec='milliseconds'), service, route, status, elapsed * 1000,
                )

    @app.get('/metrics', include_in_schema=False)
    def metrics():
        return Response(generate_latest(), headers={'Content-Type': CONTENT_TYPE_LATEST})
