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


def test_source_build_uses_the_pinned_dotty_firmware_fork() -> None:
    setup = (ROOT / "SETUP.md").read_text(encoding="utf-8")
    gitmodules = (ROOT / ".gitmodules").read_text(encoding="utf-8")

    assert "github.com/BrettKinny/StackChan.git" in gitmodules
    assert "git clone --recursive https://github.com/BrettKinny/dotty-stackchan.git" in setup
    assert "cd dotty-stackchan/firmware/firmware" in setup
    assert "git clone https://github.com/m5stack/StackChan.git" not in setup


def test_docs_only_recommend_real_firmware_kconfig_symbols() -> None:
    setup = (ROOT / "SETUP.md").read_text(encoding="utf-8")

    assert "CONFIG_OTA_URL" in setup
    assert "set `CONFIG_WIFI_SSID`" not in setup
    assert "set `CONFIG_WIFI_PASSWORD`" not in setup
    assert "does **not** define `CONFIG_WIFI_SSID`" in setup


def test_docs_explain_the_real_first_boot_path() -> None:
    setup = (ROOT / "SETUP.md").read_text(encoding="utf-8")
    quickstart = (ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8")

    assert "Skip" in setup
    assert "Welcome!" in setup
    assert "Xiaozhi" in setup and "hotspot" in setup.lower()
    assert "Skip" in quickstart


def test_current_docs_do_not_claim_the_firmware_submodule_lacks_state_manager() -> None:
    current_docs = (
        (ROOT / "README.md").read_text(encoding="utf-8")
        + (ROOT / "docs" / "modes.md").read_text(encoding="utf-8")
        + (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    )
    stale_claims = (
        "submodule pin in this repo lags",
        "submodule pin lags",
        "flashing from the submodule won't give you Phase 4",
        "does **not** include StateManager",
        "xiaozhi firmware (built from m5stack/StackChan source)",
    )
    for stale in stale_claims:
        assert stale not in current_docs


def test_compatibility_doc_matches_the_current_public_contract() -> None:
    compatibility = (ROOT / "COMPATIBILITY.md").read_text(encoding="utf-8")
    assert "`fw-v1.3.3`" in compatibility
    assert "the seven `dotty-pi-ext` voice tools" in compatibility
    assert "No formal versioning is adopted yet" not in compatibility
    assert "scripts/backup.sh" not in compatibility
