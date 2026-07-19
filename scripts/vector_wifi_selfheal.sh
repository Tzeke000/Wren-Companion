#!/bin/sh
# vector_wifi_selfheal.sh — runs ON VECTOR (WireOS, busybox sh).
# Iris, 2026-07-19: today's wifi flap (pingable in windows, sshd/443 never up)
# stranded the body out of ALL remote reach while Zeke packs for deployment.
# This watchdog runs robot-side so the body heals its own network — no BLE,
# no button press, no Iris required.
#
# Install (from the tower, once SSH is back):
#   scp -i state/vector/dev/ssh_root_key scripts/vector_wifi_selfheal.sh root@<bot>:/data/local/iris/
#   ssh <bot> 'chmod +x /data/local/iris/vector_wifi_selfheal.sh'
#   + the systemd unit (see vector_wifi_selfheal.service next to this file)
#
# Logic: ping the default gateway every CHECK_S. After FAIL_N consecutive
# misses: (1) wpa_cli reassociate, (2) if still dead, bounce connman/wifi.
# Logs to /data/local/iris/wifi_selfheal.log (truncated at ~64KB).

LOGF=/data/local/iris/wifi_selfheal.log
CHECK_S=15
FAIL_N=4

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOGF"
    # keep the log small (busybox: wc -c)
    if [ "$(wc -c < "$LOGF" 2>/dev/null || echo 0)" -gt 65536 ]; then
        tail -c 32768 "$LOGF" > "$LOGF.tmp" && mv "$LOGF.tmp" "$LOGF"
    fi
}

gateway() {
    ip route 2>/dev/null | awk '/^default/ {print $3; exit}'
}

alive() {
    gw="$(gateway)"
    [ -n "$gw" ] && ping -c 1 -W 3 "$gw" >/dev/null 2>&1
}

mkdir -p /data/local/iris
log "selfheal start (pid $$)"
fails=0
while true; do
    if alive; then
        fails=0
    else
        fails=$((fails + 1))
        if [ "$fails" -ge "$FAIL_N" ]; then
            log "gateway unreachable x$fails — reassociating"
            wpa_cli -i wlan0 reassociate >/dev/null 2>&1
            sleep 12
            if alive; then
                log "reassociate healed it"
            else
                log "reassociate insufficient — bouncing wifi stack"
                # WireOS network manager: try connman first, then raw interface bounce
                systemctl restart connman >/dev/null 2>&1
                sleep 15
                if alive; then
                    log "connman bounce healed it"
                else
                    log "connman bounce insufficient — ifdown/ifup wlan0"
                    ip link set wlan0 down; sleep 3; ip link set wlan0 up
                    sleep 15
                    if alive; then log "link bounce healed it"; else log "STILL DEAD after full ladder"; fi
                fi
            fi
            fails=0
        fi
    fi
    sleep "$CHECK_S"
done
