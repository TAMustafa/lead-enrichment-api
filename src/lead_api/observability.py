import json
import logging

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {
                "level": record.levelname,
                "event": record.getMessage(),
                "request_id": getattr(record, "request_id", None),
                "status": getattr(record, "status", None),
                "duration_ms": getattr(record, "duration_ms", None),
            }
        )


def configure(endpoint):
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger("lead_api")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # Never enable HTTP wire/debug logging: provider URLs and headers may contain sensitive data.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    provider = TracerProvider(resource=Resource.create({"service.name": "lead-qualification-api"}))
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    return provider
