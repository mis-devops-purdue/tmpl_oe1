"""OE 1 – Teammate Bot.

One private repo per student, named <term>_oe1_<purdue-username>. The student's GitHub
login is NOT taken from the repo name: setup_repos.py records it in the repository Actions
variable STUDENT_LOGIN, which the workflow passes in as an environment variable. On every
event the bot re-reads the whole repo and takes whatever action is still missing
(a "reconcile" loop), so events can arrive in any order or be missed.

Student journey:
  1. Bio PR from bio/<name>  -> bot requests one change (time zone), then approves
  2. Bio merged              -> bot opens its own PR on TEAM_NORMS.md and asks the student to review
  3. Student reviews + merges the bot's PR
  4. Student's norms/<name> PR now conflicts -> student resolves, bot approves, student merges
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from checks import (BOT_RULE, BOT_RULE_KEY, bio_problems, box, branch_suffix,  # noqa: E402
                    explain, norms_problems)
from ghclient import BOT_LOGIN, comment_once  # noqa: E402

BOT_BRANCH = "norms/teammate_bot"
NORMS_FILE = "TEAM_NORMS.md"
ISSUE_TITLE = "OE 1 Progress (Teammate Bot checklist)"
STATUS_MARK = "<!-- oe1_status:"
SIG = "<!-- bot_sig:"


def student_of(repo, student=None):
    """The student's GitHub login, or None if it can't be determined.

    In order: the explicit `student` argument (the export passes it), the STUDENT_LOGIN
    environment variable (set from the repo variable by the workflow), then the repo's
    direct collaborators, if exactly one is not a bot. The repo name is deliberately not
    used: it holds the Purdue username, not the GitHub login.
    """
    if student:
        return student
    env = os.environ.get("STUDENT_LOGIN", "").strip()
    if env:
        return env
    people = [l for l in repo.direct_collaborators() if not l.endswith("[bot]")]
    return people[0] if len(people) == 1 else None


def _same(a, b):
    return (a or "").lower() == (b or "").lower()


def _latest(pulls, pred):
    hits = [p for p in pulls if pred(p)]
    return max(hits, key=lambda p: p["number"]) if hits else None


def _bot_reviews(repo, number):
    return [r for r in repo.pr_reviews(number) if r["user"] == BOT_LOGIN]


def evaluate(repo, student=None):
    """Read-only snapshot of the student's progress (used by bot AND export)."""
    student = student_of(repo, student)
    pulls = repo.list_pulls()
    s = {"student": student, "touched_github_dir": False}

    def is_student(p):
        return student is None or _same(p["user"], student)

    bio = _latest(pulls, lambda p: is_student(p) and p["head_ref"].startswith("bio/"))
    s["bio"] = None
    if bio:
        suffix = branch_suffix(bio["head_ref"], "bio/")
        path = f"bios/{suffix}.md"
        files = repo.pr_files(bio["number"])
        names = [f["filename"] for f in files]
        s["touched_github_dir"] |= any(n.startswith(".github/") for n in names)
        text = repo.file_text(path, bio["head_sha"]) if path in names else None
        if text is None:  # tolerate a file name that doesn't match the branch
            other = [n for n in names if n.startswith("bios/") and n.endswith(".md")
                     and not n.endswith("_TEMPLATE.md")]
            if other:
                path = other[0]
                text = repo.file_text(path, bio["head_sha"])
        commits = repo.pr_commits(bio["number"])
        bot_rev = _bot_reviews(repo, bio["number"])
        s["bio"] = {
            "pr": bio, "path": path, "file_matches_branch": path == f"bios/{suffix}.md",
            "commits": len(commits),
            "problems": bio_problems(text, need_timezone=False),
            "timezone_problem": "timezone" in bio_problems(text, need_timezone=True),
            "bot_requested_change": any(r["state"] == "CHANGES_REQUESTED" for r in bot_rev),
            "bot_approved": any(r["state"] == "APPROVED" for r in bot_rev),
        }

    botpr = _latest(pulls, lambda p: p["head_ref"] == BOT_BRANCH)
    s["botpr"] = None
    if botpr:
        reviews = repo.pr_reviews(botpr["number"])
        comments = repo.pr_review_comments(botpr["number"])
        s["botpr"] = {
            "pr": botpr,
            "student_approved": any(is_student({"user": r["user"]}) and r["user"] != BOT_LOGIN
                                    and r["state"] == "APPROVED" for r in reviews),
            "student_line_comments": sum(1 for c in comments
                                         if c["user"] != BOT_LOGIN and is_student({"user": c["user"]})),
        }

    norms = _latest(pulls, lambda p: is_student(p) and p["head_ref"].startswith("norms/")
                    and p["head_ref"] != BOT_BRANCH)
    s["norms"] = None
    if norms:
        text = repo.file_text(NORMS_FILE, norms["head_sha"])
        files = [f["filename"] for f in repo.pr_files(norms["number"])]
        s["touched_github_dir"] |= any(n.startswith(".github/") for n in files)
        commits = repo.pr_commits(norms["number"])
        bot_merged_first = bool(botpr and botpr["merged"] and
                                (not norms["merged"] or botpr["merged_at"] <= norms["merged_at"]))
        s["norms"] = {
            "pr": norms,
            "problems": norms_problems(text),
            "merge_commit": any(c["parents"] > 1 for c in commits),
            "bot_merged_first": bot_merged_first,
            "bot_approved": any(r["state"] == "APPROVED" for r in _bot_reviews(repo, norms["number"])),
        }
    main_text = repo.file_text(NORMS_FILE, repo.default_branch())
    s["main_norms_problems"] = norms_problems(main_text)
    return s


def score(s):
    """Completion points out of 5, plus a short note for the gradebook."""
    notes, pts = [], 0
    b = s["bio"]
    if b and b["pr"]["merged"]:
        full = not b["problems"] and b["commits"] >= 2 and not b["timezone_problem"]
        pts += 2 if full else 1
        if not full:
            notes.append("bio merged but incomplete")
    else:
        notes.append("bio not merged")
    bp = s["botpr"]
    if bp and bp["student_approved"] and bp["student_line_comments"] >= 1:
        pts += 1
    else:
        notes.append("teammate PR not reviewed with a line comment + approval")
    n = s["norms"]
    if n and n["pr"]["merged"]:
        full = n["bot_merged_first"] and not s["main_norms_problems"] and n["merge_commit"]
        pts += 2 if full else 1
        if not full:
            why = "no merge commit (rebased, or never conflicted?)" if not n["merge_commit"] \
                else "conflict not resolved as asked"
            notes.append(f"norms merged but {why}")
    else:
        notes.append("norms PR not merged")
    if s["touched_github_dir"]:
        notes.append("FLAG: PR changed .github/ (check manually)")
    return pts, "; ".join(notes) or "complete"


# ---------------------------------------------------------------- actions
def _ensure_review(repo, pr, problems, tag, intro_ok, intro_fix):
    """Post a review only when the bot's opinion changes, so pushes don't spam."""
    sig = f"{SIG}{tag}:{','.join(sorted(problems)) or 'ok'} -->"
    mine = _bot_reviews(repo, pr["number"])
    last = mine[-1] if mine else None
    if problems:
        if last and last["state"] == "CHANGES_REQUESTED" and sig in last["body"]:
            return
        repo.create_review(pr["number"], "REQUEST_CHANGES", f"{intro_fix}\n\n{explain(problems)}\n\n{sig}")
    else:
        if last and last["state"] == "APPROVED":
            return
        repo.create_review(pr["number"], "APPROVE", f"{intro_ok}\n\n{sig}")


def _open_bot_pr(repo, student):
    base = repo.default_branch()
    if not repo.branch_sha(BOT_BRANCH):
        repo.create_branch(BOT_BRANCH, repo.branch_sha(base))
    text = repo.file_text(NORMS_FILE, BOT_BRANCH) or "# Team Norms\n\nOur #1 rule for working asynchronously: TBD\n"
    if BOT_RULE_KEY not in text.lower():
        text = text.replace("TBD", BOT_RULE, 1)
        repo.put_file(NORMS_FILE, BOT_BRANCH, text, "Propose Teammate Bot's #1 async rule")
    number = repo.create_pull(
        BOT_BRANCH, base, "Add Teammate Bot's #1 async rule",
        "Hi! I'm your teammate for OE 1. This PR replaces `TBD` in `TEAM_NORMS.md` with my "
        "proposed rule.\n\n**Please review it:** open **Files changed**, leave at least one comment "
        "on a specific line (hover over the line and click the blue **+**), then "
        "**Review changes → Approve**, and merge it.")
    if student:
        try:
            repo.request_reviewers(number, [student])
        except Exception as exc:  # reviewer request is a nicety; never fail the run on it
            print(f"could not request reviewer: {exc}")
    return number


def act(repo, student=None):
    student = student_of(repo, student)
    s = evaluate(repo, student)
    b = s["bio"]
    if b and b["pr"]["state"] == "open":
        problems = list(b["problems"])
        if b["timezone_problem"]:
            problems.append("timezone")
        if not b["file_matches_branch"]:
            print("bio file name does not match branch; accepted anyway")
        _ensure_review(repo, b["pr"], problems, "bio",
                       "Thanks for the update. This looks great, approved! 🎉 You can merge it now.",
                       "Thanks for the PR! Before I approve, could you make these changes? "
                       "Commit them to the same branch and push; this PR updates automatically.")

    bio_done = b and b["pr"]["merged"]
    if s["botpr"] is None and (bio_done or s["norms"]):
        _open_bot_pr(repo, student)
        s = evaluate(repo, student)

    bp = s["botpr"]
    if bp and (bp["student_approved"] or bp["student_line_comments"]):
        comment_once(repo, bp["pr"]["number"], "<!-- oe1_thanks -->",
                     "Thanks for the review! 🙌 Once you've approved it, go ahead and merge it.")

    n = s["norms"]
    if n and n["pr"]["state"] == "open":
        num = n["pr"]["number"]
        if not (bp and bp["pr"]["merged"]):
            comment_once(repo, num, "<!-- oe1_wait -->",
                         "Heads up: my open pull request edits the same line of `TEAM_NORMS.md`. "
                         "Please review and merge my PR first, then come back here.")
        else:
            mergeable = repo.get_mergeable(num)
            if mergeable is None:
                # GitHub hasn't finished computing; the next push or review will retry.
                update_progress_issue(repo, evaluate(repo, student))
                return
            problems = list(n["problems"])
            if mergeable is False:
                problems = ["conflict"] + [p for p in problems if p != "missing_bot_rule"]
                comment_once(repo, num, "<!-- oe1_conflict -->",
                             "I merged my rule into `main` first, so this PR now has a **merge conflict**: "
                             "we both changed the same line. That's normal on a team! Click "
                             "**Resolve conflicts**, keep **both** rules as a bulleted list, delete the "
                             "conflict markers, then **Mark as resolved → Commit merge**.")
            _ensure_review(repo, n["pr"], problems, "norms",
                           "Conflict resolved and both rules kept. Approved! Merge when ready.",
                           "Almost there:")
    update_progress_issue(repo, evaluate(repo, student))


def checklist(s):
    b, bp, n = s["bio"], s["botpr"], s["norms"]
    pts, note = score(s)
    lines = [
        "This checklist updates itself each time you push, review, or merge. "
        "(It shows completion only; your instructor confirms the final grade.)", "",
        "**Part 1 – Your bio (2 pts)**",
        f"- {box(b)} Opened a pull request from a `bio/<your-username>` branch",
        f"- {box(b and not b['problems'])} Bio file has a name and every field filled in",
        f"- {box(b and b['commits'] >= 2)} At least two commits",
        f"- {box(b and not b['timezone_problem'])} Made the change Teammate Bot asked for",
        f"- {box(b and b['pr']['merged'])} Approved by Teammate Bot and merged",
        "", "**Part 2 – Review a teammate's PR (1 pt)**",
        f"- {box(bp and bp['student_line_comments'] >= 1)} Left a comment on a specific line of Teammate Bot's PR",
        f"- {box(bp and bp['student_approved'])} Approved Teammate Bot's PR",
        f"- {box(bp and bp['pr']['merged'])} Merged Teammate Bot's PR",
        "", "**Part 3 – Resolve a merge conflict (2 pts)**",
        f"- {box(n)} Opened a pull request from a `norms/<your-username>` branch",
        f"- {box(n and n['merge_commit'])} The conflict resolution shows in the history (a merge commit)",
        f"- {box(n and n['bot_merged_first'] and not n['problems'])} Both rules kept as a bulleted list, no markers",
        f"- {box(n and n['pr']['merged'])} Approved by Teammate Bot and merged",
        "", f"**Completion so far: {pts} / 5**" + ("" if pts == 5 else f" ({note})"),
    ]
    return "\n".join(lines)


def _progress_matches(repo):
    """{number: issue} for every issue with the progress title (title only; the author doesn't matter)."""
    return {i["number"]: i for i in repo.list_issues() if i["title"] == ISSUE_TITLE}


def update_progress_issue(repo, s):
    """Create or update THE progress issue. The issue list can briefly come back empty or short,
    so look at #1 directly (setup dispatches the bot before any PR exists, so it is normally #1),
    and when several match keep the lowest-numbered one and close the rest."""
    pts, note = score(s)
    status = json.dumps({"points": pts, "note": note})
    body = f"{checklist(s)}\n\n{STATUS_MARK} {status} -->"
    matches = _progress_matches(repo)
    first = repo.get_issue(1)
    if first and not first["is_pull"] and first["title"] == ISSUE_TITLE:
        matches[1] = first
    if not matches:
        time.sleep(5)                                  # the list can lag; look once more before creating
        matches = _progress_matches(repo)
    if not matches:
        number = repo.create_issue(ISSUE_TITLE, body, [s["student"]] if s["student"] else ())
        print(f"progress issue: created #{number}")
        return
    keep = min(matches)
    print(f"progress issue: using #{keep}")
    if matches[keep]["body"].strip() != body.strip():
        repo.update_issue(keep, body)
    for number in sorted(matches):
        if number != keep and matches[number].get("state", "open") == "open":
            repo.create_comment(number, f"Duplicate of #{keep}. Teammate Bot keeps the OE 1 checklist there.")
            repo.close_issue(number)
            print(f"progress issue: closed duplicate #{number}")


def main():
    from ghclient import Repo
    act(Repo(os.environ["GITHUB_REPOSITORY"]))


if __name__ == "__main__":
    main()
