FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      coreutils \
      curl \
      file \
      findutils \
      git \
      grep \
      gzip \
      make \
      sed \
      sudo \
      tar \
      unzip \
      wget \
      xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace/repo

CMD ["bash"]
