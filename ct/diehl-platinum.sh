#!/usr/bin/env bash
# Own repository: put the raw URL of your GitHub repository here (used by "update" in the container).
_CS_DEFAULT_URL="${_CS_DEFAULT_URL:-https://raw.githubusercontent.com/felixs855/diehl-platinum-inverter-mqtt/main}"
_cs_boot="${COMMUNITY_SCRIPTS_CORE_DIR:-$(dirname "${BASH_SOURCE[0]}")/../../core}/core/build.func"
source "$_cs_boot" 2>/dev/null || source <(curl -fsSL "${COMMUNITY_SCRIPTS_CORE_URL:-https://raw.githubusercontent.com/community-scripts/core/main}/core/build.func")

# License: MIT
# Diehl Platinum inverter (RS-232) -> MQTT / Home Assistant
# Built on the script framework of community-scripts.org

APP="Diehl-Platinum"
var_tags="${var_tags:-solar;mqtt}"
var_cpu="${var_cpu:-1}"
var_ram="${var_ram:-256}"
var_disk="${var_disk:-2}"
var_os="${var_os:-debian}"
var_version="${var_version:-13}"
var_unprivileged="${var_unprivileged:-1}"
var_arm64="${var_arm64:-yes}"

header_info "$APP"
variables
color
catch_errors

function update_script() {
  header_info
  check_container_storage
  check_container_resources

  if [[ ! -f /opt/diehl-platinum/diehl_platinum_mqtt.py ]]; then
    msg_error "No ${APP} Installation Found!"
    exit
  fi

  msg_info "Updating ${APP}"
  local src
  if ! src="$(_cs_fetch_text "install/diehl-platinum/diehl_platinum_mqtt.py")" || [[ -z "$src" ]]; then
    msg_error "Could not fetch install/diehl-platinum/diehl_platinum_mqtt.py from ${COMMUNITY_SCRIPTS_URL}"
    exit
  fi
  printf '%s\n' "$src" >/opt/diehl-platinum/diehl_platinum_mqtt.py
  $STD /opt/diehl-platinum/.venv/bin/pip install --upgrade "pyserial>=3.5" "paho-mqtt>=2.0"
  systemctl restart diehl-platinum
  msg_ok "Updated successfully!"
  exit
}

# ----------------------------------------------------------------- Proxmox host only
# (on "update" this script also runs inside the container; this part is skipped there)
if command -v pveversion >/dev/null 2>&1; then
  # Find the serial adapter (FTDI); DIEHL_SERIAL_DEV overrides the detection
  if [[ -z "${DIEHL_SERIAL_DEV:-}" ]]; then
    mapfile -t _ftdi < <(ls /dev/serial/by-id/usb-FTDI* 2>/dev/null || true)
    if [[ ${#_ftdi[@]} -eq 1 ]]; then
      DIEHL_SERIAL_DEV="${_ftdi[0]}"
    elif [[ ${#_ftdi[@]} -gt 1 ]]; then
      DIEHL_SERIAL_DEV="$(prompt_select "Which adapter is connected to the inverter?" 1 120 "${_ftdi[@]}")"
    else
      msg_warn "No FTDI adapter found in /dev/serial/by-id - creating the container without adapter"
    fi
    unset _ftdi
  fi
  [[ -n "${DIEHL_SERIAL_DEV:-}" ]] && msg_ok "Serial adapter: ${DIEHL_SERIAL_DEV}"

  # MQTT access: preset environment variables take precedence, otherwise ask
  MQTT_HOST="${MQTT_HOST:-$(prompt_input "MQTT broker (IP or host name)" "" 300)}"
  if [[ -z "$MQTT_HOST" ]]; then
    msg_error "MQTT_HOST is required (e.g. set MQTT_HOST=192.168.1.10 before running the script)"
    exit 1
  fi
  MQTT_PORT="${MQTT_PORT:-$(prompt_input "MQTT port" "1883" 120)}"
  MQTT_USER="${MQTT_USER-$(prompt_input "MQTT user (empty = no authentication)" "" 120)}"
  if [[ -n "$MQTT_USER" ]]; then
    MQTT_PASS="${MQTT_PASS-$(prompt_password "MQTT password" "" 120)}"
  fi
  POLL_INTERVAL="${POLL_INTERVAL:-10}"

  # Hand the service source code over to the install script in the container
  DIEHL_MQTT_SRC="$(_cs_fetch_text "install/diehl-platinum/diehl_platinum_mqtt.py")" ||
    { msg_error "install/diehl-platinum/diehl_platinum_mqtt.py not found"; exit 1; }
  export DIEHL_SERIAL_DEV MQTT_HOST MQTT_PORT MQTT_USER MQTT_PASS POLL_INTERVAL DIEHL_MQTT_SRC
fi

start
build_container
description

# ----------------------------------------------------------------- pass the adapter through
if [[ -n "${DIEHL_SERIAL_DEV:-}" ]]; then
  msg_info "Passing ${DIEHL_SERIAL_DEV} through to container ${CTID}"
  pct set "$CTID" --dev0 "${DIEHL_SERIAL_DEV},gid=20,mode=0660" >/dev/null
  pct reboot "$CTID" >/dev/null
  for _ in $(seq 1 30); do
    pct exec "$CTID" -- true >/dev/null 2>&1 && break
    sleep 2
  done
  # dev0 normally creates the device under the same path; otherwise use the real path
  if ! pct exec "$CTID" -- test -c "$DIEHL_SERIAL_DEV"; then
    _real="$(readlink -f "$DIEHL_SERIAL_DEV")"
    pct exec "$CTID" -- sed -i "s|^DIEHL_PORT=.*|DIEHL_PORT=\"${_real}\"|" /etc/diehl-platinum.env
    pct exec "$CTID" -- systemctl restart diehl-platinum
  fi
  msg_ok "Adapter passed through"
else
  msg_warn "Pass the adapter through later: pct set ${CTID} --dev0 /dev/serial/by-id/<adapter>,gid=20,mode=0660"
fi

msg_ok "Completed successfully!\n"
echo -e "${CREATING}${GN}${APP} setup has been successfully initialized!${CL}"
echo -e "${INFO}${YW}Follow the service log:${CL}"
echo -e "${TAB}${BGN}pct exec ${CTID} -- journalctl -u diehl-platinum -f${CL}"
