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

- Only No-IP `good` and `nochg` responses count as success. Credential, account, and unknown response errors stop the process for manual intervention. After fixing the problem, restart with `systemctl --user restart noip-updater.service`. No automatic restart policy bypasses that stop.
- No-IP `911` and HTTP 5xx failures wait at least 30 minutes. Other transient failures retry after 60 seconds; requests have a 30-second timeout.
- In-memory state advances only after a successful response and state save. A router or request failure does not erase the last successful IP. If state cannot be persisted after a successful request, the next cycle may repeat the update.
- State is written into a same-directory temporary file, flushed, then installed using rename with the prior file retained as `state_bak.json`. Startup falls back to that backup if the primary is missing or corrupt. File and directory fsync improve durability; multiple file renames are not a single transaction.
- Discovery attempts, including failures and forced rediscovery, share a monotonic cooldown of 60 seconds. A stale/empty/invalid router response triggers rediscovery immediately if the cooldown permits, otherwise on a later cycle. Only global IP addresses are accepted.
- Repeated errors with the same exception type and traceback locations are logged once until recovery. Logs omit exception payloads to avoid exposing credentials and variable request data. Successful IP changes are logged. SIGINT/SIGTERM interrupt polling waits for shutdown.

Protocol references: [No-IP update endpoint](https://developer.noip.com/reference/nic_update), [response handling and required retry delays](https://www.noip.com/integrate/response).

## Validation

```sh
python3 -m unittest discover -s tests -v
python3 noip_updater.py --help
```

Tests mock the router and HTTP opener and use temporary state files; they require no credentials or miniupnpc installation. They do not establish real UPnP discovery, No-IP authentication/DNS changes, or boot-time service execution.
