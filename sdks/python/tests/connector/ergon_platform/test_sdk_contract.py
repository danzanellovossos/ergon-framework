"""Contract with the installed Ergon Platform SDK package."""

from importlib.metadata import version

from ergon_platform.resources.channels.configs.client import Configs


def test_installed_sdk_supports_channels_claim_and_settlement() -> None:
    assert tuple(int(part) for part in version("ergon-platform-sdk").split(".")[:2]) >= (0, 2)
    for method in (
        "activity_claim",
        "activity_ack",
        "activity_nack",
        "activity_attachment_file",
    ):
        assert hasattr(Configs, method), f"installed Configs is missing {method}"
