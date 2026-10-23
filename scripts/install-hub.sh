#!/usr/bin/env bash
# Rook hub installer: from a fresh Linux machine to a running hub in one command.
#
#   curl -fsSL https://raw.githubusercontent.com/Bake-Ware/rook/master/scripts/install-hub.sh | bash
#   curl -fsSL .../install-hub.sh | bash -s -- --user --version v0.2.0 --yes
#
# It installs the prerequisites (python3 + venv, curl, ca-certificates), Rook
# into a virtualenv from the release wheel, and the prebuilt relay
# (telesthete-hub) from the telesthete release; generates the band key,
# dashboard password and MCP token into the data directory (mode 600); writes
# and starts systemd units; waits until the hub answers; and prints the
# dashboard URL and password. Re-running it upgrades in place and keeps the
# data and secrets. See docs/operations/install.md.
#
# Options (all optional):
#   --system            system service: /opt/rook, data in /var/lib/rook, a
#                       dedicated `rook` user (default when root or sudo works)
#   --user              per-user service: ~/.local/share/rook-hub, systemd --user
#   --version TAG       Rook release to install (default: the latest release)
#   --relay-version TAG telesthete release for the relay (default: the one the
#                       Rook release names, else the latest)
#   --data-dir DIR      hub state directory
#   --bind ADDR         address the hub listens on (default 0.0.0.0: your LAN)
#   --public-host HOST  address workers and browsers use (default: this
#                       machine's LAN IP, or ADDR when it is a specific IP)
#   --dashboard-port N  the hub: dashboard, /mcp and /band (7005)
#   --mcp-port N        also serve the MCP server alone on its old port (off;
#                       8765 for a release from before the single-process hub)
#   --relay-port N      the UDP relay (7474)
#   --yes, -y           don't ask for confirmation
#   --uninstall         stop and remove the services and the program; keeps
#                       the data directory unless --purge is given too
#   --purge             with --uninstall: also delete the data (band keys!)
#   --no-systemd        start the processes in the background instead of as
#                       systemd units (containers, CI); pid files and logs go
#                       to DATA_DIR/run and DATA_DIR/logs
#   --relay-bin PATH    install this telesthete-hub binary instead of downloading
#   --package SPEC      pip requirement to install instead of the release wheel
#                       (a wheel path or URL, a source directory, ...)
#   --git-ref REF       install Rook from git at REF (needs git; the fallback
#                       when there is no release wheel)
#   --python PATH       Python 3.11+ to build the virtualenv with
#   --no-apk            don't fetch the Android app from the release (by default
#                       the hub serves the release's app at /apk, checked
#                       against the release's SHA256SUMS; with --git-ref or
#                       --package, the latest release's)
set -euo pipefail

# Everything runs from main(), called on the last line: a download cut short
# by the network (curl | bash) defines a partial function and runs nothing.
main() {
ROOK_REPO="${ROOK_REPO:-Bake-Ware/rook}"
TELESTHETE_REPO="${TELESTHETE_REPO:-Bake-Ware/telesthete}"
GITHUB="${ROOK_GITHUB:-https://github.com}"

say() { printf '[rook] %s\n' "$*"; }
# /dev/tty can exist and be rw yet not open (no controlling terminal: cron,
# CI, ssh without -t): try it.
have_tty() { { true > /dev/tty; } 2>/dev/null; }
warn() { printf '[rook] WARNING: %s\n' "$*" >&2; }
die() { printf '[rook] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  if [ -f "$0" ] && grep -q 'Rook hub installer' "$0" 2>/dev/null; then
    sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
  else
    echo "Options: see the header of https://github.com/Bake-Ware/rook/blob/master/scripts/install-hub.sh"
  fi
}

# ---- options -----------------------------------------------------------------
MODE="" VERSION="" RELAY_VERSION="" DATA_DIR="" BIND="" PUBLIC_HOST=""
DASHBOARD_PORT="" MCP_PORT="" RELAY_PORT="" ASSUME_YES="" UNINSTALL="" PURGE=""
NO_SYSTEMD="" RELAY_BIN="" PACKAGE="" GIT_REF="" PYTHON_BIN="" NO_APK=""
# Which network options this run was given: on a re-run only those change hub.env.
BIND_SET="" PUBLIC_SET="" DPORT_SET="" MPORT_SET="" RPORT_SET=""

need_arg() { if [ $# -lt 2 ] || [ -z "$2" ]; then die "$1 needs a value"; fi; }
while [ $# -gt 0 ]; do
  case "$1" in
    --system) MODE=system ;;
    --user) MODE=user ;;
    --version) need_arg "$@"; VERSION="$2"; shift ;;
    --relay-version) need_arg "$@"; RELAY_VERSION="$2"; shift ;;
    --data-dir) need_arg "$@"; DATA_DIR="$2"; shift ;;
    --bind) need_arg "$@"; BIND="$2"; BIND_SET=1; shift ;;
    --public-host) need_arg "$@"; PUBLIC_HOST="$2"; PUBLIC_SET=1; shift ;;
    --dashboard-port) need_arg "$@"; DASHBOARD_PORT="$2"; DPORT_SET=1; shift ;;
    --mcp-port) need_arg "$@"; MCP_PORT="$2"; MPORT_SET=1; shift ;;
    --relay-port) need_arg "$@"; RELAY_PORT="$2"; RPORT_SET=1; shift ;;
    -y|--yes) ASSUME_YES=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --purge) PURGE=1 ;;
    --no-systemd) NO_SYSTEMD=1 ;;
    --relay-bin) need_arg "$@"; RELAY_BIN="$2"; shift ;;
    --package) need_arg "$@"; PACKAGE="$2"; shift ;;
    --git-ref) need_arg "$@"; GIT_REF="$2"; shift ;;
    --python) need_arg "$@"; PYTHON_BIN="$2"; shift ;;
    --no-apk) NO_APK=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
  shift
done
[ -z "$PURGE" ] || [ -n "$UNINSTALL" ] || die "--purge only goes with --uninstall"
for p in "$DASHBOARD_PORT" "${MCP_PORT/#off/}" "$RELAY_PORT"; do
  [ -z "$p" ] || [[ "$p" =~ ^[0-9]+$ ]] || die "ports are numbers, not '$p'"
done
if [ -n "$RELAY_BIN" ]; then
  [ -x "$RELAY_BIN" ] || die "--relay-bin $RELAY_BIN is not an executable file"
  RELAY_BIN="$(cd "$(dirname "$RELAY_BIN")" && pwd)/$(basename "$RELAY_BIN")"
fi
if [ -n "$PACKAGE" ] && [ -e "$PACKAGE" ]; then
  PACKAGE="$(cd "$(dirname "$PACKAGE")" && pwd)/$(basename "$PACKAGE")"
fi

[ "$(uname -s)" = Linux ] || die "this installer sets up a hub on Linux. On macOS or Windows run \
the hub with Docker, or from source (see the README's Quickstart)."

# ---- privileges and mode -------------------------------------------------------
SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi
can_root() { [ "$(id -u)" -eq 0 ] || [ -n "$SUDO" ]; }
as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else $SUDO "$@"; fi; }
# Program and data files: root's in --system mode, the caller's own (never
# sudo) in --user mode.
as_app() { if [ "$MODE" = system ]; then as_root "$@"; else "$@"; fi; }

SYSTEM_APP_DIR="/opt/rook"
USER_APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/rook-hub"

if [ -z "$MODE" ] && [ -n "$UNINSTALL" ]; then
  # Uninstall whatever is there (an earlier --uninstall may have left the data).
  if [ -f "$SYSTEM_APP_DIR/install.env" ]; then MODE=system
  elif [ -f "$USER_APP_DIR/install.env" ]; then MODE=user
  elif [ -d "$SYSTEM_APP_DIR" ] || [ -d /var/lib/rook ]; then MODE=system
  elif [ -d "$USER_APP_DIR" ]; then MODE=user
  else die "no Rook hub installed by this script was found (no $SYSTEM_APP_DIR or $USER_APP_DIR)"; fi
fi
if [ -z "$MODE" ]; then
  if can_root; then MODE=system; else MODE=user; fi
fi
if [ "$MODE" = system ] && ! can_root; then
  die "--system needs root: run as root, install sudo, or use --user"
fi

if [ "$MODE" = system ]; then
  APP_DIR="$SYSTEM_APP_DIR"
  DEFAULT_DATA_DIR="/var/lib/rook"
  UNIT_DIR="/etc/systemd/system"
  SVC_USER="rook"
else
  APP_DIR="$USER_APP_DIR"
  DEFAULT_DATA_DIR="$APP_DIR/data"
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  SVC_USER="$(id -un)"
fi
VENV="$APP_DIR/venv"
INSTALL_ENV="$APP_DIR/install.env"

# A previous install's choices are the defaults for this run (re-run = upgrade).
PREV_DATA_DIR="" PREV_SYSTEMD=""
if [ -f "$INSTALL_ENV" ]; then
  PREV_DATA_DIR="$(sed -n 's/^DATA_DIR=//p' "$INSTALL_ENV" | head -n1)"
  PREV_SYSTEMD="$(sed -n 's/^SYSTEMD=//p' "$INSTALL_ENV" | head -n1)"
fi
DATA_DIR="${DATA_DIR:-${PREV_DATA_DIR:-$DEFAULT_DATA_DIR}}"
case "$DATA_DIR" in /*) ;; *) DATA_DIR="$PWD/$DATA_DIR" ;; esac
HUB_ENV="$DATA_DIR/hub.env"
SECRETS_ENV="$DATA_DIR/secrets.env"

USE_SYSTEMD=1
if [ -n "$NO_SYSTEMD" ]; then USE_SYSTEMD=0
elif [ -n "$UNINSTALL" ] && [ "$PREV_SYSTEMD" = 0 ]; then USE_SYSTEMD=0; fi

if [ "$MODE" = system ]; then SYSTEMCTL=(as_root systemctl); else SYSTEMCTL=(systemctl --user); fi

# ---- the service layout ----------------------------------------------------------
# "merged": the relay + the hub (`rook-hub`: dashboard and MCP server in one
#           process, one port). What every current Rook installs.
# "split":  the relay + rook-dashboard + rook-mcp, for a --version from before
#           the merge (no rook-hub entry point). A re-run removes whichever
#           layout's units are not in use.
ALL_SERVICES="relay hub dashboard mcp"

detect_layout() {
  if [ -x "$VENV/bin/rook-hub" ]; then echo merged; else echo split; fi
}
layout_services() {
  case "$1" in
    merged) echo "relay hub" ;;
    *) echo "relay dashboard mcp" ;;
  esac
}
unit_name() { echo "rook-$1"; }
service_desc() {
  case "$1" in
    relay) echo "Rook hub: band relay (telesthete-hub)" ;;
    dashboard) echo "Rook hub: dashboard" ;;
    mcp) echo "Rook hub: MCP server" ;;
    hub) echo "Rook hub: dashboard and MCP server" ;;
  esac
}
service_cmd() {
  case "$1" in
    relay) echo "$APP_DIR/bin/telesthete-hub" ;;
    dashboard) echo "$VENV/bin/rook-dashboard" ;;
    mcp) echo "$VENV/bin/rook-mcp" ;;
    hub) echo "$VENV/bin/rook-hub" ;;
  esac
}
# The relay needs only its HUB_* settings; the Python services get the secrets.
service_env_files() {
  case "$1" in
    relay) echo "$HUB_ENV" ;;
    *) echo "$HUB_ENV $SECRETS_ENV" ;;
  esac
}

# ---- uninstall -------------------------------------------------------------------
# Is pid still the service we started? A stale pid file can name a pid the
# kernel has since given to something else: never signal that.
is_our_process() {   # is_our_process pid svc
  local exe cmdline
  exe="$(service_cmd "$2")"; exe="${exe%% *}"
  [ -r "/proc/$1/cmdline" ] || return 1
  cmdline="$(tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null || true)"
  case " $cmdline " in *" $exe "*) return 0 ;; *) return 1 ;; esac
}
stop_bg() {
  local svc pidfile pid
  for svc in $ALL_SERVICES; do
    pidfile="$DATA_DIR/run/$svc.pid"
    as_app test -f "$pidfile" || continue
    pid="$(as_app cat "$pidfile" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && is_our_process "$pid" "$svc"; then
      as_app kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 50); do is_our_process "$pid" "$svc" || break; sleep 0.2; done
      ! is_our_process "$pid" "$svc" || as_app kill -9 "$pid" 2>/dev/null || true
    fi
    as_app rm -f "$pidfile"
  done
}

remove_units() {   # remove_units svc...: stop, disable and delete these units
  local svc u changed=""
  for svc in "$@"; do
    u="$(unit_name "$svc").service"
    [ -f "$UNIT_DIR/$u" ] || continue
    "${SYSTEMCTL[@]}" disable --now "$u" >/dev/null 2>&1 || true
    if [ "$MODE" = system ]; then as_root rm -f "$UNIT_DIR/$u"; else rm -f "$UNIT_DIR/$u"; fi
    say "removed $u"
    changed=1
  done
  [ -z "$changed" ] || "${SYSTEMCTL[@]}" daemon-reload || true
}

# Refuse to rm -rf anything that isn't plainly a hub data directory.
safe_data_dir() {
  local d="${DATA_DIR%/}"
  case "$d" in
    ""|/|/root|/home|/var|/var/lib|/opt|/etc|/usr|/tmp|"${HOME%/}"|"$(dirname "${HOME%/}")")
      die "refusing to delete '$DATA_DIR': not a hub data directory" ;;
  esac
  as_app test -f "$d/hub.env" || as_app test -f "$d/secrets.env" || [ -z "$(as_app ls -A "$d")" ] \
    || die "refusing to delete $DATA_DIR: it has no hub.env or secrets.env (pass the right --data-dir)"
}

if [ -n "$UNINSTALL" ]; then
  if [ -n "$PURGE" ] && as_app test -d "$DATA_DIR"; then
    safe_data_dir
    if [ -z "$ASSUME_YES" ]; then
      if ! have_tty; then
        die "--purge deletes the band keys in $DATA_DIR: add --yes to confirm"
      fi
      printf '[rook] Delete %s: band keys, passwords and history? Workers will need new pairing codes. [y/N] ' "$DATA_DIR" > /dev/tty
      read -r answer < /dev/tty || answer=""
      case "$answer" in [yY]*) ;; *) die "cancelled; nothing was removed" ;; esac
    fi
  fi
  say "uninstalling the Rook hub ($MODE)"
  stop_bg
  if command -v systemctl >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    remove_units $ALL_SERVICES
  fi
  as_app rm -rf "$VENV" "$APP_DIR/bin" "$INSTALL_ENV"
  if [ -n "$PURGE" ] && as_app test -d "$DATA_DIR"; then
    as_app rm -rf "$DATA_DIR"
    say "deleted $DATA_DIR (band keys, passwords, history)"
    if [ "$MODE" = system ] && id rook >/dev/null 2>&1; then
      as_root userdel rook 2>/dev/null || true
    fi
  elif [ -d "$DATA_DIR" ]; then
    say "kept your data in $DATA_DIR (band keys, passwords, history): re-running the"
    say "installer reuses it; --uninstall --purge deletes it"
  fi
  as_app rmdir "$APP_DIR" 2>/dev/null || true
  say "done"
  exit 0
fi

# ---- defaults that need the network config -----------------------------------------
# On a re-run, what hub.env says is the default for anything not passed again.
existing() { if as_app test -f "$HUB_ENV"; then as_app sed -n "s/^$1=//p" "$HUB_ENV" | head -n1; fi; }
[ -n "$BIND_SET" ] || BIND="$(existing ROOK_BIND)"
[ -n "$DPORT_SET" ] || DASHBOARD_PORT="$(existing ROOK_PORT)"
[ -n "$MPORT_SET" ] || { MCP_PORT="$(existing ROOK_MCP_BIND)"; MCP_PORT="${MCP_PORT##*:}"; }   # may be "off"
[ -n "$RPORT_SET" ] || { RELAY_PORT="$(existing HUB_BIND)"; RELAY_PORT="${RELAY_PORT##*:}"; }
if [ -z "$PUBLIC_SET" ] && [ -z "$BIND_SET" ]; then
  PUBLIC_HOST="$(existing ROOK_DOMAIN | sed -E 's#^[a-z]+://##; s#:[0-9]+/?$##')"
fi
BIND="${BIND:-0.0.0.0}"
DASHBOARD_PORT="${DASHBOARD_PORT:-7005}"
# MCP_PORT's default depends on the layout (set once Rook is installed).
RELAY_PORT="${RELAY_PORT:-7474}"
# Docker bridges and VPN overlays (tailscale's 100.64.0.0/10) are not where
# other machines on the LAN reach this one.
skip_ip() {
  case "$1" in
    127.*|169.254.*|172.17.*|172.18.*|172.19.*|172.20.*|fe80:*|::1) return 0 ;;
    100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*) return 0 ;;
    *) return 1 ;;
  esac
}
private_ip() {   # RFC 1918, CGNAT, loopback, link-local, IPv6 ULA
  case "$1" in
    10.*|192.168.*|127.*|169.254.*|fe80:*|fc*|fd*|::1) return 0 ;;
    172.1[6-9].*|172.2[0-9].*|172.3[01].*) return 0 ;;
    100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*) return 0 ;;
    *) return 1 ;;
  esac
}
lan_ip() {
  local ip="" a
  # The source address of the default route is the machine's LAN address.
  if command -v ip >/dev/null 2>&1; then
    ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')" || true
  fi
  if [ -z "$ip" ] || skip_ip "$ip"; then
    ip=""
    for a in $(hostname -I 2>/dev/null || true); do
      skip_ip "$a" && continue
      case "$a" in *:*) [ -n "$ip" ] || ip="[$a]" ;; *) ip="$a"; break ;; esac
    done
  fi
  echo "${ip:-127.0.0.1}"
}
if [ -z "$PUBLIC_HOST" ]; then
  case "$BIND" in 0.0.0.0|::|'[::]') PUBLIC_HOST="$(lan_ip)" ;; *) PUBLIC_HOST="$BIND" ;; esac
fi
case "$BIND" in 0.0.0.0|::|'[::]') LOCAL_HOST=127.0.0.1 ;; *) LOCAL_HOST="$BIND" ;; esac
EXPOSED=""
case "$BIND" in
  0.0.0.0|::|'[::]')
    for a in $(hostname -I 2>/dev/null || true); do private_ip "$a" || EXPOSED="$EXPOSED $a"; done ;;
  *) private_ip "$BIND" || EXPOSED=" $BIND" ;;
esac

if [ "$USE_SYSTEMD" = 1 ]; then
  command -v systemctl >/dev/null 2>&1 || die "systemd not found: re-run with --no-systemd (or use Docker)"
  if [ "$MODE" = system ]; then
    [ -d /run/systemd/system ] || die "systemd is not running here: re-run with --no-systemd"
  else
    systemctl --user show-environment >/dev/null 2>&1 || die "no systemd user session \
(common over su or in containers): log in as $SVC_USER directly, use --system, or --no-systemd"
  fi
fi

# ---- platform ------------------------------------------------------------------------
case "$(uname -m)" in
  x86_64|amd64) ARCH=x86_64 ;;
  aarch64|arm64) ARCH=aarch64 ;;
  armv7l|armv7) ARCH=armv7 ;;
  *) ARCH="" ;;   # no prebuilt relay: cargo fallback
esac
PKG=""
for m in apt-get dnf pacman zypper; do
  if command -v "$m" >/dev/null 2>&1; then PKG="$m"; break; fi
done

pkg_install() {   # pkg_install pkg...: with the detected package manager
  [ $# -gt 0 ] || return 0
  can_root || die "missing $*: install them (as root), or re-run with sudo available"
  say "installing $* ($PKG)"
  case "$PKG" in
    apt-get) as_root env DEBIAN_FRONTEND=noninteractive apt-get update -qq \
             && as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$@" >/dev/null ;;
    dnf) as_root dnf install -y -q "$@" ;;
    pacman) as_root pacman -Sy --needed --noconfirm "$@" >/dev/null ;;
    zypper) as_root zypper -n -q install "$@" ;;
    *) die "no supported package manager (apt-get, dnf, pacman, zypper): install $* yourself and re-run" ;;
  esac
}

# ---- confirm -------------------------------------------------------------------------
if [ -n "$PACKAGE" ]; then SOURCE_PLAN="$PACKAGE"
elif [ -n "$GIT_REF" ]; then SOURCE_PLAN="git $GIT_REF"
else SOURCE_PLAN="release ${VERSION:-latest}"; fi
cat <<EOF
[rook] Rook hub installer
[rook]   mode         $MODE$([ "$USE_SYSTEMD" = 1 ] || echo " (no systemd: background processes)")
[rook]   program      $APP_DIR
[rook]   data         $DATA_DIR
[rook]   dashboard    http://$PUBLIC_HOST:$DASHBOARD_PORT   (listening on $BIND)
[rook]   rook         $SOURCE_PLAN
EOF
if [ -n "$EXPOSED" ]; then
  warn "this machine has a public address (${EXPOSED# }) and the hub will listen on it over"
  warn "plain HTTP. Anyone who can reach it can try the dashboard password. Use a firewall,"
  warn "or --bind 127.0.0.1 behind an HTTPS proxy (docs/operations/install.md)."
fi
if [ -z "$ASSUME_YES" ] && have_tty; then
  printf '[rook] Continue? [Y/n] ' > /dev/tty
  read -r answer < /dev/tty || answer=""
  case "$answer" in [nN]*) die "cancelled" ;; esac
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ---- prerequisites ----------------------------------------------------------------------
need=()
command -v curl >/dev/null 2>&1 || need+=(curl)
[ -e /etc/ssl/certs/ca-certificates.crt ] || [ -e /etc/pki/tls/certs/ca-bundle.crt ] \
  || [ -e /etc/ssl/ca-bundle.pem ] || need+=(ca-certificates)
command -v tar >/dev/null 2>&1 || need+=(tar)
pkg_install "${need[@]}"

py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; }
find_python() {
  local p
  if [ -n "$PYTHON_BIN" ]; then
    py_ok "$PYTHON_BIN" && { echo "$PYTHON_BIN"; return 0; }
    return 1
  fi
  for p in python3 python3.13 python3.12 python3.11; do
    if command -v "$p" >/dev/null 2>&1 && py_ok "$p"; then command -v "$p"; return 0; fi
  done
  return 1
}
PY="$(find_python || true)"
if [ -z "$PY" ]; then
  [ -z "$PYTHON_BIN" ] || die "--python $PYTHON_BIN is not Python 3.11 or newer"
  case "$PKG" in
    apt-get) pkg_install python3 python3-venv ;;
    dnf) pkg_install python3 ;;
    pacman) pkg_install python ;;
    zypper) pkg_install python311 ;;
    *) die "Python 3.11+ is required: install it and re-run (or pass --python PATH)" ;;
  esac
  PY="$(find_python || true)"
  [ -n "$PY" ] || die "Rook needs Python 3.11 or newer and this system's python3 is older. \
Install a newer Python (e.g. Debian 12+, Ubuntu 24.04+, Fedora, Arch), pass --python PATH, or use Docker."
fi
# Debian/Ubuntu split venv/ensurepip into python3-venv: check by making one.
if ! "$PY" -m venv "$TMP/venv-check" >/dev/null 2>&1; then
  pyver="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
  case "$PKG" in
    apt-get) pkg_install python3-venv || true
             "$PY" -m venv "$TMP/venv-check2" >/dev/null 2>&1 || pkg_install "python${pyver}-venv" ;;
    zypper) pkg_install "python${pyver/./}-pip" || true ;;
    *) ;;
  esac
  "$PY" -m venv "$TMP/venv-check3" >/dev/null 2>&1 || die "$PY cannot create a virtualenv (python3-venv missing?)"
fi
say "using $("$PY" -V 2>&1) at $PY"

# ---- directories and the service user ------------------------------------------------------
if [ "$MODE" = system ]; then
  if ! id "$SVC_USER" >/dev/null 2>&1; then
    say "creating system user '$SVC_USER'"
    nologin="$(command -v nologin || echo /usr/sbin/nologin)"
    as_root useradd --system --home-dir "$DATA_DIR" --no-create-home --shell "$nologin" "$SVC_USER"
  fi
  as_root mkdir -p "$APP_DIR/bin" "$DATA_DIR"
  as_root chown "$SVC_USER:" "$DATA_DIR"
  as_root chmod 700 "$DATA_DIR"
  run_as_svc() { if [ "$(id -un)" = "$SVC_USER" ]; then "$@"; else as_root runuser -u "$SVC_USER" -- "$@" 2>/dev/null || as_root su -s /bin/sh "$SVC_USER" -c "$(printf '%q ' "$@")"; fi; }
else
  mkdir -p "$APP_DIR/bin" "$DATA_DIR"
  chmod 700 "$DATA_DIR"
  run_as_svc() { "$@"; }
fi

# ---- Rook into a virtualenv -----------------------------------------------------------------
if [ ! -x "$VENV/bin/python" ] || ! py_ok "$VENV/bin/python"; then
  say "creating virtualenv $VENV"
  as_app rm -rf "$VENV"
  as_app "$PY" -m venv "$VENV"
fi
pip_install() { as_app env PIP_DISABLE_PIP_VERSION_CHECK=1 "$VENV/bin/pip" install -q --upgrade "$@"; }

ensure_git() {
  command -v git >/dev/null 2>&1 && return 0
  case "$PKG" in apt-get|dnf|pacman|zypper) pkg_install git ;; *) die "git is needed to install from source" ;; esac
}

# The latest release's tag, from the releases/latest redirect (no API token, no
# rate limit). Returns 1 when the repository has no release (GitHub redirects
# to /releases), 2 when it couldn't tell (network, proxy).
latest_tag() {   # latest_tag owner/repo
  local url
  url="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$GITHUB/$1/releases/latest" 2>/dev/null)" || return 2
  case "$url" in
    */releases/tag/*) echo "${url##*/}" ;;
    */releases|*/releases/) return 1 ;;
    *) return 2 ;;
  esac
}
pep440() { echo "${1#v}" | sed -E 's/-(a|b|rc)([0-9]+)$/\1\2/'; }
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else "$PY" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"; fi
}

SPEC="" SOURCE_DESC=""
if [ -n "$PACKAGE" ]; then
  SPEC="$PACKAGE"; SOURCE_DESC="$PACKAGE"
elif [ -n "$GIT_REF" ]; then
  ensure_git
  SPEC="rook @ git+$GITHUB/$ROOK_REPO@$GIT_REF"; SOURCE_DESC="git $GIT_REF"
else
  TAG="$VERSION"
  if [ -z "$TAG" ] || [ "$TAG" = latest ]; then
    rc=0
    TAG="$(latest_tag "$ROOK_REPO")" || rc=$?
    case "$rc" in
      0) ;;
      1) TAG="" ;;
      *) die "could not reach $GITHUB/$ROOK_REPO to find the latest release (network or proxy?)" ;;
    esac
  fi
  if [ -n "$TAG" ]; then
    # A release exists: install exactly it, or stop. Never fall back to
    # building something else from source.
    whl="rook-$(pep440 "$TAG")-py3-none-any.whl"
    base="$GITHUB/$ROOK_REPO/releases/download/$TAG"
    say "downloading Rook $TAG"
    curl -fsSL --retry 3 -o "$TMP/$whl" "$base/$whl" \
      || die "could not download $base/$whl: is $TAG a Rook release? (network or proxy?)"
    curl -fsSL --retry 3 -o "$TMP/SHA256SUMS" "$base/SHA256SUMS" \
      || die "could not download $base/SHA256SUMS to check the wheel"
    want="$(awk -v f="$whl" '$2 == f || $2 == "*" f {print $1}' "$TMP/SHA256SUMS" | head -n1)"
    got="$(sha256_of "$TMP/$whl")"
    if [ -z "$want" ] || [ "$want" != "$got" ]; then
      die "checksum mismatch for $whl (SHA256SUMS: ${want:-none}, file: $got)"
    fi
    SPEC="$TMP/$whl"; SOURCE_DESC="release $TAG"
  else
    warn "$ROOK_REPO has no release yet: installing from source (master) with git instead"
    ensure_git
    SPEC="rook @ git+$GITHUB/$ROOK_REPO@master"; SOURCE_DESC="git master"
  fi
fi
say "installing Rook ($SOURCE_DESC) into $VENV; this takes a minute"
case "$SPEC" in
  "$TMP"/*) as_app chmod 644 "$SPEC" ;;
esac
if ! pip_install "$SPEC"; then
  case "$SPEC" in
    *git+*) die "pip could not install $SPEC" ;;
  esac
  die "pip could not install $SPEC (re-run with --git-ref master to build from source)"
fi
ROOK_VERSION="$("$VENV/bin/python" -c 'import rook; print(rook.__version__)')"
ROOK_BUILD="$("$VENV/bin/python" -c 'import rook._release as r; print(r.NAME)' 2>/dev/null || true)"
say "Rook $ROOK_VERSION${ROOK_BUILD:+ (build $ROOK_BUILD)} installed"

# ---- the relay -------------------------------------------------------------------------------
RELAY_DEST="$APP_DIR/bin/telesthete-hub"
RELAY_STAMP="$APP_DIR/bin/telesthete-hub.version"
# The relay this Rook pins (rook/_release.py: RELAY and the sha256 of each
# archive), checked against that committed hash rather than a checksum file
# from the same release page.
PINNED_RELAY="$("$VENV/bin/python" -c 'import rook._release as r; print(getattr(r, "RELAY", ""))' 2>/dev/null || true)"
RELAY_VERSION="${RELAY_VERSION:-${PINNED_RELAY:-latest}}"
pinned_relay_sha() {   # pinned_relay_sha arch
  [ "$RELAY_VERSION" = "$PINNED_RELAY" ] || return 0
  "$VENV/bin/python" -c 'import sys, rook._release as r; print(getattr(r, "RELAY_SHA256", {}).get(sys.argv[1], ""))' "$1" 2>/dev/null || true
}

fetch_relay() {   # download + verify the prebuilt relay into $TMP/relay/telesthete-hub
  [ -n "$ARCH" ] || { say "no prebuilt relay for $(uname -m)"; return 1; }
  local asset="telesthete-hub-$ARCH-linux.tar.gz" base want got
  if [ "$RELAY_VERSION" = latest ]; then
    RELAY_VERSION="$(latest_tag "$TELESTHETE_REPO")" || { say "no telesthete release found"; RELAY_VERSION=latest; return 1; }
  fi
  if [ -x "$RELAY_DEST" ] && [ "$(cat "$RELAY_STAMP" 2>/dev/null || true)" = "$RELAY_VERSION $ARCH" ]; then
    say "relay telesthete $RELAY_VERSION already installed"
    return 0
  fi
  base="$GITHUB/$TELESTHETE_REPO/releases/download/$RELAY_VERSION"
  say "downloading the relay (telesthete $RELAY_VERSION, $ARCH)"
  curl -fsSL --retry 3 -o "$TMP/$asset" "$base/$asset" || { say "could not download $base/$asset"; return 1; }
  want="$(pinned_relay_sha "$ARCH")"
  if [ -z "$want" ]; then
    warn "no pinned sha256 for telesthete $RELAY_VERSION ($ARCH): checking the release's own .sha256"
    curl -fsSL --retry 3 -o "$TMP/$asset.sha256" "$base/$asset.sha256" || { say "no checksum file for $asset"; return 1; }
    want="$(awk '{print $1}' "$TMP/$asset.sha256")"
  fi
  got="$(sha256_of "$TMP/$asset")"
  if [ -z "$want" ] || [ "$want" != "$got" ]; then
    die "checksum mismatch for $asset (expected $want, got $got)"
  fi
  mkdir -p "$TMP/relay"
  tar -xzf "$TMP/$asset" -C "$TMP/relay" telesthete-hub
  as_app install -m 0755 "$TMP/relay/telesthete-hub" "$RELAY_DEST"
  echo "$RELAY_VERSION $ARCH" | as_app tee "$RELAY_STAMP" >/dev/null
}

build_relay() {   # the fallback: compile it with an existing Rust toolchain
  local cargo
  cargo="$(command -v cargo || true)"
  [ -n "$cargo" ] || [ ! -x "$HOME/.cargo/bin/cargo" ] || cargo="$HOME/.cargo/bin/cargo"
  if [ -z "$cargo" ]; then
    die "could not install a prebuilt relay. Either install Rust (https://rustup.rs) and a C \
compiler and re-run to build it from source, or build telesthete-hub yourself \
(cd rust && cargo build --release -p telesthitium in $GITHUB/$TELESTHETE_REPO) and re-run with --relay-bin PATH."
  fi
  # Build the pinned release tag; before that tag exists, the telesthete
  # commit this Rook was installed with (a source install records it), so the
  # relay matches the transport.
  local rev built=""
  rev="$("$VENV/bin/python" -c 'import json, importlib.metadata as m
d = json.loads(m.distribution("telesthete").read_text("direct_url.json") or "{}")
print(d.get("vcs_info", {}).get("commit_id", ""))' 2>/dev/null || true)"
  say "building the relay from source with cargo; this takes several minutes"
  case "$RELAY_VERSION" in latest|"") ;; *)
    if "$cargo" install --locked --root "$TMP/cargo" --git "$GITHUB/$TELESTHETE_REPO" --tag "$RELAY_VERSION" telesthitium; then
      built="tag $RELAY_VERSION"
    fi ;;
  esac
  if [ -z "$built" ] && [ -n "$rev" ]; then
    warn "building the relay at telesthete $rev (the commit this Rook's transport came from)"
    "$cargo" install --locked --root "$TMP/cargo" --git "$GITHUB/$TELESTHETE_REPO" --rev "$rev" telesthitium \
      && built="rev $rev"
  fi
  if [ -z "$built" ]; then
    warn "building the relay from the default branch of $TELESTHETE_REPO"
    "$cargo" install --locked --root "$TMP/cargo" --git "$GITHUB/$TELESTHETE_REPO" telesthitium \
      && built="default branch"
  fi
  [ -n "$built" ] || die "cargo could not build telesthete-hub"
  as_app install -m 0755 "$TMP/cargo/bin/telesthete-hub" "$RELAY_DEST"
  echo "source $built" | as_app tee "$RELAY_STAMP" >/dev/null
}

if [ -n "$RELAY_BIN" ]; then
  as_app install -m 0755 "$RELAY_BIN" "$RELAY_DEST"
  echo "local $RELAY_BIN" | as_app tee "$RELAY_STAMP" >/dev/null
  say "relay: installed $RELAY_BIN"
else
  fetch_relay || build_relay
fi

# ---- the Android app ---------------------------------------------------------------------------
# The release's app (a generic build: no server address or band key inside) is
# published the way Manage > Updates publishes one, into DATA_DIR/updates/apk,
# so the hub serves it at /apk and /apk.json from the first start. Each file is
# checked against the release's SHA256SUMS, and the APK against its manifest.
# Never replaces a newer app the operator published since.
APK_DIR="$DATA_DIR/updates/apk"
fetch_apk() {   # fetch_apk TAG
  local base="$GITHUB/$ROOK_REPO/releases/download/$1" f want got
  curl -fsSL --retry 3 -o "$TMP/apk-SHA256SUMS" "$base/SHA256SUMS" 2>/dev/null || return 1
  for f in rook-worker.apk rook-worker-apk.json; do
    want="$(awk -v f="$f" '$2 == f || $2 == "*" f {print $1}' "$TMP/apk-SHA256SUMS" | head -n1)"
    [ -n "$want" ] || { say "release $1 has no Android app"; return 1; }
    curl -fsSL --retry 3 -o "$TMP/$f" "$base/$f" || { warn "could not download $base/$f"; return 1; }
    got="$(sha256_of "$TMP/$f")"
    [ "$want" = "$got" ] || { warn "checksum mismatch for $f (SHA256SUMS: $want, file: $got)"; return 1; }
  done
  "$VENV/bin/python" - "$TMP/rook-worker.apk" "$TMP/rook-worker-apk.json" "$APK_DIR/rook-worker-apk.json" <<'PY' || return 1
import hashlib, json, os, sys
apk, man, cur = sys.argv[1:4]
m = json.load(open(man))
h = hashlib.sha256(open(apk, "rb").read()).hexdigest()
if (m.get("package") != "systems.bake.rook" or type(m.get("version_code")) is not int
        or m.get("sha256") != h or m.get("size") != os.path.getsize(apk)):
    sys.exit("[rook] WARNING: the release's APK does not match its manifest; not published")
try:
    old = json.load(open(cur))
except (OSError, ValueError):
    old = {}
if isinstance(old.get("version_code"), int) and old["version_code"] >= m["version_code"]:
    print(f"[rook] app: keeping the published {old.get('version_name')} ({old['version_code']})")
    sys.exit(3)
PY
}
if [ -z "$NO_APK" ]; then
  APK_TAG="${TAG:-}"
  [ -n "$APK_TAG" ] || APK_TAG="$(latest_tag "$ROOK_REPO" 2>/dev/null || true)"
  rc=1
  if [ -n "$APK_TAG" ]; then rc=0; fetch_apk "$APK_TAG" || rc=$?; fi
  if [ "$rc" = 0 ]; then
    as_app mkdir -p "$APK_DIR"
    # The manifest is the approval: withdraw it, swap the file, then publish it.
    as_app rm -f "$APK_DIR/rook-worker-apk.json"
    as_app install -m 0644 "$TMP/rook-worker.apk" "$APK_DIR/rook-worker.apk.new"
    as_app mv -f "$APK_DIR/rook-worker.apk.new" "$APK_DIR/rook-worker.apk"
    as_app install -m 0644 "$TMP/rook-worker-apk.json" "$APK_DIR/rook-worker-apk.json"
    [ "$MODE" != system ] || as_root chown -R "$SVC_USER:" "$DATA_DIR/updates"
    say "app: $(sed -n 's/.*"version_name": *"\([^"]*\)".*/\1/p' "$TMP/rook-worker-apk.json") from $APK_TAG, served at /apk"
  elif [ "$rc" != 3 ]; then
    say "no Android app to serve yet: /apk stays empty until you publish one (Manage > Updates)"
  fi
fi

# ---- configuration and secrets -----------------------------------------------------------------
# hub.env: settings (written once; flags given on a re-run update their keys).
# secrets.env: band key, dashboard password, MCP token (generated once, never rewritten).
write_private() {   # write_private PATH < content: mode 600, owned by the service user
  as_app tee "$1.tmp" >/dev/null
  as_app chmod 600 "$1.tmp"
  [ "$MODE" != system ] || as_root chown "$SVC_USER:" "$1.tmp"
  as_app mv -f "$1.tmp" "$1"
}
read_kv() { as_app sed -n "s/^$2=//p" "$1" 2>/dev/null | head -n1; }
set_kv() {   # set_kv FILE KEY VALUE
  local cur
  cur="$(as_app cat "$1")"
  if printf '%s\n' "$cur" | grep -q "^$2="; then
    printf '%s\n' "$cur" | awk -v k="$2" -v v="$3" 'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' | write_private "$1"
  else
    printf '%s\n%s=%s\n' "$cur" "$2" "$3" | write_private "$1"
  fi
}

hub_settings() {
  local mcp_bind="off" hosts="$PUBLIC_HOST:$DASHBOARD_PORT"
  if [ "$MCP_PORT" != off ]; then mcp_bind="$BIND:$MCP_PORT"; hosts="$hosts,$PUBLIC_HOST:$MCP_PORT"; fi
  cat <<EOF
# Rook hub settings, written by scripts/install-hub.sh. Edit, then restart the
# services (see docs/operations/install.md). Secrets are in secrets.env.
ROOK_DATA_DIR=$DATA_DIR
EOF
  if [ "$LAYOUT" = merged ]; then
    cat <<EOF
# The hub (rook-hub): dashboard, /mcp and the /band worker bridge, one port.
ROOK_BIND=$BIND
ROOK_PORT=$DASHBOARD_PORT
ROOK_DOMAIN=http://$PUBLIC_HOST:$DASHBOARD_PORT
ROOK_HUB_PUBLIC=$PUBLIC_HOST:$DASHBOARD_PORT
ROOK_HUB_HOST=127.0.0.1
ROOK_HUB_PORT=$RELAY_PORT
# Also serve the MCP server alone on its old port (host:port, or off).
ROOK_MCP_BIND=$mcp_bind
ROOK_ALLOWED_HOSTS=$hosts
EOF
  else
    local mcp_local="http://$LOCAL_HOST:$MCP_PORT"
    cat <<EOF
# Dashboard (rook-dashboard)
ROOK_BIND=$BIND
ROOK_PORT=$DASHBOARD_PORT
ROOK_DOMAIN=http://$PUBLIC_HOST:$DASHBOARD_PORT
ROOK_HUB_PUBLIC=$PUBLIC_HOST:$MCP_PORT
ROOK_HUB_HOST=127.0.0.1
ROOK_HUB_PORT=$RELAY_PORT
# MCP server (rook-mcp)
ROOK_HUB=127.0.0.1:$RELAY_PORT
ROOK_MCP_BIND=$mcp_bind
ROOK_ALLOWED_HOSTS=$PUBLIC_HOST:$MCP_PORT
# Where the dashboard reaches the MCP server's account APIs (it assumes
# 127.0.0.1:8765 otherwise, which a specific --bind or --mcp-port moves).
ROOK_TOKEN_ADMIN_URL=$mcp_local/tokens/account-api
ROOK_KNOWLEDGE_ADMIN_URL=$mcp_local/knowledge/account-api
ROOK_GUIDANCE_ADMIN_URL=$mcp_local/guidance/account-api
ROOK_VAULT_ADMIN_URL=$mcp_local/vault/account-api
ROOK_SETTINGS_ADMIN_URL=$mcp_local/settings/account-api
ROOK_JOBS_ADMIN_URL=$mcp_local/jobs/account-api
ROOK_MANUAL_ADMIN_URL=$mcp_local/manual/account-api
ROOK_MASK_URL=$mcp_local/internal/mask
ROOK_WATCHDOG_MCP_URL=$mcp_local
EOF
  fi
  cat <<EOF
# Relay (telesthete-hub). Rook peers keep alive every 20s; the relay's default
# idle TTL is 15s.
HUB_BIND=$BIND:$RELAY_PORT
HUB_PEER_TTL_SECS=60
HUB_PRUNE_SECS=10
RUST_LOG=warn
EOF
}

# The layout decides the settings, so it comes first.
LAYOUT="$(detect_layout)"
SERVICES="$(layout_services "$LAYOUT")"
if [ -z "$MCP_PORT" ]; then
  # One port for a merged hub; the old MCP port only when asked (--mcp-port).
  if [ "$LAYOUT" = merged ]; then MCP_PORT=off; else MCP_PORT=8765; fi
fi
[ "$LAYOUT" = merged ] || [ "$MCP_PORT" != off ] || MCP_PORT=8765

if ! as_app test -f "$HUB_ENV"; then
  hub_settings | write_private "$HUB_ENV"
  say "wrote settings to $HUB_ENV"
else
  # Only what this run was explicitly told changes; hand edits survive.
  hub_settings | grep -E '^[A-Z_]+=' > "$TMP/settings"
  changed=""
  while IFS='=' read -r key value; do
    case "$key" in
      ROOK_BIND) [ -n "$BIND_SET" ] || continue ;;
      ROOK_MCP_BIND) [ -n "$BIND_SET$MPORT_SET" ] || continue ;;
      HUB_BIND) [ -n "$BIND_SET$RPORT_SET" ] || continue ;;
      ROOK_PORT) [ -n "$DPORT_SET" ] || continue ;;
      ROOK_DOMAIN|ROOK_HUB_PUBLIC|ROOK_ALLOWED_HOSTS) [ -n "$BIND_SET$PUBLIC_SET$DPORT_SET$MPORT_SET" ] || continue ;;
      ROOK_HUB_PORT|ROOK_HUB) [ -n "$RPORT_SET" ] || continue ;;
      ROOK_*_ADMIN_URL|ROOK_MASK_URL|ROOK_WATCHDOG_MCP_URL) [ -n "$BIND_SET$MPORT_SET" ] || continue ;;
      ROOK_DATA_DIR) ;;
      *) continue ;;
    esac
    if [ "$(read_kv "$HUB_ENV" "$key")" != "$value" ]; then set_kv "$HUB_ENV" "$key" "$value"; changed=1; fi
  done < "$TMP/settings"
  say "kept settings in $HUB_ENV${changed:+ (updated from the options given)}"
fi
# Effective values for the rest of this run come from the file.
DASHBOARD_PORT="$(read_kv "$HUB_ENV" ROOK_PORT)"
MCP_PORT="$(read_kv "$HUB_ENV" ROOK_MCP_BIND)"; MCP_PORT="${MCP_PORT##*:}"
DOMAIN="$(read_kv "$HUB_ENV" ROOK_DOMAIN)"
RELAY_PORT="$(read_kv "$HUB_ENV" HUB_BIND)"; RELAY_PORT="${RELAY_PORT##*:}"
BIND_NOW="$(read_kv "$HUB_ENV" ROOK_BIND)"
case "$BIND_NOW" in 0.0.0.0|::|'[::]'|"") LOCAL_HOST=127.0.0.1 ;; *) LOCAL_HOST="$BIND_NOW" ;; esac

if ! as_app test -f "$SECRETS_ENV"; then printf '' | write_private "$SECRETS_ENV"; fi
gen_secret() {   # gen_secret KEY: generate it unless secrets.env already has it
  [ -z "$(read_kv "$SECRETS_ENV" "$1")" ] || return 0
  local value
  case "$1" in
    ROOK_BAND_PSK) value="$("$VENV/bin/python" -m rook.remote.psk)" ;;
    ROOK_WEB_PASS) value="$("$VENV/bin/python" -c 'import secrets; print(secrets.token_urlsafe(12))')" ;;
    *) value="$("$VENV/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))')" ;;
  esac
  set_kv "$SECRETS_ENV" "$1" "$value"
  say "generated $1"
}
gen_secret ROOK_BAND_PSK
gen_secret ROOK_WEB_PASS
gen_secret ROOK_MCP_STATIC_TOKEN

# ---- services ----------------------------------------------------------------------------------
say "service layout: $LAYOUT ($SERVICES)"

unit_text() {   # unit_text svc
  local svc="$1" deps="" envf
  # A user manager has no network-online.target; the system one does.
  [ "$MODE" != system ] || deps="network-online.target"
  [ "$svc" = relay ] || deps="${deps:+$deps }rook-relay.service"
  echo "# Written by scripts/install-hub.sh; re-running it rewrites this file."
  echo "[Unit]"
  echo "Description=$(service_desc "$svc")"
  if [ -n "$deps" ]; then
    echo "After=$deps"
    echo "Wants=$deps"
  fi
  echo
  echo "[Service]"
  echo "Type=simple"
  for envf in $(service_env_files "$svc"); do echo "EnvironmentFile=$envf"; done
  echo "Environment=PYTHONUNBUFFERED=1"
  # The relay needs no files (and ProtectHome may hide a data dir under /home).
  if [ "$svc" = relay ]; then echo "WorkingDirectory=/"; else echo "WorkingDirectory=$DATA_DIR"; fi
  echo "ExecStart=$(service_cmd "$svc")"
  echo "Restart=on-failure"
  echo "RestartSec=3"
  if [ "$MODE" = system ]; then
    echo "User=$SVC_USER"
    echo "Group=$SVC_USER"
    echo "NoNewPrivileges=true"
    echo "PrivateTmp=true"
    if [ "$svc" != relay ]; then
      echo "ProtectSystem=full"
    else
      # The relay holds no keys and only forwards packets: lock it down hard.
      echo "ProtectSystem=strict"
      echo "ProtectHome=true"
      echo "PrivateDevices=true"
      echo "ProtectKernelTunables=true"
      echo "ProtectKernelModules=true"
      echo "ProtectControlGroups=true"
      echo "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX"
      echo "RestrictNamespaces=true"
      echo "LockPersonality=true"
      echo "MemoryDenyWriteExecute=true"
      echo "SystemCallArchitectures=native"
      echo "CapabilityBoundingSet="
    fi
  fi
  echo
  echo "[Install]"
  if [ "$MODE" = system ]; then echo "WantedBy=multi-user.target"; else echo "WantedBy=default.target"; fi
}

write_units() {   # write_units layout: install this layout's units, drop the other's
  local svc u stale=""
  for svc in $ALL_SERVICES; do
    case " $SERVICES " in *" $svc "*) ;; *) stale="$stale $svc" ;; esac
  done
  # shellcheck disable=SC2086
  remove_units $stale
  as_app mkdir -p "$UNIT_DIR"
  for svc in $SERVICES; do
    u="$(unit_name "$svc").service"
    unit_text "$svc" > "$TMP/$u"
    as_app install -m 0644 "$TMP/$u" "$UNIT_DIR/$u"
  done
  "${SYSTEMCTL[@]}" daemon-reload
  for svc in $SERVICES; do
    u="$(unit_name "$svc").service"
    "${SYSTEMCTL[@]}" enable --quiet "$u"
    "${SYSTEMCTL[@]}" restart "$u"
  done
}

start_bg() {   # the --no-systemd layout: same commands, background processes
  local svc envf cmd
  stop_bg
  as_app mkdir -p "$DATA_DIR/run" "$DATA_DIR/logs"
  [ "$MODE" != system ] || as_root chown "$SVC_USER:" "$DATA_DIR/run" "$DATA_DIR/logs"
  for svc in $SERVICES; do
    cmd="cd $(printf '%q' "$DATA_DIR") && set -a"
    for envf in $(service_env_files "$svc"); do cmd="$cmd && . $(printf '%q' "$envf")"; done
    cmd="$cmd && set +a && PYTHONUNBUFFERED=1 exec $(service_cmd "$svc")"
    # nohup: keep running after this script and its terminal are gone.
    run_as_svc sh -c "nohup sh -c $(printf '%q' "$cmd") >> $(printf '%q' "$DATA_DIR/logs/$svc.log") 2>&1 < /dev/null & echo \$! > $(printf '%q' "$DATA_DIR/run/$svc.pid")"
  done
}

if [ "$USE_SYSTEMD" = 1 ]; then
  [ "$PREV_SYSTEMD" != 0 ] || stop_bg
  write_units "$LAYOUT"
else
  if command -v systemctl >/dev/null 2>&1 && [ "$PREV_SYSTEMD" = 1 ]; then
    # shellcheck disable=SC2086
    remove_units $ALL_SERVICES
  fi
  start_bg
fi

{
  echo "# Written by scripts/install-hub.sh: what --uninstall and re-runs read."
  echo "MODE=$MODE"
  echo "DATA_DIR=$DATA_DIR"
  echo "SYSTEMD=$USE_SYSTEMD"
  echo "LAYOUT=$LAYOUT"
  echo "ROOK_VERSION=$ROOK_VERSION"
  echo "ROOK_BUILD=$ROOK_BUILD"
  echo "RELAY=$(cat "$RELAY_STAMP" 2>/dev/null || true)"
} > "$TMP/install.env"
as_app install -m 0644 "$TMP/install.env" "$INSTALL_ENV"

# ---- wait for health ------------------------------------------------------------------------------
service_logs() {   # the last lines of a service's log
  if [ "$USE_SYSTEMD" = 1 ]; then
    if [ "$MODE" = system ]; then as_root journalctl -u "$(unit_name "$1")" -n 25 --no-pager 2>/dev/null || true
    else journalctl --user -u "$(unit_name "$1")" -n 25 --no-pager 2>/dev/null || true; fi
  else
    as_app tail -n 25 "$DATA_DIR/logs/$1.log" 2>/dev/null || true
  fi
}
service_running() {
  if [ "$USE_SYSTEMD" = 1 ]; then "${SYSTEMCTL[@]}" is-active --quiet "$(unit_name "$1").service"
  else
    local pid; pid="$(as_app cat "$DATA_DIR/run/$1.pid" 2>/dev/null || true)"
    [ -n "$pid" ] && as_app kill -0 "$pid" 2>/dev/null
  fi
}
# The bearer header goes to curl from a file (mode 600 in $TMP), never in
# argv where any local user can read it from ps.
( umask 077; printf 'Authorization: Bearer %s\n' "$(read_kv "$SECRETS_ENV" ROOK_MCP_STATIC_TOKEN)" > "$TMP/mcp-auth" )
http_ok() { curl -fsS --noproxy '*' -m 5 "$@" >/dev/null 2>&1; }
healthy() {   # healthy svc
  case "$1" in
    relay) service_running relay ;;
    dashboard) http_ok "http://$LOCAL_HOST:$DASHBOARD_PORT/health" ;;
    # rook-hub serves the dashboard and /healthz (the MCP side) on one port.
    hub) http_ok "http://$LOCAL_HOST:$DASHBOARD_PORT/health" \
           && http_ok -H "@$TMP/mcp-auth" "http://$LOCAL_HOST:$DASHBOARD_PORT/healthz" ;;
    mcp) http_ok -H "@$TMP/mcp-auth" "http://$LOCAL_HOST:$MCP_PORT/healthz" ;;
  esac
}

say "waiting for the hub to come up"
for svc in $SERVICES; do
  ok=""
  for _ in $(seq 1 90); do
    if healthy "$svc"; then ok=1; break; fi
    service_running "$svc" || { sleep 1; service_running "$svc" || break; }
    sleep 1
  done
  if [ -z "$ok" ]; then
    warn "$(unit_name "$svc") did not become healthy. Its last log lines:"
    service_logs "$svc" >&2
    die "the hub did not start; fix the error above and re-run (your data and secrets are kept)"
  fi
  say "$(unit_name "$svc") is up"
done

# ---- done -------------------------------------------------------------------------------------------
WEB_PASS="$(read_kv "$SECRETS_ENV" ROOK_WEB_PASS)"
if [ "$LAYOUT" = merged ]; then MCP_URL="http://$PUBLIC_HOST:$DASHBOARD_PORT/mcp"
else MCP_URL="http://$PUBLIC_HOST:$MCP_PORT/mcp"; fi
TCP_PORTS="$DASHBOARD_PORT"
[ "$MCP_PORT" = off ] || TCP_PORTS="$DASHBOARD_PORT and $MCP_PORT"
if [ "$USE_SYSTEMD" = 1 ]; then
  if [ "$MODE" = system ]; then
    MANAGE="sudo systemctl status 'rook-*'    (logs: sudo journalctl -u 'rook-*' -f)"
  else
    MANAGE="systemctl --user status 'rook-*'    (logs: journalctl --user -u 'rook-*' -f)"
  fi
else
  MANAGE="logs in $DATA_DIR/logs/  (after a reboot, run this installer again to start it)"
fi
PORT_WORD="port"; [ "$TCP_PORTS" = "$DASHBOARD_PORT" ] || PORT_WORD="ports"
# Plain words first: the person reading this may never have run a server.
# The agent (MCP) details and upkeep come after the next steps.
cat <<EOF

[rook] Your Rook hub is running.

  Dashboard   $DOMAIN
  Password    $WEB_PASS
              (keep it safe; it is also saved in $SECRETS_ENV)

Next steps
  1. Open $DOMAIN in a web browser and enter the password above.
  2. Go to "Install a worker" and click "Get a pairing code".
  3. Run the command it shows on each computer you want to add.

  Other computers reach this one on TCP $PORT_WORD $TCP_PORTS. If they can't
  connect, allow that in this machine's firewall.

Connecting AI agents (Claude Code and others)
  Address       $MCP_URL
  Access token  ROOK_MCP_STATIC_TOKEN in $SECRETS_ENV

Looking after the hub
  Status        $MANAGE
  Settings      $HUB_ENV
  Upgrade       run this installer again (your data and passwords are kept)
  Uninstall     curl -fsSL https://raw.githubusercontent.com/$ROOK_REPO/master/scripts/install-hub.sh | bash -s -- --uninstall
  Advanced      workers can also join the relay directly on UDP $RELAY_PORT
                (rook worker --hub <this machine's address>:$RELAY_PORT)
EOF
if [ "$MODE" = user ] && [ "$USE_SYSTEMD" = 1 ] && command -v loginctl >/dev/null 2>&1; then
  if [ "$(loginctl show-user "$SVC_USER" -p Linger --value 2>/dev/null || true)" != yes ]; then
    cat <<EOF

  Note: the hub stops when you log out. To keep it running, run this once:
      sudo loginctl enable-linger $SVC_USER
EOF
  fi
fi
}

main "$@"
