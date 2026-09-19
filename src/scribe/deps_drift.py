"""Compare the dependency set forge actually runs against the one this repo locks.

**Why this exists: the tree CI audits is not the tree forge runs.**

`ci.yml`'s audit job exports `uv.lock` to a PEP 751 pylock and audits that. But scribe is
deployed by `venv-deploy.sh`, which builds a wheel from source and pip-installs it —
**re-resolving from the bounded ranges in `pyproject.toml` at deploy time**. Nothing consults
`uv.lock` on forge, and `uv` is not installed there.

So a green audit in CI is a statement about a resolution forge may never have installed. That
is vikunja#633's failure in the opposite costume: there, the audit re-resolved while the
container shipped a pinned tree; here, the audit reads a pinned tree while the host
re-resolves.

Making `venv-deploy.sh` install from the lock is the correct long-term fix and is deliberately
**out of scope** — it is a root-owned production script serving 20+ services and belongs to
sysadmin. What this module does instead is make the divergence **observable**, so that "the
audit covers what we run" stops being an assumption and becomes a measurement.

**It reports; it does not fail.** Drift is expected by construction today. A check that went
red on the expected state would be turned off within a week, and then the unexpected state
would arrive unannounced.

Stdlib only, deliberately: this runs on forge, where the point is precisely that the tooling
used to produce the lock is absent.
"""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

#: Packages that are venv furniture rather than dependencies. Their presence in the deployed
#: environment is not drift, and reporting them as such would put three permanent rows in
#: every report — which is how a report stops being read.
_FURNITURE = frozenset({"pip", "setuptools", "wheel", "pkg-resources"})

_NAME_RE = re.compile(r"[-_.]+")


def normalize(name: str) -> str:
    """PEP 503 name normalisation.

    Not cosmetic. The lock says `typing-extensions` and the installed dist-info says
    `typing_extensions`; comparing raw names reports a package as both missing from the
    deployment and extra in it, from one distribution that is in fact correctly installed.
    """
    return _NAME_RE.sub("-", name).lower()


@dataclass
class Drift:
    """The three ways the deployed set can disagree with the locked one."""

    #: name -> (locked version, deployed version). The interesting case.
    mismatched: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: Locked, but absent from the deployment. Means the audit covered something not installed.
    missing: dict[str, str] = field(default_factory=dict)
    #: Installed, but absent from the lock. Means something runs that no audit has ever seen.
    unlocked: dict[str, str] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not (self.mismatched or self.missing or self.unlocked)

    @property
    def total(self) -> int:
        return len(self.mismatched) + len(self.missing) + len(self.unlocked)


def runtime_closure(lock_path: str | Path, *, project: str = "scribe") -> dict[str, str]:
    """Resolve `uv.lock` down to the packages a RUNTIME install pulls in.

    Walks the lock's own dependency graph from the project's `dependencies` plus every
    `optional-dependencies` group except `dev` — the same set `ci.yml` exports with
    `--all-extras --no-extra dev`, computed the same way so the two cannot disagree about
    what "runtime" means.

    `telemetry` is on the runtime side and that was checked rather than assumed:
    `/opt/venvs/scribe` has `opentelemetry-* 1.44.0` installed, so it is a production
    dependency in fact.

    The project itself is excluded — it is not a dependency of itself, and its version is
    compared separately by the caller if at all.
    """
    data = tomllib.loads(Path(lock_path).read_text(encoding="utf-8"))
    packages = {normalize(p["name"]): p for p in data.get("package", ())}

    root = packages.get(normalize(project))
    if root is None:
        raise KeyError(f"{lock_path} has no package entry for {project!r}")

    seeds: list[str] = [normalize(d["name"]) for d in root.get("dependencies", ())]
    for extra, deps in root.get("optional-dependencies", {}).items():
        if extra == "dev":
            continue
        seeds.extend(normalize(d["name"]) for d in deps)

    resolved: dict[str, str] = {}
    queue = list(seeds)
    while queue:
        name = queue.pop()
        if name in resolved or name == normalize(project):
            continue
        entry = packages.get(name)
        if entry is None:
            # A dependency named by the graph but absent from it. That is a malformed lock,
            # not drift, and silently skipping it would understate the locked set.
            raise KeyError(f"{lock_path} references {name!r} but has no package entry for it")
        resolved[name] = entry["version"]
        queue.extend(normalize(d["name"]) for d in entry.get("dependencies", ()))
    return resolved


def installed_versions(venv: str | Path) -> dict[str, str]:
    """Read what is installed in `venv`, from its `.dist-info` directories.

    Reads metadata off disk rather than shelling out to `pip list`. Three reasons, in order
    of how much they matter:

      * It does not require `pip` to be present in the target venv, and a venv built by
        `venv-deploy.sh` is not guaranteed to keep it.
      * It cannot be affected by the *calling* interpreter's environment, which a subprocess
        inheriting `PYTHONPATH` or `VIRTUAL_ENV` can be. This function's whole job is to
        describe a venv that is not the one running it.
      * It is a read, so it cannot mutate the production environment it is pointed at.

    **Symlinked metadata is skipped, not followed** (audit finding, 2026-09-18). This walks a
    directory tree and head-reads every `METADATA` it finds, which is the shape that has
    surfaced `~/.secrets` elsewhere on this fleet: a `METADATA` symlinked at another file
    would have that file's header lines reported as a package name and version. Exploiting it
    needs write access to the target's site-packages, which is already a compromise, and the
    read is bounded to the header block — so this is defence in depth rather than a fix for a
    reachable hole.

    Skipping rather than raising, because a `uv pip install --link-mode=symlink` tree is a
    legitimate thing to point this at. The skip is **reported on stderr** and it biases the
    result toward *more* drift, never less: an omitted package reads as "locked but not
    deployed", which is the direction that gets investigated rather than the one that goes
    unnoticed.
    """
    root = Path(venv).expanduser()
    site_packages = sorted(root.glob("lib/python*/site-packages"))
    if not site_packages:
        raise FileNotFoundError(f"no site-packages under {root} — is it a venv?")

    found: dict[str, str] = {}
    for sp in site_packages:
        for dist_info in sp.glob("*.dist-info"):
            metadata = dist_info / "METADATA"
            if dist_info.is_symlink() or metadata.is_symlink():
                print(
                    f"scribe: skipping {dist_info.name} — symlinked metadata is not followed",
                    file=sys.stderr,
                )
                continue
            if not metadata.is_file():
                continue
            name = version = ""
            # Header block only: the body of METADATA is the long description, which can
            # contain lines that look exactly like headers (a README quoting `Version: ...`
            # is not hypothetical). Stop at the first blank line, which is where RFC822
            # headers end.
            for line in metadata.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    break
                if line.startswith("Name:"):
                    name = line.partition(":")[2].strip()
                elif line.startswith("Version:"):
                    version = line.partition(":")[2].strip()
            if name and version:
                found[normalize(name)] = version
    return found


def compare(
    locked: dict[str, str],
    deployed: dict[str, str],
    *,
    project: str = "scribe",
) -> Drift:
    """Diff the two sets, ignoring the project itself and venv furniture."""
    ignore = _FURNITURE | {normalize(project)}
    drift = Drift()

    for name, locked_version in sorted(locked.items()):
        if name in ignore:
            continue
        deployed_version = deployed.get(name)
        if deployed_version is None:
            drift.missing[name] = locked_version
        elif deployed_version != locked_version:
            drift.mismatched[name] = (locked_version, deployed_version)

    for name, deployed_version in sorted(deployed.items()):
        if name in ignore or name in locked:
            continue
        drift.unlocked[name] = deployed_version

    return drift


def render(drift: Drift, *, lock_path: str, venv: str) -> str:
    """Human-readable report. Written to be skimmable when it is clean, because that is how
    it will be read 99 times out of 100."""
    out = [f"deps-drift: {venv} vs {lock_path}"]
    if drift.clean:
        out.append("  no drift — the deployed set matches the lock exactly")
        out.append("")
        out.append(
            "  NOTE: `venv-deploy.sh` re-resolves from pyproject.toml at deploy time and does"
        )
        out.append("  not consult the lock, so agreement here is a coincidence of the ranges being")
        out.append("  tight, not a guarantee. It can stop being true without anything changing")
        out.append("  in this repository.")
        return "\n".join(out)

    if drift.mismatched:
        out.append(f"  version mismatch ({len(drift.mismatched)}) — audited one, runs another:")
        for name, (locked, deployed) in sorted(drift.mismatched.items()):
            out.append(f"    {name:<42} locked {locked:<14} deployed {deployed}")
    if drift.missing:
        out.append(f"  locked but NOT deployed ({len(drift.missing)}) — audited, never installed:")
        for name, locked in sorted(drift.missing.items()):
            out.append(f"    {name:<42} locked {locked}")
    if drift.unlocked:
        out.append(f"  deployed but NOT locked ({len(drift.unlocked)}) — runs, never audited:")
        for name, deployed in sorted(drift.unlocked.items()):
            out.append(f"    {name:<42} deployed {deployed}")
    out.append("")
    out.append(f"  {drift.total} package(s) diverge. This is EXPECTED today and is not a failure:")
    out.append("  the deploy path re-resolves from pyproject.toml. It is reported so the gap is")
    out.append("  observed rather than assumed. vikunja#904.")
    return "\n".join(out)
