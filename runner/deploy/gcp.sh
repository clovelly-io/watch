#!/usr/bin/env bash
# Clovelly Watch's runner on Google Cloud, beside a verifier you already run there
# (the sandbox SDK repository's deploy/verifier.sh): one Cloud Run service from the published
# image, reachable by GitHub's webhooks over HTTPS. The verifier's recipe is not edited.
# Written for bash 3.2.
#
#   runner/deploy/gcp.sh deploy PROJECT REGION CONFIG_DIR [SERVICE]
#       Enables the Cloud Run, Secret Manager and Artifact Registry APIs; stores CONFIG_DIR
#       (what `clovelly-watch-runner init` wrote, with runner-token beside it) as Secret Manager
#       secret SERVICE-config; lets the service's runtime account (the project's default compute
#       account) read its three secrets (roles/secretmanager.secretAccessor on each, an IAM
#       change this script makes); and deploys Cloud Run service SERVICE (default watch-runner):
#       one instance, always on, unauthenticated (GitHub signs its webhooks and the runner
#       checks each signature). Prints the service's URL: the webhook address is
#       URL/github/REPO_ID.
#
#   RUNNER_IMAGE                    the published runner image, by digest (the release page).
#                                   Cloud Run cannot pull from ghcr.io, so it comes through an
#                                   Artifact Registry remote repository, clovelly-ghcr, made in
#                                   PROJECT if missing (as the verifier's recipe does).
#   CLOVELLY_API_URL                the control plane, as for any program using the sandbox SDK
#   CLOVELLY_API_CA                 optional: a file with the control plane's CA (PEM), copied
#                                   into the configuration
#   Secret Manager secrets in PROJECT: watch-runner-api-key (a read_write API key of your
#   environment) and watch-runner-verifier-token (your verifier's SDK token).
#
# An organization policy that restricts IAM members to your domains
# (constraints/iam.allowedPolicyMemberDomains) refuses the unauthenticated invoker (allUsers):
# exempt this project, or put the service behind a load balancer that admits GitHub's
# webhooks, before you run this.
#
# Needs gcloud, logged in as someone who may create these resources and grant roles on
# secrets, and tar.
set -euo pipefail

# What Cloud Run pulls: a ghcr.io image through the remote repository clovelly-ghcr, with the
# same digest.
pullable() {
  local project=$1 region=$2 image=$3
  case $image in
    ghcr.io/*)
      gcloud artifacts repositories describe clovelly-ghcr --project "$project" --location "$region" >/dev/null 2>&1 \
        || gcloud artifacts repositories create clovelly-ghcr --project "$project" --location "$region" --repository-format docker \
          --mode remote-repository --remote-docker-repo https://ghcr.io --description "ghcr.io, for the published Clovelly images" >&2
      printf '%s-docker.pkg.dev/%s/clovelly-ghcr/%s\n' "$region" "$project" "${image#ghcr.io/}"
      ;;
    *) printf '%s\n' "$image" ;;
  esac
}

deploy() {
  local project=$1 region=$2 dir=$3 service=${4:-watch-runner} image account s ca=""
  [ -f "$dir/config.json" ] && [ -f "$dir/runner-token" ] || { echo "$dir lacks config.json or runner-token" >&2; exit 2; }
  gcloud services enable run.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com --project "$project" >&2
  image=$(pullable "$project" "$region" "${RUNNER_IMAGE:?set RUNNER_IMAGE}")
  if [ -n "${CLOVELLY_API_CA:-}" ]; then
    cp "$CLOVELLY_API_CA" "$dir/control-ca.pem"
    ca=/config/control-ca.pem
  fi
  gcloud secrets describe "$service-config" --project "$project" >/dev/null 2>&1 \
    || gcloud secrets create "$service-config" --project "$project" --replication-policy automatic >/dev/null
  tar -C "$dir" -czf - . | gcloud secrets versions add "$service-config" --project "$project" --data-file=- >/dev/null
  account=$(gcloud projects describe "$project" --format 'value(projectNumber)')-compute@developer.gserviceaccount.com
  for s in "$service-config" watch-runner-api-key watch-runner-verifier-token; do
    gcloud secrets add-iam-policy-binding "$s" --project "$project" --member "serviceAccount:$account" \
      --role roles/secretmanager.secretAccessor >/dev/null
  done
  gcloud run deploy "$service" --project "$project" --region "$region" --image "$image" \
    --allow-unauthenticated --min-instances 1 --max-instances 1 --no-cpu-throttling --port 8090 \
    --set-env-vars "CLOVELLY_API_URL=${CLOVELLY_API_URL:?},CLOVELLY_API_CA=$ca" \
    --set-secrets "/secrets/config.tgz=$service-config:latest,CLOVELLY_API_TOKEN=watch-runner-api-key:latest,CLOVELLY_VERIFIER_TOKEN=watch-runner-verifier-token:latest" \
    --command sh --args "-c,mkdir -p /config && tar -xzf /secrets/config.tgz -C /config && exec clovelly-watch-runner serve /config/config.json" >&2
  gcloud run services describe "$service" --project "$project" --region "$region" --format 'value(status.url)'
}

case ${1:-} in
  deploy) shift; deploy "$@" ;;
  *) sed -n '2,35p' "$0"; exit 2 ;;
esac
