"""Configuration loading for scribe.

`api_key` resolves **lazily, at call time** — never at config load. Eager resolution is the
defect behind vikunja#834: a provider whose key was absent turned an unrelated outage into a
crash at startup, because the config loader insisted on a value for a provider nothing was
about to use. Here a provider carries an env var *name*; `ProviderConfig.resolve_api_key()`
reads the environment when a request is actually about to be made, and only then can it fail.

Loading uses stdlib `tomllib`, so config handling adds no dependency.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_QUIET_PERIOD_MINUTES = 15
DEFAULT_MAX_SESSION_CHARS = 200_000
DEFAULT_PROJECT_GLOBS = ("~/.claude/projects/*/",)
DEFAULT_EXCLUDE = (".memsearch",)
DEFAULT_OUTPUT_DIR = "~/.local/share/scribe/digests"
#: A **sibling** of the digest root, never a child of it. The `session-digests` qmd
#: collection globs `<output_dir>/**/*.md`, so keeping event logs outside that tree is what
#: stops them being indexed — a structural exclusion rather than a pattern to maintain.
DEFAULT_EVENTLOG_DIR = "~/.local/share/scribe/eventlogs"
#: Not measured. 30 s is a guess at "long enough for a local indexer to ingest one ~2 KB
#: file, short enough that a hung one cannot stall an hourly sweep". The hook is invoked once
#: per written digest, so a sweep's worst case is this times the sessions it writes.
DEFAULT_HOOK_TIMEOUT_SECONDS = 30.0
MANIFEST_NAME = "index.jsonl"
#: Beside `index.jsonl` by default -- see `corrections`.
CORRECTIONS_NAME = "corrections.jsonl"
#: Where `scribe correct --apply` copies a file before replacing it, beside the digest root.
BACKUP_DIR_NAME = "backups"
#: One JSON line per session a live sweep finished -- see `runrecord`. Beside the digests and
#: event logs rather than with the state DB, because it is history to keep, not state to
#: rebuild, and on a host that backs up `~/.local/share/scribe` it goes with them.
DEFAULT_RUN_RECORD = "~/.local/share/scribe/runs.jsonl"
#: Below this many graded sessions `qc-report` gives no verdict (exit 3). Not tuned to any
#: host: it is the size under which one bad session moves a pass rate by 5 points.
DEFAULT_QC_MIN_SESSIONS = 20
#: The thresholds `qc-report` understands. **None has a default.** Unset is report-only, so a
#: fresh install never alerts on a number somebody else's corpus produced. Rates and coverage
#: are fractions in [0, 1]; a `_drop` is in the same units (0.10 = ten points).
QC_THRESHOLD_KEYS = (
    "min_pass_rate",
    "min_mean_coverage",
    "max_absent_per_session",
    "max_pass_rate_drop",
    "max_mean_coverage_drop",
    "max_absent_per_session_rise",
)
#: An environment variable NAME, as the hook's passthrough list accepts it. Names only: a
#: pattern or prefix here would be SC-06's glob passthrough by another route.
ENV_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


class ConfigError(ValueError):
    """Raised for a malformed config. Distinct from a missing credential at call time."""


@dataclass(frozen=True)
class ProviderConfig:
    """One summarization backend.

    `api_key_env` is the NAME of an environment variable, never a value. A literal key in a
    config file is what vikunja#437 was: `memsearch-config.toml` committed a live one.
    """

    name: str
    type: str
    base_url: str = ""
    api_key_env: str = ""
    model: str = ""

    def resolve_api_key(self) -> str:
        """Read the key from the environment, now.

        Returns "" when the provider needs no key (a local endpoint, or the `claude-cli`
        type). An empty result for a provider that *does* need one is the caller's problem
        to report at request time, with the request in hand to describe.
        """
        if not self.api_key_env:
            return ""
        return os.environ.get(self.api_key_env, "")


@dataclass(frozen=True)
class StageConfig:
    """Which provider and model serve one summarization stage."""

    provider: str
    model: str = ""


@dataclass
class Config:
    quiet_period_minutes: int = DEFAULT_QUIET_PERIOD_MINUTES
    max_session_chars: int = DEFAULT_MAX_SESSION_CHARS
    project_globs: tuple[str, ...] = DEFAULT_PROJECT_GLOBS
    exclude: tuple[str, ...] = DEFAULT_EXCLUDE
    state_path: str = "~/.local/state/scribe/scribe.sqlite3"
    output_dir: str = DEFAULT_OUTPUT_DIR
    eventlog_dir: str = DEFAULT_EVENTLOG_DIR
    host: str = "127.0.0.1"
    port: int = 8499
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    stages: dict[str, StageConfig] = field(default_factory=dict)
    #: YAML frontmatter at the top of each daily digest. Off by default: the anchors are the
    #: authoritative identity and the journal preview is pinned to the unadorned format.
    emit_frontmatter: bool = False
    #: Where `index.jsonl` lives. Empty means the default; read it through `manifest_file()`,
    #: which is the one place that resolves it.
    manifest_path: str = ""
    #: The corrections ledger. `None` means the default, `""` turns it off; read it through
    #: `corrections_file()`.
    corrections_path: str | None = None
    #: Where `scribe correct --apply` backs files up. Empty means the default; read it through
    #: `backup_root()`. There is no off switch: `correct` does not replace a file it has not
    #: copied first.
    backup_dir: str = ""
    #: argv run once per written digest, with the digest's path appended. Empty runs nothing.
    on_digest_written: tuple[str, ...] = ()
    on_digest_written_timeout_seconds: float = DEFAULT_HOOK_TIMEOUT_SECONDS
    #: Extra environment variables the hook may see, by exact name. See `pipeline.hook_env`.
    on_digest_written_env: tuple[str, ...] = ()
    #: Where a live sweep appends its per-session QC record. "" turns the record off.
    qc_run_record: str = DEFAULT_RUN_RECORD
    qc_min_sessions: int = DEFAULT_QC_MIN_SESSIONS
    #: Only the keys the operator set -- see `QC_THRESHOLD_KEYS`.
    qc_thresholds: dict[str, float] = field(default_factory=dict)
    #: Rewrite a `#N` the gate rejected as an id conflation to `id N` before the digest is
    #: written -- see `qc.rewrite_id_conflations`. On by default; turn it off for a tracker
    #: whose `#N` IS its id, where the conflation the gate names cannot happen.
    qc_rewrite_id_conflations: bool = True

    def manifest_file(self) -> Path:
        """The digest manifest: `manifest_path` if set, else `<output_dir>/../index.jsonl`.

        **A sibling of the digest root by default, never a child**, for the reason the event
        log directory is one: an indexer that globs `<output_dir>/**` must not be one pattern
        change away from ingesting the manifest as if it were a digest. Structural, not a
        pattern somebody has to keep correct.
        """
        if self.manifest_path:
            return Path(self.manifest_path).expanduser()
        return Path(self.output_dir).expanduser().parent / MANIFEST_NAME

    def corrections_file(self) -> Path | None:
        """The corrections ledger, or None when disabled. Defaults beside `index.jsonl`."""
        if self.corrections_path is None:
            return Path(self.output_dir).expanduser().parent / CORRECTIONS_NAME
        if not self.corrections_path:
            return None
        return Path(self.corrections_path).expanduser()

    def backup_root(self) -> Path:
        """`backup_dir` if set, else `<output_dir>/../backups`. Never inside either tree."""
        if self.backup_dir:
            return Path(self.backup_dir).expanduser()
        return Path(self.output_dir).expanduser().parent / BACKUP_DIR_NAME

    def provider_for(self, stage: str) -> ProviderConfig:
        """Return the provider serving `stage`, raising a config error if it is undeclared.

        A stage naming a provider that does not exist is a config error, and it is worth
        catching at load rather than at 3am when the stage first runs — which is a different
        thing from resolving its credential eagerly.
        """
        st = self.stages.get(stage)
        if st is None:
            raise ConfigError(f"no configuration for stage {stage!r}")
        prov = self.providers.get(st.provider)
        if prov is None:
            raise ConfigError(
                f"stage {stage!r} names provider {st.provider!r}, which is not declared"
            )
        return prov


def _api_key_env(raw: object, where: str) -> str:
    """Accept `api_key = { env = "NAME" }` and reject a literal.

    Rejecting the literal form is deliberate and is the only place this is enforceable. A
    config that merely *documents* "use an env ref" is what was in place when a live key was
    committed; refusing to load one makes the rule real.
    """
    if raw is None:
        return ""
    if isinstance(raw, dict):
        env = raw.get("env")
        if not isinstance(env, str) or not env:
            raise ConfigError(f"{where}: api_key table must carry a non-empty `env` name")
        return env
    raise ConfigError(
        f'{where}: api_key must be an env reference, `api_key = {{ env = "NAME" }}`, '
        "not a literal value"
    )


def _hook_argv(raw: object) -> tuple[str, ...]:
    """Accept `on_digest_written = ["/path/to/indexer", "--flag"]` and reject a string.

    **A string is refused, not split.** The digest path is appended to this argv, and that
    path is built from an agent name which reaches it from three external routes
    (`paths.valid_agent` lists them). Handed to a shell, a string is a command-injection
    surface; as an argv list run with `shell=False` it is not. Splitting a string with
    `shlex` would accept the unsafe form and quietly make it look safe, so the only
    enforceable rule is the one that refuses to load it -- the same reasoning as
    `_api_key_env`'s refusal of a literal key.
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        raise ConfigError(
            "index.on_digest_written must be an argv list, e.g. "
            '`on_digest_written = ["/usr/local/bin/my-indexer", "--file"]`, not a string -- '
            "the digest path is appended to it and a shell string would be an injection surface"
        )
    if not isinstance(raw, list) or not raw:
        raise ConfigError("index.on_digest_written must be a non-empty list of strings")
    if not all(isinstance(arg, str) and arg for arg in raw):
        raise ConfigError("index.on_digest_written: every element must be a non-empty string")
    return tuple(raw)


def _inside(child: Path, root: Path) -> bool:
    """Whether `child` is `root` or anywhere beneath it, compared as resolved paths."""
    c, r = child.resolve(strict=False), root.resolve(strict=False)
    return c == r or r in c.parents


def _refuse_inside_trees(cfg: Config) -> None:
    """Refuse any file scribe writes beside the digests that resolves INSIDE either tree.

    One check for all of them -- the manifest, the corrections ledger and the backup root --
    so a new sibling is one tuple entry rather than a second copy of the rule. Checked on the
    EFFECTIVE path, so a default that lands somewhere unsafe is caught too: an `eventlog_dir`
    set to `output_dir`'s parent would otherwise swallow the defaults. Inside `output_dir` a
    file is one indexer glob away from being indexed as if it were a digest -- for the backup
    root that is a duplicate of every corrected digest; inside `eventlog_dir` it is a file
    that directory's contract says is never a search target.
    """
    targets = [("manifest_path", cfg.manifest_file()), ("backup_dir", cfg.backup_root())]
    ledger = cfg.corrections_file()
    if ledger is not None:
        targets.append(("corrections_path", ledger))
    for name, effective in targets:
        for key, root in (("output_dir", cfg.output_dir), ("eventlog_dir", cfg.eventlog_dir)):
            if _inside(effective, Path(root).expanduser()):
                raise ConfigError(
                    f"index.{name} resolves to {effective}, inside {key}; keep it outside "
                    "both the digest tree and the event-log tree"
                )


def _load_qc(cfg: Config, qc: object) -> None:
    """The `[qc]` table. Unknown keys are refused: a misspelt threshold would otherwise load
    as report-only and never fire, which is the silent kind of wrong."""
    if not isinstance(qc, dict):
        raise ConfigError("qc: expected a table")
    unknown = set(qc) - {"run_record", "min_sessions", "rewrite_id_conflations", *QC_THRESHOLD_KEYS}
    if unknown:
        raise ConfigError(f"qc: unknown key(s) {', '.join(sorted(unknown))}")
    if "run_record" in qc:
        if not isinstance(qc["run_record"], str):
            raise ConfigError('qc.run_record must be a path string ("" disables it)')
        cfg.qc_run_record = qc["run_record"]
    if "min_sessions" in qc:
        n = qc["min_sessions"]
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise ConfigError("qc.min_sessions must be a positive integer")
        cfg.qc_min_sessions = n
    if "rewrite_id_conflations" in qc:
        if not isinstance(qc["rewrite_id_conflations"], bool):
            raise ConfigError("qc.rewrite_id_conflations must be true or false")
        cfg.qc_rewrite_id_conflations = qc["rewrite_id_conflations"]
    for key in QC_THRESHOLD_KEYS:
        if key in qc:
            cfg.qc_thresholds[key] = check_threshold(key, qc[key], where="qc.")


def check_threshold(key: str, value: object, *, where: str = "") -> float:
    """One threshold, validated. Shared with the CLI flags so both refuse the same values."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise ConfigError(f"{where}{key} must be a non-negative number")
    if key != "max_absent_per_session" and not key.endswith("_rise") and value > 1:
        raise ConfigError(f"{where}{key} is a fraction in [0, 1], got {value}")
    return float(value)


def load(path: str | os.PathLike[str] | None = None) -> Config:
    """Load `scribe.toml`. With no path, return defaults."""
    cfg = Config()
    if path is None:
        return cfg
    p = Path(path).expanduser()
    try:
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {p}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{p}: {exc}") from exc

    extract = data.get("extract", {})
    cfg.quiet_period_minutes = int(
        extract.get("quiet_period_minutes", DEFAULT_QUIET_PERIOD_MINUTES)
    )
    cfg.max_session_chars = int(extract.get("max_session_bytes", DEFAULT_MAX_SESSION_CHARS))

    disc = data.get("discovery", {})
    cfg.project_globs = tuple(disc.get("project_globs", DEFAULT_PROJECT_GLOBS))
    cfg.exclude = tuple(disc.get("exclude", DEFAULT_EXCLUDE))
    if "state_path" in disc:
        cfg.state_path = str(disc["state_path"])
    if "output_dir" in disc:
        cfg.output_dir = str(disc["output_dir"])
    if "eventlog_dir" in disc:
        cfg.eventlog_dir = str(disc["eventlog_dir"])
    if "emit_frontmatter" in disc:
        if not isinstance(disc["emit_frontmatter"], bool):
            raise ConfigError("discovery.emit_frontmatter must be true or false")
        cfg.emit_frontmatter = disc["emit_frontmatter"]

    index = data.get("index", {})
    if not isinstance(index, dict):
        raise ConfigError("index: expected a table")
    cfg.on_digest_written = _hook_argv(index.get("on_digest_written"))
    timeout = index.get("on_digest_written_timeout_seconds", DEFAULT_HOOK_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise ConfigError("index.on_digest_written_timeout_seconds must be a positive number")
    cfg.on_digest_written_timeout_seconds = float(timeout)
    env_names = index.get("on_digest_written_env", [])
    if not isinstance(env_names, list) or not all(
        isinstance(n, str) and ENV_NAME_RE.match(n) for n in env_names
    ):
        raise ConfigError(
            "index.on_digest_written_env must be a list of exact environment variable names "
            "-- no patterns or prefixes"
        )
    cfg.on_digest_written_env = tuple(env_names)
    if "manifest_path" in index:
        cfg.manifest_path = str(index["manifest_path"])
    for key in ("corrections_path", "backup_dir"):
        if key in index and not isinstance(index[key], str):
            raise ConfigError(f"index.{key} must be a path string")
    if "corrections_path" in index:
        cfg.corrections_path = index["corrections_path"]
    if "backup_dir" in index:
        cfg.backup_dir = index["backup_dir"]
    _refuse_inside_trees(cfg)

    _load_qc(cfg, data.get("qc", {}))

    service = data.get("service", {})
    cfg.host = str(service.get("host", cfg.host))
    cfg.port = int(service.get("port", cfg.port))

    for name, block in (data.get("providers") or {}).items():
        if not isinstance(block, dict):
            raise ConfigError(f"providers.{name}: expected a table")
        cfg.providers[name] = ProviderConfig(
            name=name,
            type=str(block.get("type", "")),
            base_url=str(block.get("base_url", "")),
            api_key_env=_api_key_env(block.get("api_key"), f"providers.{name}"),
            model=str(block.get("model", "")),
        )

    for stage, block in (data.get("summarize") or {}).items():
        if not isinstance(block, dict):
            raise ConfigError(f"summarize.{stage}: expected a table")
        provider = block.get("provider")
        if not provider:
            raise ConfigError(f"summarize.{stage}: `provider` is required")
        cfg.stages[stage] = StageConfig(provider=str(provider), model=str(block.get("model", "")))

    for stage in cfg.stages:
        cfg.provider_for(stage)  # fail fast on a dangling provider reference
    return cfg
