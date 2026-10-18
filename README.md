# No-IP updater

A Python 3.9+ service that reads the router's public IP with `miniupnpc`, checks every 60 seconds, and contacts No-IP only when that IP differs from the last successfully saved update. Requires Linux/systemd for the service, a reachable UPnP Internet Gateway Device, and UPnP enabled on that router. A private/CGNAT router address is rejected; DDNS cannot make a CGNAT connection publicly reachable.

## Install and start

Run `./install.sh` as your normal user from the checkout. Python 3.9+, venv/pip support, miniupnpc build prerequisites, and a functional systemd user session are required. Install missing OS packages through your distribution package manager; the installer never runs sudo. It checks the user manager, prepares dependencies, generates the service, and reloads systemd. It does not start or restart the service.

Default locations (absolute `XDG_DATA_HOME`, `XDG_CONFIG_HOME`, and `XDG_STATE_HOME` override their respective bases):

| Purpose | Default path |
|---|---|
| Application and samples | `~/.local/share/noip-updater/current/` |
| Venv shortcut | `~/.local/share/noip-updater/.venv/` |
| Credentials | `~/.config/noip-updater/config.json` |
| State and backup | `~/.local/state/noip-updater/state.json`, `state_bak.json` |
| Generated user service | `~/.config/systemd/user/noip-updater.service` |
| Logs | systemd journal |

Application directories are private (0700); configuration, state, and generated files are private (0600). Venv executables retain executable permissions. The service contains quoted absolute paths and works independently of the source checkout or working directory. `noip-updater.service` in the checkout is an installer template, not a unit to copy directly.

After installation, edit the configuration and run the exact configuration-check command printed by the installer. With default paths:

```sh
${EDITOR:-vi} "$HOME/.config/noip-updater/config.json"
"$HOME/.local/share/noip-updater/.venv/bin/python"   "$HOME/.local/share/noip-updater/current/noip_updater.py"   --config "$HOME/.config/noip-updater/config.json"   --state "$HOME/.local/state/noip-updater/state.json" --check-config
systemctl --user enable --now noip-updater.service
journalctl --user-unit noip-updater.service -f
```

The check validates JSON, required fields, sample credentials/User-Agent, and state storage access without importing miniupnpc, contacting the router, or authenticating with No-IP. Errors omit credential values. Use a No-IP DDNS Key username/password with `all.ddnskey.com`, or credentials/hostname appropriate to your account; `all.ddnskey.com` itself is a valid target. Set a descriptive User-Agent with your contact email.

For boot-time operation before login, separately run `loginctl enable-linger "$USER"`; this may require administrator authorization. The user unit uses `default.target`; network readiness is handled by retries.

## Updates, migration and removal

Run `./install.sh` again with the same XDG environment to update. Existing configuration and state are preserved. Each update creates a release and its venv directly in its permanent directory under `releases/`, verifies dependencies, then switches `current` and generates a unit pinned to that release. This layout avoids moving venvs and preserves the dependencies of an already running process. Preparation failure leaves the current release and service untouched. Old releases are retained until uninstall.

Updates never stop or restart the service automatically. After checking the configuration, explicitly run `systemctl --user restart noip-updater.service`. Request deadlines are still in memory: wait out any required retry interval (up to 30 minutes) before restarting. Customize the unit with `systemctl --user edit noip-updater.service`; drop-ins are preserved.

For an older installation that kept everything in the application directory, stop the old service first and copy your existing `config.json`, `state.json`, and `state_bak.json` into the separate configuration/state directories before starting the generated service. The installer does not migrate credentials automatically. Keep one updater per state file; when changing account/hostname, stop the service and relocate both state files.

`./uninstall.sh` stops/disables the installed unit and removes the application, all releases/venvs, and the distributed unit. Configuration and state remain. `./uninstall.sh --purge` also removes the application's configuration and state directories. Drop-ins remain for user management. Use the same XDG environment as installation.

Direct CLI use retains working-directory defaults for `config.json` and `state.json`; use `--config PATH` and `--state PATH` to override them. The state directory must exist.

Layout references: [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir/latest/) and [Python venv documentation](https://docs.python.org/3/library/venv.html).

## Behavior and differences from task.md

The supplied article and script are the baseline, with these reliability corrections:

- Only No-IP `good` and `nochg` responses count as success, and every response line must contain exactly one public IP matching the requested IP. Missing, malformed, non-ASCII, oversized, or mismatched responses are rejected. Credential, account, and unknown response errors stop the process for manual intervention. After fixing the problem, restart with `systemctl --user restart noip-updater.service`. No automatic restart policy bypasses that stop.
- No-IP requests are spaced at least 5 minutes apart, including failed attempts and rapid router IP changes. No-IP `911` and HTTP 5xx failures wait at least 30 minutes. Both intervals use a monotonic clock measured from request completion. Router checks and local persistence retries run every 60 seconds; requests have a 30-second timeout. Request deadlines are kept in memory, so restarting the process resets them; avoid restarting during a required wait.
- The in-memory last successful IP (`extip_prev`) advances only after a validated No-IP success. `state.json` and the corresponding state dictionary advance only when that success is saved. A router or request failure does not erase the last successful IP. If persistence fails after success, retain the pending state and retry only the save before checking the router again, without repeating the No-IP request.
- State is written into a same-directory temporary file, flushed and fsynced. The prior state is validated (including its IP), copied into a separate temporary file and atomically installed as `state_bak.json`; an invalid primary never replaces a usable backup. The new primary then atomically replaces `state.json`, without first removing the existing primary. Startup falls back to the backup if the primary is missing or invalid. File and directory fsync improve durability; temporary files are cleaned up on failure, and multiple file renames are not a single transaction.
- Discovery attempts, including failures and forced rediscovery, share a monotonic cooldown of 60 seconds. Zero discovered devices is treated as failure without selecting an IGD. A stale/empty/invalid router response discards the cached IGD and triggers rediscovery immediately if the cooldown permits, otherwise on a later cycle. A failed read after rediscovery also discards the IGD. Only public unicast IP addresses are accepted, with an additional check immediately before sending to No-IP.
- Errors with the same exception type, HTTP status, and traceback function locations are logged once per failure episode, even if other errors intervene. Recovery logs the total failure duration using a monotonic clock; waiting for a request deadline alone does not count as recovery. Logs omit exception payloads to avoid exposing credentials and variable request data. Successful IP changes are logged. SIGINT/SIGTERM interrupt polling waits for shutdown.

Protocol references: [No-IP update endpoint](https://developer.noip.com/reference/nic_update), [response handling and required retry delays](https://www.noip.com/integrate/response).

## Validation

```sh
python3 -m unittest discover -s tests -v
python3 noip_updater.py --help
```

Tests mock the router and HTTP opener and use temporary state files; they require no credentials or miniupnpc installation. They do not establish real UPnP discovery, No-IP authentication/DNS changes, or boot-time service execution.
