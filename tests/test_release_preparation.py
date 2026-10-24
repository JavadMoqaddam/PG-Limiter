"""Release metadata comes from one committed version and cannot overwrite old tags."""

import json
import os
import subprocess
import sys
import tarfile
import textwrap
import zipfile
from pathlib import Path

import pytest


HELPER = Path(__file__).resolve().parents[1] / "tools/prepare_release.py"


@pytest.fixture
def release_repo(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "config", "user.email", "release-test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Release test"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=repo, check=True)

    def commit(source='VERSION = "1.7.0"\n', notes=True):
        (repo / "limiter.py").write_text(source)
        note = repo / "docs/releases/v1.7.0.md"
        if notes:
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text("# PG-Limiter v1.7.0\n\nReleased 2026-10-25.\n")
        elif note.exists():
            note.unlink()
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    def run(sha, ref="refs/heads/main"):
        output = tmp_path / "outputs"
        if output.exists():
            output.unlink()
        result = subprocess.run(
            [sys.executable, str(HELPER), "--sha", sha, "--ref", ref, "--output", str(output)],
            cwd=repo,
            text=True,
            capture_output=True,
        )
        values = dict(line.split("=", 1) for line in output.read_text().splitlines()) if output.exists() else {}
        return result, values

    return repo, commit, run


def test_new_branch_release_uses_committed_version_and_notes(release_repo):
    repo, commit, run = release_repo
    sha = commit()
    (repo / "limiter.py").write_text('VERSION = "9.9.9"\n')
    result, values = run(sha)
    assert result.returncode == 0, result.stderr
    assert values == {"version": "1.7.0", "tag": "v1.7.0", "notes": "docs/releases/v1.7.0.md", "publish": "true"}


def test_existing_version_tag_skips_new_branch_release(release_repo):
    repo, commit, run = release_repo
    sha = commit(notes=False)
    subprocess.run(["git", "tag", "v1.7.0"], cwd=repo, check=True)
    result, values = run(sha)
    assert result.returncode == 0, result.stderr
    assert values["publish"] == "false"


def test_matching_tag_can_release_and_mismatched_tag_cannot(release_repo):
    _repo, commit, run = release_repo
    sha = commit()
    result, values = run(sha, "refs/tags/v1.7.0")
    assert result.returncode == 0 and values["publish"] == "true"
    result, values = run(sha, "refs/tags/v1.6.1")
    assert result.returncode != 0 and not values


@pytest.mark.parametrize("version", ["1.7", "01.7.0", "1.7.0-rc1", "1.7.0/other", "1.7.0\nother", 170])
def test_invalid_stable_version_is_refused(release_repo, version):
    _repo, commit, run = release_repo
    sha = commit(f"VERSION = {json.dumps(version)}\n")
    result, values = run(sha)
    assert result.returncode != 0 and not values


@pytest.mark.parametrize(
    "source", ["VERSION = get_version()\n", 'VERSION = "1.7.0"\nVERSION = "1.7.0"\n', 'OTHER = "1.7.0"\n']
)
def test_unknown_or_ambiguous_version_is_refused_without_execution(release_repo, source):
    _repo, commit, run = release_repo
    sha = commit(source)
    result, values = run(sha)
    assert result.returncode != 0 and not values


def test_missing_committed_notes_fails_even_if_working_file_exists(release_repo):
    repo, commit, run = release_repo
    sha = commit(notes=False)
    note = repo / "docs/releases/v1.7.0.md"
    note.parent.mkdir(parents=True)
    note.write_text("uncommitted release notes")
    result, values = run(sha)
    assert result.returncode != 0 and not values


def test_wrong_event_commit_and_unknown_remote_fail_closed(release_repo):
    repo, commit, run = release_repo
    sha = commit()
    result, values = run("0" * 40)
    assert result.returncode != 0 and not values
    subprocess.run(["git", "remote", "remove", "origin"], cwd=repo, check=True)
    result, values = run(sha)
    assert result.returncode != 0 and not values


@pytest.mark.parametrize("correct_sha", [True, False])
def test_actual_archive_step_uses_only_the_committed_event_tree(release_repo, correct_sha):
    repo, commit, _run = release_repo
    sha = commit()
    (repo / "stray-private.json").write_text("must never be published")
    (repo / "limiter.py").write_text("uncommitted workspace content")
    workflow = (HELPER.parents[1] / ".github/workflows/release.yml").read_text()
    block = workflow.split("      - name: Create source archive\n", 1)[1].split("\n      #", 1)[0]
    command = textwrap.dedent(block.split("        run: |\n", 1)[1]).strip()
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", command],
        cwd=repo,
        env={**os.environ, "GITHUB_SHA": sha if correct_sha else "0" * 40, "RELEASE_VERSION": "1.7.0"},
        text=True,
        capture_output=True,
    )
    if not correct_sha:
        assert result.returncode != 0 and not (repo / "release").exists()
        return
    assert result.returncode == 0, result.stderr
    committed = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=repo, text=True
    ).splitlines()
    expected = {name: subprocess.check_output(["git", "show", f"HEAD:{name}"], cwd=repo) for name in committed}
    with tarfile.open(repo / "release/pg-limiter-1.7.0.tar.gz") as archive:
        assert {member.name: archive.extractfile(member).read() for member in archive if member.isfile()} == expected
    with zipfile.ZipFile(repo / "release/pg-limiter-1.7.0.zip") as archive:
        assert {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")} == expected
