# Sandbox image for task execution. Deliberately minimal: Python + coreutils,
# unprivileged uid 1000. The worker adds all runtime restrictions
# (no network, read-only rootfs, cap_drop=ALL, resource limits).
FROM python:3.12-slim

RUN useradd --uid 1000 --user-group --no-create-home --shell /usr/sbin/nologin sandbox \
    && mkdir -p /workspace && chown 1000:1000 /workspace

USER 1000:1000
WORKDIR /workspace
CMD ["sleep", "infinity"]
