"""Turn the run record into a QC trend, and decide whether it has regressed.

Read-only over `runs.jsonl`. Everything it reports was classified when the record was written
(`runrecord.qc_fields`), so the report is arithmetic over lines rather than a second grading
pass -- it cannot drift from what the sweep saw.

Two statistics are reported and they are **not** the same number:

  * `pass_rate` -- per SESSION, every check including the event-coverage floor. What the
    sweep's `QC n/m passed` line has always printed (47% over 2026-09-14..28).
  * `groundedness.failure_rate` -- per BLOCK, groundedness only. What `qc-survey` reports as
    `failure_rate` (21% of the corpus on 2026-09-28).

Neither is wrong. Comparing one against the other is.

Findings are broken out by kind and by bucket, never only pooled. Most of today's findings are
gate artefacts (`suffix`, `id-conflation`), and a pooled count moves every time the gate is
tuned. `absent` is the bucket that says the model invented something.

A sudden move in `absent` per session with no change in `scribe_version` or `prompt_sha256`
is the practical signal of an upstream model change. `model_resolved` will not show it on a
provider that echoes the requested alias (Mistral does), so the report does not claim it can.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .qc_survey import ABSENT

#: The exit-code contract. Every code is distinct and no success path returns 1 (#875): a
#: cron that alerts on 1 must never be paged by a report that simply ran.
EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_USAGE = 2
EXIT_INSUFFICIENT = 3
EXIT_TOOL_FAILURE = 4

SOURCE_ALL = "all"
#: Thresholds judged against `--compare`'s previous window rather than as a floor.
RELATIVE_KEYS = ("max_pass_rate_drop", "max_mean_coverage_drop", "max_absent_per_session_rise")
#: `--by` name -> record field.
GROUP_FIELDS = {
    "agent": "agent",
    "version": "scribe_version",
    "prompt": "prompt_sha256",
    "model": "model_resolved",
}


def parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def select(records: list[dict], *, start: datetime, end: datetime, source: str) -> list[dict]:
    """Records in `[start, end)` from `source`. A record with no parseable `ts` is in no window.

    Windowed on `ts`, when the record was WRITTEN -- for a backfill, when it was graded, not
    when the session ran. So a backfill is read with a window that covers the day it ran, and
    it reports the whole corpus it graded as one population. That is what it is for: a
    baseline, not a history.
    """
    out = []
    for r in records:
        if source != SOURCE_ALL and r.get("source") != source:
            continue
        ts = parse_ts(r.get("ts"))
        if ts is not None and start <= ts < end:
            out.append(r)
    return out


def _percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile. None for an empty list rather than a number nobody measured."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def _num(value: object) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def stats(records: list[dict]) -> dict:
    """Every figure the report shows, for one population of records.

    Placeholders are counted and then excluded from every quality rate. A placeholder is a
    LOST summary, not a poor one -- `scribe-recover-check` already alerts on it -- and grading
    `render_failure`'s text would put a transport outage into the quality trend.
    """
    placeholders = [r for r in records if r.get("placeholder")]
    graded = [r for r in records if not r.get("placeholder") and r.get("qc_ok") is not None]
    n = len(graded)

    passed = sum(1 for r in graded if r.get("qc_ok"))
    coverage = [_num(r.get("qc_coverage")) for r in graded]
    floor_failed = sum(
        1 for r in graded if (r.get("findings_by_check") or {}).get("event_coverage")
    )
    ground_failed = sum(1 for r in graded if (r.get("findings_by_check") or {}).get("groundedness"))

    by_check: Counter = Counter()
    by_kind: Counter = Counter()
    by_bucket: dict[str, Counter] = {}
    for r in graded:
        by_check.update(r.get("findings_by_check") or {})
        by_kind.update(r.get("findings_by_kind") or {})
        for kind, buckets in (r.get("findings_by_bucket") or {}).items():
            by_bucket.setdefault(kind, Counter()).update(buckets)
    absent = {k: b.get(ABSENT, 0) for k, b in sorted(by_bucket.items()) if b.get(ABSENT)}
    findings_total = sum(by_check.values())
    absent_total = sum(absent.values())

    def _trunc(rows: list[dict]) -> dict:
        hit = sum(1 for r in rows if _num(r.get("truncated_items")) > 0)
        return {"sessions": len(rows), "truncated": hit, "rate": _rate(hit, len(rows))}

    lags = [_num(r.get("lag_s")) for r in records if r.get("lag_s") is not None]
    models = Counter(str(r.get("model_resolved")) for r in records if r.get("model_resolved"))
    return {
        "sessions": len(records),
        "graded": n,
        "passed": passed,
        # Per session, all checks. See the module docstring before comparing it to anything.
        "pass_rate": _rate(passed, n),
        "coverage_mean": round(sum(coverage) / n, 4) if n else None,
        "coverage_p10": _percentile(coverage, 0.10),
        "coverage_floor_failed": floor_failed,
        "coverage_floor_failed_share": _rate(floor_failed, n),
        # Per block, groundedness only -- `qc-survey`'s `failure_rate`.
        "groundedness": {"blocks_failing": ground_failed, "failure_rate": _rate(ground_failed, n)},
        "findings": {
            "total": findings_total,
            "per_session": _rate(findings_total, n),
            "by_check": dict(sorted(by_check.items())),
            "by_kind": dict(sorted(by_kind.items())),
            "by_bucket": {k: dict(sorted(b.items())) for k, b in sorted(by_bucket.items())},
            "absent_by_kind": absent,
            "absent_total": absent_total,
            "absent_per_session": _rate(absent_total, n),
        },
        # Split, with denominators, because a whole-session read and an incremental one are
        # different populations (#884). A backfilled record does not know which it was.
        "truncation": {
            "full_read": _trunc([r for r in records if r.get("full_read") is True]),
            "incremental": _trunc([r for r in records if r.get("full_read") is False]),
            "unknown": sum(1 for r in records if r.get("full_read") is None),
        },
        "placeholders": len(placeholders),
        "suppressed": sum(1 for r in records if r.get("suppressed")),
        "discarded": sum(1 for r in records if r.get("discarded")),
        # Should always be 0. Any other value is loud in the text report.
        "post_render_redactions": int(sum(_num(r.get("post_render_redactions")) for r in records)),
        "input_tokens": int(sum(_num(r.get("input_tokens")) for r in records)),
        "output_tokens": int(sum(_num(r.get("output_tokens")) for r in records)),
        "models_resolved": dict(sorted(models.items())),
        "lag_s_p50": _percentile(lags, 0.50),
        "lag_s_p95": _percentile(lags, 0.95),
    }


def group(records: list[dict], by: list[str]) -> dict[tuple, list[dict]]:
    out: dict[tuple, list[dict]] = {}
    for r in records:
        key = tuple(r.get(GROUP_FIELDS[b]) for b in by)
        out.setdefault(key, []).append(r)
    return dict(sorted(out.items(), key=lambda kv: tuple(str(v) for v in kv[0])))


@dataclass
class Verdict:
    code: int = EXIT_OK
    breaches: list[str] = field(default_factory=list)
    #: Why a threshold could not be checked. Reported next to the breaches, never instead.
    unchecked: list[str] = field(default_factory=list)


def evaluate(
    current: dict, previous: dict | None, thresholds: dict[str, float], min_sessions: int
) -> Verdict:
    """Apply the thresholds to the whole window. Unset thresholds are not checked.

    Order matters and is the contract:

      1. Too few graded sessions in the current window -> 3. No verdict at all: a pass rate
         over six sessions is noise, and "no regression" read off it would be a false pass.
      2. Any absolute floor breached -> 1.
      3. Any relative threshold breached, provided the previous window ALSO has enough
         sessions -> 1. A thin previous window leaves the relative checks unchecked.
      4. A relative threshold was set but could not be checked -> 3, since the verdict the
         operator asked for was not given. Otherwise 0.
    """
    v = Verdict()
    if current["graded"] < min_sessions:
        v.code = EXIT_INSUFFICIENT
        v.unchecked.append(
            f"current window has {current['graded']} graded session(s), fewer than "
            f"--min-sessions {min_sessions}"
        )
        return v

    def _check(name: str, value: float | None, limit: float, worse: bool) -> None:
        if value is None:
            v.unchecked.append(f"{name}: no value in this window")
        elif worse:
            v.breaches.append(f"{name}: {value:.4g} breaches {limit:.4g}")

    t = thresholds
    cur_absent = current["findings"]["absent_per_session"]
    if "min_pass_rate" in t:
        pr = current["pass_rate"]
        _check("min_pass_rate", pr, t["min_pass_rate"], pr is not None and pr < t["min_pass_rate"])
    if "min_mean_coverage" in t:
        cm = current["coverage_mean"]
        limit = t["min_mean_coverage"]
        _check("min_mean_coverage", cm, limit, cm is not None and cm < limit)
    if "max_absent_per_session" in t:
        limit = t["max_absent_per_session"]
        over = cur_absent is not None and cur_absent > limit
        _check("max_absent_per_session", cur_absent, limit, over)

    relative = [k for k in RELATIVE_KEYS if k in t]
    if relative:
        if previous is None or previous["graded"] < min_sessions:
            got = 0 if previous is None else previous["graded"]
            v.unchecked.append(
                f"{', '.join(relative)}: previous window has {got} graded session(s), fewer "
                f"than --min-sessions {min_sessions}"
            )
        else:
            pairs = {
                "max_pass_rate_drop": (previous["pass_rate"], current["pass_rate"], -1),
                "max_mean_coverage_drop": (previous["coverage_mean"], current["coverage_mean"], -1),
                "max_absent_per_session_rise": (
                    previous["findings"]["absent_per_session"],
                    cur_absent,
                    1,
                ),
            }
            for key in relative:
                before, now, sign = pairs[key]
                if before is None or now is None:
                    v.unchecked.append(f"{key}: no value in one of the windows")
                    continue
                delta = (now - before) * sign
                if delta > t[key]:
                    v.breaches.append(
                        f"{key}: moved {delta:.4g} ({before:.4g} -> {now:.4g}), limit {t[key]:.4g}"
                    )

    if v.breaches:
        v.code = EXIT_REGRESSION
    elif v.unchecked and relative:
        v.code = EXIT_INSUFFICIENT
    return v


def build(
    records: list[dict],
    *,
    days: int,
    end: datetime,
    source: str,
    by: list[str],
    compare: bool,
    thresholds: dict[str, float],
    min_sessions: int,
) -> tuple[dict, Verdict]:
    """The whole report, as data, plus its verdict. The CLI only formats this."""
    start = end - timedelta(days=days)
    cur_rows = select(records, start=start, end=end, source=source)
    want_previous = compare or any(k in thresholds for k in RELATIVE_KEYS)
    prev_rows = (
        select(records, start=start - timedelta(days=days), end=start, source=source)
        if want_previous
        else None
    )
    current = stats(cur_rows)
    previous = stats(prev_rows) if prev_rows is not None else None
    verdict = evaluate(current, previous, thresholds, min_sessions)

    groups = []
    if by:
        prev_groups = group(prev_rows, by) if prev_rows is not None else {}
        for key, rows in group(cur_rows, by).items():
            groups.append(
                {
                    "key": dict(zip(by, key, strict=True)),
                    "current": stats(rows),
                    "previous": stats(prev_groups[key]) if key in prev_groups else None,
                }
            )

    new_models = (
        sorted(set(current["models_resolved"]) - set(previous["models_resolved"]))
        if previous is not None and previous["sessions"]
        else []
    )
    report = {
        "source": source,
        "window": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        "previous_window": (
            {"start": (start - timedelta(days=days)).isoformat(), "end": start.isoformat()}
            if previous is not None
            else None
        ),
        "min_sessions": min_sessions,
        "current": current,
        "previous": previous,
        "groups": groups,
        "new_models": new_models,
        "thresholds": thresholds,
        "breaches": verdict.breaches,
        "unchecked": verdict.unchecked,
        "exit": verdict.code,
    }
    return report, verdict
