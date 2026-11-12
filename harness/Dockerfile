# Clovelly Watch's base image (SPEC-watch.md, "The template and the two jobs"): the harness,
# the Claude Agent SDK (whose wheel bundles the Claude Code binary, unmodified), git, Python
# and Node. A repository whose tests need more builds its template FROM this image.
#
# Built from the repository's root: docker build -f harness/Dockerfile -t clovelly-watch-base .
# Published by digest; the release pins the node image by digest too.
FROM mirror.gcr.io/library/node:24-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY runner /src/runner
COPY harness /src/harness
RUN python3 -m venv /opt/watch \
 && /opt/watch/bin/pip install --no-cache-dir --disable-pip-version-check /src/runner /src/harness \
 && rm -rf /src \
 && ln -s /opt/watch/bin/clovelly-watch-harness /usr/local/bin/clovelly-watch-harness

# The session runs as an ordinary user: Claude Code refuses to run as root with some
# permission modes, and nothing in a session needs root.
RUN useradd --create-home --uid 1001 watch && mkdir /job && chown watch /job
USER watch
WORKDIR /job
