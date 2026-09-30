"""Keeps the shared class homework list in data/classes.json.

Nobody but the repo owner can edit this repo. Friends send updates as comments on the
issue titled "Updates" (the shared page posts them for them). This script checks each
comment and only applies what the rules allow:
  * only the owner and GitHub usernames listed in friends.json are accepted
  * a friend's button update can change any class except one the owner's automatic check
    keeps up to date (unless that check is over 3 days old). It can't tell which classes a
    friend is really in, so only add friends you trust
  * an update with no items never wipes a class's existing items
  * links and join codes are removed from teacher posts before saving (the repo is public)
  * "auto" updates (the owner's nightly Claude check) are only accepted from the owner
  * "pro" updates (a friend's own nightly Claude check) are accepted from friends. They rank
    between the owner's check and button clicks: owner auto > pro > button
  * nothing in a comment can change code, the page, or friends.json
Comments are deleted after they are read. Runs in GitHub Actions (sync.yml).
"""
import datetime as dt
import json
import os
import re
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
ROOT = Path(__file__).parent
DATA = ROOT / "data" / "classes.json"
FRIENDS = ROOT / "friends.json"
MARK = "<!-- share -->"
ISSUE_TITLE = "Updates"
ID_OK = re.compile(r"^[a-z0-9-]{1,60}$")
AUTO_FRESH_DAYS = 3
PRO_FRESH_DAYS = 2


def now():
    return dt.datetime.now(TZ)


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def gh(method, path, body=None):
    req = urllib.request.Request(
        "https://api.github.com" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                 "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40].strip("-")


def class_key(title, teacher):
    return slug(title) + ("--" + slug(teacher) if teacher else "")


def s(v, n):
    return str(v or "").replace("\r", "")[:n]


def clean_item(i):
    if not isinstance(i, dict):
        return None
    iid = s(i.get("id"), 60).lower()
    if not ID_OK.match(iid):
        return None
    due = s(i.get("due"), 10)
    if due and not re.match(r"^\d{4}-\d{2}-\d{2}$", due):
        due = ""
    url = s(i.get("url"), 300)
    if url and not url.startswith("/"):
        url = ""  # only Schoology paths, never outside links
    return {"id": iid, "kind": "tests" if i.get("kind") == "tests" else "hw", "n": s(i.get("n"), 160),
            "tr": s(i.get("tr"), 120), "detail": s(i.get("detail"), 300), "due": due,
            "dueTime": s(i.get("dueTime"), 8), "dueNote": s(i.get("dueNote"), 40), "url": url,
            "src": s(i.get("src"), 20)}


def clean_items(items, limit=60):
    out, seen = [], set()
    for i in (items or [])[:200]:
        c = clean_item(i)
        if c and c["id"] not in seen:
            seen.add(c["id"])
            out.append(c)
    return out[:limit]


LINK = re.compile(r"(https?://\S+|www\.\S+|\b[\w.-]+\.(?:com|org|net|io|me|ly|gl)/\S*)", re.I)


def scrub(text):
    """Remove links and join codes from teacher posts, since the shared list is public."""
    text = LINK.sub("[link removed]", text or "")
    return re.sub(r"(?i)\b(join|class) code:?\s*\S+", r"\1 code [removed]", text)


def clean_posts(posts):
    out = []
    for p in (posts or [])[:2]:
        if isinstance(p, dict) and p.get("text"):
            out.append({"id": s(p.get("id"), 20), "at": s(p.get("at"), 30), "by": s(p.get("by"), 60),
                        "text": scrub(s(p.get("text"), 1500))})
    return out


def resolve_key(classes, title, teacher):
    """Same class, same key. If the teacher name is missing, use the one existing class with this title."""
    if not teacher:
        same = [k for k, c in classes.items() if c.get("title") == title and c.get("teacher")]
        if len(same) == 1:
            return same[0], classes[same[0]]["teacher"]
    return class_key(title, teacher), teacher


def fresh(cl, via, days):
    """True if this class was last updated by `via` less than `days` days ago."""
    if cl.get("via") != via or not cl.get("updatedAt"):
        return False
    return (now() - dt.datetime.fromisoformat(cl["updatedAt"])).days < days


def not_past(items):
    today = now().date().isoformat()
    return [i for i in items if not i.get("due") or i["due"] >= today]


def apply(data, payload, sender, owner, friends):
    """Apply one update. Returns a short note for the log."""
    classes = data.setdefault("classes", {})
    kind = payload.get("type")
    stamp = now().isoformat(timespec="seconds")
    notes = []
    if kind == "auto":
        if sender.lower() != owner.lower():
            return f"ignored auto update from {sender}, only {owner} can send those"
        for c in (payload.get("courses") or [])[:20]:
            title, teacher = s(c.get("title"), 80), s(c.get("teacher"), 60)
            if not title:
                continue
            k, teacher = resolve_key(classes, title, teacher)
            cl = classes.setdefault(k, {"title": title, "teacher": teacher})
            cl.update({"title": title, "teacher": teacher, "items": not_past(clean_items(c.get("items"))),
                       "posts": clean_posts(c.get("posts")) or cl.get("posts", []),
                       "updatedAt": stamp, "updatedBy": sender, "via": "auto", "aiItems": []})
            cl["sections"] = sorted(set(cl.get("sections", [])) | {s(x, 20) for x in c.get("sections", []) if x})
            notes.append(f"auto {k}")
        for k, items in (payload.get("postItems") or {}).items():
            k = s(k, 90)
            if k in classes and classes[k].get("via") != "auto":
                classes[k]["aiItems"] = not_past(clean_items(items))
                classes[k]["aiAt"] = stamp
                notes.append(f"read posts {k}")
        return ", ".join(notes) or "auto update had nothing to apply"
    if kind == "pro":
        if sender.lower() != owner.lower() and sender.lower() not in friends:
            return f"ignored pro update from {sender}, not in friends.json"
        for c in (payload.get("courses") or [])[:20]:
            title, teacher = s(c.get("title"), 80), s(c.get("teacher"), 60)
            if not title or c.get("error"):
                continue
            k, teacher = resolve_key(classes, title, teacher)
            cl = classes.get(k)
            if cl and fresh(cl, "auto", AUTO_FRESH_DAYS):
                notes.append(f"skipped {k}, the owner's check keeps it up to date")
                continue
            cl = classes.setdefault(k, {"title": title, "teacher": teacher})
            new_items = not_past(clean_items(c.get("items")))
            cl.update({"title": title, "teacher": teacher,
                       "items": new_items or not_past(cl.get("items", [])),
                       "posts": clean_posts(c.get("posts")) or cl.get("posts", []),
                       "updatedAt": stamp, "updatedBy": sender, "via": "pro", "aiItems": []})
            cl["sections"] = sorted(set(cl.get("sections", [])) | {s(c.get("section"), 30)} - {""})
            notes.append(f"pro {k}")
        return ", ".join(notes) or "pro update had nothing to apply"
    if kind == "button":
        if sender.lower() != owner.lower() and sender.lower() not in friends:
            return f"ignored update from {sender}, not in friends.json"
        for c in (payload.get("courses") or [])[:20]:
            title, teacher = s(c.get("title"), 80), s(c.get("teacher"), 60)
            if not title or c.get("error"):
                continue
            k, teacher = resolve_key(classes, title, teacher)
            cl = classes.get(k)
            if cl and (fresh(cl, "auto", AUTO_FRESH_DAYS) or fresh(cl, "pro", PRO_FRESH_DAYS)):
                notes.append(f"skipped {k}, kept up to date automatically")
                continue
            cl = classes.setdefault(k, {"title": title, "teacher": teacher})
            new_items = not_past(clean_items(c.get("items")))
            cl.update({"title": title, "teacher": teacher,
                       "items": new_items or not_past(cl.get("items", [])),
                       "posts": clean_posts(c.get("posts")) or cl.get("posts", []),
                       "updatedAt": stamp, "updatedBy": sender, "via": "button"})
            cl["sections"] = sorted(set(cl.get("sections", [])) | {s(c.get("section"), 30)} - {""})
            cl.setdefault("aiItems", [])
            notes.append(f"button {k}")
        return ", ".join(notes) or "button update had nothing to apply"
    return f"ignored comment with unknown type {kind!r}"


def find_issue(repo):
    for i in gh("GET", f"/repos/{repo}/issues?state=all&per_page=100"):
        if i.get("title") == ISSUE_TITLE and not i.get("pull_request"):
            return i
    return None


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    owner = repo.split("/")[0]
    friends = {f.lower() for f in load(FRIENDS, []) if isinstance(f, str)}
    data = load(DATA, {"classes": {}})
    issue = find_issue(repo)
    if not issue:
        issue = gh("POST", f"/repos/{repo}/issues", {"title": ISSUE_TITLE, "body":
                   "Class updates arrive here as comments from the shared homework page. "
                   "They are read and deleted automatically. Please don't post here by hand."})
        print("created issue", issue["number"])
    if issue.get("state") != "open":
        gh("PATCH", f"/repos/{repo}/issues/{issue['number']}", {"state": "open"})
    comments = [c for c in gh("GET", f"/repos/{repo}/issues/{issue['number']}/comments?per_page=100")
                if (c.get("body") or "").startswith(MARK)]
    comments.sort(key=lambda c: c.get("created_at") or "")
    for c in comments:
        sender = (c.get("user") or {}).get("login", "")
        m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", c["body"], re.S)
        try:
            payload = json.loads(m[1]) if m else {}
            print(sender, "->", apply(data, payload, sender, owner, friends))
        except Exception as e:
            print("bad comment from", sender, e)
        try:
            gh("DELETE", f"/repos/{repo}/issues/comments/{c['id']}")
        except Exception as e:
            print("could not delete comment", e)
    # drop anything whose date has passed
    for cl in data.get("classes", {}).values():
        cl["posts"] = [dict(p, text=scrub(p.get("text"))) for p in cl.get("posts", [])]
        cl["items"] = not_past(cl.get("items", []))
        cl["aiItems"] = not_past(cl.get("aiItems", []))
    data["updated"] = now().isoformat(timespec="seconds")
    data["owner"] = owner
    save(DATA, data)


if __name__ == "__main__":
    main()
