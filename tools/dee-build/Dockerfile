# A one-off setup container, not part of the service. It builds the Linux
# dee_dthd_encoder that BEQ Encoder needs from the macOS dee_dthd_encoder of your own
# DEE 6.7, with dthd-mac2linux (https://github.com/truehdd/dthd-mac2linux, Apache-2.0),
# and writes it into your DEE folder.
#
#   docker build -t beq-dee-build tools/dee-build
#   docker run --rm \
#       -v /path/to/macos/dee_dthd_encoder:/in/dee_dthd_encoder:ro \
#       -v "$DEE_PATH":/out \
#       beq-dee-build
#
# The image holds no Dolby code. Everything the build downloads is fetched here, at image
# build time, so the run itself needs no network.

# dthd-mac2linux pins its toolchain to Ubuntu 24.04's.
FROM ubuntu:24.04

# The dthd-mac2linux release, the commit its tag points at, and the Rust toolchain in
# its rust-toolchain.toml.
ARG DTHD_MAC2LINUX_VERSION=v0.2.1
ARG DTHD_MAC2LINUX_COMMIT=f80e853dcde532cfc0140e35060e1ec10548b448
ARG RUST_VERSION=1.97.1

ENV DEBIAN_FRONTEND=noninteractive \
    RUSTUP_HOME=/opt/rustup \
    CARGO_HOME=/opt/cargo \
    PATH=/opt/cargo/bin:$PATH \
    M2L_REPO=/opt/dthd-mac2linux \
    M2L_SOURCES=/opt/dthd-mac2linux-sources

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential binutils git curl xz-utils ca-certificates rpm2cpio cpio \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain "${RUST_VERSION}" --no-modify-path \
 && rustc --version

# The build reads its version from the release tag, so this is a full clone with tags.
RUN git clone -q https://github.com/truehdd/dthd-mac2linux.git "${M2L_REPO}" \
 && cd "${M2L_REPO}" \
 && git checkout -q "${DTHD_MAC2LINUX_COMMIT}" \
 && { [ "$(git describe --tags --exact-match 2>/dev/null)" = "${DTHD_MAC2LINUX_VERSION}" ] \
      || { echo "DTHD_MAC2LINUX_COMMIT is not the ${DTHD_MAC2LINUX_VERSION} tag"; exit 1; }; } \
 && cargo build -q --locked --release \
 && install -m 0755 target/release/dthd-mac2linux /usr/local/bin/dthd-mac2linux \
 && dthd-mac2linux fetch-sources "${M2L_SOURCES}" \
 && cargo fetch -q --locked \
 && chmod -R a+rX /opt

COPY build-encoder.sh /usr/local/bin/build-encoder.sh

ENTRYPOINT ["/bin/sh", "/usr/local/bin/build-encoder.sh"]
