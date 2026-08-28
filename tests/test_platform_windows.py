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
    assert 'shell.Run("cmd /c """"' in content
    assert f'""""{binary}""' in content
    assert content.endswith(', 0, True)\r\n')


def test_wrapper_waits_and_exits_with_the_sync_exit_code(data_dir):
    """Task Scheduler reads LastTaskResult off wscript's exit code. A
    fire-and-forget Run hands back 0 the instant the sync starts, so every run
    records success whatever the sync did - which is how 24 days of failing
    runs went unnoticed on a real install. Run must wait, and its return value
    must become wscript's."""
    binary = "C:\\Tools\\eufy-sync.exe"
    content = windows._wrapper_content(binary)

    assert "WScript.Quit shell.Run(" in content
    # bWaitOnReturn True, window style still 0 so no console flashes.
    assert content.endswith(", 0, True)\r\n")
    assert ", 0, False" not in content


def test_wrapper_rotates_the_log_past_one_megabyte(data_dir):
    """The log is appended to on every run and nothing else trims it, so the
    wrapper rolls it over past 1 MB and keeps one previous generation."""
    binary = "C:\\Tools\\eufy-sync.exe"
    log = str(data_dir / "sync.log")
    content = windows._wrapper_content(binary)

    assert 'Set fso = CreateObject("Scripting.FileSystemObject")\r\n' in content
    assert f'If fso.FileExists("{log}") Then\r\n' in content
    assert f'If fso.GetFile("{log}").Size > 1048576 Then\r\n' in content
    # The previous generation is dropped before the move: MoveFile refuses to
    # overwrite an existing destination.
    assert f'If fso.FileExists("{log}.1") Then fso.DeleteFile "{log}.1"\r\n' in content
    assert f'fso.MoveFile "{log}", "{log}.1"\r\n' in content


def test_only_the_rotation_is_wrapped_in_resume_next(data_dir):
    """A rotation that fails (a stray lock, a full disk) must cost us the log
    roll and nothing else. The error guard covers the rotation block alone; the
    launch sits outside it and keeps reporting its own failures."""
    content = windows._wrapper_content("C:\\Tools\\eufy-sync.exe")

    assert content.count("On Error Resume Next") == 1
    before, rest = content.split("On Error Resume Next", 1)
    guarded, after = rest.split("On Error GoTo 0", 1)

    assert "fso.MoveFile" in guarded
    assert "WScript.Quit" not in guarded
    assert "WScript.Quit shell.Run(" in after


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

    out = capsys.readouterr().out
    assert "Access is denied." in out
    # The printed fallback command must quote the wrapper path inside the /TR
    # value with escaped inner quotes, matching what the code passes
    # programmatically, so a cmd.exe paste keeps a spaced path intact.
    wrapper_path = data_dir / windows.WRAPPER_NAME
    assert f'/TR "wscript.exe \\"{wrapper_path}\\""' in out


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


def test_notify_flattens_newlines_so_here_string_cannot_terminate_early(data_dir):
    # Raw exception text reaches notify (str(e)[:200] at the call sites), and a
    # PowerShell here-string ends on any line starting with '@. A message with a
    # newline followed by '@ would close the here-string and hand the rest to
    # PowerShell as code, so newlines must be flattened before embedding.
    with patch.object(windows.subprocess, "run", return_value=_ok()) as run:
        windows.notify("t", "line1\n'@\nWrite-Host pwned")

    decoded = _decoded_script(run)
    terminator_lines = [line for line in decoded.splitlines() if line.startswith("'@")]
    # Exactly one '@-opening line: the template's own here-string terminator.
    assert len(terminator_lines) == 1
    # The message survives as one flattened, escaped line inside the XML.
    assert "line1 '@ Write-Host pwned" in decoded


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
