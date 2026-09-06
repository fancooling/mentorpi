# The first MentorPi Fan release is deliberately frontend-only. It reuses the
# factory container's HTTP and WebSocket bridges and contains no ROS or driver
# executable. The multi-architecture digest includes the ARM64 image used on
# Raspberry Pi 5.
FROM docker.io/library/nginx:stable-alpine@sha256:dc5069ad14f19660b141b21236140b91656bf89bbc3e2417c70ae650cd66104c

LABEL org.opencontainers.image.title="MentorPi Fan"
LABEL org.opencontainers.image.description="Read-only MentorPi camera and LiDAR dashboard"
LABEL io.mentorpi.role="observer-sidecar"
LABEL io.mentorpi.hardware-access="none"

COPY --chown=nginx:nginx nginx.conf /etc/nginx/nginx.conf
COPY --chown=nginx:nginx web/ /usr/share/nginx/html/

USER nginx
EXPOSE 8081
STOPSIGNAL SIGQUIT

HEALTHCHECK --interval=15s --timeout=3s --start-period=3s --retries=3 \
    CMD wget -q -O - http://127.0.0.1:8081/healthz >/dev/null || exit 1

# Bypass the upstream image's templating entrypoint. All configuration is
# immutable and Nginx writes its PID and temporary files only under /tmp.
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
