# HALO dashboard controller deployment

SENSE connects to the HALO controller as `Pi_Mz@10.42.10.2` over PACE Wi-Fi.
On HALO, the controller runs as `Pi_Mz` from
`/home/Pi_Mz/Documents/pace-routing/HALO/control/`. The restricted dashboard
key entry in that account's `~/.ssh/authorized_keys` uses
`restrict,command="/home/Pi_Mz/Documents/pace-routing/HALO/control/forced_command.sh"`.

The controller uses a transient systemd unit for a recording. The unit survives
SSH disconnects and its runner watches the 4 GiB disk reserve. `record_ui.sh`
shares the existing recording lock and extraction/upload services. A requested
stop asks both camera processes to finalize, validates the MKVs, and then uses
the existing capture-to-extraction queue. If finalization fails, the session
remains incomplete. No camera capture was run while the connectors are damaged.
After SENSE verifies a matching processed copy, cleanup removes local `frames/`
and the source MKVs, while preserving capture metadata and a checksum manifest
for the retained metadata files.
