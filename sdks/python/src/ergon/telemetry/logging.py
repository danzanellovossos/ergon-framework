import logging
import logging.config
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Union

from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    ConsoleLogExporter,
    SimpleLogRecordProcessor,
)

# ---------------------------
# OpenTelemetry imports
# ---------------------------
from opentelemetry.sdk.resources import Resource
from pydantic import BaseModel, Field, model_validator

from ._resource import _inject_otel_resource_attributes

# ============================================================
# Pydantic CONFIG OBJECTS
# ============================================================


class LogFormatter(BaseModel):
    name: str = "default"
    fmt: str = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
    datefmt: str = "%Y-%m-%d %H:%M:%S"


# ---------------------------
# Filter config
# ---------------------------
class LogFilter(BaseModel):
    name: str  # filter name in dictConfig
    filter: Any  # class inheriting from logging.Filter
    config: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------
# Base Handler config
# ---------------------------
class BaseLogHandler(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    formatter: str = "default"
    filters: List[str] = Field(default_factory=list)  # MUST be names


class ConsoleLogHandler(BaseLogHandler):
    type: Literal["console"] = "console"
    stream: Literal["stdout", "stderr"] = "stdout"


class FileLogHandler(BaseLogHandler):
    type: Literal["file"] = "file"
    filename: str
    mode: Literal["a", "w"] = "a"


class JSONLogHandler(BaseLogHandler):
    filename: str
    type: Literal["json"] = "json"
    formatter: str = "json"
    encoding: str = "utf-8"
    mode: Literal["a", "w"] = "a"


# ---------------------------
# EXPORTER CONFIG (for lazy instantiation)
# ---------------------------
class ExporterConfig(BaseModel):
    """
    Configuration for lazy exporter instantiation.
    Required for multiprocessing support since gRPC channels cannot be pickled.
    """

    exporter: Any  # Exporter class (e.g., OTLPLogExporter)
    args: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------
# PROCESSOR CONFIG
# ---------------------------
class LogProcessor(BaseModel):
    processor: Any
    config: Dict[str, Any] = Field(default_factory=dict)
    # Exporters can be either instances or ExporterConfig for lazy instantiation
    exporters: List[Any] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_exporters(self):
        if not isinstance(self.exporters, list):
            raise ValueError("exporters must be a list of exporter instances or ExporterConfig")
        return self


# ---------------------------
# OTLP HANDLER CONFIG
# ---------------------------


class OTLPLogHandler(BaseLogHandler):
    type: Literal["otlp"] = "otlp"
    resource: Dict[str, Any] = Field(default_factory=dict)
    processors: List[LogProcessor] = Field(default_factory=list)

    @model_validator(mode="after")
    def confirm_processors(self):
        if not self.processors:
            raise ValueError("OTLPHandlerConfig requires at least one processor.")
        return self


class RotatingFileLogHandler(BaseLogHandler):
    type: Literal["rotating_file"] = "rotating_file"
    filename: str
    max_bytes: int = 10_000_000
    backup_count: int = 5
    callback: Optional[Callable[[Any, str], str]] = None


class TimedRotatingFileLogHandler(BaseLogHandler):
    type: Literal["timed_rotating_file"] = "timed_rotating_file"
    filename: str
    when: Literal["S", "M", "H", "D", "midnight"] = "midnight"
    interval: int = 1
    backupCount: int = 7
    callback: Optional[Callable[[Any, str], str]] = None


# ---------------------------
# Handlers union
# ---------------------------
LogHandlers = Union[
    ConsoleLogHandler,
    FileLogHandler,
    JSONLogHandler,
    OTLPLogHandler,
    RotatingFileLogHandler,
    TimedRotatingFileLogHandler,
]


# ---------------------------
# Main logging config object
# ---------------------------
class LoggingConfig(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    handlers: List[LogHandlers] = Field(default_factory=list)
    filters: List[LogFilter] = Field(default_factory=list)
    formatters: List[LogFormatter] = Field(default_factory=list)


# ============================================================
# Handler builders
# ============================================================


def _default_formatter_dict():
    return {
        "format": "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        "datefmt": "%Y-%m-%d %H:%M:%S",
    }


def _build_console_handler_dict(cfg: ConsoleLogHandler, *args, **kwargs):
    return {
        "class": "logging.StreamHandler",
        "level": cfg.level,
        "formatter": cfg.formatter,
        "stream": "ext://sys.stdout" if cfg.stream == "stdout" else "ext://sys.stderr",
        "filters": cfg.filters,
    }


def _resolve_filename(cfg, *args, **kwargs):
    template = cfg.filename
    task = kwargs["task"]
    metadata = kwargs["metadata"]
    task_name = metadata["task_name"]
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    final = template.format(
        pid=metadata["pid"],
        task=task_name,
        timestamp=timestamp,
    )

    if getattr(cfg, "callback", None):
        return cfg.callback(task=task, filename=final)

    return final


def _build_file_handler_dict(cfg: FileLogHandler, *args, **kwargs):
    filename = _resolve_filename(cfg, *args, **kwargs)
    Path(filename).parent.mkdir(parents=True, exist_ok=True)

    return {
        "class": "logging.FileHandler",
        "level": cfg.level,
        "formatter": cfg.formatter,
        "filename": filename,
        "mode": cfg.mode,
        "encoding": "utf-8",
        "filters": cfg.filters,
    }


def _build_rotating_handler_dict(cfg: RotatingFileLogHandler, *args, **kwargs):
    filename = _resolve_filename(cfg, *args, **kwargs)
    Path(filename).parent.mkdir(parents=True, exist_ok=True)

    return {
        "class": "logging.handlers.RotatingFileHandler",
        "level": cfg.level,
        "formatter": cfg.formatter,
        "filename": filename,
        "maxBytes": cfg.max_bytes,
        "backupCount": cfg.backup_count,
        "encoding": "utf-8",
        "filters": cfg.filters,
    }


def _build_timed_rotating_handler_dict(cfg: TimedRotatingFileLogHandler, *args, **kwargs):
    filename = _resolve_filename(cfg, *args, **kwargs)
    Path(filename).parent.mkdir(parents=True, exist_ok=True)

    return {
        "class": "logging.handlers.TimedRotatingFileHandler",
        "level": cfg.level,
        "formatter": cfg.formatter,
        "filename": filename,
        "when": cfg.when,
        "interval": cfg.interval,
        "backupCount": cfg.backupCount,
        "encoding": "utf-8",
        "filters": cfg.filters,
    }


def _build_json_handler_dict(cfg: JSONLogHandler, *args, **kwargs):
    if cfg.filename:
        filename = _resolve_filename(cfg, *args, **kwargs)
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        handler_class = "logging.FileHandler"
        handler_args = {"filename": filename, **cfg.model_dump(exclude={"filename", "type", "formatter"})}
    else:
        handler_class = "logging.StreamHandler"
        handler_args = {"stream": "ext://sys.stdout"}

    return {
        "class": handler_class,
        "level": cfg.level,
        "formatter": cfg.formatter,
        "filters": cfg.filters,
        **handler_args,
    }


# ---------------------------
# REAL OTel logging pipeline
# ---------------------------


def _build_otlp_handler_dict(cfg: OTLPLogHandler, *args, **kwargs):
    """Build an OTLP log handler dictionary."""

    metadata = kwargs["metadata"]

    cfg.resource = _inject_otel_resource_attributes(resource=cfg.resource, metadata=metadata)

    resource = Resource(attributes=cfg.resource)
    provider = LoggerProvider(resource=resource)

    for p in cfg.processors:
        for exporter in p.exporters:
            # Handle lazy configuration for multiprocessing support
            if isinstance(exporter, ExporterConfig):
                exporter_instance = exporter.exporter(**exporter.args)
            else:
                exporter_instance = exporter

            provider.add_log_record_processor(p.processor(**p.config, exporter=exporter_instance))

    set_logger_provider(provider)

    return {
        "class": "opentelemetry.sdk._logs.LoggingHandler",
        "level": cfg.level,
        "formatter": cfg.formatter,
        "filters": cfg.filters,
    }


# Registry
_LOG_HANDLER_BUILDERS_DICT = {
    "console": lambda cfg, *args, **kwargs: _build_console_handler_dict(cfg, *args, **kwargs),
    "file": lambda cfg, *args, **kwargs: _build_file_handler_dict(cfg, *args, **kwargs),
    "json": lambda cfg, *args, **kwargs: _build_json_handler_dict(cfg, *args, **kwargs),
    "otlp": lambda cfg, *args, **kwargs: _build_otlp_handler_dict(cfg, *args, **kwargs),
    "rotating_file": lambda cfg, *args, **kwargs: _build_rotating_handler_dict(cfg, *args, **kwargs),
    "timed_rotating_file": lambda cfg, *args, **kwargs: _build_timed_rotating_handler_dict(cfg, *args, **kwargs),
}

# ============================================================
# Apply entire LoggingConfig to dictConfig
# ============================================================

_LOGGING_CONFIGURED = False


def _apply_logging_config(cfg: LoggingConfig, task: object, metadata: dict):
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return

    formatters = {f.name: {"format": f.fmt, "datefmt": f.datefmt} for f in cfg.formatters}
    formatters["default"] = _default_formatter_dict()
    formatters["json"] = {
        "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
        "fmt": "%(asctime)s %(levelname)s %(name)s %(message)s",
    }
    formatters["default"] = _default_formatter_dict()

    # Build filters dictionary:
    filters = {f.name: {"()": f.filter, **f.config} for f in cfg.filters}

    handlers_dict = {}
    handlers_order = []

    for idx, handler_cfg in enumerate(cfg.handlers):
        builder = _LOG_HANDLER_BUILDERS_DICT[handler_cfg.type]
        handler_dict = builder(cfg=handler_cfg, task=task, metadata=metadata)

        handler_name = f"handler_{idx}"
        handlers_dict[handler_name] = handler_dict
        handlers_order.append(handler_name)

    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": formatters,
        "filters": filters,
        "handlers": handlers_dict,
        "root": {"level": cfg.level, "handlers": handlers_order},
    }

    logging.config.dictConfig(config)
    _LOGGING_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


__all__ = [
    "OTLPLogExporter",
    "BatchLogRecordProcessor",
    "ConsoleLogExporter",
    "SimpleLogRecordProcessor",
    "ExporterConfig",
    "set_logger_provider",
    "LoggerProvider",
]
