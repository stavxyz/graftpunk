"""The unit suite's guard against starting a real Chrome (#351).

Each refusal here is made on purpose, so each test empties the guard's record
before teardown, where a non-empty record fails the test.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REFUSED = pytest.fail.Exception


@pytest.mark.parametrize(
    ("args", "kwargs"),
    [
        (["/nonexistent/Google Chrome.app/Contents/MacOS/Google Chrome", "--headless"], {}),
        (["/nonexistent/chromedriver", "--port=0"], {}),
        ("/nonexistent/chromium-browser --headless", {"shell": True}),
        (["anything"], {"executable": "/nonexistent/google-chrome"}),
        (["/nonexistent/chromium", "--headless"], {"shell": True}),
    ],
)
def test_popen_of_a_browser_executable_is_refused(
    args: object, kwargs: dict[str, object], _no_real_browser: list[str]
) -> None:
    with pytest.raises(_REFUSED, match="tried to start a real browser"):
        subprocess.Popen(args, **kwargs)  # noqa: S603
    assert len(_no_real_browser) == 1
    _no_real_browser.clear()


def test_popen_of_another_executable_runs(_no_real_browser: list[str]) -> None:
    result = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, check=True)  # noqa: S603
    assert result.stdout.strip() == b"ok"
    assert _no_real_browser == []


def test_the_refusal_is_not_caught_by_except_exception(_no_real_browser: list[str]) -> None:
    caught = False
    with pytest.raises(_REFUSED):
        try:
            subprocess.Popen(["/nonexistent/chrome"])  # noqa: S603
        except Exception:  # noqa: BLE001 - the point: production code catches this broadly
            caught = True
    assert caught is False
    _no_real_browser.clear()


def test_a_fake_browser_under_the_tests_own_tmp_path_runs(
    tmp_path: Path, _no_real_browser: list[str]
) -> None:
    fake = tmp_path / "chromedriver"
    fake.write_text("#!/bin/sh\necho fake\n")
    fake.chmod(0o755)

    result = subprocess.run([str(fake)], capture_output=True, check=True)  # noqa: S603

    assert result.stdout.strip() == b"fake"
    assert _no_real_browser == []


def _write_fake(directory: Path, name: str) -> Path:
    fake = directory / name
    fake.write_text("#!/bin/sh\necho fake\n")
    fake.chmod(0o755)
    return fake


def test_a_fake_run_relative_to_the_calls_own_cwd_runs(
    tmp_path: Path, _no_real_browser: list[str]
) -> None:
    _write_fake(tmp_path, "chrome")

    result = subprocess.run(["./chrome"], cwd=tmp_path, capture_output=True, check=True)  # noqa: S603

    assert result.stdout.strip() == b"fake"
    assert _no_real_browser == []


def test_a_fake_found_on_the_calls_own_path_runs(
    tmp_path: Path, _no_real_browser: list[str]
) -> None:
    _write_fake(tmp_path, "chromedriver")

    result = subprocess.run(  # noqa: S603
        ["chromedriver"],  # noqa: S607
        env={"PATH": str(tmp_path)},
        capture_output=True,
        check=True,
    )

    assert result.stdout.strip() == b"fake"
    assert _no_real_browser == []


def test_a_fake_found_on_the_process_path_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_real_browser: list[str]
) -> None:
    _write_fake(tmp_path, "chromedriver")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])

    result = subprocess.run(["chromedriver"], capture_output=True, check=True)  # noqa: S603, S607

    assert result.stdout.strip() == b"fake"
    assert _no_real_browser == []


def test_a_chrome_outside_tmp_path_is_refused_however_it_is_named(
    tmp_path_factory: pytest.TempPathFactory, _no_real_browser: list[str]
) -> None:
    outside = _executable_outside_tmp_path(tmp_path_factory, "chrome").parent
    with pytest.raises(_REFUSED):
        subprocess.Popen(["./chrome"], cwd=outside)  # noqa: S603
    with pytest.raises(_REFUSED):
        subprocess.Popen(["chrome"], env={"PATH": str(outside)})  # noqa: S603, S607
    assert len(_no_real_browser) == 2
    _no_real_browser.clear()


@pytest.mark.parametrize("name", ["posix_spawn", "posix_spawnp"])
def test_posix_spawn_of_a_browser_is_refused(name: str, _no_real_browser: list[str]) -> None:
    spawn = getattr(os, name, None)
    if spawn is None:
        pytest.skip(f"os.{name} is not available here")
    with pytest.raises(_REFUSED, match=f"os.{name}"):
        spawn("/nonexistent/chromedriver", ["chromedriver"], os.environ)
    _no_real_browser.clear()


def test_posix_spawn_of_another_executable_runs(_no_real_browser: list[str]) -> None:
    if not hasattr(os, "posix_spawn"):
        pytest.skip("os.posix_spawn is not available here")
    pid = os.posix_spawn(sys.executable, [sys.executable, "-c", "pass"], os.environ)
    _, status = os.waitpid(pid, 0)
    assert status == 0
    assert _no_real_browser == []


def test_os_system_of_a_browser_is_refused(_no_real_browser: list[str]) -> None:
    with pytest.raises(_REFUSED, match="os.system"):
        os.system("/nonexistent/chromedriver --version")  # noqa: S605
    _no_real_browser.clear()


def test_os_system_of_another_command_runs(_no_real_browser: list[str]) -> None:
    assert os.system("exit 0") == 0  # noqa: S605, S607
    assert _no_real_browser == []


def test_a_shell_command_naming_a_browser_only_as_an_argument_runs(
    _no_real_browser: list[str],
) -> None:
    as_string = subprocess.run("echo chromedriver", shell=True, capture_output=True, check=True)  # noqa: S602, S607
    as_list = subprocess.run(  # noqa: S602
        ["echo $0", "chromedriver"],  # noqa: S607
        shell=True,
        capture_output=True,
        check=True,
    )
    assert as_string.stdout.strip() == b"chromedriver"
    assert as_list.stdout.strip() == b"chromedriver"
    assert _no_real_browser == []


def test_undetected_chromedriver_detached_launch_is_refused(_no_real_browser: list[str]) -> None:
    import undetected_chromedriver as uc

    with pytest.raises(_REFUSED, match="undetected_chromedriver.start_detached"):
        uc.start_detached("/nonexistent/chrome", "--headless")
    _no_real_browser.clear()


def _executable_outside_tmp_path(tmp_path_factory: pytest.TempPathFactory, name: str) -> Path:
    """An executable named like a browser that the guard does not treat as the
    test's own fake: it is outside the test's tmp_path. Never run: refused."""
    path = tmp_path_factory.mktemp("bin") / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def test_selenium_starting_chromedriver_is_refused(
    tmp_path_factory: pytest.TempPathFactory, _no_real_browser: list[str]
) -> None:
    from selenium.webdriver.chrome.service import Service

    chromedriver = _executable_outside_tmp_path(tmp_path_factory, "chromedriver")
    service = Service(executable_path=str(chromedriver))
    with pytest.raises(_REFUSED, match="chromedriver"):
        service.start()
    _no_real_browser.clear()


async def test_asyncio_starting_chrome_is_refused(
    tmp_path_factory: pytest.TempPathFactory, _no_real_browser: list[str]
) -> None:
    """nodriver starts Chrome with asyncio.create_subprocess_exec."""
    import asyncio

    chrome = _executable_outside_tmp_path(tmp_path_factory, "chrome")
    with pytest.raises(_REFUSED, match="chrome"):
        await asyncio.create_subprocess_exec(str(chrome), "--headless")
    _no_real_browser.clear()


@pytest.mark.real_browser
def test_the_marker_exempts_a_test(_no_real_browser: list[str]) -> None:
    import undetected_chromedriver as uc

    assert uc.start_detached.__module__ == "undetected_chromedriver.dprocess"
    assert subprocess.Popen.__module__ == "subprocess"
    assert subprocess.Popen.__name__ == "Popen"
