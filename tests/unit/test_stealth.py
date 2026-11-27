"""The stealth driver runs the chromedriver webdriver-manager resolves (#356).

undetected-chromedriver's own download is the mac-x64 build on every Mac (its
Patcher has no arm64 branch), which runs under Rosetta on Apple Silicon and
starts Chrome translated too. webdriver-manager resolves the host's
architecture, so that is where the driver must come from. Patching rewrites
bytes inside that signed binary, and macOS kills an arm64 executable whose
signature no longer matches, so on macOS the patched driver is signed again
before Chrome starts.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import selenium.webdriver

from graftpunk.exceptions import ChromeDriverError
from graftpunk.stealth import create_stealth_driver

# The block undetected-chromedriver's Patcher looks for and overwrites.
_CDC_BLOCK = b"\x00{window.cdc_adoQpoasnfa76pfcZLmcfl_Array;}\x00"
_PATCHED_MARKER = b"undetected chromedriver"


def _started_chrome() -> MagicMock:
    # selenium-stealth refuses anything that is not a selenium Chrome driver.
    return MagicMock(spec=selenium.webdriver.Chrome)


@pytest.fixture()
def wdm_driver(tmp_path: Path) -> Path:
    """An unpatched chromedriver as webdriver-manager would have cached it."""
    driver = tmp_path / "wdm" / "chromedriver-mac-arm64" / "chromedriver"
    driver.parent.mkdir(parents=True)
    driver.write_bytes(_CDC_BLOCK)
    driver.chmod(0o755)
    return driver


@pytest.fixture()
def codesign_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `codesign` on PATH that records its arguments instead of signing."""
    log = tmp_path / "codesign.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "codesign"
    fake.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    return log


@patch("graftpunk.stealth.uc.Chrome")
@patch("webdriver_manager.chrome.ChromeDriverManager")
@patch("graftpunk.stealth.get_chrome_version")
def test_stealth_driver_runs_the_chromedriver_webdriver_manager_resolved(
    mock_version: MagicMock,
    mock_manager: MagicMock,
    mock_chrome: MagicMock,
    tmp_path: Path,
    wdm_driver: Path,
    codesign_log: Path,
) -> None:
    mock_version.return_value = "156"
    mock_manager.return_value.install.return_value = str(wdm_driver)
    mock_chrome.return_value = _started_chrome()

    create_stealth_driver(headless=True, profile_dir=tmp_path / "profile")

    assert mock_manager.call_args.kwargs == {"driver_version": "156"}
    started = mock_chrome.call_args.kwargs
    assert started.get("driver_executable_path") == str(wdm_driver)
    assert started["version_main"] == 156


@patch("graftpunk.stealth.uc.Chrome")
@patch("webdriver_manager.chrome.ChromeDriverManager")
@patch("graftpunk.stealth.get_chrome_version")
def test_stealth_driver_lets_webdriver_manager_detect_chrome_when_detection_fails(
    mock_version: MagicMock,
    mock_manager: MagicMock,
    mock_chrome: MagicMock,
    tmp_path: Path,
    wdm_driver: Path,
    codesign_log: Path,
) -> None:
    mock_version.side_effect = ChromeDriverError("Chrome not found")
    mock_manager.return_value.install.return_value = str(wdm_driver)
    mock_chrome.return_value = _started_chrome()

    create_stealth_driver(headless=True, profile_dir=tmp_path / "profile")

    assert mock_manager.call_args.kwargs == {"driver_version": None}
    started = mock_chrome.call_args.kwargs
    assert started.get("driver_executable_path") == str(wdm_driver)
    assert started["version_main"] is None


@patch("graftpunk.stealth.uc.Chrome")
@patch("webdriver_manager.chrome.ChromeDriverManager")
@patch("graftpunk.stealth.get_chrome_version")
def test_on_macos_the_driver_is_patched_and_signed_again_before_chrome_starts(
    mock_version: MagicMock,
    mock_manager: MagicMock,
    mock_chrome: MagicMock,
    tmp_path: Path,
    wdm_driver: Path,
    codesign_log: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("graftpunk.stealth.platform_mod.system", lambda: "Darwin")
    mock_version.return_value = "156"
    mock_manager.return_value.install.return_value = str(wdm_driver)
    at_chrome_start: list[tuple[bytes, str]] = []

    def start_chrome(**kwargs: object) -> MagicMock:
        signed = codesign_log.read_text() if codesign_log.exists() else ""
        at_chrome_start.append((wdm_driver.read_bytes(), signed))
        return _started_chrome()

    mock_chrome.side_effect = start_chrome

    create_stealth_driver(headless=True, profile_dir=tmp_path / "profile")

    [(driver_bytes, signed)] = at_chrome_start
    assert _PATCHED_MARKER in driver_bytes
    assert signed.split() == ["--force", "--sign", "-", str(wdm_driver)]


@patch("graftpunk.stealth.uc.Chrome")
@patch("webdriver_manager.chrome.ChromeDriverManager")
@patch("graftpunk.stealth.get_chrome_version")
def test_on_macos_a_missing_codesign_is_a_chromedriver_error_before_chrome_starts(
    mock_version: MagicMock,
    mock_manager: MagicMock,
    mock_chrome: MagicMock,
    tmp_path: Path,
    wdm_driver: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("graftpunk.stealth.platform_mod.system", lambda: "Darwin")
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    mock_version.return_value = "156"
    mock_manager.return_value.install.return_value = str(wdm_driver)
    mock_chrome.return_value = _started_chrome()

    with pytest.raises(ChromeDriverError, match="codesign"):
        create_stealth_driver(headless=True, profile_dir=tmp_path / "profile")

    assert mock_chrome.call_args is None


@patch("graftpunk.stealth.uc.Chrome")
@patch("webdriver_manager.chrome.ChromeDriverManager")
@patch("graftpunk.stealth.get_chrome_version")
def test_elsewhere_the_driver_is_patched_but_not_signed(
    mock_version: MagicMock,
    mock_manager: MagicMock,
    mock_chrome: MagicMock,
    tmp_path: Path,
    wdm_driver: Path,
    codesign_log: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("graftpunk.stealth.platform_mod.system", lambda: "Linux")
    mock_version.return_value = "156"
    mock_manager.return_value.install.return_value = str(wdm_driver)
    mock_chrome.return_value = _started_chrome()

    create_stealth_driver(headless=True, profile_dir=tmp_path / "profile")

    assert _PATCHED_MARKER in wdm_driver.read_bytes()
    assert not codesign_log.exists()


# ---------------------------------------------------------------------------
# The ChromeDriver asked for is the installed Chrome's full version.
# webdriver-manager 4.x picks the last known-good ChromeDriver whose version
# string contains the first three parts of the one it is given, so a bare "158"
# matched "160.0.8158.0", a ChromeDriver Chrome 158 refuses.
# ---------------------------------------------------------------------------


def _versions(full: str | None, major: str | None):
    def read(major: bool = True) -> str:
        value = major_value if major else full
        if value is None:
            raise ChromeDriverError("Chrome not found")
        return value

    major_value = major
    return read


@pytest.mark.parametrize(
    ("full", "major", "driver_version", "version_main"),
    [
        ("158.0.8127.37", "158", "158.0.8127.37", 158),
        (None, "158", "158", 158),
        (None, None, None, None),
    ],
    ids=["full", "major-only", "none"],
)
@patch("graftpunk.stealth.uc.Chrome")
@patch("webdriver_manager.chrome.ChromeDriverManager")
def test_the_stealth_driver_asks_webdriver_manager_for_chromes_full_version(
    mock_manager: MagicMock,
    mock_chrome: MagicMock,
    full: str | None,
    major: str | None,
    driver_version: str | None,
    version_main: int | None,
    tmp_path: Path,
    wdm_driver: Path,
    codesign_log: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("graftpunk.stealth.get_chrome_version", _versions(full, major))
    mock_manager.return_value.install.return_value = str(wdm_driver)
    mock_chrome.return_value = _started_chrome()

    create_stealth_driver(headless=True, profile_dir=tmp_path / "profile")

    assert mock_manager.call_args.kwargs == {"driver_version": driver_version}
    assert mock_chrome.call_args.kwargs["version_main"] == version_main


@pytest.mark.parametrize(
    ("full", "major", "driver_version"),
    [("158.0.8127.37", "158", "158.0.8127.37"), (None, "158", "158")],
    ids=["full", "major-only"],
)
def test_the_standard_selenium_driver_asks_for_chromes_full_version(
    full: str | None, major: str | None, driver_version: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graftpunk.backends.selenium import SeleniumBackend

    asked: list[str | None] = []

    def manager(driver_version: str | None) -> MagicMock:
        asked.append(driver_version)
        made = MagicMock()
        made.install.return_value = "/path/to/chromedriver"
        return made

    monkeypatch.setattr("graftpunk.backends.selenium.get_chrome_version", _versions(full, major))
    monkeypatch.setattr(
        "graftpunk.backends.selenium.webdriver_manager.chrome.ChromeDriverManager", manager
    )
    with patch("selenium.webdriver.Chrome", return_value=MagicMock()):
        SeleniumBackend(use_stealth=False, headless=True).start()

    assert asked == [driver_version]
