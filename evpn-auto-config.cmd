# auto-config for srl-evpn lab leaves & spines
set /system gnmi-server unix-socket admin-state enable
set /auto-config-agent igp bgp-unnumbered evpn model asymmetric-irb auto-lags encoded-ipv6 bgp-peering ipv4
set /auto-config-agent gateway ipv4 10.0.0.1/24

/acl cpm-filter ipv4-filter entry 345
		description "Allow communication to Netbox"
    match protocol tcp destination-port value 8000
    action accept

/acl cpm-filter ipv4-filter entry 346
		description "Allow communication from Netbox"
    match protocol tcp source-port value 8000
    action accept

set /netbox-agent netbox-url http://172.20.20.1:8000
set /netbox-agent admin-state enable
