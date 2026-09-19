#!/usr/bin/env bash
#
# Verify a built distribution before it is trusted.
#
# ONE DEFINITION, TWO CALL SITES: ci.yml runs it on every PR, release.yml runs it between
# `build` and `create-github-release`. That second call is the point of the script. Before it
# existed there was no release workflow at all — all eight releases (v0.1.0 -> v0.7.0) were cut
# by hand, and nothing ever asserted anything about the artefact attached to them.
#
# scribe does not publish to PyPI (that is part 5 of this programme), so a bad artefact here is
# recoverable in a way a bad PyPI upload is not. The check still runs on the release path,
# because the point is that the path which cannot be re-run by a reviewer asserts the same
# things the PR path does, before attaching rather than after.
#
# Usage:  verify-dist.sh [DIST_DIR] [EXPECTED_VERSION]
#
#   DIST_DIR          defaults to ./dist
#   EXPECTED_VERSION  optional. When set (release.yml passes the tag with the leading `v`
#                     stripped) the artefact must carry exactly this version. This is the
#                     assertion the release path needs and the PR path cannot make: the job
#                     that attaches is not the job that built, and it trusts a downloaded
#                     artifact.
set -euo pipefail

DIST_DIR="${1:-dist}"
EXPECTED_VERSION="${2:-}"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "  ok: $*"; }

[ -d "$DIST_DIR" ] || fail "no such directory: $DIST_DIR"

echo "== verify-dist: $DIST_DIR =="

# --------------------------------------------------------------------------
# 1. Exactly one wheel and one sdist.
#
# More than one means a stale build is sitting in the directory, and any `dist/*.whl` glob
# would then pick one nondeterministically. Zero means the build silently produced nothing,
# which `set -e` on `python -m build` does not always catch.
# --------------------------------------------------------------------------
shopt -s nullglob
wheels=("$DIST_DIR"/*.whl)
sdists=("$DIST_DIR"/*.tar.gz)
shopt -u nullglob

[ "${#wheels[@]}" -eq 1 ] || fail "expected exactly 1 wheel in $DIST_DIR, found ${#wheels[@]}: ${wheels[*]:-none}"
[ "${#sdists[@]}" -eq 1 ] || fail "expected exactly 1 sdist in $DIST_DIR, found ${#sdists[@]}: ${sdists[*]:-none}"
WHEEL="${wheels[0]}"
SDIST="${sdists[0]}"
ok "one wheel ($(basename "$WHEEL")) and one sdist ($(basename "$SDIST"))"

# --------------------------------------------------------------------------
# 2. Version agreement.
#
# pyproject.toml is the source of truth. The wheel and sdist filenames must agree with it,
# and with EXPECTED_VERSION when the caller supplies one.
#
# NOTE: src/scribe/__init__.py carries __version__ separately, and tests/test_version.py is
# what keeps those two in step. This script deliberately reads pyproject rather than importing
# the package, because at this point the package is not installed yet — and because a stale
# __version__ is exactly the failure that test exists to catch, so trusting it here would make
# the two checks share a blind spot.
# --------------------------------------------------------------------------
PYPROJECT_VERSION="$(python3 -c '
import re, sys, pathlib
text = pathlib.Path("pyproject.toml").read_text(encoding="utf-8")
m = re.search(r"^version\s*=\s*\"([^\"]+)\"", text, re.M)
if not m:
    sys.exit("could not read version from pyproject.toml")
print(m.group(1))
')"

WHEEL_VERSION="$(basename "$WHEEL" | awk -F- '{print $2}')"
SDIST_VERSION="$(basename "$SDIST" | sed -E 's/^scribe-(.+)\.tar\.gz$/\1/')"

[ "$WHEEL_VERSION" = "$PYPROJECT_VERSION" ] \
  || fail "wheel version $WHEEL_VERSION != pyproject version $PYPROJECT_VERSION"
[ "$SDIST_VERSION" = "$PYPROJECT_VERSION" ] \
  || fail "sdist version $SDIST_VERSION != pyproject version $PYPROJECT_VERSION"
ok "wheel, sdist and pyproject all say $PYPROJECT_VERSION"

if [ -n "$EXPECTED_VERSION" ]; then
  [ "$PYPROJECT_VERSION" = "$EXPECTED_VERSION" ] \
    || fail "tag says $EXPECTED_VERSION but the artefact is $PYPROJECT_VERSION — refusing to attach"
  ok "artefact version matches the tag ($EXPECTED_VERSION)"
fi

# --------------------------------------------------------------------------
# 3. Wheel top-level layout.
#
# A wheel for this project should contain exactly two top-level entries: the package and its
# dist-info. Anything else is build context that escaped into the artefact — the usual culprit
# being a stray `tests/` from a misconfigured `packages.find`.
# --------------------------------------------------------------------------
mapfile -t top_level < <(unzip -Z1 "$WHEEL" | awk -F/ 'NF>1 {print $1}' | sort -u)
expected_dist_info="scribe-${PYPROJECT_VERSION}.dist-info"
for entry in "${top_level[@]}"; do
  case "$entry" in
    scribe|"$expected_dist_info") ;;
    *) fail "unexpected top-level entry in wheel: $entry (expected only scribe/ and $expected_dist_info/)" ;;
  esac
done
[ "${#top_level[@]}" -eq 2 ] || fail "wheel has ${#top_level[@]} top-level entries, expected 2: ${top_level[*]}"
ok "wheel top level is exactly scribe/ and $expected_dist_info/"

# --------------------------------------------------------------------------
# 4. Nothing that should never ship, in EITHER artefact.
#
# The directory checks apply to the wheel only — an sdist legitimately carries tests/ and
# docs/, that is what an sdist is for. The secret-pattern check applies to both. Splitting
# them matters: a blanket rule over the sdist would either permit everything and catch
# nothing, or reject a correct sdist.
# --------------------------------------------------------------------------
wheel_manifest="$(unzip -Z1 "$WHEEL")"
for forbidden in tests/ test/ docs/ hooks/ build/ .github/ .venv/; do
  if grep -qE "(^|/)${forbidden//./\\.}" <<<"$wheel_manifest"; then
    fail "wheel ships $forbidden — build context escaped into the artefact"
  fi
done
ok "wheel ships no tests/, docs/, hooks/, build/, .github/ or .venv/"

# Secret-shaped filenames. A backstop, not the primary control — .gitignore and the gitleaks
# gate are — but it is the last check before an artefact is attached to a public release.
sdist_manifest="$(tar tzf "$SDIST")"
secret_hits="$(printf '%s\n%s\n' "$wheel_manifest" "$sdist_manifest" \
  | grep -EI '(^|/)(\.env($|\..*)|.*\.(key|pem|p12|pfx)|secrets?\.(ya?ml|json|toml)|id_(rsa|ed25519)|.*\.kdbx)$' || true)"
if [ -n "$secret_hits" ]; then
  # Prints the matched PATHS only, never their contents — vikunja#606, keep exploitable
  # detail out of CI logs on a repo whose logs are readable.
  fail "secret-shaped file(s) in a built artefact:"$'\n'"$secret_hits"
fi
ok "no secret-shaped filenames in either artefact"

# Real transcripts and state databases must never be inside a build. scribe's whole input
# domain is session transcripts containing whatever was typed into a terminal, so this is a
# more pointed risk here than the generic secret-name check above.
data_hits="$(printf '%s\n%s\n' "$wheel_manifest" "$sdist_manifest" \
  | grep -EI '\.(jsonl|sqlite3?|db)$' || true)"
if [ -n "$data_hits" ]; then
  fail "transcript- or state-shaped file(s) in a built artefact:"$'\n'"$data_hits"
fi
ok "no .jsonl transcripts or .sqlite3 state files in either artefact"

# --------------------------------------------------------------------------
# 5. The package is actually in there.
#
# Assertions that things are ABSENT pass trivially against an empty artefact. This is the
# paired positive: without it, a wheel containing nothing but dist-info sails through
# everything above.
# --------------------------------------------------------------------------
for required in \
  "scribe/__init__.py" \
  "scribe/__main__.py" \
  "scribe/state.py" \
  "scribe/pipeline.py" \
  "scribe/extract/__init__.py" \
  "scribe/extract/parser.py" \
  "scribe/extract/redact.py" \
  "${expected_dist_info}/METADATA" \
  "${expected_dist_info}/entry_points.txt"
do
  grep -qxF "$required" <<<"$wheel_manifest" || fail "wheel is missing $required"
done
ok "wheel carries the package, its metadata and the entry point"

grep -qE "^${expected_dist_info}/(licenses/)?LICENSE" <<<"$wheel_manifest" \
  || fail "wheel does not carry the LICENSE"
ok "wheel carries the licence"

# The console script is the documented way to run the extractor. A wheel that installs but
# exposes no entry point is broken in a way `import scribe` cannot detect.
unzip -p "$WHEEL" "${expected_dist_info}/entry_points.txt" | grep -q '^scribe-extract *=' \
  || fail "entry_points.txt does not declare the scribe-extract console script"
ok "entry_points.txt declares the scribe-extract console script"

# --------------------------------------------------------------------------
# 6. Install smoke.
#
# Into a throwaway venv rather than the job's environment: installing into the ambient
# environment lets the subsequent import succeed against the source tree or an already
# installed copy rather than against the wheel under test.
# --------------------------------------------------------------------------
VENV_DIR="$(mktemp -d)"
trap 'rm -rf "$VENV_DIR"' EXIT
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --disable-pip-version-check "$WHEEL"

installed_version="$("$VENV_DIR/bin/python" -c 'import scribe; print(scribe.__version__)')"
[ "$installed_version" = "$PYPROJECT_VERSION" ] \
  || fail "installed package reports $installed_version, artefact claims $PYPROJECT_VERSION"
ok "installs into a clean venv and reports $installed_version"

# --------------------------------------------------------------------------
# 7. `scribe.extract` is stdlib-only IN THE ARTEFACT.
#
# tests/test_stdlib_only.py asserts this by walking imports in src/. This asserts it of the
# thing a consumer installs, and it asserts it differently: the wheel is installed into a venv
# where httpx is NOT present, and the extractor is imported anyway. An AST walk can be fooled
# by a deferred import it does not model; an import into an environment that lacks the
# dependency cannot.
#
# This is the invariant pyproject.toml's own comment calls out as the reason httpx stays out
# of `scribe.extract` — "a packaging convention is not a guarantee". Neither is a source-tree
# check, quite.
#
# A SECOND VENV, INSTALLED --no-deps. It cannot be the venv above: httpx is a declared runtime
# dependency of this package, so a normal install puts it there and the import below would
# succeed no matter what `scribe.extract` imports. That is not a hypothetical — the first
# version of this script reused $VENV_DIR and the vacuity guard immediately caught it.
NODEPS_DIR="$(mktemp -d)"
trap 'rm -rf "$VENV_DIR" "$NODEPS_DIR"' EXIT
python3 -m venv "$NODEPS_DIR"
"$NODEPS_DIR/bin/pip" install --quiet --disable-pip-version-check --no-deps "$WHEEL"

# The guard that keeps the assertion meaningful. If httpx is somehow present, the import below
# proves nothing and must not be reported as proof.
if "$NODEPS_DIR/bin/python" -c 'import httpx' 2>/dev/null; then
  fail "httpx is present in the --no-deps venv — the stdlib-only check would be vacuous"
fi

"$NODEPS_DIR/bin/python" -c '
import scribe.extract
from scribe.extract import extract, Redactor, EventLog   # noqa: F401
' || fail "the installed scribe.extract cannot be imported without httpx — it is not stdlib-only"
ok "installed scribe.extract imports with no httpx present"

# --------------------------------------------------------------------------
# 8. The artefact enforces the contract that would make shipping it harmful if it were wrong.
#
# For scribe that is REDACTION. Every other defect produces a bad summary; this one writes a
# live credential into a digest file that gets indexed and synced.
#
# BOTH DIRECTIONS, AND THE ACCEPT CASE IS NOT OPTIONAL. A redactor that replaced its entire
# input would satisfy every "the secret is gone" assertion below while destroying the whole
# signal the event log exists to carry. tests/test_redact.py says the suite is "deliberately
# weighted toward the two ways a redactor fails quietly" for this reason; the same weighting
# has to survive into the artefact-level check, or this becomes a test that cannot fail in the
# direction that matters.
# --------------------------------------------------------------------------
"$NODEPS_DIR/bin/python" - <<'PY' || fail "the installed wheel does not enforce the redaction contract"
import sys
from scribe.extract import REDACTED, Redactor

problems = []

# REFUSE — secret-shaped values must not survive. Synthetic literals only; every one of these
# is a FAKE marker string, not a credential.
secrets = {
    "bearer token":  "curl -H 'Authorization: Bearer sk-ant-api03-FAKEdeadbeef1234567890' https://api",
    "env assignment": "export GITHUB_TOKEN=ghp_FAKE1234567890abcdefgh",
    "json value":     '{"api_key": "FAKElivekey12345"}',
    "aws key":        "aws key AKIAFAKE1234567890XY here",
    "private key":    "-----BEGIN RSA PRIVATE KEY-----\nFAKEkeymaterial\n-----END RSA PRIVATE KEY-----",
}
for label, text in secrets.items():
    out = Redactor().scrub(text)
    if REDACTED not in out:
        problems.append(f"did NOT redact a {label}")
    # The literal must be gone, not merely accompanied by a marker.
    for fragment in ("FAKEdeadbeef1234567890", "ghp_FAKE1234567890abcdefgh",
                     "FAKElivekey12345", "AKIAFAKE1234567890XY", "FAKEkeymaterial"):
        if fragment in text and fragment in out:
            problems.append(f"left the {label} literal {fragment!r} in the output")

# ACCEPT — ordinary text must come through untouched. This is the control, and it is the half
# a refusal-only test cannot make: it is what distinguishes a working redactor from one that
# scrubs indiscriminately.
benign = [
    "ran pytest -q and 412 tests passed",
    "the file is at src/scribe/extract/parser.py line 240",
    "PATH=/usr/bin",
    "git commit -m 'fix the offset accounting'",
]
for text in benign:
    r = Redactor()
    out = r.scrub(text)
    if out != text:
        problems.append(f"MODIFIED benign text {text!r} -> {out!r}")
    if r.count != 0:
        problems.append(f"counted {r.count} redactions in benign text {text!r}")

# The count must correspond to what was removed — a redactor reporting 0 while scrubbing, or
# reporting a number while scrubbing nothing, is how the loss goes unnoticed.
r = Redactor()
r.scrub("export GITHUB_TOKEN=ghp_FAKE1234567890abcdefgh")
if r.count < 1:
    problems.append(f"redacted a secret but reported count={r.count}")

if problems:
    for p in problems:
        print(f"    redaction contract: {p}", file=sys.stderr)
    sys.exit(1)
print(f"    redaction contract: {len(secrets)} secrets removed, {len(benign)} benign strings untouched")
PY
ok "the installed wheel enforces the redaction contract in both directions"

echo "== verify-dist: PASS =="
