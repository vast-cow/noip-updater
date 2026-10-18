"""Install an isolated user service without starting or restarting it."""
import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import uuid


def locations():
    home = Path.home()
    paths = []
    for name, fallback in (("XDG_DATA_HOME", home / ".local/share"),
                           ("XDG_CONFIG_HOME", home / ".config"),
                           ("XDG_STATE_HOME", home / ".local/state")):
        value = Path(os.environ.get(name) or fallback)
        if not value.is_absolute():
            raise ValueError(f"{name} must be absolute")
        paths.append(value)
    data, config, state = paths
    return data / "noip-updater", config / "noip-updater", state / "noip-updater", config / "systemd/user/noip-updater.service"


def run(*args):
    subprocess.run(args, check=True)


def unit_quote(path):
    # systemd quoted arguments still expand specifiers and environment variables.
    return '"' + str(path).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def install(data, config, state, unit):
    import venv
    source = Path(__file__).resolve().parent
    for directory in (data, config, state):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    releases = data / "releases"
    releases.mkdir(exist_ok=True, mode=0o700)
    release = releases / uuid.uuid4().hex
    release.mkdir(mode=0o700)
    try:
        for name in ("noip_updater.py", "requirements.txt", "config.example.json"):
            shutil.copyfile(source / name, release / name)
        # Create in its permanent location: venv scripts embed absolute paths.
        venv.EnvBuilder(with_pip=True).create(release / ".venv")
        python = release / ".venv/bin/python"
        run(str(python), "-m", "pip", "install", "-r", str(release / "requirements.txt"))
        run(str(python), "-c", "import miniupnpc")
        run(str(python), str(release / "noip_updater.py"), "--help")
    except BaseException:
        shutil.rmtree(release)
        raise
    target_config = config / "config.json"
    try:
        with target_config.open("x", encoding="utf-8") as stream:
            stream.write((release / "config.example.json").read_text())
    except FileExistsError:
        pass
    target_config.chmod(0o600)
    for name in ("state.json", "state_bak.json"):
        if (state / name).exists():
            (state / name).chmod(0o600)
    current = data / "current"
    pending = data / ".current-new"
    pending.unlink(missing_ok=True)
    pending.symlink_to(release, target_is_directory=True)
    os.replace(pending, current)
    # Convenient stable venv path, while each running process keeps its old release.
    link = data / ".venv"
    if not link.exists() and not link.is_symlink():
        link.symlink_to("current/.venv", target_is_directory=True)
    template = (source / "noip-updater.service").read_text()
    command = " ".join((unit_quote(release / ".venv/bin/python"), "-u",
                        unit_quote(release / "noip_updater.py"), "--config",
                        unit_quote(target_config), "--state", unit_quote(state / "state.json")))
    template = template.replace("@EXEC_START@", command)
    unit.parent.mkdir(parents=True, exist_ok=True)
    temporary = unit.with_suffix(".service.tmp")
    temporary.write_text(template)
    os.replace(temporary, unit)
    run("systemctl", "--user", "daemon-reload")
    check = [str(current / ".venv/bin/python"), str(current / "noip_updater.py"),
             "--config", str(target_config), "--state", str(state / "state.json"), "--check-config"]
    print(f"Edit {target_config}, then run:\n{shlex.join(check)}")
    print("Start: systemctl --user enable --now noip-updater.service")
    print("Updates require an explicit restart. Respect any existing 30-minute retry wait.")
    print("Restart: systemctl --user restart noip-updater.service")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("install", "uninstall"))
    parser.add_argument("--purge", action="store_true")
    args = parser.parse_args()
    if args.purge and args.operation != "uninstall":
        parser.error("--purge is only valid for uninstall")
    os.umask(0o077)
    try:
        paths = locations()
        # Fail before changing anything if the user manager is unavailable.
        subprocess.run(("systemctl", "--user", "show-environment"), check=True, stdout=subprocess.DEVNULL)
        if args.operation == "install":
            install(*paths)
        else:
            data, config, state, unit = paths
            if unit.exists():
                run("systemctl", "--user", "disable", "--now", "noip-updater.service")
                unit.unlink()
            if data.exists():
                shutil.rmtree(data)
            if args.purge:
                for directory in (config, state):
                    if directory.exists():
                        shutil.rmtree(directory)
            run("systemctl", "--user", "daemon-reload")
    except (OSError, ValueError, ImportError, subprocess.CalledProcessError) as error:
        print(f"{args.operation} failed ({type(error).__name__}). Check Python 3.9+, python3-venv/pip, miniupnpc build prerequisites, and the systemd user session. Install OS packages with your distribution package manager; no sudo was run.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
