# No-IP updater

A Python 3.10+ service that reads the router's public IP with `miniupnpc`, checks every 60 seconds, and contacts No-IP only when that IP differs from the last successfully saved update. Requires Linux/systemd for the service, a reachable UPnP Internet Gateway Device, and UPnP enabled on that router. A private/CGNAT router address is rejected; DDNS cannot make a CGNAT connection publicly reachable.

## Install

Run as your normal user from this project directory:

```sh
mkdir -p "$HOME/.local/share/noip-updater"
cp noip_updater.py requirements.txt config.example.json "$HOME/.local/share/noip-updater/"
cd "$HOME/.local/share/noip-updater"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config.example.json config.json
chmod 600 config.json
${EDITOR:-vi} config.json
```

Use the username/password of a No-IP DDNS Key and `all.ddnskey.com`, or credentials and hostname appropriate to your No-IP account. Set a descriptive User-Agent identifying your client and contact email. The example contains placeholders only. Python venv support and build prerequisites for miniupnpc may need installation through your distribution's package manager.

Test in the foreground (this performs real updates with your credentials):

```sh
.venv/bin/python noip_updater.py
```

Stop with Ctrl-C. By default `config.json`, `state.json`, and `state_bak.json` are relative to the working directory. Optional `--config PATH` and `--state PATH` select other locations; the state directory must already exist and be writable. Keep one updater process per state file. When changing hostname/account, stop the service and remove or relocate both state files so the new target receives its first update.

## User service

From the original project directory:

```sh
mkdir -p "$HOME/.config/systemd/user"
cp noip-updater.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now noip-updater.service
loginctl enable-linger "$USER"
journalctl --user-unit noip-updater.service -f
```

The unit uses `%h/.local/share/noip-updater` and its virtual environment. Edit both `WorkingDirectory` and `ExecStart` if installing elsewhere. Enabling linger may require administrator authorization. Check status with `systemctl --user status noip-updater.service`. Stop and disable with `systemctl --user disable --now noip-updater.service`.

User instances normally start `default.target`; this unit uses it instead of the article's `network-online.target`, which requires an unspecified system-to-user bridge. Network readiness is handled by retries, so no bridge is required. Linger starts the user manager at boot without an interactive login. This project does not install or start services automatically.

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
