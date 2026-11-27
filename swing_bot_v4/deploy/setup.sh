#!/usr/bin/env bash
# Ubuntu 22.04/24.04 installer for swingbot v4. Idempotent; preserves /etc/swing-bot.env and /var/lib/swing-bot.
# Usage: sudo bash deploy/setup.sh            (run from the swing_bot_v4 directory)
set -Eeuo pipefail
umask 027
trap 'echo "setup stopped at line $LINENO; existing state and env were not modified beyond this point" >&2' ERR

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
SRC=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
for f in pyproject.toml requirements.lock swingbot/cli.py deploy/swing-bot.service deploy/swing-bot.logrotate deploy/swing-bot.env.template; do
    [[ -f "$SRC/$f" ]] || { echo "missing $SRC/$f"; exit 1; }
done
. /etc/os-release
[[ $ID == ubuntu ]] || { echo "Ubuntu required"; exit 1; }
[[ $(ps -p 1 -o comm=) == systemd ]] || { echo "systemd required"; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv ca-certificates logrotate cron sqlite3
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "Python >= 3.11 required"; exit 1; }

getent group swing-bot >/dev/null || groupadd --system swing-bot
id swing-bot >/dev/null 2>&1 || useradd --system --gid swing-bot --home-dir /var/lib/swing-bot \
    --no-create-home --shell /usr/sbin/nologin swing-bot
install -d -o root -g root -m 0755 /opt/swing-bot
install -d -o swing-bot -g swing-bot -m 0750 /var/lib/swing-bot

if systemctl cat swing-bot.service >/dev/null 2>&1; then
    systemctl stop swing-bot.service
fi
if [[ -e /var/lib/swing-bot/paper_state.json ]]; then
    echo "NOTE: v3 paper_state.json found. v4 does not import it. Any v3 positions still open at Alpaca"
    echo "      will be reported as 'untracked' (never traded by v4, counted against slots)."
    echo "      Close or manage them manually. The v3 file is left untouched."
fi

rm -rf /opt/swing-bot/src.new
install -d -o root -g root -m 0755 /opt/swing-bot/src.new
cp -a "$SRC/pyproject.toml" "$SRC/requirements.lock" "$SRC/swingbot" /opt/swing-bot/src.new/
chown -R root:root /opt/swing-bot/src.new
rm -rf /opt/swing-bot/src && mv /opt/swing-bot/src.new /opt/swing-bot/src

[[ -x /opt/swing-bot/venv/bin/python ]] || python3 -m venv /opt/swing-bot/venv
/opt/swing-bot/venv/bin/python -m pip install -q --disable-pip-version-check -r /opt/swing-bot/src/requirements.lock
/opt/swing-bot/venv/bin/python -m pip install -q --disable-pip-version-check --no-deps --force-reinstall /opt/swing-bot/src
/opt/swing-bot/venv/bin/python -m pip check
/opt/swing-bot/venv/bin/python -m pip freeze > /opt/swing-bot/requirements-installed.txt

if [[ ! -e /etc/swing-bot.env ]]; then
    install -o root -g root -m 0600 "$SRC/deploy/swing-bot.env.template" /etc/swing-bot.env
fi
chown root:root /etc/swing-bot.env && chmod 0600 /etc/swing-bot.env
chown -R swing-bot:swing-bot /var/lib/swing-bot

install -o root -g root -m 0644 "$SRC/deploy/swing-bot.service" /etc/systemd/system/swing-bot.service
install -o root -g root -m 0644 "$SRC/deploy/swing-bot.logrotate" /etc/logrotate.d/swing-bot
cat > /etc/cron.daily/swing-bot-backup <<'CRON'
#!/bin/sh
# Consistent online SQLite snapshot, 30 kept. Local only: copy backups off-host separately.
exec /usr/sbin/runuser -u swing-bot -- env SWING_DATA_DIR=/var/lib/swing-bot /opt/swing-bot/venv/bin/python -m swingbot backup
CRON
chmod 0755 /etc/cron.daily/swing-bot-backup
systemctl enable --now cron.service
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/swing-bot.service
systemctl enable swing-bot.service
systemctl restart swing-bot.service

cat <<'MSG'

Installed. Placeholder keys make the service exit with a configuration error and retry every 60 s.
  1. sudo nano /etc/swing-bot.env        (paste PAPER keys)
  2. sudo systemctl restart swing-bot
  3. sudo journalctl -u swing-bot -n 100 --no-pager
  4. sudo -u swing-bot env SWING_DATA_DIR=/var/lib/swing-bot /opt/swing-bot/venv/bin/python -m swingbot status
MSG
