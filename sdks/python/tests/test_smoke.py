"""Smoke tests to verify the test infrastructure works."""

import ergon


def test_ergon_package_is_importable() -> None:
    assert ergon.__version__ == "0.1.0"
