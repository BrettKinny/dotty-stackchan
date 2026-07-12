"""Regression guards for the documented StackChan provisioning path."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ONBOARDING_FILES = (
    ROOT / "docs" / "quickstart.md",
    ROOT / "SETUP.md",
    ROOT / "docs" / "troubleshooting.md",
    ROOT / "Makefile",
)


def test_docs_do_not_claim_ota_url_is_in_on_device_advanced_options() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in ONBOARDING_FILES)
    stale_directions = (
        "device's Advanced Options",
        "robot's Advanced Options",
        "Settings > Advanced Options",
    )
    for stale in stale_directions:
        assert stale not in combined


def test_docs_identify_compiled_ota_url_and_missing_device_editor() -> None:
    quickstart = (ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8")
    assert "CONFIG_OTA_URL" in quickstart
    assert "does **not** contain an Advanced" in quickstart
    assert "fw-v1.3.3" in quickstart
