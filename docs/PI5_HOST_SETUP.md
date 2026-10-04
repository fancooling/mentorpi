# Pi 5 host preparation

Fresh Ubuntu 26.04 ARM64; Docker Engine and Compose v2 already installed.
Use separate boot media from the factory system. Keep tracks raised.
Host ROS installation is unnecessary; ROS runs in the runtime image.

Run Pi commands in Bash, in order. Replace uppercase placeholders. Stop on errors.
Existing installations: retain configuration and keys; skip first-time creation.

## 1. Host prerequisites

On the Pi:

```bash
cat /etc/os-release
uname -m                         # aarch64
tr -d '\0' < /proc/device-tree/model  # Raspberry Pi 5
sudo apt-get update
sudo apt-get install -y openssh-server python3 openssl udev logrotate usbutils
sudo systemctl enable --now ssh docker
sudo docker info
sudo docker compose version
sudo docker context inspect      # unix:///var/run/docker.sock
sudo timedatectl set-ntp true
timedatectl status               # Clock synchronized before generating keys
```

Docker must be local and rootful, without user namespace remapping.
If UFW is already active, allow SSH and HTTPS from your trusted LAN:

```bash
sudo ufw status
sudo ufw allow from LAN_CIDR to any port 22 proto tcp
sudo ufw allow from LAN_CIDR to any port 8443 proto tcp
```

## 2. Copy configuration templates

From the workstation repository root, copy only the manual setup inputs.
Use the source revision you will deploy:

```bash
pi_host='USER@PI_HOST'
ssh "$pi_host" 'mkdir -p ~/mentorpi-host-setup'
scp -r ubuntu_tank/config "$pi_host:mentorpi-host-setup/"
```

On the Pi, keep this shell open:

```bash
set -euo pipefail
cd ~/mentorpi-host-setup
prep_dir="$PWD"
```

No factory `MentorPi`, `MentorPiFan`, native Ubuntu Tank services, or other
hardware containers may coexist on this fresh host.

## 3. Identify USB devices

### Motor controller

```bash
lsusb                          # Controller: 1a86:55d4
ls -l /dev/serial/by-id/ /dev/serial/by-path/ 2>/dev/null || true
serial_device='/dev/ttyACM0'    # Replace with the observed controller tty
udevadm info --query=property --name="$serial_device"
stat -Lc '%a %G %n' "$serial_device"  # Must be 660; note its group
```

Require `ID_VENDOR_ID=1a86`, `ID_MODEL_ID=55d4`, and `ID_SERIAL_SHORT` or
`ID_PATH`. Do not select a camera or LiDAR tty.

`prepare-host --serial-device ...` creates the persistent `/dev/rrc` rule.
[Compose](../docker/ubuntu_tank/compose.yaml) already maps `/dev/rrc` into runtime
and adds its numeric device group. No privileged mode or whole-USB mount is needed.

### Camera and LiDAR discovery

On the Pi, with the controller stopped/disarmed, reconnect one sensor at a time.
Watch its USB and device-node events; stop the monitor with Ctrl-C:

```bash
sudo udevadm monitor --udev --property
```

In another terminal:

```bash
lsusb
lsusb -t                        # USB topology and bound kernel drivers
ls -l /dev/serial/by-id/ /dev/serial/by-path/ 2>/dev/null || true
ls -l /dev/v4l/by-id/ /dev/v4l/by-path/ 2>/dev/null || true
```

**LiDAR:** use the tty that appears when its USB adapter connects:

```bash
lidar_device='/dev/ttyUSBX'       # Replace with observed ttyUSB* or ttyACM*
udevadm info --query=property --name="$lidar_device"
udevadm info --attribute-walk --name="$lidar_device"
```

The repository's MS200 driver uses serial at 230400 baud. `/dev/ldlidar` is a
configured alias, not a guaranteed fresh-Ubuntu device. Record VID:PID, serial
number, and `ID_PATH`; adapter VID:PID alone does not identify the sensor.

**Other cameras exposing V4L2:** list video nodes and capture capabilities.
Skip this block for the Aurora 930:

```bash
sudo apt-get install -y v4l-utils
v4l2-ctl --list-devices
camera_device='/dev/videoX'       # Replace with observed video node
udevadm info --query=property --name="$camera_device"
v4l2-ctl --device="$camera_device" --all
v4l2-ctl --device="$camera_device" --list-formats-ext
```

One camera may expose multiple video nodes. Record the capture node, formats,
VID:PID, serial number, and USB path. A depth camera may require its vendor SDK
and expose no V4L2 capture node; inspect its USB device from `lsusb` instead:

```bash
camera_usb='/dev/bus/usb/BBB/DDD'  # Bus/device numbers shown by lsusb
udevadm info --query=property --name="$camera_usb"
udevadm info --attribute-walk --name="$camera_usb"
```

USB bus/device numbers and tty/video indexes can change after reconnecting.
These commands identify sensors only; `--serial-device` selects the motor
controller. Camera/LiDAR driver and container access setup are separate.

### Aurora 930 host setup

Confirmed USB ID: `3251:1930`. This camera uses vendor-specific bulk USB;
`Driver=[none]` is expected without a kernel driver. It does not expose a
standard UVC video interface.

The [Deptrum driver](../third_party_src/deptrum-ros-driver-aurora930/README.md)
includes an ARM64 SDK. Install its USB permission rule on the host.
From the workstation repository root:

```bash
sdk_dir='third_party_src/deptrum-ros-driver-aurora930/ext/deptrum-stream-aurora900-linux-aarch64-v1.1.22-18.04'
scp "$sdk_dir/scripts/99-deptrum-libusb.rules" "$pi_host:mentorpi-host-setup/"
```

On the Pi:

```bash
sudo install -m 644 "$prep_dir/99-deptrum-libusb.rules" \
  /etc/udev/rules.d/99-deptrum-libusb.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --action=change --subsystem-match=usb \
  --attr-match=idVendor=3251 --attr-match=idProduct=1930
sudo udevadm settle
lsusb -d 3251:1930
camera_usb='/dev/bus/usb/BBB/DDD'  # Use current Bus/Device numbers above
stat -Lc '%a %U:%G %n' "$camera_usb"  # Vendor rule: mode 666
```

The vendor rule grants all local users read/write access to all Deptrum devices
(`3251`). Reconnect the camera if permissions have not updated.

Host setup ends here. The camera SDK/ROS driver belongs inside the runtime
container, with explicit camera USB access; no host camera kernel module is
required. Use `-DSTREAM_SDK_TYPE=AURORA930` when building the driver.
These permissions alone do not enable streaming. Ubuntu 26.04/ROS Lyrical SDK
compatibility and real image capture remain unverified.

## 4. Create initial configuration

Fresh host only; commands refuse existing configuration files:

```bash
sudo install -d -m 755 /etc/opt/ubuntu_tank/web /etc/opt/ubuntu_tank/security
sudo test ! -e /etc/opt/ubuntu_tank/controller.yaml
sudo test ! -e /etc/opt/ubuntu_tank/web/web.yaml
sudo install -m 600 config/controller.yaml /etc/opt/ubuntu_tank/controller.yaml
sudo install -m 600 config/web/web.yaml /etc/opt/ubuntu_tank/web/web.yaml
sudoedit /etc/opt/ubuntu_tank/controller.yaml
```

Confirm track dimensions, wheel diameter, correction factors, and speed limits
for your robot. Retain `/dev/rrc` and controller-only mode. The container entrypoint
binds HTTPS to `0.0.0.0:8443`; `setup-tls` adds exact browser origins below.

## 5. Generate ROS security keys

Fresh host only. Two CAs sign participant identities and DDS permissions.
CA private keys stay under `/root`, outside the container-mounted keystore.
The block refuses to overwrite an existing keystore or CA directory.

```bash
sudo bash -s -- "$prep_dir/config/sros2" <<'KEYS'
set -euo pipefail
umask 077
policy_dir="$1"
ca_dir=/root/ubuntu-tank-ca
key_store=/etc/opt/ubuntu_tank/security/keystore
test ! -e "$ca_dir"
test ! -e "$key_store"
mkdir -m 700 "$ca_dir" "$key_store"

for name in identity permissions; do
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -subj "/CN=UbuntuTank${name}CA" \
    -keyout "$ca_dir/${name}_ca.key.pem" \
    -out "$key_store/${name}_ca.cert.pem"
done

# Sign a policy as attached S/MIME, the format consumed by DDS.
sign_policy() {
  openssl cms -sign -nodetach -text -outform SMIME \
    -in "$1" -out "$2" \
    -signer "$key_store/permissions_ca.cert.pem" \
    -inkey "$ca_dir/permissions_ca.key.pem"
}
cp "$policy_dir/governance.xml" "$key_store/governance.xml"
sign_policy "$policy_dir/governance.xml" "$key_store/governance.p7s"

for enclave in controller guard bridge operator status; do
  enclave_dir="$key_store/enclaves/ubuntu_tank/$enclave"
  mkdir -p "$enclave_dir"
  openssl req -new -newkey rsa:2048 -nodes \
    -subj "/CN=\/ubuntu_tank\/$enclave" \
    -keyout "$enclave_dir/key.pem" -out "$enclave_dir/request.csr"
  openssl x509 -req -days 3650 \
    -in "$enclave_dir/request.csr" -out "$enclave_dir/cert.pem" \
    -CA "$key_store/identity_ca.cert.pem" \
    -CAkey "$ca_dir/identity_ca.key.pem" \
    -set_serial "0x$(openssl rand -hex 16)"
  rm "$enclave_dir/request.csr"
  sign_policy "$policy_dir/permissions/${enclave}_permissions.xml" \
    "$enclave_dir/permissions.p7s"
  cp "$key_store/identity_ca.cert.pem" "$key_store/permissions_ca.cert.pem" \
    "$key_store/governance.p7s" "$enclave_dir/"
  openssl verify -CAfile "$key_store/identity_ca.cert.pem" "$enclave_dir/cert.pem"
  for document in governance permissions; do
    openssl cms -verify -inform SMIME -in "$enclave_dir/$document.p7s" \
      -CAfile "$key_store/permissions_ca.cert.pem" -out /dev/null
  done
done
KEYS
```

Back up `/root/ubuntu-tank-ca` and the keystore to private storage. Do not commit
keys. Policy validity ends on 2036-01-01; retain identities during later updates.
If interrupted, inspect and recover the partial output before retrying.

## 6. Run automated deployment

On the workstation, use the observed controller tty and every browser DNS name/IP.
DNS names must resolve to the Pi. Repeat TLS options for additional addresses.

```bash
source .venv/bin/activate
./docker/ubuntu_tank/deploy.sh "$pi_host" --first-install \
  --serial-device /dev/OBSERVED_CONTROLLER_TTY \
  --tls-hostname PI_DNS_NAME --tls-ip PI_LAN_IP
# Add --build-dir /ABSOLUTE/PATH/TO/BUILD_OUTPUT to reuse an existing build.
```

The script builds, verifies, transfers, loads images, generates HTTPS credentials,
runs `prepare-host`, deploys, and verifies stopped/disarmed operation.
No manual Docker USB mapping, image transfer, or service installation is needed.

`--first-install` refuses existing container state. For later updates, omit it
and use the normal [deployment command](../docker/ubuntu_tank/README.md#build-and-deploy).
After a failed first installation, inspect Pi state before retrying; retain all keys.

## 7. Trust the HTTPS certificate

On the Pi, inspect and export only the public certificate:

```bash
sudo openssl x509 -in /var/opt/ubuntu_tank/web/certs/server.crt \
  -noout -dates -ext subjectAltName -fingerprint -sha256
sudo cat /var/opt/ubuntu_tank/web/certs/server.crt > "$prep_dir/server.crt"
```

On the workstation:

```bash
scp "$pi_host:mentorpi-host-setup/server.crt" ./pi-server.crt
```

Import `pi-server.crt` into each browser device's trusted certificate store.
Never transfer `server.key` to clients.

Validation: command syntax checked; fresh-Pi execution remains pending.
No motor test is included.
