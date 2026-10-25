#!/usr/bin/env bash

# License: MIT
# Diehl Platinum inverter (RS-232) -> MQTT / Home Assistant
# Runs inside the container; settings arrive as environment variables from the ct/ script.

source /dev/stdin <<<"$FUNCTIONS_FILE_PATH"
color
verb_ip6
catch_errors
setting_up_container
network_check
update_os

msg_info "Installing Dependencies"
$STD apt install -y python3 python3-venv
msg_ok "Installed Dependencies"

msg_info "Installing Diehl-Platinum"
if [[ -z "${DIEHL_MQTT_SRC:-}" ]]; then
  msg_error "Service source code missing (DIEHL_MQTT_SRC) - please start via ct/diehl-platinum.sh"
  exit 1
fi
id diehl >/dev/null 2>&1 || useradd --system --no-create-home --groups dialout diehl
mkdir -p /opt/diehl-platinum
printf '%s\n' "$DIEHL_MQTT_SRC" >/opt/diehl-platinum/diehl_platinum_mqtt.py
python3 -m venv /opt/diehl-platinum/.venv
$STD /opt/diehl-platinum/.venv/bin/pip install "pyserial>=3.5" "paho-mqtt>=2.0"
msg_ok "Installed Diehl-Platinum"

msg_info "Configuring Diehl-Platinum"
# systemd EnvironmentFile: values in "...", with \ and " escaped (systemd does not expand $)
env_q() {
  local v="${1//\\/\\\\}"
  printf '"%s"' "${v//\"/\\\"}"
}
umask 077
{
  echo "DIEHL_PORT=$(env_q "${DIEHL_SERIAL_DEV:-/dev/ttyUSB0}")"
  echo "MQTT_HOST=$(env_q "${MQTT_HOST}")"
  echo "MQTT_PORT=$(env_q "${MQTT_PORT:-1883}")"
  echo "MQTT_USER=$(env_q "${MQTT_USER:-}")"
  echo "MQTT_PASS=$(env_q "${MQTT_PASS:-}")"
  echo "POLL_INTERVAL=$(env_q "${POLL_INTERVAL:-10}")"
} >/etc/diehl-platinum.env
umask 022
msg_ok "Configured Diehl-Platinum"

msg_info "Creating Service"
cat <<EOF >/etc/systemd/system/diehl-platinum.service
[Unit]
Description=Diehl Platinum inverter -> MQTT
After=network-online.target
Wants=network-online.target

[Service]
User=diehl
Group=dialout
EnvironmentFile=/etc/diehl-platinum.env
ExecStart=/opt/diehl-platinum/.venv/bin/python /opt/diehl-platinum/diehl_platinum_mqtt.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
systemctl enable -q --now diehl-platinum
msg_ok "Created Service"

motd_ssh
customize
cleanup_lxc
