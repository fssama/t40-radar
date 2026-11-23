#!/usr/bin/env bash
set -euo pipefail

repo=$(cd -- "$(dirname -- "$0")/.." && pwd)
app=/opt/t40-radar
config=/etc/t40-radar
units=/etc/systemd/system
rules=/etc/udev/rules.d/81-t40-radar.rules
port=${T40_HTTP_PORT:-8000}

usage() {
    cat <<'EOF'
Usage:
  deploy_ubuntu.sh monitor [sim|udp]
  deploy_ubuntu.sh ar1021x DRIVER_DIR FIRMWARE_DIR COUNTRY RADAR_MAC [FC_CLIENT]
  deploy_ubuntu.sh status [ar1021x]

Install on a systemd Ubuntu host. Use sudo for installation.
HTTP listens on 127.0.0.1 (port T40_HTTP_PORT, default 8000).
For Secure Boot on the AR1021x host, pass an enrolled MOK as
T40_SIGN_KEY (private key) and T40_SIGN_CERT (DER certificate).
FC_CLIENT is the trusted, standalone radar_min_client.py. It is optional.
EOF
}

fail() {
    printf 't40-radar: %s\n' "$*" >&2
    exit 1
}

need_packages() {
    DEBIAN_FRONTEND=noninteractive apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
}

monitor() {
    local source=$1
    if ! command -v python3 >/dev/null || ! command -v curl >/dev/null; then
        need_packages python3 curl
    fi
    install -d -m 0755 "$app" "$config"
    install -m 0644 "$repo/run.py" "$app/run.py"
    (
        cd "$repo"
        find backend frontend -type f ! -path '*/__pycache__/*' -print0 |
            while IFS= read -r -d '' file; do
                install -D -m 0644 "$file" "$app/$file"
            done
    )
    printf 'T40_SOURCE=%s\nT40_PORT=%s\n' "$source" "$port" > "$config/monitor.env"
    install -m 0644 "$repo/deploy/t40-radar.service" "$units/t40-radar.service"
    systemctl daemon-reload
    systemctl enable t40-radar.service
    systemctl restart t40-radar.service
    curl --fail --silent --show-error --retry 5 --retry-connrefused --max-time 3 \
        "http://127.0.0.1:$port/api/state" | python3 -m json.tool
}

install_ar1021x() {
    local driver=$1 firmware=$2 country=$3 mac=$4 fc=$5
    local kernel dest module ko info expected boot_state iface instance drop file
    local -a missing devices
    [[ -f "$driver/Makefile" && -d "$driver/ath6kl" ]] || fail "invalid patched driver directory"
    [[ -s "$firmware/bdata.bin" ]] || fail "missing AR6004 hw3.0 bdata.bin"
    [[ -s "$firmware/fw-5.bin" || -s "$firmware/fw.ram.bin" ]] || fail "missing AR6004 hw3.0 firmware binary"
    [[ -z $fc || ( -s "$fc" && -f "$fc" ) ]] || fail "FC_CLIENT does not exist"
    [[ -z ${T40_SIGN_KEY:-} && -z ${T40_SIGN_CERT:-} ]] ||
        [[ -r ${T40_SIGN_KEY:-/nonexistent} && -r ${T40_SIGN_CERT:-/nonexistent} ]] ||
        fail "both T40_SIGN_KEY and T40_SIGN_CERT are required for signing"

    kernel=$(uname -r)
    missing=()
    [[ -e /lib/modules/$kernel/build/Makefile ]] || missing+=("linux-headers-$kernel")
    command -v make >/dev/null && command -v gcc >/dev/null || missing+=(build-essential)
    command -v modinfo >/dev/null || missing+=(kmod)
    command -v lsusb >/dev/null || missing+=(usbutils)
    command -v ip >/dev/null || missing+=(iproute2)
    command -v hostapd >/dev/null || missing+=(hostapd)
    if [[ -d /sys/firmware/efi ]] && ! command -v mokutil >/dev/null; then
        missing+=(mokutil)
    fi
    if [[ -n $fc ]]; then
        command -v python3 >/dev/null && python3 -c 'import numpy' 2>/dev/null || missing+=(python3-numpy)
    fi
    if (( ${#missing[@]} )); then
        need_packages "${missing[@]}"
    fi
    if [[ -d /sys/firmware/efi ]]; then
        boot_state=$(LC_ALL=C mokutil --sb-state) || fail "cannot determine Secure Boot state"
        if grep -q 'SecureBoot enabled' <<< "$boot_state"; then
            [[ -n ${T40_SIGN_CERT:-} ]] || fail "Secure Boot requires an enrolled MOK and signing inputs"
            mokutil --test-key "$T40_SIGN_CERT" || fail "signing certificate is not enrolled"
        fi
    fi

    driver=$(realpath "$driver")
    if [[ -n ${SUDO_USER:-} && $SUDO_USER != root ]]; then
        runuser -u "$SUDO_USER" -- make -C "$driver"
    else
        make -C "$driver"
    fi
    for module in ath6kl_core ath6kl_usb; do
        ko=$driver/ath6kl/$module.ko
        [[ -s $ko ]] || fail "build did not produce $ko"
        info=$(modinfo -F vermagic "$ko")
        [[ ${info%% *} == "$kernel" ]] || fail "$module vermagic does not match $kernel"
        expected=$(modinfo -F srcversion "$ko")
        [[ -n $expected ]] || fail "$module has no srcversion for loaded-module verification"
        if [[ -n ${T40_SIGN_KEY:-} ]]; then
            "/lib/modules/$kernel/build/scripts/sign-file" sha256 "$T40_SIGN_KEY" "$T40_SIGN_CERT" "$ko"
        fi
    done
    modinfo -F alias "$driver/ath6kl/ath6kl_usb.ko" | grep -i 'usb:v0cf3p1022' >/dev/null ||
        fail "ath6kl_usb does not support 0cf3:1022"

    dest=/lib/modules/$kernel/updates/t40-radar
    install -d -m 0755 "$dest" /etc/depmod.d
    for module in ath6kl_core ath6kl_usb; do
        install -m 0644 "$driver/ath6kl/$module.ko" "$dest/$module.ko"
    done
    cat > /etc/depmod.d/t40-ar1021x.conf <<'EOF'
override ath6kl_core * updates/t40-radar
override ath6kl_usb * updates/t40-radar
EOF
    dest=/lib/firmware/ath6k/AR6004/hw3.0
    install -d -m 0755 "$dest"
    for file in bdata.bin fw-5.bin fw.ram.bin; do
        if [[ -s "$firmware/$file" ]] && [[ ! "$firmware/$file" -ef "$dest/$file" ]]; then
            install -m 0644 "$firmware/$file" "$dest/$file"
        fi
    done
    depmod -a "$kernel"
    bash "$repo/tools/t40_ap.sh" selected
    if [[ -f /boot/initrd.img-$kernel ]] && command -v update-initramfs >/dev/null; then
        update-initramfs -u -k "$kernel"
    fi

    install -d -m 0755 "$app" "$config"
    install -m 0755 "$repo/tools/t40_ap.sh" "$app/t40_ap.sh"
    printf 'COUNTRY=%s\n' "$country" > "$config/ap.env"
    printf '%s\n' "$mac" > "$config/radar.accept"
    install -m 0644 "$repo/deploy/t40-radar-ap@.service" "$units/t40-radar-ap@.service"
    drop=$units/t40-radar-ap@.service.d/10-fc.conf
    if [[ -n $fc ]]; then
        install -m 0644 "$fc" "$app/radar_min_client.py"
        install -m 0644 "$repo/deploy/t40-radar-fc@.service" "$units/t40-radar-fc@.service"
        install -d -m 0755 "$(dirname "$drop")"
        printf '[Unit]\nWants=t40-radar-fc@%%i.service\n' > "$drop"
    elif [[ -f $drop ]]; then
        systemctl stop 't40-radar-fc@*.service'
        rm -- "$drop"
    fi
    printf '%s\n' \
        'ACTION=="add|change", SUBSYSTEM=="net", SUBSYSTEMS=="usb", ATTRS{idVendor}=="0cf3", ATTRS{idProduct}=="1022", ENV{NM_UNMANAGED}="1"' \
        'ACTION=="add", SUBSYSTEM=="net", SUBSYSTEMS=="usb", ATTRS{idVendor}=="0cf3", ATTRS{idProduct}=="1022", TAG+="systemd", PROGRAM="/usr/bin/systemd-escape --template=t40-radar-ap@.service $name", ENV{SYSTEMD_WANTS}+="%c"' \
        > "$rules"
    udevadm control --reload-rules
    monitor udp

    if ! lsusb -d 0cf3:1022; then
        printf '%s\n' 'USB=waiting (0cf3:1022 absent in Ubuntu; setup resumes on device attach)'
        return
    fi
    modprobe ath6kl_usb
    bash "$app/t40_ap.sh" loaded
    for _ in {1..10}; do
        mapfile -t devices < <(bash "$app/t40_ap.sh" interfaces)
        (( ${#devices[@]} )) && break
        sleep 1
    done
    (( ${#devices[@]} == 1 )) || fail "expected one AR1021x net interface; inspect journalctl -k"
    iface=${devices[0]}
    instance=$(systemd-escape --template=t40-radar-ap@.service "$iface")
    systemctl restart "$instance"
    for _ in {1..10}; do
        if hostapd_cli -p /run/t40-radar-ap -i "$iface" status | grep -Fxq 'state=ENABLED'; then
            printf 'AP=enabled iface=%s (wait for radar association and valid 1E61)\n' "$iface"
            return
        fi
        sleep 1
    done
    fail "AP did not reach ENABLED; inspect journalctl -u $instance"
}

status() {
    local failed=0 iface ap_state station mac
    if [[ -r $config/monitor.env ]]; then
        . "$config/monitor.env"
        port=$T40_PORT
    fi
    systemctl --no-pager --full status t40-radar.service || failed=1
    curl --fail --silent --show-error --max-time 3 \
        "http://127.0.0.1:$port/api/state" | python3 -m json.tool || failed=1
    [[ $1 == ar1021x ]] || return "$failed"
    bash "$app/t40_ap.sh" selected || failed=1
    lsusb -d 0cf3:1022 || fail "USB=absent in Ubuntu; check VMware USB ownership or physical USB"
    bash "$app/t40_ap.sh" loaded || failed=1
    iface=$(bash "$app/t40_ap.sh" interfaces)
    [[ -n $iface && $iface != *$'\n'* ]] || fail "expected one AR1021x net interface"
    ip -br -4 addr show dev "$iface"
    ap_state=$(hostapd_cli -p /run/t40-radar-ap -i "$iface" status)
    printf '%s\n' "$ap_state"
    grep -Fxq 'state=ENABLED' <<< "$ap_state" || failed=1
    mac=$(<"$config/radar.accept")
    station=$(hostapd_cli -p /run/t40-radar-ap -i "$iface" sta "$mac") || station=
    if grep -q '^flags=.*\[ASSOC\].*\[AUTHORIZED\]' <<< "$station"; then
        printf 'RADAR_STA=authorized %s\n' "$mac"
    else
        printf 'RADAR_STA=waiting %s\n' "$mac"
        failed=1
    fi
    return "$failed"
}

mode=${1:-help}
case "$mode" in
    -h|--help|help)
        usage
        exit 0
        ;;
    monitor)
        source=${2:-sim}
        [[ $# -le 2 && ( $source == sim || $source == udp ) ]] || fail "expected monitor [sim|udp]"
        ;;
    ar1021x)
        [[ $# -eq 5 || $# -eq 6 ]] || fail "expected ar1021x DRIVER_DIR FIRMWARE_DIR COUNTRY RADAR_MAC [FC_CLIENT]"
        [[ $4 =~ ^[A-Z]{2}$ ]] || fail "COUNTRY must be a two-letter regulatory country code"
        [[ $5 =~ ^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$ ]] || fail "RADAR_MAC must be a MAC address"
        ;;
    status)
        [[ $# -eq 1 || ( $# -eq 2 && $2 == ar1021x ) ]] || fail "expected status [ar1021x]"
        ;;
    *)
        usage >&2
        fail "unknown command: $mode"
        ;;
esac

[[ $port =~ ^[1-9][0-9]{0,4}$ ]] && (( port <= 65535 )) || fail "T40_HTTP_PORT must be 1-65535"
[[ $(uname -s) == Linux && -d /run/systemd/system ]] || fail "run inside a systemd Linux host, not the Windows host"
if [[ $mode != status ]]; then
    [[ $EUID -eq 0 ]] || fail "run installation with sudo"
    . /etc/os-release
    [[ $ID == ubuntu ]] || fail "installation is supported on Ubuntu"
fi

case "$mode" in
    monitor) monitor "$source" ;;
    ar1021x) install_ar1021x "$2" "$3" "$4" "$5" "${6:-}" ;;
    status) status "${2:-}" ;;
esac
