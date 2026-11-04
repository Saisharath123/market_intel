"""Optional vendor-neutral OTLP metrics and tracing; disabled without an endpoint."""
import os
from threading import Lock

_lock = Lock()
_providers = None


def configure_telemetry(service_name, app=None):
    global _providers
    endpoint = os.getenv('OTEL_EXPORTER_OTLP_ENDPOINT', '').rstrip('/')
    if not endpoint:
        return False

    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    with _lock:
        if _providers is None:
            resource = Resource.create({
                'service.name': service_name,
                'deployment.environment.name': 'internal',
            })
            tracer = TracerProvider(resource=resource)
            tracer.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
                endpoint=endpoint + '/v1/traces', timeout=10,
            )))
            meter = MeterProvider(resource=resource, metric_readers=[PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=endpoint + '/v1/metrics', timeout=10),
                export_interval_millis=30000, export_timeout_millis=10000,
            )])
            trace.set_tracer_provider(tracer)
            metrics.set_meter_provider(meter)
            RequestsInstrumentor().instrument(tracer_provider=tracer, meter_provider=meter)
            _providers = (tracer, meter)
        if app is not None:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            FastAPIInstrumentor.instrument_app(
                app, tracer_provider=_providers[0], meter_provider=_providers[1],
                excluded_urls='/health,/metrics',
            )
    return True
