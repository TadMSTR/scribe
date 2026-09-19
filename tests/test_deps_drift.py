"""Tests for the deployed-vs-locked drift check.

The check's whole value is that it is believed when it says "no drift", so the suite is
weighted toward the ways it could say that while measuring nothing: a closure that quietly
drops packages, a name comparison that treats `typing_extensions` and `typing-extensions` as
different distributions, and a metadata reader that stops finding versions.
"""

from __future__ import annotations

import pytest

from scribe.deps_drift import (
    Drift,
    compare,
    installed_versions,
    normalize,
    render,
    runtime_closure,
)
from scribe.deps_drift_cli import main as drift_main

LOCK = """\
version = 1
requires-python = ">=3.11"

[[package]]
name = "scribe"
version = "0.7.0"
source = { editable = "." }
dependencies = [{ name = "httpx" }]

[package.optional-dependencies]
dev = [{ name = "pytest" }]
telemetry = [{ name = "opentelemetry-sdk" }]

[[package]]
name = "httpx"
version = "0.28.1"
dependencies = [{ name = "idna" }]

[[package]]
name = "idna"
version = "3.20"

[[package]]
name = "opentelemetry-sdk"
version = "1.44.0"
dependencies = [{ name = "typing-extensions" }]

[[package]]
name = "typing-extensions"
version = "4.16.0"

[[package]]
name = "pytest"
version = "8.4.0"
dependencies = [{ name = "pluggy" }]

[[package]]
name = "pluggy"
version = "1.6.0"
"""


@pytest.fixture
def lock(tmp_path):
    p = tmp_path / "uv.lock"
    p.write_text(LOCK)
    return p


def _venv(root, packages: dict[str, str], *, python="python3.13"):
    """Build a venv-shaped tree with real .dist-info/METADATA files."""
    sp = root / "lib" / python / "site-packages"
    sp.mkdir(parents=True)
    for name, version in packages.items():
        d = sp / f"{name}-{version}.dist-info"
        d.mkdir()
        (d / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\nA description.\n"
        )
    return root


# --------------------------------------------------------------------------- closure


def test_the_closure_is_transitive_and_excludes_dev(lock) -> None:
    """The set must match what `uv export --all-extras --no-extra dev` produces: runtime
    dependencies and every non-dev extra, walked transitively."""
    assert runtime_closure(lock) == {
        "httpx": "0.28.1",
        "idna": "3.20",
        "opentelemetry-sdk": "1.44.0",
        "typing-extensions": "4.16.0",
    }


def test_dev_only_packages_are_absent_from_the_closure(lock) -> None:
    """The discriminating half of the test above. A closure that walked every extra would
    still contain the four packages asserted there — it is `pytest` and its transitive
    `pluggy` being ABSENT that proves `--no-extra dev` is being honoured."""
    closure = runtime_closure(lock)
    assert "pytest" not in closure
    assert "pluggy" not in closure


def test_the_project_itself_is_not_in_its_own_closure(lock) -> None:
    assert "scribe" not in runtime_closure(lock)


def test_a_lock_naming_a_package_it_does_not_define_is_an_error(tmp_path) -> None:
    """Skipping it silently would understate the locked set, which makes the whole report
    read cleaner than the truth — the one direction this tool must not fail in."""
    p = tmp_path / "uv.lock"
    p.write_text(
        'version = 1\n\n[[package]]\nname = "scribe"\nversion = "0.7.0"\n'
        'dependencies = [{ name = "ghost" }]\n'
    )
    with pytest.raises(KeyError, match="ghost"):
        runtime_closure(p)


def test_a_lock_without_the_project_is_an_error(tmp_path) -> None:
    p = tmp_path / "uv.lock"
    p.write_text('version = 1\n\n[[package]]\nname = "httpx"\nversion = "0.28.1"\n')
    with pytest.raises(KeyError, match="scribe"):
        runtime_closure(p)


# --------------------------------------------------------------------------- names


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("typing_extensions", "typing-extensions"),
        ("Typing-Extensions", "typing-extensions"),
        ("zope.interface", "zope-interface"),
        ("opentelemetry__api", "opentelemetry-api"),
        ("httpx", "httpx"),
    ],
)
def test_names_normalize_per_pep503(raw, expected) -> None:
    assert normalize(raw) == expected


def test_an_underscore_spelling_is_not_reported_as_two_problems(tmp_path, lock) -> None:
    """The specific failure normalisation prevents, asserted end to end.

    `uv.lock` writes `typing-extensions`; the installed dist-info writes
    `typing_extensions`. Comparing raw names reports it as BOTH missing from the deployment
    AND present-but-unlocked — two findings from one correctly installed package.
    """
    venv = _venv(
        tmp_path / "venv",
        {
            "httpx": "0.28.1",
            "idna": "3.20",
            "opentelemetry_sdk": "1.44.0",
            "typing_extensions": "4.16.0",
        },
    )
    drift = compare(runtime_closure(lock), installed_versions(venv))
    assert drift.clean, render(drift, lock_path=str(lock), venv=str(venv))


# --------------------------------------------------------------------------- compare


def test_each_kind_of_divergence_lands_in_its_own_bucket() -> None:
    locked = {"httpx": "0.28.1", "idna": "3.20", "protobuf": "7.36.2"}
    deployed = {"httpx": "0.28.1", "idna": "3.19", "grpcio": "1.84.0"}
    drift = compare(locked, deployed)

    assert drift.mismatched == {"idna": ("3.20", "3.19")}
    assert drift.missing == {"protobuf": "7.36.2"}
    assert drift.unlocked == {"grpcio": "1.84.0"}
    assert not drift.clean
    assert drift.total == 3


def test_an_identical_set_is_clean() -> None:
    """The control. Every assertion above is about finding differences; without this, a
    `compare` that reported everything as drift would satisfy all of them."""
    same = {"httpx": "0.28.1", "idna": "3.20"}
    assert compare(same, dict(same)).clean


def test_venv_furniture_and_the_project_are_not_drift() -> None:
    """`pip` and `scribe` are in every deployed venv and in no lock. Reporting them would put
    permanent rows in every report, which is how a report stops being read."""
    drift = compare({"httpx": "0.28.1"}, {"httpx": "0.28.1", "pip": "26.2.1", "scribe": "0.7.0"})
    assert drift.clean


# --------------------------------------------------------------------------- reading a venv


def test_versions_are_read_from_dist_info(tmp_path) -> None:
    venv = _venv(tmp_path / "v", {"httpx": "0.28.1", "idna": "3.19"})
    assert installed_versions(venv) == {"httpx": "0.28.1", "idna": "3.19"}


def test_a_description_that_quotes_a_version_header_is_not_read_as_one(tmp_path) -> None:
    """METADATA is RFC822: headers, blank line, then the long description. A README that
    quotes `Version: 9.9.9` in its body is ordinary, and a reader that scans the whole file
    would take the last match — reporting a version the package does not have."""
    sp = tmp_path / "v" / "lib" / "python3.13" / "site-packages"
    sp.mkdir(parents=True)
    d = sp / "httpx-0.28.1.dist-info"
    d.mkdir()
    (d / "METADATA").write_text(
        "Metadata-Version: 2.1\n"
        "Name: httpx\n"
        "Version: 0.28.1\n"
        "\n"
        "# httpx\n"
        "Upgrade with care.\n"
        "Version: 9.9.9\n"
        "Name: not-httpx\n"
    )
    assert installed_versions(tmp_path / "v") == {"httpx": "0.28.1"}


def test_a_dist_info_without_metadata_is_skipped(tmp_path) -> None:
    venv = _venv(tmp_path / "v", {"httpx": "0.28.1"})
    (venv / "lib" / "python3.13" / "site-packages" / "broken-1.0.dist-info").mkdir()
    assert installed_versions(venv) == {"httpx": "0.28.1"}


def test_a_directory_that_is_not_a_venv_is_an_error(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="site-packages"):
        installed_versions(tmp_path)


# --------------------------------------------------------------------------- render


def test_the_clean_report_does_not_claim_a_guarantee_it_cannot_make(tmp_path) -> None:
    """Agreement today is a coincidence of tight ranges, not coverage — the deploy path never
    reads the lock. A clean report that read as "verified" would be the exact false assurance
    this whole module exists to remove."""
    text = render(Drift(), lock_path="uv.lock", venv="/opt/venvs/scribe")
    assert "no drift" in text
    assert "coincidence" in text


def test_the_dirty_report_names_both_versions() -> None:
    drift = compare({"idna": "3.20"}, {"idna": "3.19"})
    text = render(drift, lock_path="uv.lock", venv="/opt/venvs/scribe")
    assert "3.20" in text and "3.19" in text
    assert "not a failure" in text


# --------------------------------------------------------------------------- CLI


def test_drift_does_not_change_the_exit_code(tmp_path, lock, capsys) -> None:
    """The report-do-not-fail contract, asserted rather than described. Something will
    eventually run this on a schedule, and a non-zero exit on the expected state would page
    on it every time until it was switched off."""
    venv = _venv(tmp_path / "v", {"httpx": "0.28.1", "idna": "3.19"})
    code = drift_main(["--lock", str(lock), "--venv", str(venv)])
    out = capsys.readouterr().out
    assert code == 0
    assert "version mismatch" in out
    assert "idna" in out


def test_a_clean_comparison_also_exits_zero(tmp_path, lock, capsys) -> None:
    venv = _venv(
        tmp_path / "v",
        {
            "httpx": "0.28.1",
            "idna": "3.20",
            "opentelemetry_sdk": "1.44.0",
            "typing_extensions": "4.16.0",
        },
    )
    assert drift_main(["--lock", str(lock), "--venv", str(venv)]) == 0
    assert "no drift" in capsys.readouterr().out


def test_an_unreadable_venv_exits_two(tmp_path, lock, capsys) -> None:
    """Distinct from 0. "I could not look" must not render as "I looked and found nothing"."""
    assert drift_main(["--lock", str(lock), "--venv", str(tmp_path / "nope")]) == 2
    assert "scribe:" in capsys.readouterr().err


def test_a_missing_lock_exits_two_with_an_actionable_message(tmp_path, capsys) -> None:
    assert drift_main(["--lock", str(tmp_path / "nope.lock"), "--venv", str(tmp_path)]) == 2
    assert "--lock" in capsys.readouterr().err


def test_json_output_is_machine_readable(tmp_path, lock, capsys) -> None:
    import json

    venv = _venv(tmp_path / "v", {"httpx": "0.28.1", "idna": "3.19"})
    assert drift_main(["--lock", str(lock), "--venv", str(venv), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["clean"] is False
    assert payload["mismatched"]["idna"] == {"locked": "3.20", "deployed": "3.19"}
    assert payload["locked_packages"] == 4
