"""Windows implementation: a per-user scheduled task that runs the sync in a
hidden window, plus the --doctor status check.

Task Scheduler runs the sync through a small VBScript wrapper rather than the
pipx/uv binary directly. schtasks would flash a console window every four hours;
launching the sync from VBScript with WScript.Shell.Run and a window style of 0
keeps it invisible. The wrapper also points the task at a filename whose bytes
stay stable across updates, so a pipx/uv upgrade that swaps the underlying
binary does not require re-registering the task.

The public entry points (notify, install_agent, uninstall_agent, offer_agent,
agent_status, agent_installed, purge_agent) are what the platform_support
package dispatches to. notify raises a native toast through PowerShell WinRT,
fire-and-forget.
"""
from __future__ import annotations

import base64
import platform
import shutil
import subprocess
import sys

from eufy_sync.cli import shared

TASK_NAME = "eufy-sync"
WRAPPER_NAME = "eufy-sync-agent.vbs"

# PowerShell script that raises a native toast through the WinRT notification
# APIs. The title and message are XML-escaped and interpolated with .format(),
# so the AppId GUID braces are doubled to survive that pass as single literal
# braces. The AppId points at the built-in Windows PowerShell shortcut so the
# toast has a registered source and actually shows (an unregistered AppId is
# silently dropped).
_PS_TOAST = """\
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml(@'
<toast><visual><binding template="ToastGeneric"><text>{title}</text><text>{message}</text></binding></visual></toast>
'@)
$appid = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appid).Show(
    [Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def _wrapper_content(binary_path: str) -> str:
    """Build the VBScript that runs the sync with no visible console.

    WScript.Shell.Run takes one command string and a window style; style 0
    hides the window. The command is a `cmd /c` line so its stdout and stderr
    can be appended to the log with `>>`. VBScript has no backslash escaping:
    a literal double quote inside a string is written as two double quotes.
    The inner command already contains quotes (around the binary path and the
    log path), so every one of them is doubled before it is embedded in the
    Run string literal.

    Run is called with bWaitOnReturn True and its return value goes straight to
    WScript.Quit, so wscript exits with the sync's own exit code. Launched
    fire-and-forget, wscript returns 0 the moment it hands the command off and
    Task Scheduler's LastTaskResult reads 0 forever, whatever the sync did -
    which is how 24 days of failing runs went unnoticed on a real install.

    Rotation belongs here, ahead of the launch, and not in Python: this is the
    only moment when nothing holds the log open. Once the run starts, cmd's
    `>>` keeps a handle on the file in a sharing mode that refuses the rename.
    """
    log = str(shared.LOG_FILE)
    rotated = f"{log}.1"
    inner = f'cmd /c ""{binary_path}" --headless >> "{log}" 2>&1"'
    escaped = inner.replace('"', '""')
    # The rotation block spells the paths out as standalone VBScript string
    # literals, so the same doubling rule applies to them. A Windows filename
    # cannot hold a double quote, so this never fires in practice; it is here so
    # no path can turn the generated script into something that will not parse.
    log_lit = log.replace('"', '""')
    rotated_lit = rotated.replace('"', '""')
    return (
        'Set shell = CreateObject("WScript.Shell")\r\n'
        'Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
        # Roll the log over past 1 MB and keep one previous generation, so an
        # unattended install cannot grow sync.log without bound. A failed
        # rotation (a stray lock, a full disk) must never cost us the sync, so
        # errors are swallowed across this block and only this block: the launch
        # below stays outside the guard and keeps reporting its own failures.
        'On Error Resume Next\r\n'
        f'If fso.FileExists("{log_lit}") Then\r\n'
        f'  If fso.GetFile("{log_lit}").Size > 1048576 Then\r\n'
        f'    If fso.FileExists("{rotated_lit}") Then fso.DeleteFile "{rotated_lit}"\r\n'
        f'    fso.MoveFile "{log_lit}", "{rotated_lit}"\r\n'
        '  End If\r\n'
        'End If\r\n'
        'On Error GoTo 0\r\n'
        f'WScript.Quit shell.Run("{escaped}", 0, True)\r\n'
    )


def _write_wrapper(binary_path: str):
    """Write the hidden-window wrapper, but only when its content would change.

    The task registration points at this file, so leaving the bytes untouched
    across updates avoids needless rewrites (the pipx/uv shim path is stable, so
    the content normally never changes anyway).
    """
    wrapper_path = shared.DATA_DIR / WRAPPER_NAME
    # Write and compare as raw bytes: text mode would translate the CRLF line
    # endings (dropping the carriage returns on read, and doubling them on write
    # under Windows), so the round-trip would never match and the file would be
    # rewritten on every run.
    content = _wrapper_content(binary_path).encode("utf-8")
    if wrapper_path.exists() and wrapper_path.read_bytes() == content:
        return wrapper_path
    wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    wrapper_path.write_bytes(content)
    return wrapper_path


def _install_scheduled_task() -> None:
    """Register the per-user scheduled task for automatic sync."""
    binary = shutil.which("eufy-sync")
    if not binary:
        print("Warning: could not find eufy-sync on PATH. Skipping auto-sync setup.")
        return

    shared.DATA_DIR.mkdir(parents=True, exist_ok=True)
    wrapper_path = _write_wrapper(binary)

    result = subprocess.run(
        ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "HOURLY", "/MO", "4",
         "/TR", f'wscript.exe "{wrapper_path}"'],
        capture_output=True, text=True, timeout=15,
    )

    if result.returncode == 0:
        print(f"Automatic sync installed (every 4 hours). Logs: {shared.LOG_FILE}")
    else:
        print(result.stderr.strip() or "Could not register the scheduled task.")
        # Match the /TR value the code passes programmatically: the wrapper path
        # is quoted inside the /TR argument, and those inner quotes are escaped
        # with backslashes so a cmd.exe paste keeps the path (which may contain
        # spaces) as a single argument.
        print(
            "Register it manually with: "
            f'schtasks /Create /F /TN {TASK_NAME} /SC HOURLY /MO 4 '
            f'/TR "wscript.exe \\"{wrapper_path}\\""'
        )


def _uninstall_scheduled_task() -> None:
    """Remove the scheduled task and its wrapper."""
    result = subprocess.run(
        ["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
        capture_output=True, text=True, timeout=15,
    )

    # schtasks returns nonzero (a "cannot find" error) when nothing is
    # registered; treat that as "nothing to remove" rather than a failure.
    if result.returncode != 0:
        print("No scheduled task installed.")
    else:
        print("Scheduled task removed. Auto-sync disabled.")

    wrapper_path = shared.DATA_DIR / WRAPPER_NAME
    if wrapper_path.exists():
        wrapper_path.unlink()


def _offer_scheduled_task() -> None:
    """Offer to install the scheduled task after first-run setup."""
    if platform.system() != "Windows":
        return
    if not sys.stdin.isatty():
        return

    print("")
    answer = input("Set up automatic sync every 4 hours? [y/N] ").strip()
    if not answer.lower().startswith("y"):
        return

    _install_scheduled_task()


def agent_status() -> dict | None:
    """Report the scheduled task's health for --doctor.

    Three states: the wrapper missing or the task query failing means it is not
    installed; the wrapper present but out of date with the current binary means
    an outdated registration; both good means it is installed and scheduled.
    """
    try:
        wrapper_path = shared.DATA_DIR / WRAPPER_NAME
        if not wrapper_path.exists():
            return {"status": "WARN", "label": "scheduled task", "detail": "not installed", "fix": "eufy-sync --install-agent"}

        result = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return {"status": "WARN", "label": "scheduled task", "detail": "not installed", "fix": "eufy-sync --install-agent"}

        binary = shutil.which("eufy-sync")
        if binary and wrapper_path.read_bytes() != _wrapper_content(binary).encode("utf-8"):
            return {"status": "WARN", "label": "scheduled task", "detail": "outdated registration", "fix": "eufy-sync --install-agent"}

        return {"status": "PASS", "label": "scheduled task", "detail": "installed, runs every 4h", "fix": None}
    except Exception as e:
        return {"status": "WARN", "label": "scheduled task", "detail": f"could not check ({e})", "fix": "eufy-sync --install-agent"}


def agent_installed() -> bool:
    """Whether the scheduled task's wrapper file is present on disk."""
    return (shared.DATA_DIR / WRAPPER_NAME).exists()


def purge_agent() -> None:
    """Remove the scheduled task and wrapper without printing. Used by the full
    --uninstall, which reports its own summary."""
    wrapper_path = shared.DATA_DIR / WRAPPER_NAME
    if wrapper_path.exists():
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True)
        wrapper_path.unlink()


def notify(title: str, message: str, command: str | None = None) -> None:
    """Send a native toast through PowerShell WinRT. Fails silently.

    A notification is a courtesy, never the point of the run, so anything that
    goes wrong here (no powershell, WinRT unavailable, a slow shell) must not
    turn a completed sync into a crash. Every failure is swallowed, and the
    call is bounded by a timeout so it cannot hang the process.

    The title and message are XML-escaped and the whole script is handed to
    powershell as a base64 -EncodedCommand (UTF-16-LE, the encoding PowerShell
    expects), so no shell quoting layer can misread the text. command is
    accepted for interface parity but ignored: v1 toasts are not clickable, and
    call sites already put the fix command in the message text.
    """
    try:
        from xml.sax.saxutils import escape
        # Flatten all whitespace runs (including CR/LF) to single spaces before
        # the XML escape. The text is embedded in a PowerShell here-string, which
        # terminates on any line starting with '@, so a newline followed by '@
        # in the message (raw exception text reaches here) would close the
        # here-string early and hand the remainder to PowerShell as code.
        # Newlines must never reach the embedded XML.
        title = " ".join(title.split())
        message = " ".join(message.split())
        script = _PS_TOAST.format(title=escape(title), message=escape(message))
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-EncodedCommand", encoded],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def install_agent() -> None:
    _install_scheduled_task()


def uninstall_agent() -> None:
    _uninstall_scheduled_task()


def offer_agent() -> None:
    _offer_scheduled_task()
