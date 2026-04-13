# BIND 9 wrapped with libfaketime.
#
# BIND and libfaketime are both built from source:
#
#   * BIND: Debian's bind9 package links against jemalloc, which spawns
#     its own internal threads and deadlocks against libfaketime's
#     function interposition during process startup (named hangs in
#     jemalloc's futex before printing its banner — confirmed via
#     strace). BIND upstream supports --without-jemalloc, so we build
#     that way. Ripping out the jemalloc DT_NEEDED via patchelf on the
#     Debian binary does NOT work: BIND actually calls jemalloc-specific
#     symbols like mallocx() which aren't in glibc.
#
#   * libfaketime: Debian ships 0.9.10, which has a clock_gettime()
#     reentrancy bug triggered during BIND's startup. v0.9.11 fixes it.
#
# To swap BIND versions, change BIND_VERSION below.

# ---------------------------------------------------------------------------
# Stage 1: build BIND and libfaketime
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS build

ARG BIND_VERSION=9.18.47
ARG LIBFAKETIME_REF=v0.9.11

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git ca-certificates curl xz-utils pkg-config \
        libssl-dev libuv1-dev libcap-dev liblmdb-dev libmaxminddb-dev \
        libfstrm-dev libprotobuf-c-dev libxml2-dev libjson-c-dev \
        libkrb5-dev libnghttp2-dev python3 \
    && rm -rf /var/lib/apt/lists/*

# BIND from source, --without-jemalloc to avoid the startup deadlock.
# Install to /opt/bind so it's easy to COPY --from= into the runtime image.
RUN curl -fsSL "https://downloads.isc.org/isc/bind9/${BIND_VERSION}/bind-${BIND_VERSION}.tar.xz" \
      | tar -xJ -C /tmp \
    && cd "/tmp/bind-${BIND_VERSION}" \
    && ./configure --prefix=/opt/bind --sysconfdir=/etc/bind \
         --localstatedir=/var --runstatedir=/run \
         --without-jemalloc \
         --with-openssl --with-libuv --with-libidn2=no \
         --disable-doc \
    && make -j"$(nproc)" \
    && make install

# libfaketime from source (v0.9.11+) — bookworm's 0.9.10 has the
# clock_gettime reentrancy bug.
RUN git clone --depth=1 --branch ${LIBFAKETIME_REF} \
        https://github.com/wolfcw/libfaketime.git /tmp/libfaketime \
    && make -C /tmp/libfaketime -j"$(nproc)" \
    && make -C /tmp/libfaketime PREFIX=/opt/faketime install

# ---------------------------------------------------------------------------
# Stage 2: runtime image
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl3 libuv1 libcap2 liblmdb0 libmaxminddb0 libfstrm0 \
        libprotobuf-c1 libxml2 libjson-c5 libkrb5-3 libgssapi-krb5-2 \
        libnghttp2-14 \
        tini ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system bind && useradd --system --gid bind --home-dir /var/lib/bind bind

# BIND binaries + libs (built --without-jemalloc).
COPY --from=build /opt/bind /opt/bind
COPY --from=build /opt/faketime /opt/faketime

# Make BIND binaries available as /usr/sbin/named etc.
ENV PATH=/opt/bind/sbin:/opt/bind/bin:$PATH
RUN ln -sf /opt/bind/sbin/named /usr/sbin/named \
    && ln -sf /opt/bind/sbin/rndc /usr/sbin/rndc \
    && ln -sf /opt/bind/sbin/rndc-confgen /usr/sbin/rndc-confgen \
    && ln -sf /opt/bind/bin/dig /usr/bin/dig \
    && ldconfig /opt/bind/lib

# libfaketime config.  See earlier comment in git history for why we
# can't use FAKETIME_NO_CACHE=1 or FAKETIME_FORCE_MONOTONIC_FIX.
#
# The multi-threaded (MT) variant is required — BIND's worker threads all
# hit clock_gettime concurrently, and the single-threaded libfaketime.so
# races on its internal state.
ENV FAKETIME_LIB=/opt/faketime/lib/faketime/libfaketimeMT.so.1
ENV FAKETIME_TIMESTAMP_FILE=/opt/lab/faketime.rc
ENV FAKETIME_CACHE_DURATION=1
# Don't fake CLOCK_MONOTONIC. Faking it is what triggered the 0.9.10
# recursion; even with 0.9.11 fixed, BIND uses CLOCK_MONOTONIC for its
# internal timer queue — leaving it at real-time avoids timer drift
# between BIND's sleeps and its scheduling decisions.
ENV DONT_FAKE_MONOTONIC=1

RUN mkdir -p /opt/lab /var/lib/bind /var/log/bind /run/named /etc/bind \
    && chown -R bind:bind /var/lib/bind /var/log/bind /run/named

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 53/udp 53/tcp 953/tcp

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
