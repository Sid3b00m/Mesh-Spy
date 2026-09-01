#!/usr/bin/env bash
# Mesh-Spy installer.
#
# Supports Raspberry Pi OS, Debian, Ubuntu, Mint, Fedora, RHEL/Rocky/Alma,
# Arch, openSUSE, Alpine, Void and Gentoo, with systemd or OpenRC auto-start.
# On an unrecognised system it still sets up the Python app and tells you which
# system packages to install by hand.
#
# Useful overrides:
#   INSTALL_PACKAGES=0    skip system packages, set up the Python app only
#   ENABLE_SERVICE=0      do not install an auto-start service
#   SERVICE_USER=name     run the service as this user (default: the sudo caller)
#   SERIAL_GROUP=name     group granted serial access (default: autodetected)
#   RECREATE_VENV=1       delete and rebuild .venv from scratch
#
# Windows is not handled here; it has install.ps1. Both delegate the Python
# setup to bootstrap.py, which is also usable on its own on any platform.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$(cd "$(dirname "$0")" && pwd)}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
ENABLE_SERVICE="${ENABLE_SERVICE:-1}"
INSTALL_PACKAGES="${INSTALL_PACKAGES:-1}"

log() { echo "[Mesh-Spy] $*"; }
warn() { echo "[Mesh-Spy] warning: $*" >&2; }

log "Install directory: $INSTALL_DIR"
cd "$INSTALL_DIR"

# --------------------------------------------------------------------------
# System packages
# --------------------------------------------------------------------------

detect_pm() {
  local pm
  for pm in apt-get dnf yum pacman zypper apk xbps-install emerge; do
    if command -v "$pm" >/dev/null 2>&1; then
      echo "$pm"
      return 0
    fi
  done
  echo "none"
}

PM="$(detect_pm)"

# Optional packages go in one at a time: BLE stacks in particular are absent or
# renamed on several distros, and one miss must not abort the whole run.
install_optional() {
  local p
  for p in "$@"; do
    case "$PM" in
      apt-get) DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$p" >/dev/null 2>&1 || warn "optional package unavailable: $p" ;;
      dnf)     dnf install -y -q "$p" >/dev/null 2>&1 || warn "optional package unavailable: $p" ;;
      yum)     yum install -y -q "$p" >/dev/null 2>&1 || warn "optional package unavailable: $p" ;;
      pacman)  pacman -S --noconfirm --needed "$p" >/dev/null 2>&1 || warn "optional package unavailable: $p (try the AUR)" ;;
      zypper)  zypper --non-interactive install "$p" >/dev/null 2>&1 || warn "optional package unavailable: $p" ;;
      apk)     apk add --no-cache "$p" >/dev/null 2>&1 || warn "optional package unavailable: $p" ;;
      xbps-install) xbps-install -y "$p" >/dev/null 2>&1 || warn "optional package unavailable: $p" ;;
      emerge)  emerge --quiet --noreplace "$p" >/dev/null 2>&1 || warn "optional package unavailable: $p" ;;
    esac
  done
}

# BLE is optional: a serial or TCP radio needs none of it, and pulling bluez
# onto a headless box that will only ever use USB is rude.
install_system_packages() {
  case "$PM" in
    apt-get)
      log "Detected apt (Debian / Ubuntu / Raspberry Pi OS). Installing packages..."
      apt-get update -qq
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        python3 python3-venv python3-pip git
      install_optional bluez libglib2.0-0 libusb-1.0-0
      ;;
    dnf|yum)
      log "Detected $PM (Fedora / RHEL / Rocky / Alma). Installing packages..."
      "$PM" install -y -q python3 python3-pip git
      install_optional bluez glib2 libusbx
      ;;
    pacman)
      log "Detected pacman (Arch / Manjaro / EndeavourOS). Installing packages..."
      pacman -Sy --noconfirm --needed python python-pip git
      install_optional bluez bluez-utils glib2 libusb
      ;;
    zypper)
      log "Detected zypper (openSUSE). Installing packages..."
      zypper --non-interactive refresh
      zypper --non-interactive install python3 python3-pip git
      install_optional bluez glib2 libusb-1_0-0
      ;;
    apk)
      log "Detected apk (Alpine). Installing packages..."
      apk add --no-cache python3 py3-pip git
      # musl wheels are not published for every dependency, so pip may have to
      # compile and needs a toolchain present.
      install_optional build-base python3-dev linux-headers libffi-dev
      install_optional bluez glib libusb
      ;;
    xbps-install)
      log "Detected xbps (Void). Installing packages..."
      xbps-install -Sy python3 python3-pip git
      install_optional bluez glib libusb
      ;;
    emerge)
      log "Detected emerge (Gentoo). Installing packages..."
      warn "Gentoo builds from source - this step can take a long time."
      emerge --quiet --noreplace dev-lang/python dev-vcs/git
      install_optional net-wireless/bluez dev-libs/glib
      ;;
    none)
      warn "No supported package manager found."
      warn "Tried: apt-get, dnf, yum, pacman, zypper, apk, xbps-install, emerge."
      warn "Install these yourself, then re-run: python3 3.9+ with venv and pip,"
      warn "git, and (only if you want BLE radios) bluez."
      ;;
  esac
}

if [[ "$INSTALL_PACKAGES" == "1" && $EUID -eq 0 ]]; then
  install_system_packages
elif [[ "$INSTALL_PACKAGES" == "1" && $EUID -ne 0 ]]; then
  log "Skipping system packages (not root). Re-run as: sudo ./install.sh"
fi

# --------------------------------------------------------------------------
# Python application
# --------------------------------------------------------------------------

# Dropping privileges via sudo assumes root is itself listed in sudoers. Debian,
# Fedora and Arch all grant that; Alpine does not, so as root sudo answers
# "root is not in the sudoers file" and the venv is never created. runuser and
# su ask the kernel directly and need no sudo policy at all.
run_as_user() {
  if [[ $EUID -ne 0 ]]; then
    "$@"
  elif command -v runuser >/dev/null 2>&1; then
    runuser -u "$SERVICE_USER" -- "$@"
  else
    su -s /bin/sh "$SERVICE_USER" -c "$(printf '%q ' "$@")"
  fi
}

PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  warn "Python 3 not found. Install Python 3.9 or newer and re-run this script."
  exit 1
fi

# meshtastic requires >=3.9,<3.15. Warn rather than refuse, since a distro may
# ship a usable interpreter under an unexpected name.
if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if (3, 9) <= sys.version_info < (3, 15) else 1)'; then
  warn "$("$PYTHON_BIN" --version 2>&1) is outside the range meshtastic supports (3.9 - 3.14)."
fi

# The virtualenv, the dependencies, config/config.yaml and data/ are all
# bootstrap.py's job. It is the same code Windows and macOS run, so there is
# one place where "what a working install looks like" is defined rather than
# three that drift apart.
log "Setting up the virtual environment and dependencies..."
BOOTSTRAP_ARGS=(--setup-only)
if [[ "${RECREATE_VENV:-0}" == "1" ]]; then
  BOOTSTRAP_ARGS+=(--recreate)
fi
run_as_user "$PYTHON_BIN" "$INSTALL_DIR/bootstrap.py" "${BOOTSTRAP_ARGS[@]}"

# Under SELinux everything below /home is labelled user_home_t, which systemd is
# not permitted to execute, so the unit dies with 203/EXEC ("Permission denied")
# and retries forever without the app ever starting. Relabelling the interpreter
# as bin_t is what makes ExecStart work from a home directory.
if [[ $EUID -eq 0 ]] && command -v selinuxenabled >/dev/null 2>&1 && selinuxenabled; then
  log "SELinux is enabled; labelling the virtualenv so systemd can execute it..."
  venv_labelled=0
  if command -v semanage >/dev/null 2>&1 && command -v restorecon >/dev/null 2>&1; then
    # Preferred: a file context rule survives a full filesystem relabel.
    if semanage fcontext -a -t bin_t "$INSTALL_DIR/.venv/bin(/.*)?" 2>/dev/null &&
      restorecon -R "$INSTALL_DIR/.venv/bin" 2>/dev/null; then
      venv_labelled=1
    fi
  fi
  if [[ "$venv_labelled" -eq 0 ]]; then
    # Fallback: applies immediately but is lost on a relabel.
    chcon -R -t bin_t "$INSTALL_DIR/.venv/bin" 2>/dev/null ||
      warn "could not relabel $INSTALL_DIR/.venv/bin - the service may fail with 203/EXEC"
  fi
fi

if [[ $EUID -eq 0 ]]; then
  # A trailing colon means "the user's primary group", which is not reliably
  # named after the user.
  chown -R "$SERVICE_USER:" "$INSTALL_DIR/data" "$INSTALL_DIR/config"
fi

chmod +x "$INSTALL_DIR/run.sh" "$INSTALL_DIR/install.sh" "$INSTALL_DIR/bootstrap.py"

# --------------------------------------------------------------------------
# Serial access
# --------------------------------------------------------------------------

# A USB Meshtastic or MeshCore node appears as /dev/ttyUSB* or /dev/ttyACM*,
# owned by root and a group that differs by distro: dialout on Debian, Fedora
# and Alpine, uucp on Arch and openSUSE. Guessing wrong means the service gets
# "permission denied" on the port, so detect it rather than hardcoding.
group_exists() {
  getent group "$1" >/dev/null 2>&1 || grep -q "^$1:" /etc/group 2>/dev/null
}

detect_serial_group() {
  local g
  for g in dialout uucp; do
    if group_exists "$g"; then
      echo "$g"
      return 0
    fi
  done
  echo "dialout"
}

SERIAL_GROUP="${SERIAL_GROUP:-$(detect_serial_group)}"

setup_serial_access() {
  if ! group_exists "$SERIAL_GROUP"; then
    log "Creating the $SERIAL_GROUP group for serial access..."
    groupadd -f "$SERIAL_GROUP" 2>/dev/null ||
      addgroup "$SERIAL_GROUP" 2>/dev/null ||
      warn "could not create the $SERIAL_GROUP group"
  fi

  if group_exists "$SERIAL_GROUP"; then
    log "Adding $SERVICE_USER to $SERIAL_GROUP..."
    usermod -aG "$SERIAL_GROUP" "$SERVICE_USER" 2>/dev/null ||
      addgroup "$SERVICE_USER" "$SERIAL_GROUP" 2>/dev/null ||
      warn "could not add $SERVICE_USER to $SERIAL_GROUP; serial access may need root"
  fi

  local rules_src="$INSTALL_DIR/scripts/60-mesh-spy-serial.rules"
  if [[ -d /etc/udev/rules.d && -f "$rules_src" ]]; then
    log "Installing serial udev rules (group $SERIAL_GROUP)..."
    sed "s|GROUP=\"dialout\"|GROUP=\"$SERIAL_GROUP\"|g" \
      "$rules_src" > /etc/udev/rules.d/60-mesh-spy-serial.rules
    udevadm control --reload-rules >/dev/null 2>&1 || true
    udevadm trigger >/dev/null 2>&1 || true
  fi

  log "Group changes apply at next login; replug the node to pick up the rules."
}

if [[ $EUID -eq 0 ]]; then
  setup_serial_access
fi

# --------------------------------------------------------------------------
# Auto-start (systemd or OpenRC)
# --------------------------------------------------------------------------

install_systemd_service() {
  log "Installing systemd service..."
  sed "s|User=pi|User=$SERVICE_USER|g; \
       s|SupplementaryGroups=dialout|SupplementaryGroups=$SERIAL_GROUP|g; \
       s|/home/pi/Mesh-Spy|$INSTALL_DIR|g" \
    "$INSTALL_DIR/scripts/mesh-spy.service" > /etc/systemd/system/mesh-spy.service
  systemctl daemon-reload
  systemctl enable mesh-spy.service
  systemctl restart mesh-spy.service || true
  log "Check status: sudo systemctl status mesh-spy"
}

install_openrc_service() {
  local src="$INSTALL_DIR/scripts/mesh-spy.openrc"
  if [[ ! -f "$src" ]]; then
    warn "scripts/mesh-spy.openrc missing; skipping the auto-start service."
    return 0
  fi
  log "Installing OpenRC service..."
  sed "s|command_user=\"pi\"|command_user=\"$SERVICE_USER\"|g; \
       s|/home/pi/Mesh-Spy|$INSTALL_DIR|g" \
    "$src" > /etc/init.d/mesh-spy
  chmod +x /etc/init.d/mesh-spy
  rc-update add mesh-spy default >/dev/null 2>&1 || warn "could not enable the service at boot"
  rc-service mesh-spy restart >/dev/null 2>&1 || true
  log "Check status: rc-service mesh-spy status"
}

if [[ "$ENABLE_SERVICE" == "1" && $EUID -eq 0 ]]; then
  if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
    install_systemd_service
  elif command -v rc-update >/dev/null 2>&1; then
    install_openrc_service
  else
    warn "No systemd or OpenRC found; skipping the auto-start service."
    warn "Start manually with ./run.sh, or wire it into your init system"
    warn "(runit, s6, dinit) using scripts/mesh-spy.openrc as a reference."
  fi
fi

# busybox hostname (Alpine, Void's minimal images) has no -I.
IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
if [[ -z "$IP" ]] && command -v ip >/dev/null 2>&1; then
  IP=$(ip -4 route get 1.1.1.1 2>/dev/null |
    awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' || true)
fi

log "Done."
echo
# Prints the ports it found plus the config block to paste, which is the step
# that otherwise stalls a first install.
run_as_user "$INSTALL_DIR/.venv/bin/python" -m app.main --list-ports || true
echo
log "Local: http://127.0.0.1:8090"
log "No radio configured yet, so the console shows a simulated network."
log "  Add one under mesh.radios in $INSTALL_DIR/config/config.yaml"
log "  Re-list ports any time with: ./run.sh --list-ports"
log "Transmitting is off by default (mesh.read_only: true) and needs auth."
log "For LAN access: set server.host=0.0.0.0, enable auth, then open http://${IP:-<host-ip>}:8090"
log "Manual start: cd $INSTALL_DIR && ./run.sh"
log "Full guide: $INSTALL_DIR/docs/INSTALL.md"
