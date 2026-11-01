FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      sway wl-mirror grim jq procps psmisc ca-certificates fonts-dejavu-core \
      libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libatspi2.0-0t64 libgtk-3-0t64 \
      libgbm1 libasound2t64 libdrm2 libxkbcommon0 libcups2t64 libxcomposite1 libxdamage1 \
      libxrandr2 libxfixes3 libpango-1.0-0 libcairo2 libexpat1 libdbus-1-3 libxshmfence1 \
  && rm -rf /var/lib/apt/lists/*
RUN useradd -m -s /bin/bash kid
