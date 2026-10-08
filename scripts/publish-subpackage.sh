#!/usr/bin/env bash
# Publishes the TS subpackage in the working directory as VERSION: npm under
# the `dev` dist-tag on the dev channel, npm then JSR (CLI JSR_VERSION) on
# stable. A registry that already serves the version is skipped, so a rerun
# completes a partial publish. OIDC only: trusted publishers at npmjs.com and
# jsr.io.
set -euo pipefail
: "${VERSION:?}" "${CHANNEL:?}"

ver="${VERSION#v}"
pkg="$(npm pkg get name | tr -d '\"')"
npm pkg set version="$ver"
node -e "const fs=require('fs'),f='jsr.json',j=JSON.parse(fs.readFileSync(f));j.version='${ver}';fs.writeFileSync(f,JSON.stringify(j,null,2)+'\n')"
NPM_TAG_ARGS=()
if [ "$CHANNEL" != "stable" ]; then
  NPM_TAG_ARGS=(--tag dev)
fi
if npm view "$pkg@$ver" version >/dev/null 2>&1; then
  echo "::notice::npm: $pkg@$ver already published — skipping"
else
  npm publish --access public "${NPM_TAG_ARGS[@]}"
fi
if [ "$CHANNEL" != "stable" ]; then
  echo "dev channel: JSR not published"
  exit 0
fi
: "${JSR_VERSION:?}"
if curl -fsSL "https://jsr.io/${pkg}/meta.json" \
  | jq -e --arg v "$ver" '.versions[$v]' >/dev/null 2>&1; then
  echo "::notice::jsr: $pkg@$ver already published — skipping"
else
  # deno's `jsr publish` uses BYONM, so runtime dependencies (e.g.
  # @cplieger/actions -> @cplieger/reactive) must already be in node_modules,
  # which the npm publish above does not install.
  npm install --no-save
  npx -y "jsr@${JSR_VERSION}" publish --allow-dirty
fi
