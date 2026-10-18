"""Router-derived, change-only No-IP updater (Python 3.10+)."""
import argparse
import ipaddress
import json
import logging
import os
from pathlib import Path
import signal
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

LOG = logging.getLogger("noip-updater")
NOIP_ENDPOINT = "https://dynupdate.no-ip.com/nic/update"


class PermanentUpdateError(Exception):
    """Requires configuration changes or No-IP support before restarting."""


class RetryLater(Exception):
    """No-IP requires a delay of at least 30 minutes."""


def _bak_name(path):
    path = Path(path)
    return path.with_name(path.stem + "_bak" + path.suffix)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError(f"{path}: expected a JSON object")
        return value
    except FileNotFoundError:
        return default


def load_state(path):
    path = Path(path)
    for candidate in (path, _bak_name(path)):
        try:
            if not candidate.exists():
                continue
            state = load_json(candidate, {})
            if not isinstance(state.get("noip", {}), dict):
                raise ValueError("invalid noip state")
            if state.get("noip", {}).get("myip") is not None:
                ipaddress.ip_address(state["noip"]["myip"])
            return state
        except (ValueError, OSError):
            LOG.warning("Cannot load state from %s; trying backup", candidate)
    return {"noip": {}}


def atomic_save_json(path, bak_path, data):
    """Prepare a complete file before rotating the old state into its backup."""
    path, bak_path = Path(path), Path(bak_path)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            # Do not replace a usable backup with a corrupt primary.
            try:
                load_json(path, {})
            except (ValueError, OSError):
                pass
            else:
                os.replace(path, bak_path)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_config(path):
    config = load_json(path, {})
    noip = config.get("noip")
    if not isinstance(noip, dict):
        raise ValueError("config requires a noip object")
    for key in ("username", "password", "hostname", "useragent"):
        if not isinstance(noip.get(key), str) or not noip[key].strip():
            raise ValueError(f"config requires noip.{key}")
    if any(c in noip["useragent"] for c in "\r\n"):
        raise ValueError("useragent must be a single line")
    return config


def noip_build_opener(config):
    credentials = config["noip"]
    manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    manager.add_password(None, NOIP_ENDPOINT, credentials["username"], credentials["password"])
    return urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(manager))


def noip_update(config, opener, ipaddr):
    params = urllib.parse.urlencode({"hostname": config["noip"]["hostname"], "myip": ipaddr})
    request = urllib.request.Request(NOIP_ENDPOINT + "?" + params,
                                     headers={"User-Agent": config["noip"]["useragent"]})
    try:
        with opener.open(request, timeout=30) as response:
            body = response.read(4096).decode("ascii").strip()
    except urllib.error.HTTPError as error:
        if error.code >= 500:
            raise RetryLater("No-IP server failure; waiting 30 minutes") from None
        if error.code in (401, 403):
            raise PermanentUpdateError("No-IP authentication rejected; check credentials") from None
        raise
    lines = body.splitlines()
    if any(line.split()[:1] == ["911"] for line in lines):
        raise RetryLater("No-IP 911; waiting 30 minutes")
    if not lines or any(line.split()[:1] not in (["good"], ["nochg"]) for line in lines):
        raise PermanentUpdateError("No-IP rejected update; check configuration and No-IP account")
    return body


class IGDClient:
    def __init__(self, discoverdelay_ms=200, rediscover_cooldown_sec=60, factory=None):
        if factory is None:
            import miniupnpc
            factory = miniupnpc.UPnP
        self.factory = factory
        self.discoverdelay_ms = discoverdelay_ms
        self.rediscover_cooldown_sec = rediscover_cooldown_sec
        self._u = None
        self._last_discover_ts = None

    def _ensure_igd(self, force=False):
        if self._u is not None and not force:
            return
        now = time.monotonic()
        if self._last_discover_ts is not None and now - self._last_discover_ts < self.rediscover_cooldown_sec:
            raise RuntimeError("UPnP discovery cooling down")
        self._last_discover_ts = now
        self._u = None
        router = self.factory()
        router.discoverdelay = self.discoverdelay_ms
        router.discover()
        router.selectigd()
        self._u = router

    def _read_ip(self):
        address = ipaddress.ip_address(self._u.externalipaddress())
        if not address.is_global:
            raise ValueError("router returned a non-global IP address")
        return str(address)

    def get_external_ip(self):
        self._ensure_igd()
        try:
            return self._read_ip()
        except Exception:
            self._u = None
            self._ensure_igd(force=True)
            return self._read_ip()


def update_once(config, state, state_path, igd, opener):
    address = igd.get_external_ip()
    if address == state.get("noip", {}).get("myip"):
        return False
    noip_update(config, opener, address)
    updated = {**state, "noip": {**state.get("noip", {}), "myip": address}}
    atomic_save_json(state_path, _bak_name(state_path), updated)
    state.update(updated)
    LOG.info("Updated No-IP to %s", address)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--state", type=Path, default=Path("state.json"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    try:
        config = load_config(args.config)
        state = load_state(args.state)
        opener = noip_build_opener(config)
        igd = IGDClient()
    except Exception as error:
        LOG.error("Startup failed (%s); check configuration, file access and miniupnpc installation", type(error).__name__)
        return 1
    previous_error = None
    while not stop.is_set():
        delay = 60
        try:
            update_once(config, state, args.state, igd, opener)
            previous_error = None
        except PermanentUpdateError as error:
            LOG.error("%s; stopping until manually restarted", error)
            return 1
        except Exception as error:
            if isinstance(error, RetryLater):
                delay = 1800
            # Use frames and exception type, avoiding variable values and secrets.
            pattern = (type(error).__name__, tuple((f.filename, f.lineno, f.name)
                       for f in traceback.extract_tb(error.__traceback__)))
            if pattern != previous_error:
                LOG.error("Update failed (%s); retrying in %s seconds", type(error).__name__, delay)
            previous_error = pattern
        stop.wait(delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
