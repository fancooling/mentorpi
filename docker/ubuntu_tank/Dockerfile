# Build only from build.py's staged allowlist context, never from the repository root.
ARG UBUNTU_BASE=ubuntu:26.04@sha256:da6fc2be547864451aa253836dd926da33623312df4a9a243e35dc877c378a78
ARG NODE_BASE=node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6
FROM --platform=$BUILDPLATFORM ${NODE_BASE} AS frontend
WORKDIR /frontend
COPY ubuntu_tank/web/package*.json ./
RUN npm ci
COPY ubuntu_tank/web/ ./
RUN npm run build

FROM ${UBUNTU_BASE} AS base
ARG TARGETARCH
ARG UBUNTU_BASE
RUN test "$TARGETARCH" = arm64
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates \
    && mkdir -p /opt/ubuntu_tank/manifests \
    && python3 -c "import hashlib,json,os,pathlib,subprocess; archives=[{'archive':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in pathlib.Path('/var/cache/apt/archives').glob('*.deb')]; pathlib.Path('/opt/ubuntu_tank/manifests/bootstrap.json').write_text(json.dumps({'base_image':os.environ['UBUNTU_BASE'],'archives':archives,'installed':subprocess.check_output(['dpkg-query','-W'],text=True)},indent=2))" \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
# Match the native closure capture date, including base-library dependencies of
# pinned development packages. Keep ordinary APT signature/hash verification.
RUN cp /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list.d/ubuntu-snapshot.sources \
    && sed -i 's@http://ports.ubuntu.com/ubuntu-ports@http://snapshot.ubuntu.com/ubuntu/20260909T000000Z@g' /etc/apt/sources.list.d/ubuntu-snapshot.sources \
    && printf '%s\n' 'Package: *' 'Pin: origin "snapshot.ubuntu.com"' 'Pin-Priority: 1001' > /etc/apt/preferences.d/ubuntu-snapshot \
    && apt-get update
COPY docker/ubuntu_tank/install_dependencies.py docker/ubuntu_tank/dependencies.json versions.json /build/
RUN groupadd --gid 10001 ubuntu-tank && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp ubuntu-tank

FROM base AS ros-base
# Reuse the repository's hashed ROS APT source package, never the native installer.
COPY docker/ubuntu_tank/ros-snapshot.asc /usr/share/keyrings/ros-snapshot.asc
RUN python3 -c "import hashlib,json,pathlib,urllib.request; p=json.load(open('/build/versions.json'))['ros_apt_source']; data=urllib.request.urlopen(p['url']).read(); assert hashlib.sha256(data).hexdigest()==p['sha256']; pathlib.Path('/tmp/ros-apt.deb').write_bytes(data)" \
    && dpkg -i /tmp/ros-apt.deb && rm /tmp/ros-apt.deb \
    && printf '%s\n' 'deb [signed-by=/usr/share/keyrings/ros-snapshot.asc] http://snapshots.ros.org/lyrical/2026-08-19/ubuntu resolute main' > /etc/apt/sources.list.d/ros-snapshot.list
RUN python3 /build/install_dependencies.py runtime

FROM ros-base AS builder
RUN python3 /build/install_dependencies.py builder
COPY ubuntu_tank/src/ /build/ubuntu_tank/src/
COPY ubuntu_tank/scripts/build_workspace.sh /build/ubuntu_tank/scripts/build_workspace.sh
RUN rm -rf /build/ubuntu_tank/src/ubuntu_tank_web \
    && printf '%s\n' '{"build":{"cmake-args":["-DBUILD_TESTING=OFF"]}}' > /build/colcon-defaults.json \
    && COLCON_DEFAULTS_FILE=/build/colcon-defaults.json bash /build/ubuntu_tank/scripts/build_workspace.sh --production --merge-install --install-base /opt/ubuntu_tank/current/install --build-base /build/colcon

FROM base AS web-deps
RUN python3 /build/install_dependencies.py web
FROM web-deps AS web-builder
RUN python3 /build/install_dependencies.py web-builder
COPY ubuntu_tank/src/ubuntu_tank_protocol/ /build/protocol/
COPY ubuntu_tank/src/ubuntu_tank_web/ /build/web/
RUN cd /build/protocol && python3 setup.py bdist_wheel --dist-dir /wheels \
    && cd /build/web && python3 setup.py bdist_wheel --dist-dir /wheels \
    && python3 -m pip install --no-index --no-deps --target /opt/ubuntu_tank/current/python /wheels/*.whl

FROM ros-base AS runtime
ARG SOURCE_REVISION
LABEL org.opencontainers.image.revision=$SOURCE_REVISION
COPY build-identity.json /opt/ubuntu_tank/manifests/build-identity.json
COPY --from=builder /opt/ubuntu_tank/current/install/ /opt/ubuntu_tank/current/install/
COPY --from=builder /opt/ubuntu_tank/manifests/builder.json /opt/ubuntu_tank/manifests/builder.json
COPY ubuntu_tank/bin/ /opt/ubuntu_tank/current/bin/
COPY ubuntu_tank/scripts/ /opt/ubuntu_tank/current/scripts/
COPY ubuntu_tank/config/fastdds/ /opt/ubuntu_tank/current/config/fastdds/
COPY docker/ubuntu_tank/supervisord.conf docker/ubuntu_tank/runtime_start.py docker/ubuntu_tank/healthcheck.py /opt/ubuntu_tank/container/
COPY --chmod=755 docker/ubuntu_tank/runtime-entrypoint.sh /usr/local/bin/runtime-entrypoint
ENV UBUNTU_TANK_PREFIX=/opt/ubuntu_tank/current UBUNTU_TANK_PRIVATE_DIR=/run/ubuntu_tank-private UBUNTU_TANK_PYTHON=/usr/bin/python3
USER 10001:10001
WORKDIR /opt/ubuntu_tank/current
RUN /usr/local/bin/runtime-entrypoint python3 -c "import rclpy, ros_robot_controller_msgs.msg, ubuntu_tank_operator, ubuntu_tank_supervisor, ubuntu_tank_protocol" \
    && /usr/local/bin/runtime-entrypoint python3 bin/mentorpi-tank-run --help \
    && /usr/local/bin/runtime-entrypoint ros2 run ubuntu_tank_bringup operator_client --help
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s CMD ["/usr/local/bin/runtime-entrypoint", "python3", "/opt/ubuntu_tank/container/healthcheck.py", "runtime"]
ENTRYPOINT ["/usr/local/bin/runtime-entrypoint"]

FROM web-deps AS web
ARG SOURCE_REVISION
LABEL org.opencontainers.image.revision=$SOURCE_REVISION
COPY build-identity.json /opt/ubuntu_tank/manifests/build-identity.json
COPY --from=web-builder /opt/ubuntu_tank/current/python/ /opt/ubuntu_tank/current/python/
COPY --from=web-builder /opt/ubuntu_tank/manifests/web-builder.json /opt/ubuntu_tank/manifests/web-builder.json
COPY --from=frontend /frontend/dist/ /opt/ubuntu_tank/current/web/dist/
COPY ubuntu_tank/web/package-lock.json /opt/ubuntu_tank/manifests/frontend-package-lock.json
COPY docker/ubuntu_tank/healthcheck.py /opt/ubuntu_tank/container/healthcheck.py
COPY --chmod=755 docker/ubuntu_tank/web-entrypoint.sh /usr/local/bin/web-entrypoint
ENV PYTHONPATH=/opt/ubuntu_tank/current/python
USER 10001:10001
WORKDIR /opt/ubuntu_tank/current
RUN python3 -c "import importlib.util; assert importlib.util.find_spec('rclpy') is None; assert importlib.util.find_spec('ubuntu_tank_operator') is None; from ubuntu_tank_web.app import create_app; assert create_app().openapi()['paths']" \
    && /usr/local/bin/web-entrypoint --help
EXPOSE 8443
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s CMD ["python3", "/opt/ubuntu_tank/container/healthcheck.py", "web"]
ENTRYPOINT ["/usr/local/bin/web-entrypoint"]
