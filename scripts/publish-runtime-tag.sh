#!/bin/bash
# Points a registry tag at a reviewed HPC runtime image digest, preserving the digest.
#
# Usage: scripts/publish-runtime-tag.sh [--move] SOURCE REPOSITORY TAG DIGEST
#
#   SOURCE      skopeo source of the image: `oci-archive:<path>` (the archive CI
#               built and tested) or `docker://<repository>@<digest>`.
#   REPOSITORY  destination, such as ghcr.io/cerulean-works/nest-hpc-runtime.
#   TAG         destination tag.
#   DIGEST      sha256:<hex> that SOURCE must have and TAG must resolve to.
#
# Without --move the tag is immutable: if it already exists it must resolve to
# DIGEST (the copy is then skipped), otherwise the script fails and changes
# nothing. --move is for convenience tags such as `main`, which may be repointed,
# but only to a digest that already has an immutable tag. After the copy the tag
# is read back and must resolve to DIGEST.
#
# Registry credentials come from the caller's Docker or containers auth file
# (`docker login`); this script never reads or prints tokens. Set
# NEST_RUNTIME_PACKAGE_ABSENT=1 when the package is known not to exist yet, so an
# access-denied answer for a missing tag counts as "absent". Used by
# .github/workflows/hpc-runtime-image.yml; see docs/source/runtime-image.md.

set -euo pipefail

move=0
if [[ "${1:-}" == --move ]]; then
    move=1
    shift
fi
if [[ $# -ne 4 ]]; then
    sed -n '2,/^$/p' "$0" >&2
    exit 64
fi
source_ref=$1
repository=$2
tag=$3
digest=$4

if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "error: digest must be sha256:<64 hex digits>, got '$digest'" >&2
    exit 64
fi

errors=$(mktemp)
trap 'rm -f "$errors"' EXIT

# Prints the digest a reference resolves to, or `absent` if it does not exist.
resolve() {
    local ref=$1 raw
    if raw=$(skopeo inspect --raw "$ref" 2>"$errors"); then
        printf '%s' "$raw" | sha256sum | sed 's/^/sha256:/; s/ .*//'
    elif grep -qiE 'manifest unknown|name unknown|not found|404' "$errors"; then
        echo absent
    elif [[ "${NEST_RUNTIME_PACKAGE_ABSENT:-}" == 1 ]] && grep -qiE 'denied|unauthorized' "$errors"; then
        echo absent
    else
        echo "error: could not inspect $ref:" >&2
        cat "$errors" >&2
        echo "Check the registry login and that the token has package access." >&2
        return 1
    fi
}

if [[ "$source_ref" == oci-archive:* ]]; then
    archive=${source_ref#oci-archive:}
    source_digest=$(tar -xOf "$archive" index.json | python3 -c \
        'import json, sys; print(json.load(sys.stdin)["manifests"][0]["digest"])')
else
    source_digest=$(resolve "$source_ref")
fi
if [[ "$source_digest" != "$digest" ]]; then
    echo "error: $source_ref is $source_digest, expected the reviewed digest $digest" >&2
    exit 1
fi

current=$(resolve "docker://$repository:$tag")
if [[ "$current" == "$digest" ]]; then
    echo "$repository:$tag already resolves to $digest"
    exit 0
fi
if [[ "$current" != absent && $move -eq 0 ]]; then
    echo "error: immutable tag $repository:$tag already resolves to $current, not $digest." >&2
    echo "Published tags are never overwritten. Leave it, or remove it deliberately" >&2
    echo "after review; a rebuild of the same commit has a different digest." >&2
    exit 1
fi

skopeo copy --all --preserve-digests --retry-times 3 "$source_ref" "docker://$repository:$tag"

published=$(resolve "docker://$repository:$tag")
if [[ "$published" != "$digest" ]]; then
    echo "error: $repository:$tag resolves to $published after the copy, expected $digest" >&2
    exit 1
fi
echo "$repository:$tag -> $digest"
