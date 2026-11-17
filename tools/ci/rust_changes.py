#!/usr/bin/env python3
"""Select CI checks from one fail-closed Git diff.

The default answers Rust selection; --launcher uses the launcher's dependency paths.
--all emits the shared Rust, launcher and tooling decisions for dyna-checks (the machine sample is a daily leg,
machine-sample.yml, so dyna-checks selects nothing for it).
server_build: whether verify's clippy of the server build has anything new to check (needs_server_build).
"""

import argparse
import functools
import posixpath
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# The repository root, appended so a script run finds `tools` without shadowing anything already importable.
sys.path.append(str(ROOT))
from tools import release_train  # noqa: E402

# The root workspace's manifest and toolchain files: the launcher's path crates inherit from them.
ROOT_RUST = {"Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "rustfmt.toml", "clippy.toml"}
# The check itself: a change to it runs it.
LAUNCHER_CHECK = {".github/workflows/ci.yml", ".github/workflows/dyna-ci.yml",
                  "tools/ci/rust_changes.py", "tools/release_train.py"}


# The Python under tools/ and tests/: verify's Python steps and the parity jobs run it, and no Rust build or test reads
# it (Rust does read tools/native-hook-fragments/*.json and tools/ci/*.sh, so those still count). This script and the
# module it imports decide whether Rust runs at all, so a change to them runs it.
RUST_DECIDES = {"tools/ci/rust_changes.py", "tools/release_train.py"}


def rust_free_by_name(path):
    return (path.startswith("docs/") or path.endswith(".md")
            or path.startswith(("tools/", "tests/")) and path.endswith(".py") and path not in RUST_DECIDES)


@functools.lru_cache(maxsize=None)
def rust_literals():
    """(crate folders, {last segment: [(source, literal)]}) for every string literal with no space, brace or backslash
    in this checkout's tracked Rust sources; None if they can't be read."""
    def git(*args):
        return subprocess.run(["git", "-C", str(ROOT), *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              check=False)

    manifests = git("ls-files", "-z", "--", "Cargo.toml", "*/Cargo.toml")
    literals = git("grep", "-I", "-z", "-o", "-E", r'"[^"[:space:]{}\\]+"', "--", "*.rs")
    if manifests.returncode != 0 or literals.returncode not in (0, 1):
        return None
    by_name = {}
    for line in literals.stdout.decode("utf-8", "replace").splitlines():
        source, _, literal = line.partition("\0")
        literal = literal[1:-1]
        by_name.setdefault(literal.rstrip("/").rsplit("/", 1)[-1], []).append((source, literal))
    return {posixpath.dirname(path) for path in manifests.stdout.decode("utf-8").split("\0") if path}, by_name


def rust_reads(paths):
    """The paths among `paths` (each Rust-free by name) that Rust builds or tests read anyway, found in this checkout's
    Rust sources without a build:
    - a string literal names the file, as include_str!/include_bytes!/include!/#[path] and the tests' reads do: a bare
      name from the source's folder, a path with a folder from the source's folder or any folder above it (so
      concat!(env!("CARGO_MANIFEST_DIR"), "/..."), Path::new(env!("CARGO_MANIFEST_DIR")).join("...") and paths from
      the repository root resolve);
    - or a literal with a folder names a folder holding the file, below the top level and not a crate's own folder
      (that crate's sources count already);
    - or the file belongs to a Cargo package under docs/ (the book's examples), other than its *.md.
    A deleted file still counts when a literal names it. If the sources can't be read, every path counts."""
    paths = set(paths)
    scanned = rust_literals()
    if scanned is None:
        return paths
    manifests, by_name = scanned
    folders = {}
    for path in paths:
        parts = path.split("/")
        for end in range(2, len(parts)):
            folders.setdefault("/".join(parts[:end]), set()).add(path)
    read = set()
    # Only a literal ending in a changed file's name, or in the name of a folder holding one, can name it.
    for name in {part for path in paths for part in path.split("/")} - {".", ".."}:
        for source, literal in by_name.get(name, ()):
            bases = [posixpath.dirname(source)]
            while "/" in literal and bases[-1]:
                bases.append(posixpath.dirname(bases[-1]))
            for base in bases:
                target = posixpath.normpath(posixpath.join(base, literal.lstrip("/")))
                if target in paths:
                    read.add(target)
                elif "/" in literal and target in folders and target not in manifests:
                    read |= folders[target]
    packages = tuple(folder + "/" for folder in manifests if folder.startswith("docs/"))
    return read | {path for path in paths if path.startswith(packages) and not path.endswith(".md")}


def needs_rust(paths):
    # Docs, *.md and the Python tools are Rust-free by name, unless Rust embeds or tests them (rust_reads).
    paths = list(paths)
    if any(not rust_free_by_name(path) for path in paths):
        return True
    return bool(paths) and bool(rust_reads(paths))


# verify's second clippy, the server build (sunrise-client without default features): what it checks differently from
# the first is the feature-gated code of the client crates and the features the manifests choose. Pull requests that
# change neither skip it; main's pushes and the merge queue always run it.
SERVER_BUILD_CHECK = {".github/workflows/ci.yml", "tools/ci/rust_changes.py"}


def needs_server_build(paths):
    return any(path.startswith("crates/sunrise-client") or path.endswith("Cargo.toml") or path in ROOT_RUST
               or path.startswith(".cargo/") or path in SERVER_BUILD_CHECK for path in paths)


def needs_launcher(paths):
    return any(path in ROOT_RUST or path in LAUNCHER_CHECK or (
        path.startswith(tuple(release_train.LAUNCHER_PATHS))
        and not path.startswith(tuple(release_train.LAUNCHER_IGNORED))
        and not path.endswith(".md")
    ) for path in paths)


# Stands for "everything changed" when no diff can be computed: a root manifest every check and shard selector
# treats as touching all of them.
UNKNOWN_CHANGE = "Cargo.lock"


def needs_sample(paths):
    return any(path in ROOT_RUST or path == "tools/ci/check_machine_sample.py"
               or path.startswith(("crates/sunrise-mod-", "crates/sunrise-core/",
                                   "crates/sunrise-game-discovery/")) for path in paths)


def needs_tooling(changes):
    paths = [path for _, path in changes]
    return needs_rust(paths) or any(
        path.startswith(("tools/", "tests/", ".github/", "docs/modding/", "roadmap/"))
        or path in {"README.md", "ROADMAP.md", "docs/README.md"}
        # Tests check linked target existence, even when they do not read its contents.
        or status != "M"
        for status, path in changes
    )


def selections(changes):
    paths = [path for _, path in changes]
    return {"rust_required": needs_rust(paths),
            "launcher_required": needs_launcher(paths),
            "tooling_required": needs_tooling(changes)}


def changed_entries(base, head):
    # Disable rename folding: retain both deleted and added paths, including source moved into docs.
    command = ["git", "diff", "--name-status", "--no-renames", "-z", f"{base}...{head}", "--"]
    for fetch_base in (False, True):
        # The event's base can be newer than the checked-out history (main moved on): fetch it once.
        if fetch_base:
            subprocess.run(["git", "fetch", "-q", "--no-tags", "origin", base], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        diff = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        if diff.returncode == 0:
            fields = diff.stdout.decode("utf-8").split("\0")[:-1]
            return list(zip(fields[::2], fields[1::2]))
    # Never an error and never "nothing changed": assume everything did.
    print(f"rust_changes: no diff for {base}...{head}; treating everything as changed", file=sys.stderr)
    return [("?", UNKNOWN_CHANGE)]


def listed_entries(path):
    """The (status, path) pairs of a `git diff --name-status -z` listing saved in a file (tools/ci/pr_files.py)."""
    fields = Path(path).read_bytes().decode("utf-8").split("\0")[:-1]
    return list(zip(fields[::2], fields[1::2]))


def changed_paths(base, head):
    return [path for _, path in changed_entries(base, head)]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base")
    parser.add_argument("head")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--launcher", action="store_true", help="answer for the launcher instead")
    mode.add_argument("--all", action="store_true", help="emit centralized dyna-checks selections")
    parser.add_argument("--files", metavar="LIST", help="read base...head's changes from this listing (pr_files.py) "
                        "instead of git")
    args = parser.parse_args()
    changes = listed_entries(args.files) if args.files else changed_entries(args.base, args.head)
    paths = [path for _, path in changes]
    if args.all:
        for name, required in selections(changes).items():
            print(f"{name}={str(required).lower()}")
    else:
        needs = needs_launcher if args.launcher else needs_rust
        print(f"required={str(needs(paths)).lower()}")
        print(f"sample={str(needs_sample(paths)).lower()}")
        print(f"server_build={str(needs_server_build(paths)).lower()}")
