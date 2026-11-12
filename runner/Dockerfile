# The runner's image (SPEC-watch.md, "The runner"), published by digest as the verifier is.
# Built from the repository's root: docker build -f runner/Dockerfile -t clovelly-watch-runner .
FROM python:3.12-slim-bookworm
COPY runner /src/runner
RUN pip install --no-cache-dir --disable-pip-version-check "/src/runner[sandbox]" && rm -rf /src \
 && useradd --create-home --uid 1001 runner \
 && mkdir /config && chown runner:runner /config
USER runner
# The directory `init` wrote (config.json, the runner token, each repository's secrets).
VOLUME /config
EXPOSE 8090
ENTRYPOINT ["clovelly-watch-runner"]
CMD ["serve", "/config/config.json"]
