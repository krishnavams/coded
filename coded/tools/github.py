"""GitHub tool: list/get/create pull requests and add comments via the REST API.

Requires a ``GITHUB_TOKEN`` (or ``GH_TOKEN``) environment variable. The target
repository is auto-detected from the ``origin`` remote unless ``repo`` (as
"owner/name") is given. The API base is ``$GITHUB_API_URL`` or api.github.com,
so tests can point it at a fake server.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Dict, Optional

import httpx

from coded.permissions import Decision
from coded.tools.base import Tool, ToolContext, ToolResult

_READ_ACTIONS = {"list_prs", "get_pr"}


def _detect_repo(cwd: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=cwd,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"github\.com[:/]+([^/]+)/(.+?)(?:\.git)?$", out)
    return f"{m.group(1)}/{m.group(2)}" if m else None


class GitHubTool(Tool):
    name = "github"
    description = """Interact with GitHub pull requests. Actions: list_prs, get_pr, \
create_pr (title, head, base, body), comment_pr (number, body). Needs GITHUB_TOKEN. \
Repo is auto-detected from the git remote unless you pass repo='owner/name'."""
    requires_permission = False  # writes are gated per-action inside run()
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list_prs", "get_pr", "create_pr", "comment_pr"]},
            "repo": {"type": "string", "description": "owner/name (defaults to origin remote)."},
            "number": {"type": "integer", "description": "PR number (get_pr, comment_pr)."},
            "title": {"type": "string", "description": "PR title (create_pr)."},
            "head": {"type": "string", "description": "Source branch (create_pr)."},
            "base": {"type": "string", "description": "Target branch (create_pr), default main."},
            "body": {"type": "string", "description": "PR/comment body."},
        },
        "required": ["action"],
    }

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            return ToolResult.error("No GITHUB_TOKEN (or GH_TOKEN) set; cannot call GitHub.")
        action = args.get("action")
        repo = args.get("repo") or _detect_repo(ctx.cwd)
        if not repo:
            return ToolResult.error("Could not determine repo; pass repo='owner/name'.")

        if action not in _READ_ACTIONS:
            decision = ctx.permissions.request(
                key="github", title="GitHub write",
                detail=f"{action} on {repo}", target=action,
            )
            if decision is Decision.DENY:
                return ToolResult.error("The user denied this GitHub action. Do not retry it.")

        base_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            return self._dispatch(action, repo, args, base_url, headers)
        except httpx.HTTPError as exc:
            return ToolResult.error(f"GitHub request failed: {exc}")

    def _dispatch(self, action, repo, args, base_url, headers) -> ToolResult:
        with httpx.Client(timeout=30, headers=headers) as client:
            if action == "list_prs":
                r = client.get(f"{base_url}/repos/{repo}/pulls", params={"state": "open", "per_page": 20})
                r.raise_for_status()
                prs = r.json()
                if not prs:
                    return ToolResult.ok("No open pull requests.")
                lines = [f"#{p['number']} {p['title']} ({p['head']['ref']} → {p['base']['ref']})" for p in prs]
                return ToolResult.ok("\n".join(lines))

            if action == "get_pr":
                num = args.get("number")
                if not num:
                    return ToolResult.error("get_pr requires 'number'.")
                r = client.get(f"{base_url}/repos/{repo}/pulls/{num}")
                r.raise_for_status()
                p = r.json()
                return ToolResult.ok(
                    f"#{p['number']} {p['title']}\nstate: {p['state']}  "
                    f"{p['head']['ref']} → {p['base']['ref']}\n{p.get('html_url','')}\n\n{p.get('body') or ''}"
                )

            if action == "create_pr":
                for f in ("title", "head"):
                    if not args.get(f):
                        return ToolResult.error(f"create_pr requires '{f}'.")
                payload = {
                    "title": args["title"], "head": args["head"],
                    "base": args.get("base", "main"), "body": args.get("body", ""),
                }
                r = client.post(f"{base_url}/repos/{repo}/pulls", json=payload)
                if r.status_code >= 400:
                    return ToolResult.error(f"create_pr failed ({r.status_code}): {r.text[:400]}")
                p = r.json()
                return ToolResult.ok(f"Created PR #{p['number']}: {p.get('html_url','')}")

            if action == "comment_pr":
                num = args.get("number")
                if not num or not args.get("body"):
                    return ToolResult.error("comment_pr requires 'number' and 'body'.")
                # PR comments use the issues endpoint.
                r = client.post(f"{base_url}/repos/{repo}/issues/{num}/comments",
                                json={"body": args["body"]})
                if r.status_code >= 400:
                    return ToolResult.error(f"comment_pr failed ({r.status_code}): {r.text[:400]}")
                return ToolResult.ok(f"Commented on #{num}.")

        return ToolResult.error(f"Unknown github action: {action}")
