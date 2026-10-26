FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-server \
    && rm -rf /var/lib/apt/lists/*
CMD ["sh", "-c", "mkdir -p /run/sshd && exec /usr/sbin/sshd -D -e -f /fixture/sshd_config"]
