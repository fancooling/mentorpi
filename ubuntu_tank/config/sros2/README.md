# SROS2 Security Governance Policies

This directory holds security governance policies and keystore configuration templates for native ROS 2 Lyrical controller operations.

## Security Model (Target Design Milestone 4)

- **Localhost Discovery Only**: DDS discovery and communication are strictly confined to the loopback interface (`ROS_LOCALHOST_ONLY=1`).
- **Enclave Segregation**: Dedicated, distinct SROS2 enclaves are assigned to each node:
  - `/ubuntu_tank/supervisor`
  - `/ubuntu_tank/guard`
  - `/ubuntu_tank/bridge`
  - `/ubuntu_tank/controller`
  - `/ubuntu_tank/teleop`
- **Deny-by-Default Access Control**: Topics and services require explicit allow rules in each enclave's permission XML.
- **Hardware Boundary**: Only the verified serial bridge interacts with `/dev/rrc`.
