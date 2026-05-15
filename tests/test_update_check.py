from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

from eufy_sync.cli import _check_for_updates, UPDATE_CHECK_INTERVAL


def _mock_pypi_response(version: str) -> MagicMock:
    """Create a mock urllib response returning the given version."""
    body = json.dumps({"info": {"version": version}}).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_skips_when_cache_is_fresh(tmp_path: Path):
    cache_file = tmp_path / "update_check"
    cache_file.write_text(str(time.time()))  # just checked

    with patch("eufy_sync.cli.DATA_DIR", tmp_path), \
         patch("eufy_sync.cli.urllib.request.urlopen") as mock_urlopen:

        _check_for_updates()

        mock_urlopen.assert_not_called()


def test_checks_pypi_when_cache_stale(tmp_path: Path, capsys):
    cache_file = tmp_path / "update_check"
    cache_file.write_text(str(time.time() - UPDATE_CHECK_INTERVAL - 1))

    with patch("eufy_sync.cli.DATA_DIR", tmp_path), \
         patch("eufy_sync.cli.urllib.request.urlopen", return_value=_mock_pypi_response("99.0.0")), \
         patch("eufy_sync.cli.sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = True

        _check_for_updates()

    output = capsys.readouterr().out
    assert "Update available" in output
    assert "99.0.0" in output


def test_no_message_when_up_to_date(tmp_path: Path, capsys):
    cache_file = tmp_path / "update_check"
    cache_file.write_text(str(time.time() - UPDATE_CHECK_INTERVAL - 1))

    from eufy_sync import __version__

    with patch("eufy_sync.cli.DATA_DIR", tmp_path), \
         patch("eufy_sync.cli.urllib.request.urlopen", return_value=_mock_pypi_response(__version__)):

        _check_for_updates()

    assert capsys.readouterr().out == ""


def test_no_message_when_local_is_newer(tmp_path: Path, capsys):
    cache_file = tmp_path / "update_check"
    cache_file.write_text(str(time.time() - UPDATE_CHECK_INTERVAL - 1))

    with patch("eufy_sync.cli.DATA_DIR", tmp_path), \
         patch("eufy_sync.cli.urllib.request.urlopen", return_value=_mock_pypi_response("0.0.1")):

        _check_for_updates()

    assert capsys.readouterr().out == ""


def test_silent_on_network_error(tmp_path: Path, capsys):
    cache_file = tmp_path / "update_check"
    cache_file.write_text(str(time.time() - UPDATE_CHECK_INTERVAL - 1))

    with patch("eufy_sync.cli.DATA_DIR", tmp_path), \
         patch("eufy_sync.cli.urllib.request.urlopen", side_effect=OSError("no network")):

        _check_for_updates()

    assert capsys.readouterr().out == ""


def test_cache_written_after_successful_check(tmp_path: Path):
    # No cache file initially
    from eufy_sync import __version__

    with patch("eufy_sync.cli.DATA_DIR", tmp_path), \
         patch("eufy_sync.cli.urllib.request.urlopen", return_value=_mock_pypi_response(__version__)):

        _check_for_updates()

    cache_file = tmp_path / "update_check"
    assert cache_file.exists()
    last_check = float(cache_file.read_text().strip())
    assert time.time() - last_check < 5


def test_cache_not_written_on_network_error(tmp_path: Path):
    with patch("eufy_sync.cli.DATA_DIR", tmp_path), \
         patch("eufy_sync.cli.urllib.request.urlopen", side_effect=OSError("no network")):

        _check_for_updates()

    cache_file = tmp_path / "update_check"
    assert not cache_file.exists()


def test_suggests_pipx_when_available(tmp_path: Path, capsys):
    cache_file = tmp_path / "update_check"
    cache_file.write_text(str(time.time() - UPDATE_CHECK_INTERVAL - 1))

    with patch("eufy_sync.cli.DATA_DIR", tmp_path), \
         patch("eufy_sync.cli.urllib.request.urlopen", return_value=_mock_pypi_response("99.0.0")), \
         patch("eufy_sync.cli.shutil.which", return_value="/usr/local/bin/pipx"), \
         patch("eufy_sync.cli.sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = True

        _check_for_updates()

    assert "pipx install --force" in capsys.readouterr().out


def test_handles_pypi_prerelease_version(tmp_path: Path, capsys):
    """PyPI can return versions like '1.7.2rc1' or '1.7.2.dev0' - parser must not crash silently."""
    cache_file = tmp_path / "update_check"
    cache_file.write_text(str(time.time() - UPDATE_CHECK_INTERVAL - 1))

    with patch("eufy_sync.cli.DATA_DIR", tmp_path), \
         patch("eufy_sync.cli.urllib.request.urlopen", return_value=_mock_pypi_response("99.0.0rc1")), \
         patch("eufy_sync.cli.sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = True

        _check_for_updates()

    output = capsys.readouterr().out
    assert "Update available" in output
    assert "99.0.0rc1" in output

    # Cache should be written - we successfully evaluated the version
    assert cache_file.exists()


def test_suggests_pip_when_no_pipx(tmp_path: Path, capsys):
    cache_file = tmp_path / "update_check"
    cache_file.write_text(str(time.time() - UPDATE_CHECK_INTERVAL - 1))

    with patch("eufy_sync.cli.DATA_DIR", tmp_path), \
         patch("eufy_sync.cli.urllib.request.urlopen", return_value=_mock_pypi_response("99.0.0")), \
         patch("eufy_sync.cli.shutil.which", return_value=None), \
         patch("eufy_sync.cli.sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = True

        _check_for_updates()

    assert "pip install --upgrade" in capsys.readouterr().out
