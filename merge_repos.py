#!/usr/bin/env python3
"""Append repos missed by jstrieb's contribution-graph discovery.

jstrieb discovers repos via `contributionsCollection.commitContributionsByRepository`
(the green-squares calendar). That field skips commits whose committer is
`GitHub <noreply@github.com>` (i.e. PR squash-merges done via web/API), so repos
like openIndu/openindu-station never enter jstrieb's data, even though you are a
recognised contributor there.

This script queries `repositoriesContributedTo` (which DOES list those repos) and
appends any repo that is missing from stats.json (produced by jstrieb's pass 1),
so jstrieb's `--json-input-file` pass picks them up. Own forks are excluded.
"""
import json
import os
import subprocess
import sys
import time

OWNER = os.environ.get("OWNER", "TomNewChao")
STATS_FILE = os.environ.get("STATS_FILE", "stats.json")


def gh_rest(path):
    return subprocess.run(
        ["gh", "api", path], capture_output=True, text=True
    )


def gh_graphql(query):
    r = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        print(f"graphql error: {r.stderr}", file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        print(f"graphql non-json: {r.stdout[:200]}", file=sys.stderr)
        sys.exit(1)


def get_lines_changed(full_name):
    """Sum this user's add+del across weeks via /stats/contributors.
    Endpoint computes async (HTTP 202) then caches. Poll, fall back to 0."""
    url = f"repos/{full_name}/stats/contributors"
    for _ in range(6):
        r = gh_rest(url)
        if r.returncode == 0 and r.stdout.strip():
            try:
                arr = json.loads(r.stdout)
            except json.JSONDecodeError:
                arr = None
            if isinstance(arr, list):
                total = 0
                for c in arr:
                    if c.get("author", {}).get("login") == OWNER:
                        for w in c.get("weeks", []):
                            total += int(w.get("a", 0)) + int(w.get("d", 0))
                return total
        time.sleep(3)
    return 0


def get_views(full_name):
    r = gh_rest(f"repos/{full_name}/traffic/views")
    if r.returncode == 0 and r.stdout.strip():
        try:
            return int(json.loads(r.stdout).get("count", 0))
        except (json.JSONDecodeError, ValueError):
            pass
    return 0


def main():
    with open(STATS_FILE) as f:
        stats = json.load(f)
    existing = {r["name"] for r in stats.get("repositories", [])}
    print(f"pass-1 stats.json has {len(existing)} repos")

    r = subprocess.run(
        [
            "gh", "api",
            f"users/{OWNER}/repos?per_page=100&type=owner",
            "--paginate",
            "--jq", ".[] | select(.fork==true) | .full_name",
        ],
        capture_output=True,
        text=True,
    )
    forks = {ln for ln in r.stdout.strip().splitlines() if ln.strip()}
    print(f"own forks to skip: {len(forks)}")

    query = """
    query { viewer { repositoriesContributedTo(contributionTypes: [COMMIT], first: 100) {
      nodes { nameWithOwner stargazerCount forkCount isPrivate
        languages(first: 100, orderBy: {direction: DESC, field: SIZE}) {
          edges { size node { name color } } } } } } }
    """
    data = gh_graphql(query)
    contributed = data["data"]["viewer"]["repositoriesContributedTo"]["nodes"]
    print(f"repositoriesContributedTo: {len(contributed)} repos")

    missing = [
        n for n in contributed
        if n["nameWithOwner"] not in existing
        and n["nameWithOwner"] not in forks
    ]
    print(f"missing (will merge): {len(missing)}")
    for n in missing:
        print(f"  - {n['nameWithOwner']}")

    added = 0
    for n in missing:
        fn = n["nameWithOwner"]
        edges = (n.get("languages") or {}).get("edges") or []
        langs = [
            {
                "name": e["node"]["name"],
                "size": int(e["size"]),
                "color": e["node"].get("color"),
            }
            for e in edges
        ]
        lc = get_lines_changed(fn)
        views = get_views(fn)
        stats["repositories"].append({
            "name": fn,
            "stars": int(n["stargazerCount"]),
            "forks": int(n["forkCount"]),
            "languages": langs,
            "lines_changed": lc,
            "views": views,
            "private": bool(n["isPrivate"]),
        })
        added += 1
        print(
            f"merged {fn}: stars={n['stargazerCount']} "
            f"langs={len(langs)} lines={lc} views={views}"
        )

    with open(STATS_FILE, "w") as f:
        json.dump(stats, f, indent=2)
    print(f"done: merged {added}, total repos now {len(stats['repositories'])}")


if __name__ == "__main__":
    main()
