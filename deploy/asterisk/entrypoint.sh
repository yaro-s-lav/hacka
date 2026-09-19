#!/bin/sh
set -eu
mkdir -p /provision /media /run/asterisk
[ -f /provision/users.conf ] || touch /provision/users.conf
cat > /etc/asterisk/manager.conf <<CONF
[general]
enabled=yes
port=5038
bindaddr=0.0.0.0
[arm112]
secret=${ASTERISK_AMI_SECRET:?AMI secret required}
read=system,call,reporting
write=system,call,originate,command
CONF
# Docker bridge addresses are local; LAN phones need the host RTP address.
sip_public=${SIP_PUBLIC_ADDRESS:-127.0.0.1}
case "$sip_public" in *[!a-zA-Z0-9.:_-]*) echo 'Invalid SIP_PUBLIC_ADDRESS' >&2; exit 1;; esac
sed -i "/protocol=udp/a external_media_address=$sip_public\nexternal_signaling_address=$sip_public\nlocal_net=172.16.60.0/24" /etc/asterisk/pjsip.conf
exec asterisk -f -vvv
