#!/usr/bin/env python3
"""Make the language card and "lines of code changed" describe only the
viewer's own recent code changes.

Input is the JSON written by `github-stats --json-output-file` (after
merge_repos.py). For every repository this script replaces `languages` and
`lines_changed` with what the viewer authored on the default branch during the
last --days days. Repositories keep their place in the list, so every other
overview number is computed exactly as before. The one exception: repositories
with more than --stars-threshold stars (upstream projects such as apache/plc4x)
get stars, forks and views set to 0, so other people's popularity is not
counted as the viewer's own. The result is rendered with
`github-stats --json-input-file`.

What counts: non-merge, non-root commits that GitHub attributes to the viewer.
Per file, additions plus deletions, except whole-file deletions, generated and
vendored code, documentation, data and files in unrecognised languages.

Standard library only. The token comes from GH_TOKEN or ACCESS_TOKEN and is
never printed; private repository names are replaced by a short hash in all
output so that public CI logs do not reveal them.
"""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import PurePosixPath
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API = "https://api.github.com"
# 36 months. The workflow, the card labels in src/templates and the README must
# say the same; tests/test_recent_stats.py checks it.
DEFAULT_DAYS = 1095
MAX_LISTED_FILES = 3000  # GitHub's cap on the files it lists for one commit
MAX_ATTEMPTS = 5
MAX_WAIT_SECONDS = 20 * 60
NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')

LANGUAGES = {
    ".bash": "Shell", ".bat": "Batchfile", ".c": "C", ".cc": "C++",
    ".cjs": "JavaScript", ".cmake": "CMake", ".cmd": "Batchfile",
    ".cpp": "C++", ".cs": "C#", ".cshtml": "Razor", ".css": "CSS",
    ".cxx": "C++", ".dart": "Dart", ".ex": "Elixir", ".exs": "Elixir",
    ".fs": "F#", ".g4": "ANTLR", ".go": "Go", ".groovy": "Groovy",
    ".h": "C", ".hbs": "Handlebars", ".hcl": "HCL", ".hh": "C++",
    ".hpp": "C++", ".hs": "Haskell", ".htm": "HTML", ".html": "HTML",
    ".hxx": "C++", ".j2": "Jinja", ".java": "Java", ".jinja": "Jinja",
    ".jinja2": "Jinja", ".js": "JavaScript", ".jsonnet": "Jsonnet",
    ".jsx": "JavaScript", ".kt": "Kotlin", ".kts": "Kotlin", ".less": "Less",
    ".lua": "Lua", ".mjs": "JavaScript", ".pl": "Perl", ".php": "PHP",
    ".ps1": "PowerShell", ".psm1": "PowerShell", ".py": "Python",
    ".pyx": "Cython", ".r": "R", ".raku": "Raku", ".razor": "Razor",
    ".rb": "Ruby", ".rs": "Rust", ".scala": "Scala", ".scss": "SCSS",
    ".sh": "Shell", ".sql": "SQL", ".svelte": "Svelte", ".swift": "Swift",
    ".tf": "HCL", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".vb": "Visual Basic .NET", ".vue": "Vue", ".xaml": "XAML",
    ".xsl": "XSLT", ".xslt": "XSLT", ".zig": "Zig", ".zsh": "Shell",
}
SPECIAL_NAMES = {
    "cmakelists.txt": "CMake", "dockerfile": "Dockerfile", "gemfile": "Ruby",
    "gnumakefile": "Makefile", "jenkinsfile": "Groovy", "makefile": "Makefile",
    "rakefile": "Ruby",
}
# GitHub linguist colours, used when the input JSON has no colour for a language.
COLORS = {
    "ANTLR": "#9DC3FF", "Batchfile": "#C1F12E", "C": "#555555",
    "C#": "#178600", "C++": "#f34b7d", "CMake": "#DA3434", "CSS": "#663399",
    "Cython": "#fedf5b", "Dart": "#00B4AB", "Dockerfile": "#384d54",
    "Elixir": "#6e4a7e", "F#": "#b845fc", "Go": "#00ADD8",
    "Groovy": "#4298b8", "HCL": "#844FBA", "HTML": "#e34c26",
    "Handlebars": "#f7931e", "Haskell": "#5e5086", "Java": "#b07219",
    "JavaScript": "#f1e05a", "Jinja": "#a52a22", "Jsonnet": "#0064bd",
    "Kotlin": "#A97BFF", "Less": "#1d365d", "Lua": "#000080",
    "Makefile": "#427819", "PHP": "#4F5D95", "Perl": "#0298c3",
    "PowerShell": "#012456", "Python": "#3572A5", "R": "#198CE7",
    "Raku": "#0000fb", "Razor": "#512be4", "Ruby": "#701516",
    "Rust": "#dea584", "SCSS": "#c6538c", "SQL": "#e38c00",
    "Scala": "#c22d40", "Shell": "#89e051", "Svelte": "#ff3e00",
    "Swift": "#F05138", "TypeScript": "#3178c6",
    "Visual Basic .NET": "#945db7", "Vue": "#41b883", "XAML": "#0c54c2",
    "XSLT": "#EB8CEB", "Zig": "#ec915c",
}
# Known non-source files. Only used to tell "ignored on purpose" apart from
# "unrecognised extension" in the run summary; neither kind is counted.
NON_CODE_SUFFIXES = {
    ".adoc", ".bmp", ".config", ".conf", ".cfg", ".crt", ".csproj", ".csv",
    ".diff", ".eot", ".env", ".gif", ".ico", ".interp", ".ini", ".ipynb",
    ".jpeg", ".jpg", ".json", ".json5", ".jsonl", ".key", ".lock", ".log",
    ".map", ".markdown", ".md", ".mdx", ".mod", ".mo", ".org", ".otf",
    ".patch", ".pdf", ".pem", ".png", ".po", ".pot", ".properties", ".proto",
    ".props", ".resx", ".rst", ".sln", ".snap", ".sum", ".svg", ".targets",
    ".toml", ".tokens", ".tsv", ".ttf", ".txt", ".vcxproj", ".webp",
    ".woff", ".woff2", ".xml", ".yaml", ".yml",
}
VENDORED_DIRS = {
    "bower_components", "deps", "node_modules", "third-party", "third_party",
    "thirdparty", "vendor", "vendors",
}
GENERATED_DIRS = {
    ".next", ".nuxt", ".svelte-kit", ".venv", "__generated__", "__pycache__",
    "build", "coverage", "dist", "generated", "obj", "site-packages", "venv",
}
# "django-allauth-65.4.1": a directory named after a released version is a
# copy of somebody else's source tree.
VERSIONED_DIR = re.compile(r"(?:^|[-_])v?\d+\.\d+(?:\.\d+)*$")
GENERATED_NAMES = {"assemblyinfo.cs"}
GENERATED_NAME = re.compile(
    r"(\.min\.(js|css)|\.bundle\.js|\.g(\.i)?\.cs|\.designer\.cs"
    r"|\.generated\.\w+|_generated\.\w+|_pb2(_grpc)?\.pyi?|\.pb\.(go|cc|h)"
    r"|_pb\.js)$"
)
# Looked for in the first added lines of a file that starts at line 1.
GENERATED_MARKERS = (
    "auto-generated", "autogenerated", "auto generated", "generated by",
    "generated from", "do not edit", "@generated", "code generated",
    "this file was generated",
)
STARTS_AT_LINE_ONE = re.compile(r"^@@ -\d+(?:,\d+)? \+1(?:,\d+)? @@")


class GitHubError(RuntimeError):
    """A GitHub API failure. The message never contains a repository name."""


def language_for(name):
    """Language of a lower-cased file name, or None if it is not recognised."""
    if name in SPECIAL_NAMES:
        return SPECIAL_NAMES[name]
    if name.startswith("dockerfile.") or name.endswith(".dockerfile"):
        return "Dockerfile"
    return LANGUAGES.get(PurePosixPath(name).suffix)


def has_generated_header(patch):
    if not patch or not STARTS_AT_LINE_ONE.match(patch):
        return False
    added = [line[1:] for line in patch.splitlines()[1:60] if line.startswith("+")]
    head = "\n".join(added[:20]).lower()
    return any(marker in head for marker in GENERATED_MARKERS)


def classify(filename, patch=None):
    """Return (language, reason). The language is None if the file is not counted."""
    path = PurePosixPath(filename)
    for part in (part.lower() for part in path.parts[:-1]):
        if part in VENDORED_DIRS or VERSIONED_DIR.search(part):
            return None, "vendored"
        if part in GENERATED_DIRS:
            return None, "generated"
    name = path.name.lower()
    if name in GENERATED_NAMES or GENERATED_NAME.search(name):
        return None, "generated"
    language = language_for(name)
    if language is None:
        suffix = PurePosixPath(name).suffix
        if suffix in NON_CODE_SUFFIXES:
            return None, "non-code"
        # Only the extension: file names of private repositories must not
        # end up in a public CI log.
        return None, "unrecognised:" + (suffix or "(no extension)")
    if has_generated_header(patch):
        return None, "generated"
    return language, ""


def retry_delay(status, headers, body, attempt, now=time.time):
    """Seconds to wait before retrying, or None when retrying cannot help."""
    if status in (500, 502, 503, 504):
        return min(2 ** attempt, 60)
    if status not in (403, 429):
        return None
    retry_after = headers.get("Retry-After") or ""
    reset = headers.get("X-RateLimit-Reset") or ""
    if retry_after.isdigit():
        wait = int(retry_after)
    elif headers.get("X-RateLimit-Remaining") == "0" and reset.isdigit():
        wait = max(int(reset) - int(now()), 0) + 1
    elif "rate limit" in body.lower() or "abuse" in body.lower():
        wait = 30 * attempt
    else:
        return None  # e.g. "Resource not accessible by personal access token"
    return wait if wait <= MAX_WAIT_SECONDS else None


class GitHubClient:
    def __init__(self, token, opener=urlopen, sleep=time.sleep):
        if not token:
            raise ValueError("GH_TOKEN or ACCESS_TOKEN is required")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-stats-recent-contributions",
        }
        self.opener = opener
        self.sleep = sleep

    def _get(self, url):
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with self.opener(Request(url, headers=self.headers), timeout=60) as response:
                    return json.load(response), response.headers.get("Link", "")
            except HTTPError as error:
                if error.code == 409:  # An empty repository has no commits.
                    return None, ""
                body = error.read().decode("utf-8", "replace")[:500] if error.fp else ""
                delay = retry_delay(error.code, error.headers, body, attempt)
                if delay is None or attempt == MAX_ATTEMPTS:
                    raise GitHubError(f"HTTP {error.code}") from error
            except (URLError, OSError, HTTPException, ValueError) as error:
                if attempt == MAX_ATTEMPTS:
                    raise GitHubError(type(error).__name__) from error
                delay = min(2 ** attempt, 60)
            self.sleep(delay)

    def pages(self, path, params=None):
        url = API + path + ("?" + urlencode(params) if params else "")
        while url:
            payload, link = self._get(url)
            if payload is None:
                return
            yield payload
            match = NEXT_LINK.search(link)
            url = match.group(1) if match else None


def display_name(repo):
    """Name that is safe to print in public CI logs."""
    if repo.get("private"):
        return "<private:" + hashlib.sha1(repo["name"].encode()).hexdigest()[:6] + ">"
    return repo["name"]


def list_commits(client, repo_name, user, since, until):
    params = {"author": user, "per_page": 100,
              "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "until": until.strftime("%Y-%m-%dT%H:%M:%SZ")}
    shas = []
    for page in client.pages(f"/repos/{repo_name}/commits", params):
        for commit in page:
            login = (commit.get("author") or {}).get("login") or ""
            # A root commit may be an import of somebody else's code and a
            # merge commit's diff mixes in other people's work.
            if login.lower() == user.lower() and len(commit.get("parents", [])) == 1:
                shas.append(commit["sha"])
    return shas


def commit_changes(client, repo_name, sha):
    """Return ({language: lines}, Counter of ignored lines by reason, [(lines, file)])."""
    files = [file for page in client.pages(f"/repos/{repo_name}/commits/{sha}")
             for file in page.get("files", [])]
    if len(files) >= MAX_LISTED_FILES:
        # The API lists at most 3000 files, so this list is cut off and the
        # commit cannot be measured. A commit this big is a bulk import,
        # rename or removal rather than code written by hand: skip it.
        lines = sum(file["additions"] + file["deletions"] for file in files)
        return {}, Counter({"bulk-commit": lines}), []
    changes, ignored, counted = defaultdict(int), Counter(), []
    for file in files:
        # Per-file numbers are the source of truth: the commit-level total
        # does not always add up.
        lines = file["additions"] + file["deletions"]
        if not lines:
            continue
        if file.get("status") == "removed":
            ignored["removed-file"] += lines
            continue
        language, reason = classify(file["filename"], file.get("patch"))
        if language is None:
            ignored[reason] += lines
        else:
            changes[language] += lines
            counted.append((lines, file["filename"]))
    return changes, ignored, counted


def repo_changes(client, repo_name, user, since, until, workers=6):
    """Return per-language changed lines of the user's commits in one repository."""
    shas = list_commits(client, repo_name, user, since, until)
    totals, ignored, files = defaultdict(int), Counter(), []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for changes, skipped, counted in pool.map(
                lambda sha: commit_changes(client, repo_name, sha), shas):
            for language, lines in changes.items():
                totals[language] += lines
            ignored.update(skipped)
            files.extend(counted)
    return {"languages": dict(totals), "commits": len(shas),
            "ignored": ignored, "top_files": sorted(files, reverse=True)[:5]}


def is_excluded(name, patterns):
    return any(fnmatchcase(name, pattern) for pattern in patterns)


def transform(stats, client, since, until, excluded=(), stars_threshold=1000,
              workers=6, log=print, audit=None):
    user = stats["user"]
    colors = {lang["name"]: lang["color"] for repo in stats["repositories"]
              for lang in (repo.get("languages") or []) if lang.get("color")}
    ignored = Counter()
    commits = 0
    for repo in stats["repositories"]:
        if repo["stars"] > stars_threshold:
            repo["stars"] = repo["forks"] = repo["views"] = 0
        if is_excluded(repo["name"], excluded):
            continue  # Dropped by the renderer anyway; save the API calls.
        try:
            result = repo_changes(client, repo["name"], user, since, until, workers)
        except GitHubError as error:
            raise GitHubError(f"{display_name(repo)}: {error}") from error
        languages = sorted(result["languages"].items(), key=lambda item: (-item[1], item[0]))
        repo["languages"] = [{"name": name, "size": size,
                              "color": COLORS.get(name) or colors.get(name)}
                             for name, size in languages]
        repo["lines_changed"] = sum(size for _, size in languages)
        ignored.update(result["ignored"])
        commits += result["commits"]
        if repo["lines_changed"]:
            log(f"{display_name(repo)}: {result['commits']} commits, "
                f"{repo['lines_changed']} lines")
        if audit is not None and result["commits"]:
            audit.append({"name": repo["name"], "commits": result["commits"],
                          "lines": repo["lines_changed"],
                          "languages": result["languages"],
                          "ignored": dict(result["ignored"]),
                          "top_files": result["top_files"]})
    return stats, commits, ignored


def summarize(stats, commits, ignored, log=print):
    totals = Counter()
    for repo in stats["repositories"]:
        for lang in repo.get("languages") or []:
            totals[lang["name"]] += lang["size"]
    grand = sum(totals.values())
    log(f"counted {grand} lines in {commits} commits")
    for name, size in totals.most_common(12):
        log(f"  {name}: {size} ({100.0 * size / grand:.1f}%)")
    log("ignored lines by reason:")
    for reason, size in ignored.most_common(12):
        log(f"  {reason}: {size}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", default="stats.json")
    parser.add_argument("--output", default="recent-stats.json")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--exclude", default=os.environ.get("EXCLUDE_REPOS", ""),
                        help="comma or space separated repository names or globs "
                             "(default: $EXCLUDE_REPOS)")
    parser.add_argument("--stars-threshold", type=int, default=1000)
    parser.add_argument("--audit-file",
                        help="write per-repository details for local review; "
                             "contains private repository names, never publish it")
    args = parser.parse_args(argv)
    if args.days <= 0:
        parser.error("--days must be positive")
    until = datetime.now(timezone.utc)
    since = until - timedelta(days=args.days)
    with open(args.input, encoding="utf-8") as source:
        stats = json.load(source)
    excluded = [item for item in re.split(r"[\s,|\"']+", args.exclude) if item]
    audit = [] if args.audit_file else None
    try:
        client = GitHubClient(os.environ.get("GH_TOKEN") or os.environ.get("ACCESS_TOKEN"))
        stats, commits, ignored = transform(stats, client, since, until, excluded,
                                            args.stars_threshold, audit=audit)
    except (GitHubError, ValueError) as error:
        sys.exit(f"error: {error}")
    with open(args.output, "w", encoding="utf-8") as target:
        json.dump(stats, target, indent=2)
    if audit is not None:
        with open(args.audit_file, "w", encoding="utf-8") as target:
            json.dump(audit, target, indent=2)
    print(f"window: {since.date()} to {until.date()}")
    summarize(stats, commits, ignored)


if __name__ == "__main__":
    main()
