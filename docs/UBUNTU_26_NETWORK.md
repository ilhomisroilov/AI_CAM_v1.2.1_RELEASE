# AI_CAM Ubuntu Network Requirements

The Ubuntu host needs VLAN membership or routed reachability to all three
industrial subnets. Its own IP is site-assigned and is intentionally not
hardcoded.

| Device | Address | Purpose |
|---|---:|---|
| SICK LECTOR652 | `10.123.86.42:2111/tcp` | CoLa-A control |
| SICK LECTOR652 | `10.123.86.42:2113/tcp` | BLOB image stream |
| Mitsubishi PLC | `10.123.40.99:5003/tcp` | MC protocol, Q series, D2222 |
| Impinj R700 | `10.123.18.3:80/tcp` | RFID reader |
| Operator UI | `SERVER_IP:8080/tcp` | Factory LAN browser access |

Run:

```bash
ip -brief address
ip route get 10.123.86.42
ip route get 10.123.40.99
ip route get 10.123.18.3
nc -vz -w2 10.123.86.42 2111
nc -vz -w2 10.123.86.42 2113
nc -vz -w2 10.123.40.99 5003
nc -vz -w2 10.123.18.3 80
sudo /opt/ai-cam/scripts/diagnose_linux.sh
```

Ping failure alone is not conclusive because industrial devices or ACLs may
block ICMP. TCP probes are read-only: they connect and close without issuing
device commands.

AI_CAM binds exactly one Uvicorn worker to `0.0.0.0:8080`. Restrict ingress to
the factory management/operator LAN. Do not publish or NAT this port to the
Internet. Preserve outbound/stateful return traffic to the device subnets.

At startup AI_CAM probes every enabled endpoint concurrently. An offline
device is logged and exposed by health while normal reconnect backoff
continues; it does not create a permanent service crash loop.
