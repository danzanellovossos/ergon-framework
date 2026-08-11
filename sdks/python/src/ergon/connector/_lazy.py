"""Helpers for loading connector implementations only when requested."""

from importlib import import_module
from typing import Any, Mapping, MutableMapping, Sequence


def load_export(
    *,
    name: str,
    package: str,
    exports: Mapping[str, str],
    namespace: MutableMapping[str, Any],
) -> Any:
    """Load and cache one lazily exported attribute."""
    module_name = exports.get(name)
    if module_name is None:
        raise AttributeError(f"module {package!r} has no attribute {name!r}")

    module = import_module(f".{module_name}", package)
    value = getattr(module, name)
    namespace[name] = value
    return value


def load_optional_export(
    *,
    name: str,
    package: str,
    exports: Mapping[str, str],
    namespace: MutableMapping[str, Any],
    extra: str,
    dependencies: Sequence[str],
) -> Any:
    """Load a connector export and explain how to install a missing extra."""
    try:
        return load_export(name=name, package=package, exports=exports, namespace=namespace)
    except ModuleNotFoundError as exc:
        missing_module = exc.name or ""
        if not any(
            missing_module == dependency or missing_module.startswith(f"{dependency}.") for dependency in dependencies
        ):
            raise
        raise ImportError(
            f"The {extra!r} connector dependencies are not installed. "
            f"Install them with: pip install 'ergon-framework-python[{extra}]'"
        ) from exc


def exported_names(namespace: Mapping[str, Any], exports: Mapping[str, str]) -> list[str]:
    """Return module names including lazy exports for interactive discovery."""
    return sorted(set(namespace) | set(exports))
