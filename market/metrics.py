"""Bounded-cardinality HTTP metrics; no query strings or user input labels."""
import time

from fastapi import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

REQUESTS = Counter('market_intel_http_requests_total', 'API responses', ['service', 'route', 'status'])
LATENCY = Histogram('market_intel_http_request_seconds', 'API latency', ['service', 'route'])


def instrument(app, service):
    @app.middleware('http')
    async def observe(request, call_next):
        started = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = getattr(request.scope.get('route'), 'path', 'unmatched')
            if route not in ('/metrics', '/health'):
                REQUESTS.labels(service, route, str(status)).inc()
                LATENCY.labels(service, route).observe(time.monotonic() - started)

    @app.get('/metrics', include_in_schema=False)
    def metrics():
        return Response(generate_latest(), headers={'Content-Type': CONTENT_TYPE_LATEST})
