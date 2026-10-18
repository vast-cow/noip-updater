"""Router-derived, change-only No-IP updater (Python 3.9+)."""
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
import urllib.error
import urllib.parse
import urllib.request

LOG = logging.getLogger("noip-updater")
MIN_UPDATE_INTERVAL = 300
SERVER_RETRY_INTERVAL = 1800

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


def public_ip(value):
    if not isinstance(value, str) or "%" in value:
        raise ValueError("expected a public IP address")
    address = ipaddress.ip_address(value)
    if not address.is_global or address.is_multicast or address.is_unspecified:
        raise ValueError("expected a public IP address")
    return str(address)


def validate_state(state):
    if not isinstance(state, dict) or not isinstance(state.get("noip", {}), dict):
        raise ValueError("invalid noip state")
    address = state.get("noip", {}).get("myip")
    if address is not None:
        public_ip(address)
    return state


def load_state(path):
    path = Path(path)
    for candidate in (path, _bak_name(path)):
        try:
            with candidate.open(encoding="utf-8") as stream:
                return validate_state(json.load(stream))
        except FileNotFoundError:
            continue
        except (ValueError, OSError):
            LOG.warning("Cannot load state from %s; trying backup", candidate)
    return {"noip": {}}


def _prepare_json(path, data):
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        return name
    except BaseException:
        os.unlink(name)
        raise


def _sync_directory(path):
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def atomic_save_json(path, bak_path, data):
    """Replace each file atomically, retaining only a validated prior state."""
    validate_state(data)
    path, bak_path = Path(path), Path(bak_path)
    name = _prepare_json(path, data)
    backup_name = None
    try:
        try:
            with path.open(encoding="utf-8") as stream:
                old = validate_state(json.load(stream))
        except (ValueError, OSError):
            pass
        else:
            backup_name = _prepare_json(bak_path, old)
            os.replace(backup_name, bak_path)
            _sync_directory(bak_path.parent)
        os.replace(name, path)
        _sync_directory(path.parent)
    finally:
        for temporary in (name, backup_name):
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)


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
    ipaddr = public_ip(ipaddr)
    params = urllib.parse.urlencode({"hostname": config["noip"]["hostname"], "myip": ipaddr})
    request = urllib.request.Request(NOIP_ENDPOINT + "?" + params,
                                     headers={"User-Agent": config["noip"]["useragent"]})
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read(4097)
            if len(raw) > 4096:
                raise PermanentUpdateError("No-IP response too large")
            try:
                body = raw.decode("ascii").strip()
            except UnicodeDecodeError:
                raise PermanentUpdateError("No-IP response is not ASCII") from None
    except urllib.error.HTTPError as error:
        if 500 <= error.code < 600:
            raise RetryLater("No-IP server failure; waiting 30 minutes") from None
        if error.code in (401, 403):
            raise PermanentUpdateError("No-IP authentication rejected; check credentials") from None
        raise
    lines = body.splitlines()
    if any(line.split()[:1] == ["911"] for line in lines):
        raise RetryLater("No-IP 911; waiting 30 minutes")
    if not lines or any(line.split()[:1] not in (["good"], ["nochg"]) for line in lines):
        raise PermanentUpdateError("No-IP rejected update; check configuration and No-IP account")
    for line in lines:
        fields = line.split()
        try:
            valid = len(fields) == 2 and public_ip(fields[1]) == ipaddr
        except ValueError:
            valid = False
        if not valid:
            raise PermanentUpdateError("No-IP returned a missing, invalid or mismatched IP")
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
        if router.discover() == 0:
            raise RuntimeError("UPnP discovery found no devices")
        router.selectigd()
        self._u = router

    def _read_ip(self):
        return public_ip(self._u.externalipaddress())

    def get_external_ip(self):
        self._ensure_igd()
        try:
            return self._read_ip()
        except Exception:
            self._u = None
            # Keep the original error during cooldown, rather than replacing it
            # with a different error on each polling cycle.
            if time.monotonic() - self._last_discover_ts < self.rediscover_cooldown_sec:
                raise
        self._ensure_igd(force=True)
        try:
            return self._read_ip()
        except Exception:
            self._u = None
            raise


class UpdateDeferred(Exception):
    """A request is waiting for its minimum interval."""


class UpdateSession:
    def __init__(self, state):
        self.extip_prev = state.get("noip", {}).get("myip")
        self.next_request_at = 0
        self.pending = None


def update_once(config, state, state_path, igd, opener, session):
    if session.pending is None:
        address = public_ip(igd.get_external_ip())
        if address == session.extip_prev:
            return False
        if time.monotonic() < session.next_request_at:
            raise UpdateDeferred()
        interval = MIN_UPDATE_INTERVAL
        try:
            noip_update(config, opener, address)
        except RetryLater:
            interval = SERVER_RETRY_INTERVAL
            raise
        finally:
            # Include failed/time-out attempts and measure from completion.
            session.next_request_at = time.monotonic() + interval
        session.extip_prev = address
        session.pending = {**state, "noip": {**state.get("noip", {}), "myip": address}}
    atomic_save_json(state_path, _bak_name(state_path), session.pending)
    state.update(session.pending)
    session.pending = None
    LOG.info("Updated No-IP to %s", session.extip_prev)
    return True


class ErrorTracker:
    def __init__(self):
        self.started = None
        self.seen = set()

    def failed(self, error, delay):
        if self.started is None:
            self.started = time.monotonic()
        # Payloads can contain credentials; classify without logging them.
        pattern = (type(error), getattr(error, "code", None),
                   tuple((frame.tb_frame.f_code.co_filename,
                          frame.tb_frame.f_code.co_name)
                         for frame in self._frames(error.__traceback__)))
        if pattern not in self.seen:
            LOG.error("Update failed (%s); retrying in %s seconds", type(error).__name__, delay)
            self.seen.add(pattern)

    @staticmethod
    def _frames(tb):
        while tb is not None:
            yield tb
            tb = tb.tb_next

    def recovered(self):
        if self.started is not None:
            LOG.info("Recovered after %.0f seconds", time.monotonic() - self.started)
            self.started = None
            self.seen.clear()


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
    session = UpdateSession(state)
    errors = ErrorTracker()
    while not stop.is_set():
        delay = 60
        try:
            update_once(config, state, args.state, igd, opener, session)
            errors.recovered()
        except UpdateDeferred:
            pass
        except PermanentUpdateError as error:
            LOG.error("%s; stopping until manually restarted", error)
            return 1
        except Exception as error:
            if isinstance(error, RetryLater):
                delay = SERVER_RETRY_INTERVAL
            errors.failed(error, delay)
        stop.wait(delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
