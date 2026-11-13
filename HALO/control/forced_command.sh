#!/usr/bin/env bash
set -Eeuo pipefail
read -r -a args <<< "${SSH_ORIGINAL_COMMAND:-}"
case "${args[0]:-}" in
  status|sessions|start|stop)
    (( ${#args[@]} == 1 )) || exit 2 ;;
  cleanup)
    (( ${#args[@]} == 4 )) || exit 2
    [[ "${args[1]}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ && "${args[2]}" =~ ^[a-f0-9]{64}$ && "${args[3]}" =~ ^[a-f0-9]{64}$ ]] || exit 2 ;;
  *) exit 2 ;;
esac
exec /usr/bin/python3 /home/Pi_Mz/Documents/pace-routing/HALO/control/controller.py "${args[@]}"
