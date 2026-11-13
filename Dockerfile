# syntax=docker/dockerfile:1

# BEQ Encoder: best audio track -> BEQ filter -> lossless TrueHD (Atmos or channel-based)
# via the Dolby Encoding Engine 6.7 TrueHD encoder, run through dee_shim -> extra track
# in the same MKV.
#
# The Dolby Encoding Engine is not part of the image. Its host folder is bind-mounted
# read-only at runtime and found through the DEE_PATH environment variable (set from .env
# by compose.yaml): the Linux DEE 6.7 tools with the dthd-mac2linux build of
# dee_dthd_encoder in it. The beqforge designer runs as a separate service (see
# compose.yaml).

ARG BASE_IMAGE=debian:trixie-slim
# The dee_shim release built into the image, the commit its tag points at, and the Rust
# toolchain that release is built with (RUST_TOOLCHAIN in its release workflow).
# .github/workflows/tooling-updates.yml opens a PR when a newer release is out.
ARG DEE_SHIM_VERSION=v0.2.0
ARG DEE_SHIM_COMMIT=396caf4980cd878faead7f028e58300b026d6d71
ARG DEE_SHIM_RUST=1.97.1
# The truehdd release, and a later commit to build from source in its place ("" for the
# release). The commit is pinned only until a release includes it: tooling-updates.yml opens
# a PR when a newer release is out, and that PR clears it.
ARG TRUEHDD_VERSION=0.6.2
ARG TRUEHDD_COMMIT=0e800fbd720348baf57061f0e47db05a0b638d15

# cavern-dump: decodes DD+ Atmos (E-AC-3 + JOC) into its objects as a Dolby Atmos master
# file set, with the Cavern library (https://github.com/VoidXH/Cavern). Cavern's licence
# (LICENSE.md in that repository) allows free, non-commercial use and modification; an
# image bundling it must stay free, credit the creator and link the repository.
# Set CAVERN_COMMIT="" to build without it (DD+ Atmos tracks are then refused).
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS cavern
ARG CAVERN_COMMIT=c6426fe81f0c7b022483e7feab3fdc0bc20189e5
COPY cavern/CavernDump /build/CavernDump
RUN mkdir -p /out && if [ -n "${CAVERN_COMMIT}" ]; then       git clone -q https://github.com/VoidXH/Cavern.git /build/Cavern    && git -C /build/Cavern checkout -q "${CAVERN_COMMIT}"    && cp /build/Cavern/LICENSE.md /out/CAVERN-LICENSE.md    && cd /build/CavernDump    && dotnet publish -c Release -r linux-x64 --self-contained true -nologo -v q -o /out    && /out/cavern-dump 2>&1 | grep -q usage; fi

# dee_shim (https://github.com/truehdd/dee-shim, Apache-2.0): runs DEE's TrueHD encoder
# with --as-is / --oamd-as-is / --drc, which make the encode sample-exact. Built from the
# DEE_SHIM_VERSION release's commit with that release's toolchain, unmodified.
# Linux x86_64 only, as DEE is.
FROM rust:${DEE_SHIM_RUST}-trixie AS dee-shim
ARG DEE_SHIM_VERSION
ARG DEE_SHIM_COMMIT
RUN git clone -q https://github.com/truehdd/dee-shim.git /build/dee-shim \
 && cd /build/dee-shim \
 && git checkout -q "${DEE_SHIM_COMMIT}" \
 && { [ "$(git describe --tags --exact-match 2>/dev/null)" = "${DEE_SHIM_VERSION}" ] \
      || { echo "DEE_SHIM_COMMIT is not the ${DEE_SHIM_VERSION} tag"; exit 1; }; } \
 && cargo build -q --locked --release -p dee_shim \
 && mkdir -p /out/doc \
 && cp target/release/dee_shim /out/ \
 && cp LICENSE NOTICE THIRD_PARTY_LICENSES.txt /out/doc/ \
 && /out/dee_shim -V

# truehdd (https://github.com/truehdd/truehdd): TrueHD / TrueHD Atmos -> DAMF decoder. The
# TRUEHDD_VERSION release's static musl build, or TRUEHDD_COMMIT built from source, which
# must come after that release. The source build uses the dee_shim toolchain: truehdd
# builds with any stable Rust from its rust-version (1.95) on.
FROM rust:${DEE_SHIM_RUST}-trixie AS truehdd
ARG TRUEHDD_VERSION
ARG TRUEHDD_COMMIT
ARG TARGETARCH=amd64
RUN mkdir -p /out/doc \
 && if [ -n "${TRUEHDD_COMMIT}" ]; then \
      git clone -q https://github.com/truehdd/truehdd.git /build/truehdd \
      && cd /build/truehdd \
      && git checkout -q "${TRUEHDD_COMMIT}" \
      && { git merge-base --is-ancestor "${TRUEHDD_VERSION}" HEAD \
           || { echo "TRUEHDD_COMMIT does not come after ${TRUEHDD_VERSION}"; exit 1; }; } \
      && cargo build -q --locked --release \
      && cp target/release/truehdd /out/ \
      && { cp LICENSE* NOTICE* /out/doc/ 2>/dev/null || true; }; \
    else \
      case "${TARGETARCH}" in \
        amd64) arch=x86_64 ;; arm64) arch=aarch64 ;; *) echo "unsupported arch ${TARGETARCH}"; exit 1 ;; \
      esac \
      && mkdir -p /build/truehdd \
      && curl -fsSL "https://github.com/truehdd/truehdd/releases/download/${TRUEHDD_VERSION}/truehdd-${TRUEHDD_VERSION}-${arch}-unknown-linux-musl.tar.gz" \
         | tar -xz -C /build/truehdd --no-same-owner \
      && cp /build/truehdd/truehdd /out/ \
      && { cp /build/truehdd/LICENSE* /build/truehdd/NOTICE* /out/doc/ 2>/dev/null || true; }; \
    fi \
 && chmod 0755 /out/truehdd \
 && /out/truehdd --version

FROM ${BASE_IMAGE}

ARG TARGETARCH=amd64

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    HOME=/config/home \
    CONFIG_DIR=/config \
    WORK_DIR=/work \
    MEDIA_ROOTS=/media \
    DEE_PATH=/opt/dee \
    DEE_SHIM=/usr/local/bin/dee_shim \
    MPLCONFIGDIR=/opt/matplotlib

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv ca-certificates curl tini \
      ffmpeg mkvtoolnix mediainfo libmediainfo0v5 \
 && rm -rf /var/lib/apt/lists/*

# truehdd, with its licence and notices where its source or archive has them
COPY --from=truehdd /out/truehdd /usr/local/bin/truehdd
COPY --from=truehdd /out/doc /usr/local/share/doc/truehdd
RUN truehdd --version

# dee_shim, with its licence and notices
COPY --from=dee-shim /out/dee_shim /usr/local/bin/dee_shim
COPY --from=dee-shim /out/doc /usr/local/share/doc/dee-shim
RUN [ "${TARGETARCH}" = amd64 ] || { echo "dee_shim is x86_64 only, as DEE is"; exit 1; } \
 && dee_shim -V

# Inter for the before/after charts (falls back to DejaVu Sans without it)
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-inter \
 && rm -rf /var/lib/apt/lists/*

COPY --from=cavern /out /opt/cavern-dump
ENV CAVERN_DUMP=/opt/cavern-dump/cavern-dump

# --chmod: a Linux checkout keeps the mode git stores, a Windows build context marks every file executable
COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY app /src/app
RUN python3 -m venv "$VIRTUAL_ENV" \
 && pip install /src/app \
 && rm -rf /src/app \
 && python3 -c "import beq_encoder.pipeline, beq_encoder.synccheck, beq_encoder.charts" \
 && mkdir -p "$MPLCONFIGDIR" \
 && python3 -c "import matplotlib.pyplot; from matplotlib import font_manager as f; \
assert 'Inter' in {x.name for x in f.fontManager.ttflist}, 'Inter font missing'" \
 && chmod -R a+rwX "$MPLCONFIGDIR"

RUN mkdir -p /config /work /media && chmod 0777 /config /work

EXPOSE 8080
VOLUME ["/config", "/work"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python3 -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["beq-encoder"]
