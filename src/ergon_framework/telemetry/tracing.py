import threading
from typing import Any, Dict, List, Optional

from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.trace import set_tracer_provider
from pydantic import BaseModel, ConfigDict, Field

from ._resource import _inject_otel_resource_attributes

# ============================================================
# Pydantic CONFIG OBJECTS
# ============================================================


class SamplerConfig(BaseModel):
    """
    Configures sampling strategy such as:
        - AlwaysOnSampler
        - AlwaysOffSampler
        - TraceIdRatioBased
    """

    sampler: Any  # class or callable
    args: Dict[str, Any] = Field(default_factory=dict)


class ExporterConfig(BaseModel):
    """
    Configuration for lazy exporter instantiation.
    Required for multiprocessing support since gRPC channels cannot be pickled.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    exporter: Any  # Exporter class (e.g., OTLPSpanExporter)
    args: Dict[str, Any] = Field(default_factory=dict)


class SpanProcessor(BaseModel):
    """
    Mirrors logging.ProcessorConfig.
    Defines a span processor with one or multiple span exporters.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    processor: Any  # BatchSpanProcessor, SimpleSpanProcessor, custom
    config: Dict[str, Any] = Field(default_factory=dict)
    # Exporters can be either instances or ExporterConfig for lazy instantiation
    exporters: List[Any] = Field(default_factory=list)


class TracingConfig(BaseModel):
    """
    Root configuration object for tracing.
    Mirrors LoggingConfig structure.
    """

    resource: Dict[str, Any] = Field(default_factory=dict)
    sampler: Optional[SamplerConfig] = None
    processors: List[SpanProcessor] = Field(default_factory=list)


# ============================================================
# INTERNAL STATE
# ============================================================

_TRACING_CONFIGURED = False
_TRACING_LOCK = threading.Lock()


# ============================================================
# APPLY TRACING CONFIG
# ============================================================


def _apply_tracing_config(cfg: TracingConfig, metadata: dict):
    """
    Initializes global OpenTelemetry TracerProvider.
    - Builds Resource
    - Applies sampler
    - Instantiates span processors
    - Registers exporters
    - Sets provider globally

    Mirrors logging.apply_logger_config but adapted for tracing.
    """

    global _TRACING_CONFIGURED

    with _TRACING_LOCK:
        if _TRACING_CONFIGURED:
            return

        cfg.resource = _inject_otel_resource_attributes(resource=cfg.resource, metadata=metadata)

        # Build Resource
        resource = Resource(attributes=cfg.resource)

        # Build Sampler
        if cfg.sampler:
            sampler_instance = cfg.sampler.sampler(**cfg.sampler.args)
            provider = TracerProvider(resource=resource, sampler=sampler_instance)
        else:
            provider = TracerProvider(resource=resource)

        # Build processors
        for p in cfg.processors:
            for exporter in p.exporters:
                # Handle lazy configuration for multiprocessing support
                if isinstance(exporter, ExporterConfig):
                    exporter_instance = exporter.exporter(**exporter.args)
                else:
                    exporter_instance = exporter

                processor = p.processor(exporter_instance, **p.config)
                provider.add_span_processor(processor)

        # Register provider globally
        set_tracer_provider(provider)

        _TRACING_CONFIGURED = True


# ============================================================
# GET TRACER
# ============================================================


def get_tracer(name: str = "ergon"):
    """
    Returns a tracer with the given name.
    Mirrors get_logger(name).
    """
    from opentelemetry.trace import get_tracer

    return get_tracer(name)


__all__ = [
    "OTLPSpanExporter",
    "BatchSpanProcessor",
    "SimpleSpanProcessor",
    "SamplerConfig",
    "SpanProcessor",
    "TracingConfig",
    "ExporterConfig",
    "get_tracer",
]
