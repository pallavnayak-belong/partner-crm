#!/usr/bin/env python3
"""Start the CRM automatically at login and restart it if it ever stops (macOS LaunchAgent).

    python3 autostart.py install     # create the login item and start the CRM under it
    python3 autostart.py status
    python3 autostart.py restart     # the right way to restart the CRM once it is managed this way
    python3 autostart.py uninstall   # remove the login item (the CRM keeps running until you stop it)

Why: backups run inside the CRM, so a CRM that isn't running isn't backing anything up.
"""
import os
import plistlib
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LABEL = "com.belong.partner-crm"
PORT = int(os.environ.get("PARTNER_CRM_PORT", "8765"))
PLIST = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", f"{LABEL}.plist")


def uid():
    return os.getuid()


def plist_dict(python=None, here=HERE, home=None):
    home = home or os.path.expanduser("~")
    return {
        "Label": LABEL,
        "ProgramArguments": [python or sys.executable, os.path.join(here, "server.py")],
        "WorkingDirectory": here,
        "RunAtLoad": True,
        "KeepAlive": True,                      # restart if it crashes or is killed
        "ThrottleInterval": 10,                 # but never faster than every 10 s, so a startup error can't spin
        "StandardOutPath": os.path.join(here, "data", "server.log"),
        "StandardErrorPath": os.path.join(here, "data", "server.log"),
        "EnvironmentVariables": {"HOME": home, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"},
        "ProcessType": "Interactive",
    }


def _run(*args):
    return subprocess.run(args, capture_output=True, text=True)


def installed():
    return os.path.exists(PLIST) and _run("launchctl", "print", f"gui/{uid()}/{LABEL}").returncode == 0


def _listener_pids():
    out = _run("lsof", "-ti", f"tcp:{PORT}", "-sTCP:LISTEN").stdout.split()
    return [int(p) for p in out]


def _free_port():
    """Stop a hand-started CRM on our port so the managed one can take over. Only ever stops this app's own server.py."""
    for pid in _listener_pids():
        cmd = _run("ps", "-p", str(pid), "-o", "command=").stdout
        cwd = _run("lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn").stdout
        if "server.py" in cmd and HERE in cwd:
            os.kill(pid, 15)
        else:
            sys.exit(f"Port {PORT} is used by something else ({cmd.strip()[:80]}). Not touching it.")
    for _ in range(30):
        if not _listener_pids():
            return
        time.sleep(0.3)
    sys.exit(f"Port {PORT} did not free up.")


def install():
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, "wb") as f:
        plistlib.dump(plist_dict(), f)
    if _run("launchctl", "print", f"gui/{uid()}/{LABEL}").returncode == 0:
        _run("launchctl", "bootout", f"gui/{uid()}/{LABEL}")
    _free_port()
    r = _run("launchctl", "bootstrap", f"gui/{uid()}", PLIST)
    if r.returncode != 0:
        sys.exit("launchctl could not load the login item: " + (r.stderr or r.stdout).strip())
    for _ in range(40):
        if _listener_pids():
            print(f"Installed. The CRM now starts at login and restarts itself if it stops. Open http://127.0.0.1:{PORT}")
            return
        time.sleep(0.5)
    sys.exit("Installed, but the CRM did not come up. See data/server.log")


def status():
    ok = installed()
    pids = _listener_pids()
    print(f"login item: {'installed' if ok else 'not installed'}   CRM listening on {PORT}: {'yes (pid ' + str(pids[0]) + ')' if pids else 'no'}")
    return ok


def restart():
    if not installed():
        sys.exit("The login item isn't installed. Run: python3 autostart.py install")
    r = _run("launchctl", "kickstart", "-k", f"gui/{uid()}/{LABEL}")
    if r.returncode != 0:
        sys.exit("Could not restart: " + (r.stderr or r.stdout).strip())
    print("Restarting…")


def uninstall():
    _run("launchctl", "bootout", f"gui/{uid()}/{LABEL}")
    if os.path.exists(PLIST):
        os.remove(PLIST)
    print("Login item removed. The CRM won't start by itself any more.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"install": install, "status": status, "restart": restart, "uninstall": uninstall}.get(cmd, lambda: sys.exit(__doc__))()
