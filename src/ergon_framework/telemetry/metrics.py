# metrics.py

from typing import Any, Dict, List

from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import get_meter_provider, set_meter_provider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from pydantic import BaseModel, Field

from ._resource import _inject_otel_resource_attributes

# ============================================================
# CONFIG OBJECTS
# ============================================================


class MetricReader(BaseModel):
    """
    Defines how a metric reader should be constructed.
    Only push-based readers are supported (PeriodicExportingMetricReader).
    """

    reader: Any
    config: Dict[str, Any] = Field(default_factory=dict)
    exporters: List[Any] = Field(default_factory=list)


class MetricsConfig(BaseModel):
    """
    Root configuration object for the metrics subsystem.
    """

    resource: Dict[str, Any] = Field(default_factory=dict)
    readers: List[MetricReader] = Field(default_factory=list)


# internal global flag
_CONFIGURED_METRICS = False


# ============================================================
# APPLY METRICS CONFIGURATION
# ============================================================


def _apply_metrics_config(cfg: MetricsConfig, metadata: dict):
    global _CONFIGURED_METRICS
    if _CONFIGURED_METRICS:
        return

    cfg.resource = _inject_otel_resource_attributes(resource=cfg.resource, metadata=metadata)

    # 1. Create Resource
    resource = Resource(attributes=cfg.resource)

    # 2. Create MeterProvider
    provider = MeterProvider(resource=resource)

    # 3. Register metric readers (PUSH-BASED ONLY)
    for r in cfg.readers:
        reader_cls = r.reader

        # Create reader instance
        # Each reader may have multiple exporters
        # For push-based metrics, exporters are passed inside PeriodicExportingMetricReader
        for exporter in r.exporters:
            reader = reader_cls(exporter=exporter, **r.config)
            provider.add_metric_reader(reader)

    # 4. Register globally
    set_meter_provider(provider)

    _CONFIGURED_METRICS = True


# ============================================================
# ACCESSOR
# ============================================================


def get_metric_meter(name: str):
    """
    Returns a Meter, similar to get_logger for logging.
    """
    provider = get_meter_provider()
    return provider.get_meter(name)


__all__ = [
    "OTLPMetricExporter",
    "ConsoleMetricExporter",
    "PeriodicExportingMetricReader",
    "MetricReader",
    "MetricsConfig",
    "get_metric_meter",
]
