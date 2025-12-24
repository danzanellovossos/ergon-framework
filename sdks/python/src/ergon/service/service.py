from pydantic import BaseModel
from typing import Any


class ServiceConfig(BaseModel):
    """
    Generic service configuration.

    service: class implementing a service
    args: positional arguments passed to service's __init__
    kwargs: keyword arguments passed to service's __init__
    """

    service: object
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = {}
