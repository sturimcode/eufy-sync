"""The Windows implementation: the hidden-window VBScript wrapper, the
schtasks lifecycle, and the --doctor status check.

Every subprocess and shutil.which call is patched, and shared.DATA_DIR /
LOG_FILE are pointed at a tmp_path, so the suite runs on macOS without
touching a real Task Scheduler.
"""
from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from eufy_sync import platform_support
from eufy_sync.cli import shared
from eufy_sync.platform_support import windows


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the shared paths at a throwaway directory for the test."""
    monkeypatch.setattr(shared, "DATA_DIR", tmp_path)
    monkeypatch.setattr(shared, "LOG_FILE", tmp_path / "sync.log")
    return tmp_path


def _ok():
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def _write_current_wrapper(data_dir, binary):
    """Write the wrapper exactly as the module would, preserving CRLF bytes."""
    (data_dir / windows.WRAPPER_NAME).write_bytes(windows._wrapper_content(binary).encode("utf-8"))


def test_select_picks_windows_on_windows():
    with patch("eufy_sync.platform_support.platform.system", return_value="Windows"):
        assert platform_support._select() is windows


def test_install_invokes_schtasks_with_expected_arguments(data_dir):
    binary = "C:\\Tools\\eufy-sync.exe"
    with patch.object(windows.shutil, "which", return_value=binary), \
         patch.object(windows.subprocess, "run", return_value=_ok()) as run:
        windows.install_agent()

    wrapper_path = data_dir / windows.WRAPPER_NAME
    run.assert_called_once_with(
        ["schtasks", "/Create", "/F", "/TN", "eufy-sync", "/SC", "HOURLY", "/MO", "4",
         "/TR", f'wscript.exe "{wrapper_path}"'],
        capture_output=True, text=True, timeout=15,
    )


def test_install_writes_wrapper_with_doubled_quote_escaping(data_dir):
    binary = "C:\\Tools\\eufy-sync.exe"
    with patch.object(windows.shutil, "which", return_value=binary), \
         patch.object(windows.subprocess, "run", return_value=_ok()):
        windows.install_agent()

    # Read as raw bytes so the CRLF line endings are not translated away.
    content = (data_dir / windows.WRAPPER_NAME).read_bytes().decode("utf-8")

    # Full fidelity against the module's own generator.
    assert content == windows._wrapper_content(binary)

    # VBScript escapes a literal quote by doubling it. The original inner command
    # opens with `cmd /c ""` and wraps the binary path in a quote, so after
    # doubling every quote the leading marker becomes four quotes and the path is
    # bracketed by four quotes on the left and two on the right. A wrong quote
    # level here silently breaks the hidden-window run, so pin the exact bytes.
    assert content.startswith('Set shell = CreateObject("WScript.Shell")\r\n')
    assert 'shell.Run "cmd /c """"' in content
    assert f'""""{binary}""' in content
    assert content.endswith(', 0, False\r\n')


def test_unchanged_wrapper_is_not_rewritten(data_dir):
    binary = "C:\\Tools\\eufy-sync.exe"
    _write_current_wrapper(data_dir, binary)

    with patch.object(windows.shutil, "which", return_value=binary), \
         patch.object(windows.subprocess, "run", return_value=_ok()), \
         patch.object(Path, "write_bytes", autospec=True) as write_bytes:
        windows.install_agent()

    # The wrapper already holds the current content, so its bytes stay put.
    write_bytes.assert_not_called()


def test_install_without_binary_warns_and_skips(data_dir, capsys):
    with patch.object(windows.shutil, "which", return_value=None), \
         patch.object(windows.subprocess, "run") as run:
        windows.install_agent()

    run.assert_not_called()
    assert "could not find eufy-sync on PATH" in capsys.readouterr().out


def test_install_failure_prints_stderr(data_dir, capsys):
    with patch.object(windows.shutil, "which", return_value="C:\\Tools\\eufy-sync.exe"), \
         patch.object(windows.subprocess, "run",
                      return_value=SimpleNamespace(returncode=1, stdout="", stderr="Access is denied.")):
        windows.install_agent()

    assert "Access is denied." in capsys.readouterr().out


def test_uninstall_deletes_task_and_removes_wrapper(data_dir):
    wrapper_path = data_dir / windows.WRAPPER_NAME
    wrapper_path.write_text("stale")

    with patch.object(windows.subprocess, "run", return_value=_ok()) as run:
        windows.uninstall_agent()

    run.assert_called_once_with(
        ["schtasks", "/Delete", "/F", "/TN", "eufy-sync"],
        capture_output=True, text=True, timeout=15,
    )
    assert not wrapper_path.exists()


def test_uninstall_without_task_reports_none_installed(data_dir, capsys):
    with patch.object(windows.subprocess, "run",
                      return_value=SimpleNamespace(returncode=1, stdout="", stderr="ERROR: cannot find")):
        windows.uninstall_agent()

    assert "No scheduled task installed." in capsys.readouterr().out


def test_agent_status_warns_when_wrapper_missing(data_dir):
    status = windows.agent_status()

    assert status["status"] == "WARN"
    assert status["label"] == "scheduled task"
    assert status["detail"] == "not installed"
    assert status["fix"] == "eufy-sync --install-agent"


def test_agent_status_warns_when_task_query_fails(data_dir):
    binary = "C:\\Tools\\eufy-sync.exe"
    _write_current_wrapper(data_dir, binary)

    with patch.object(windows.shutil, "which", return_value=binary), \
         patch.object(windows.subprocess, "run",
                      return_value=SimpleNamespace(returncode=1, stdout="", stderr="")):
        status = windows.agent_status()

    assert status["status"] == "WARN"
    assert status["detail"] == "not installed"


def test_agent_status_warns_when_wrapper_outdated(data_dir):
    (data_dir / windows.WRAPPER_NAME).write_text("stale wrapper contents")

    with patch.object(windows.shutil, "which", return_value="C:\\Tools\\eufy-sync.exe"), \
         patch.object(windows.subprocess, "run", return_value=_ok()):
        status = windows.agent_status()

    assert status["status"] == "WARN"
    assert status["detail"] == "outdated registration"
    assert status["fix"] == "eufy-sync --install-agent"


def test_agent_status_passes_when_installed_and_current(data_dir):
    binary = "C:\\Tools\\eufy-sync.exe"
    _write_current_wrapper(data_dir, binary)

    with patch.object(windows.shutil, "which", return_value=binary), \
         patch.object(windows.subprocess, "run", return_value=_ok()):
        status = windows.agent_status()

    assert status["status"] == "PASS"
    assert status["detail"] == "installed, runs every 4h"
    assert status["fix"] is None


def test_agent_installed_reflects_wrapper_presence(data_dir):
    assert windows.agent_installed() is False
    (data_dir / windows.WRAPPER_NAME).write_text("x")
    assert windows.agent_installed() is True


def test_purge_agent_removes_task_and_wrapper_silently(data_dir, capsys):
    wrapper_path = data_dir / windows.WRAPPER_NAME
    wrapper_path.write_text("x")

    with patch.object(windows.subprocess, "run", return_value=_ok()) as run:
        windows.purge_agent()

    run.assert_called_once_with(
        ["schtasks", "/Delete", "/F", "/TN", "eufy-sync"],
        capture_output=True,
    )
    assert not wrapper_path.exists()
    assert capsys.readouterr().out == ""


def test_purge_agent_noop_when_not_installed(data_dir):
    with patch.object(windows.subprocess, "run") as run:
        windows.purge_agent()

    run.assert_not_called()


def _decoded_script(run):
    """Pull the -EncodedCommand payload off a captured subprocess.run call and
    decode it back into the PowerShell script text."""
    argv = run.call_args.args[0]
    encoded = argv[argv.index("-EncodedCommand") + 1]
    return base64.b64decode(encoded).decode("utf-16-le")


def test_notify_invokes_powershell_with_encoded_command(data_dir):
    with patch.object(windows.subprocess, "run", return_value=_ok()) as run:
        assert windows.notify("Sync failed", "Reauth needed") is None

    run.assert_called_once()
    argv = run.call_args.args[0]
    assert argv[:2] == ["powershell", "-NoProfile"]
    assert "-EncodedCommand" in argv
    assert run.call_args.kwargs["capture_output"] is True
    assert run.call_args.kwargs["timeout"] == 10


def test_notify_encodes_xml_escaped_message(data_dir):
    with patch.object(windows.subprocess, "run", return_value=_ok()) as run:
        windows.notify("Title", "Tom & Jerry <fixed>")

    decoded = _decoded_script(run)
    # The ampersand and angle brackets are neutralized as XML entities, so no
    # raw special survives to break the toast XML.
    assert "&amp;" in decoded
    assert "&lt;fixed&gt;" in decoded
    assert "Tom & Jerry" not in decoded


def test_notify_message_with_specials_and_quotes_cannot_break_out(data_dir):
    # A payload packed with the characters that would end the here-string, close
    # a quote, or open an XML tag if any of them leaked through unescaped.
    nasty = "'@ \"; <script>alert(1)</script> & done"
    with patch.object(windows.subprocess, "run", return_value=_ok()) as run:
        windows.notify("t", nasty)

    decoded = _decoded_script(run)
    # The XML specials are entity-encoded, so no live tag or bare ampersand lands
    # in the text node.
    assert "<script>" not in decoded
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in decoded
    # The AppId GUID braces must survive .format() intact (they are doubled in
    # the template so format() emits a single literal brace on each side).
    assert "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}" in decoded


def test_notify_swallows_oserror(data_dir):
    with patch.object(windows.subprocess, "run", side_effect=OSError("powershell missing")):
        assert windows.notify("t", "m") is None


def test_notify_ignores_command_argument(data_dir):
    calls = []

    def record(argv, **kwargs):
        calls.append(argv)
        return _ok()

    with patch.object(windows.subprocess, "run", side_effect=record):
        windows.notify("t", "m")
        windows.notify("t", "m", command="eufy-sync --reauth garmin")

    # The fix command belongs in the message text on Windows; the invocation is
    # byte-for-byte identical whether or not command is passed.
    assert calls[0] == calls[1]


def test_offer_installs_on_yes(data_dir):
    with patch.object(windows.platform, "system", return_value="Windows"), \
         patch.object(windows.sys.stdin, "isatty", return_value=True), \
         patch("builtins.input", return_value="y"), \
         patch.object(windows, "_install_scheduled_task") as install:
        windows.offer_agent()

    install.assert_called_once()


def test_offer_skips_without_a_tty(data_dir):
    with patch.object(windows.platform, "system", return_value="Windows"), \
         patch.object(windows.sys.stdin, "isatty", return_value=False), \
         patch.object(windows, "_install_scheduled_task") as install:
        windows.offer_agent()

    install.assert_not_called()
