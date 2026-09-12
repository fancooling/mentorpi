# SROS2 Security Governance Policies

This directory holds security governance policies and keystore configuration templates for native ROS 2 Lyrical controller operations.

## Security Model (Target Design Milestone 4)

- **Localhost Discovery Only**: DDS discovery and communication are strictly confined to the loopback interface (`ROS_LOCALHOST_ONLY=1`).
- **Enclave Segregation**: Dedicated, distinct SROS2 enclaves are assigned to each node:
  - `/ubuntu_tank/guard`
  - `/ubuntu_tank/bridge`
  - `/ubuntu_tank/controller`
  - `/ubuntu_tank/operator`
  - `/ubuntu_tank/status`
- **Deny-by-Default Access Control**: Topics and services require explicit allow rules in each enclave's permission XML.
- **Hardware Boundary**: Only the verified serial bridge interacts with `/dev/rrc`.

## Provisioning and policy lifecycle

The deployment manager validates these templates before signing governance and
all permissions as S/MIME using the permissions CA. Participant certificates
use the separate identity CA. The service reads its three enclaves through
`mentorpi-rrc`; operator and status credentials use `ubuntu-tank-operators` and
`ubuntu-tank-status`, respectively. Private CA keys are root-only.

A stopped activation updates the signed policies without rotating identities.
Checksummed transaction snapshots preserve the corresponding security state for
rollback and crash recovery. See the workspace README for initial role access,
legacy-keystore repair and the hardware-free native DDS test.
