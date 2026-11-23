#!/bin/sh
# Run on the radar with busybox sh. Reads only; never touches motor control.
set -eu
ps | grep '[a]pp\.elf' >&2 || true
set -- $(busybox pidof app.elf)
if [ "$#" -ne 1 ]; then
    echo 'Expected exactly one live app.elf process' >&2
    exit 1
fi
mem="/proc/$1/mem"

dump() {
    printf '%s (%s bytes):\n' "$1" "$3"
    # Check for short/failed reads; od alone would hide a dd error.
    # The radar has BusyBox 1.22.1 with the small od (no -A/-t).
    bytes=$(busybox dd if="$mem" bs=1 skip="$2" count="$3" 2>/dev/null | busybox od -v -b |
        busybox awk '{for (i=2; i<=NF; i++) printf "%02x ", 64*substr($i,1,1)+8*substr($i,2,1)+substr($i,3,1); if (NF>1) print ""}')
    [ "$(printf '%s\n' "$bytes" | busybox wc -w)" -eq "$3" ] || exit 1
    printf '%s\n' "$bytes"
}

dump sector_build_buffer "$((0x284548))" 116  # firmware clears this buffer between sends
dump avoidance_snapshot "$((0xCD44330))" "$((0x224))"
dump flight_mode "$((0x283295))" 1
dump height_gate_counter "$((0xCACDEB2))" 2
# In the inspected app.elf, sub_50A54 reads u16 at +0x20 of each
# 0x28-byte record passed from the table at 0xCAC5F28.
slot=0
while [ "$slot" -lt 16 ]; do
    dump "native_sector_slot_${slot}_presence" "$((0xCAC5F28 + slot * 0x28 + 0x20))" 2
    slot=$((slot + 1))
done
i=0
while [ "$i" -lt 5 ]; do
    dump point_count_and_frame_token "$((0xC442F90))" 4
    # Two adjacent little-endian u16 counts. Output is from the last gridding run.
    dump gridding_input_and_last_output "$((0xCC2DB59))" 4
    i=$((i + 1))
    [ "$i" -eq 5 ] || sleep 1
done
