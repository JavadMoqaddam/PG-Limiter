"""Validate a committed stable release before CI, image build and publication."""

import argparse
import ast
from pathlib import Path
import re
import subprocess
import sys


def git(*arguments: str) -> subprocess.CompletedProcess:
    """Capture Git output without forwarding authentication diagnostics."""
    return subprocess.run(["git", *arguments], text=True, capture_output=True)


def committed_text(path: str) -> str:
    result = git("show", f"HEAD:{path}")
    if result.returncode:
        raise ValueError(f"Missing committed release file: {path}")
    return result.stdout


def prepare(sha: str, ref: str) -> dict[str, str]:
    """Refuse mismatched tags and skip branch publication of existing versions."""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise ValueError("Invalid event commit")
    head = git("rev-parse", "HEAD")
    if head.returncode or head.stdout.strip() != sha:
        raise ValueError("Checkout does not match the event commit")
    assignments = []
    for node in ast.parse(committed_text("limiter.py")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets
        ):
            assignments.append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "VERSION":
            assignments.append(node.value)
    if len(assignments) != 1:
        raise ValueError("limiter.py must declare exactly one literal VERSION")
    version = ast.literal_eval(assignments[0])
    if not isinstance(version, str) or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError("VERSION must be a stable major.minor.patch version")
    tag = f"v{version}"
    values = {"version": version, "tag": tag, "notes": f"docs/releases/{tag}.md", "publish": "true"}
    if ref.startswith("refs/tags/"):
        if ref != f"refs/tags/{tag}":
            raise ValueError("Event tag does not match the committed application version")
    elif ref.startswith("refs/heads/"):
        existing = git("ls-remote", "--exit-code", "--tags", "origin", f"refs/tags/{tag}")
        if existing.returncode == 0:
            values["publish"] = "false"
            return values
        if existing.returncode != 2:
            raise ValueError("Cannot establish whether the version tag already exists")
    else:
        raise ValueError("Release requires a branch or matching version tag")
    if not committed_text(values["notes"]).strip():
        raise ValueError("Committed release notes must not be empty")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        values = prepare(args.sha, args.ref)
    except (ValueError, SyntaxError) as error:
        print(f"Release preparation failed: {error}", file=sys.stderr)
        return 1
    with args.output.open("a") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")
    print(f"Release {values['tag']}: publish={values['publish']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
