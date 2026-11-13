# HALO dashboard controller deployment

These files are the SENSE-maintained source for HALO's restricted control path.
These files live in `/home/Pi_Mz/Documents/pace-routing/HALO/control/` on HALO.
The dashboard key's `authorized_keys` entry uses
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
