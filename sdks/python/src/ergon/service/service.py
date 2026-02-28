from typing import Any

from pydantic import BaseModel


class ServiceConfig(BaseModel):
    """
    Generic service configuration.

    service: class implementing a service
    args: positional arguments passed to service's __init__
    kwargs: keyword arguments passed to service's __init__
    """

    service: type
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = {}
