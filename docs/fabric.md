# Run satlink on a FABRIC node

Use one simulator VM with two dedicated data interfaces. Select the
terminal-facing Linux interface with `-terminal` and the ground-facing one
with `-ground`. The local smoke test creates two temporary veth pairs to
stand in for these links. On FABRIC, the slice supplies the links and you
attach the selected interfaces to a Linux bridge.

## Wire the slice

```text
UE or n3iwue ── sat-access ── [ terminal NIC | br-sat | ground NIC ] ── sat-ground ── ground gNB or N3IWF
                                          simulator VM
```

Create **two separate L2 networks** in the slice. Attach the terminal and the
simulator's first port to `sat-access`. Attach the simulator's second port
and the ground endpoint to `sat-ground`. This puts the simulator on the only
data path between the endpoints. Keep the management interface and SSH route
separate. For a same-site deployment, FABRIC provides the `L2Bridge` network
service. The middle VM needs data ports that can forward frames carrying
the endpoints' MAC addresses. [FABlib L2 networks](https://learn.fabric-testbed.net/docs/fablib/fablib.html#slice.Slice.add_l2network)

| Deployment | Terminal side | Ground side |
| --- | --- | --- |
| freeRANUE | UE access interface for the secondary gNB | UE-facing data interface on the secondary gNB. Set `gnb.ntn.enabled: false` so delay is applied once. Keep the master leg, N2, N3 and Xn on their existing networks. |
| N3IWF | Outer NWu interface of `n3iwue` | Outer NWu interface of the N3IWF. IKE and ESP cross the bridge. N2 and N3 leave the N3IWF on its core-facing interfaces. |

## Identify the two Linux devices

In your FABRIC notebook, use the allocated slice object and substitute your
node and network names. The role follows the network attachment, regardless
of the order in which Linux enumerated the NICs.

```python
sim = slice.get_node(name="sat-sim")
for network, flag in [("sat-access", "-terminal"), ("sat-ground", "-ground")]:
    iface = sim.get_interface(network_name=network)
    print(flag, iface.get_device_name(), network)
print("SSH management", sim.get_management_os_interface())
```

Use the device names printed for the two flags. If FABRIC assigns a tagged
interface, `get_device_name()` returns the VLAN device name. The shell
examples below assume the notebook printed `ens7` for the terminal side and
`ens8` for the ground side. [FABlib interface lookup](https://learn.fabric-testbed.net/docs/fablib/fablib.html#node.Node.get_interface)
· [Linux device name](https://learn.fabric-testbed.net/docs/fablib/fablib.html#interface.Interface.get_device_name)

## Install and connect the bridge

For a precompiled controller, copy the matching Actions ZIP to the simulator
VM and extract it under `~/satlink-run`. Run `chmod +x ~/satlink-run/satlink`,
then continue with the NIC setup below. [Download a Linux binary](running-satlink.md#download-a-linux-binary)

To build from source instead, use Go 1.23 or newer on your workstation and
copy the binary and trace over SSH. Here `amd64` is the target architecture
and `fabric-sat` is an example SSH alias. The VM only needs the binary, trace, iproute2, and kernel
support for bridge and netem.

```sh
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go -C satlink build -trimpath -o ../bin/satlink .
ssh fabric-sat 'mkdir -p ~/satlink-run'
scp bin/satlink examples/ns3-tdcho-ue0.json fabric-sat:~/satlink-run/
```

On the simulator VM, set the two variables to the names you looked up. Check
their addresses and bridge membership before attaching them. Use unnumbered
data ports dedicated to this experiment. Keep the access IPs on the two
endpoints in the same subnet.

```sh
terminal_if=ens7
ground_if=ens8
ip -br addr
bridge link show
```

Create the bridge once, with both ports administratively up. If you already
configured this bridge, use the last command to check its membership.

```sh
sudo ip link add br-sat type bridge
sudo ip link set dev "$terminal_if" master br-sat
sudo ip link set dev "$ground_if" master br-sat
sudo ip link set dev "$terminal_if" up
sudo ip link set dev "$ground_if" up
sudo ip link set dev br-sat up
bridge link show master br-sat
```

Once both ports show `state forwarding`, ping the **ground-side access IP
from the terminal**.
For example, if the ground access IP is `192.0.2.2` and the terminal's access
device is `ens5`, run `ping -I ens5 -c 5 192.0.2.2` on the terminal. On the
simulator VM, `bridge fdb show br br-sat` should show the terminal and ground
MAC addresses learned on their respective ports. If baseline traffic fails,
check endpoint addressing, VLAN selection and MAC forwarding on the allocated
data NICs before interpreting any packet loss as a satellite outage.

## Replay, observe and stop

In the same simulator shell, validate the copied trace and start the controller.
It passes traffic during the 15-second lead-in. Establish the freeRANUE session
or the N3IWF tunnel before the switch and send traffic through it.

```sh
cd ~/satlink-run
./satlink -check -trace ns3-tdcho-ue0.json
sudo ./satlink -terminal "$terminal_if" -ground "$ground_if" \
  -trace ns3-tdcho-ue0.json -start +15s 2>replay.jsonl
```

Watch `tail -f ~/satlink-run/replay.jsonl` from another SSH session. The
`armed` record gives the replay start. In this trace, the link goes down at
replay time **9.00000 s** and returns on the new satellite at **9.04571 s**.
Use numbered packets at short intervals to measure the gap. A default
one-ping-per-second stream can miss it. To observe the queues directly, run
`tc -s qdisc show dev ens7` and the equivalent command for the other actual
port. [Example ns-3 trace](../examples/ns3-tdcho-ue0.json)

Press **Ctrl-C** in the replay shell to stop and remove the simulator queues.
The bridge can carry baseline traffic or another replay. To dismantle a
bridge you created for this experiment, run the following on the simulator
VM after stopping the controller.

```sh
sudo ip link set dev "$terminal_if" nomaster
sudo ip link set dev "$ground_if" nomaster
sudo ip link del dev br-sat type bridge
```

For a different satellite schedule, copy another timeline and pass its filename
to `-trace`. The general guide covers ns-3 export, timing tolerances, and
queue recovery after a forced stop. [Run and export guide](running-satlink.md)
