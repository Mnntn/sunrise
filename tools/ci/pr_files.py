#!/usr/bin/env python3
"""A pull request's changed files from GitHub's API, in the form rust_changes.py reads them, so dyna-checks' `changes`
job can select checks without fetching the history.

usage: pr_files.py OUT    (env: GITHUB_API_URL, GITHUB_REPOSITORY, GH_TOKEN, PR, BASE_SHA, HEAD_SHA, CHANGED_FILES)

Writes OUT as `git diff --name-status --no-renames -z BASE_SHA...HEAD_SHA` would, and prints `listed=true`. A rename is
a deletion and an addition, as with --no-renames; a copy is an addition. Prints `listed=false` (and the reason on
stderr) whenever the list may differ from git's, so the job falls back to the history:
- an API error, or a file status other than added, removed, modified, renamed or copied;
- the list reaches the endpoint's 3,000 files, or its count differs from the event's changed_files;
- the pull request's head or base has moved since the event.
Never fails the step.
"""

import json
import os
import sys
import urllib.request

PAGE = 100
# GET /pulls/{n}/files lists at most this many files.
API_LIMIT = 3000
STATUS = {"added": "A", "removed": "D", "modified": "M", "copied": "A"}


def get(path):
    request = urllib.request.Request(
        f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}/repos/{os.environ['GITHUB_REPOSITORY']}/{path}",
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    if os.environ.get("GH_TOKEN"):
        request.add_header("Authorization", f"Bearer {os.environ['GH_TOKEN']}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def entries(files):
    """git's (status, path) pairs for the API's file list, or None if a status has no exact git equivalent."""
    out = []
    for item in files:
        if item["status"] == "renamed":
            out += [("D", item["previous_filename"]), ("A", item["filename"])]
        elif item["status"] in STATUS:
            out.append((STATUS[item["status"]], item["filename"]))
        else:
            return None
    return out


def listed():
    """The (status, path) pairs, or the reason they can't stand for git's diff."""
    number, expected = os.environ["PR"], int(os.environ["CHANGED_FILES"])
    if expected >= API_LIMIT:
        return f"{expected} files: the API lists at most {API_LIMIT}"
    files = []
    for page in range(1, API_LIMIT // PAGE + 1):
        batch = get(f"pulls/{number}/files?per_page={PAGE}&page={page}")
        files += batch
        if len(batch) < PAGE:
            break
    if len(files) != expected:
        return f"the API lists {len(files)} files, the event {expected}"
    # Read after the list: a head or base that moved since the event may have changed it.
    now = get(f"pulls/{number}")
    if now["head"]["sha"] != os.environ["HEAD_SHA"] or now["base"]["sha"] != os.environ["BASE_SHA"]:
        return "the pull request's head or base moved since this event"
    changes = entries(files)
    if changes is None:
        return "a file status with no exact git equivalent: " + ", ".join(sorted({f["status"] for f in files}))
    return changes


def main(out):
    try:
        changes = listed()
    except Exception as error:  # noqa: BLE001 - any failure only means falling back to git
        changes = f"{type(error).__name__}: {error}"
    if isinstance(changes, str):
        print(f"pr_files: {changes}; selecting from the history instead", file=sys.stderr)
        print("listed=false")
        return
    with open(out, "wb") as stream:
        stream.write(b"".join(f"{status}\0{path}\0".encode("utf-8") for status, path in changes))
    print(f"pr_files: {len(changes)} entries from the API", file=sys.stderr)
    print("listed=true")


if __name__ == "__main__":
    main(sys.argv[1])
