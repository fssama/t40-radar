#!/usr/bin/env bash
set -euo pipefail

fail() {
    printf 't40-radar AP: %s\n' "$*" >&2
    exit 1
}

interfaces() {
    local net node
    for net in /sys/class/net/*; do
        [[ -d "$net/wireless" || -e "$net/phy80211" ]] || continue
        node=$(readlink -f "$net/device") || continue
        while [[ $node == /sys/devices/* ]]; do
            if [[ -r $node/idVendor && -r $node/idProduct ]]; then
                if [[ $(<"$node/idVendor") == 0cf3 && $(<"$node/idProduct") == 1022 ]]; then
                    printf '%s\n' "${net##*/}"
                fi
                break
            fi
            node=${node%/*}
        done
    done
}

check_modules() {
    local kernel module selected expected actual
    kernel=$(uname -r)
    for module in ath6kl_core ath6kl_usb; do
        selected=$(modinfo -k "$kernel" -n "$module") || fail "no $module for $kernel"
        expected=/lib/modules/$kernel/updates/t40-radar/$module.ko
        [[ $selected -ef $expected ]] ||
            fail "selected $module is $selected; expected $expected (check depmod)"
        printf 'DRIVER_SELECTED=%s\n' "$selected"
        if [[ $1 == loaded ]]; then
            [[ -r /sys/module/$module/srcversion ]] || fail "$module is not loaded"
            actual=$(<"/sys/module/$module/srcversion")
            [[ $actual == "$(modinfo -F srcversion "$expected")" ]] ||
                fail "$module still uses an older/different build ($actual); schedule a maintenance reboot"
            printf 'DRIVER_LOADED=%s %s\n' "$module" "$actual"
        fi
    done
}

start_ap() {
    local iface=$1 conflict
    local -a devices
    mapfile -t devices < <(interfaces)
    [[ ${#devices[@]} -eq 1 && ${devices[0]} == "$iface" ]] ||
        fail "expected exactly one 0cf3:1022 interface matching $iface"
    check_modules loaded
    . /etc/t40-radar/ap.env
    [[ $COUNTRY =~ ^[A-Z]{2}$ && -s /etc/t40-radar/radar.accept ]] || fail "missing AP configuration"
    conflict=$(ip -4 -o addr show | awk -v own="$iface" '$2 != own && $4 ~ /^192[.]168[.]8[.]/ { print $2; exit }')
    [[ -z $conflict ]] || fail "192.168.8.0/24 is already used by $conflict"
    if command -v nmcli >/dev/null && systemctl is-active --quiet NetworkManager.service; then
        nmcli device set "$iface" managed no
    fi
    ip link set dev "$iface" up
    ip address replace 192.168.8.1/24 dev "$iface"

    install -d -m 0755 /run/t40-radar-ap
    cat > "/run/t40-radar-ap/$iface.conf" <<EOF
ctrl_interface=/run/t40-radar-ap
interface=$iface
driver=nl80211
country_code=$COUNTRY
ssid=radar_wifi
channel=48
hw_mode=a
ap_max_inactivity=5
wmm_enabled=1
ieee80211n=0
auth_algs=1
ignore_broadcast_ssid=0
macaddr_acl=1
accept_mac_file=/etc/t40-radar/radar.accept
EOF
    exec hostapd "/run/t40-radar-ap/$iface.conf"
}

case "${1:-}" in
    interfaces) interfaces ;;
    selected|loaded) check_modules "$1" ;;
    start) start_ap "$2" ;;
    *) fail "expected interfaces, selected, loaded or start IFACE" ;;
esac
