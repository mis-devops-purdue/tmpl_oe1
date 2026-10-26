"""Thin wrapper around the GitHub REST API, using the `gh` CLI for auth.

The same class runs inside GitHub Actions (GH_TOKEN = the workflow token)
and on the instructor's laptop (whatever `gh auth login` stored).
Every method returns plain dicts so the checkers can be tested with a fake.
"""
import base64
import json
import subprocess
import time

BOT_LOGIN = "github-actions[bot]"


class GitHubError(RuntimeError):
    pass


def api(path, method="GET", data=None, paginate=False, allow_404=False):
    cmd = ["gh", "api", "-X", method, "-H", "Accept: application/vnd.github+json", path]
    if paginate:
        cmd += ["--paginate", "--slurp"]
    stdin = None
    if data is not None:
        cmd += ["--input", "-"]
        stdin = json.dumps(data)
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if proc.returncode != 0:
        msg = (proc.stderr + " " + proc.stdout).strip()
        if allow_404 and ("404" in msg or "Not Found" in msg):
            return None
        raise GitHubError(f"gh api {method} {path} failed: {msg[:600]}")
    out = proc.stdout.strip()
    if not out:
        return None
    value = json.loads(out)
    if paginate:
        flat = []
        for page in value:
            flat.extend(page if isinstance(page, list) else [page])
        return flat
    return value


def _pull(p):
    return {
        "number": p["number"],
        "user": p["user"]["login"],
        "title": p.get("title", ""),
        "head_ref": p["head"]["ref"],
        "head_sha": p["head"]["sha"],
        "state": p["state"],
        "merged": bool(p.get("merged_at")),
        "merged_at": p.get("merged_at"),
        "created_at": p.get("created_at"),
        "requested_reviewers": [r["login"] for r in p.get("requested_reviewers", [])],
    }


class Repo:
    """Live repository client. `full_name` is 'org/repo'."""

    def __init__(self, full_name):
        self.full_name = full_name
        self.name = full_name.split("/", 1)[1]
        self._r = f"repos/{full_name}"

    # ---- pull requests -------------------------------------------------
    def list_pulls(self):
        return [_pull(p) for p in api(f"{self._r}/pulls?state=all&per_page=100", paginate=True)]

    def get_mergeable(self, number, tries=8):
        """True / False / None (unknown). GitHub computes this lazily."""
        for _ in range(tries):
            p = api(f"{self._r}/pulls/{number}")
            if p.get("mergeable") is not None:
                return p["mergeable"]
            time.sleep(3)
        return None

    def pr_files(self, number):
        return [{"filename": f["filename"], "status": f["status"]}
                for f in api(f"{self._r}/pulls/{number}/files?per_page=100", paginate=True)]

    def pr_commits(self, number):
        out = []
        for c in api(f"{self._r}/pulls/{number}/commits?per_page=100", paginate=True):
            out.append({"sha": c["sha"], "parents": len(c.get("parents", [])),
                        "author": (c.get("author") or {}).get("login")})
        return out

    def pr_reviews(self, number):
        return [{"user": r["user"]["login"] if r.get("user") else None, "state": r["state"],
                 "body": r.get("body") or "", "submitted_at": r.get("submitted_at"),
                 "commit_id": r.get("commit_id")}
                for r in api(f"{self._r}/pulls/{number}/reviews?per_page=100", paginate=True)]

    def pr_review_comments(self, number):
        return [{"user": c["user"]["login"], "body": c.get("body", "")}
                for c in api(f"{self._r}/pulls/{number}/comments?per_page=100", paginate=True)]

    def create_review(self, number, event, body):
        api(f"{self._r}/pulls/{number}/reviews", "POST", {"event": event, "body": body})

    def create_pull(self, head, base, title, body):
        return api(f"{self._r}/pulls", "POST",
                   {"head": head, "base": base, "title": title, "body": body})["number"]

    def request_reviewers(self, number, logins):
        api(f"{self._r}/pulls/{number}/requested_reviewers", "POST", {"reviewers": logins})

    # ---- collaborators -------------------------------------------------
    def direct_collaborators(self):
        """Logins added to this repo by name (not via a team or the org), bots excluded."""
        return [c["login"] for c in api(f"{self._r}/collaborators?affiliation=direct&per_page=100", paginate=True)
                if c.get("type") != "Bot"]

    # ---- comments & issues ---------------------------------------------
    def issue_comments(self, number):
        return [{"id": c["id"], "user": c["user"]["login"], "body": c.get("body") or ""}
                for c in api(f"{self._r}/issues/{number}/comments?per_page=100", paginate=True)]

    def create_comment(self, number, body):
        api(f"{self._r}/issues/{number}/comments", "POST", {"body": body})

    def update_comment(self, comment_id, body):
        api(f"{self._r}/issues/comments/{comment_id}", "PATCH", {"body": body})

    def list_issues(self):
        return [{"number": i["number"], "title": i["title"], "body": i.get("body") or "",
                 "user": i["user"]["login"], "state": i.get("state", "open")}
                for i in api(f"{self._r}/issues?state=all&per_page=100", paginate=True)
                if "pull_request" not in i]

    def get_issue(self, number):
        """One issue or pull request by number, straight from the API (None if it doesn't exist).

        Unlike list_issues(), this is not a search, so it is the reliable way to look at a known number."""
        i = api(f"{self._r}/issues/{number}", allow_404=True)
        if not i:
            return None
        return {"number": i["number"], "title": i["title"], "body": i.get("body") or "",
                "user": (i.get("user") or {}).get("login"), "state": i.get("state", "open"),
                "is_pull": "pull_request" in i}

    def close_issue(self, number):
        """Close as 'not planned'; fall back to a plain close if GitHub rejects state_reason."""
        try:
            api(f"{self._r}/issues/{number}", "PATCH", {"state": "closed", "state_reason": "not_planned"})
        except GitHubError:
            api(f"{self._r}/issues/{number}", "PATCH", {"state": "closed"})

    def create_issue(self, title, body, assignees=()):
        data = {"title": title, "body": body}
        if assignees:
            try:
                return api(f"{self._r}/issues", "POST", dict(data, assignees=list(assignees)))["number"]
            except GitHubError as exc:  # e.g. invitation not yet accepted
                print(f"could not assign issue: {exc}")
        return api(f"{self._r}/issues", "POST", data)["number"]

    def update_issue(self, number, body):
        api(f"{self._r}/issues/{number}", "PATCH", {"body": body})

    # ---- contents & branches -------------------------------------------
    def default_branch(self):
        return api(self._r)["default_branch"]

    def file_text(self, path, ref):
        f = api(f"{self._r}/contents/{path}?ref={ref}", allow_404=True)
        if not f or isinstance(f, list) or "content" not in f:
            return None
        return base64.b64decode(f["content"]).decode("utf-8", errors="replace")

    def file_blob_sha(self, path, ref):
        f = api(f"{self._r}/contents/{path}?ref={ref}", allow_404=True)
        return f.get("sha") if isinstance(f, dict) else None

    def branch_sha(self, branch):
        b = api(f"{self._r}/git/ref/heads/{branch}", allow_404=True)
        return b["object"]["sha"] if b else None

    def create_branch(self, branch, sha):
        api(f"{self._r}/git/refs", "POST", {"ref": f"refs/heads/{branch}", "sha": sha})

    def put_file(self, path, branch, text, message):
        data = {"message": message, "branch": branch,
                "content": base64.b64encode(text.encode()).decode()}
        sha = self.file_blob_sha(path, branch)
        if sha:
            data["sha"] = sha
        api(f"{self._r}/contents/{path}", "PUT", data)

    def main_commits(self, branch, limit=100):
        return [{"sha": c["sha"], "parents": len(c.get("parents", [])),
                 "message": c["commit"]["message"]}
                for c in api(f"{self._r}/commits?sha={branch}&per_page={limit}")]


def upsert_marked_comment(repo, number, marker, body):
    """Create or edit the bot's single comment that carries `marker`."""
    full = f"{body}\n\n{marker}"
    for c in repo.issue_comments(number):
        if c["user"] == BOT_LOGIN and marker in c["body"]:
            if c["body"].strip() != full.strip():
                repo.update_comment(c["id"], full)
            return
    repo.create_comment(number, full)


def comment_once(repo, number, marker, body):
    """Post a comment only if the bot has never posted one with `marker`."""
    for c in repo.issue_comments(number):
        if c["user"] == BOT_LOGIN and marker in c["body"]:
            return False
    repo.create_comment(number, f"{body}\n\n{marker}")
    return True
