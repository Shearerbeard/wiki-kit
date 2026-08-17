FROM ubuntu:24.04

# The container runs the kit's scripts under the system python3:
# python3-jsonschema is installed at build time because the smoke runs
# with --network none (uv-managed environments are for development; the
# pinned dev environment is exercised by pytest outside Docker).
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ca-certificates \
    git \
    python3 \
    python3-jsonschema \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
