"""GitLab Forge implementation using REST API v4."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Literal
from urllib.parse import quote

import flowmodel
from forge import Forge, ForgeError
from providers import http

PIPEDOG_REPLY_MARKER = "<!-- pipedog-reply -->"


def _is_ignored_comment(body: str | None) -> bool:
    if not body:
        return False
    text = str(body)
    return PIPEDOG_REPLY_MARKER in text or "Opened by pipedog" in text


class GitLabForge(Forge):
    def __init__(self, base_url: str, token: str | None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api = f"{self.base_url}/api/v4"
        self._token = token or None
        self._headers: dict[str, str] = {"PRIVATE-TOKEN": self._token} if self._token else {}
        self._me: str | None = None

    @contextmanager
    def _wrap_errors(self, is_write: bool = False) -> Iterator[None]:
        try:
            yield
        except http.HttpError as e:
            if e.status == 409 or (e.message and "409" in e.message):
                raise ForgeError("head moved") from None
            if is_write and e.status == 403:
                raise ForgeError("token lacks write scope (GitLab needs api)") from None
            msg = e.message
            if self._token:
                msg = msg.replace(self._token, "***")
            raise ForgeError(msg) from None
        except Exception as e:
            msg = str(e)
            if self._token:
                msg = msg.replace(self._token, "***")
            raise ForgeError(msg) from None

    @staticmethod
    def _pid(project: dict) -> str:
        ref = str(project.get("id") or project.get("project_ref") or "")
        if not ref:
            raise ForgeError("project has no id")
        return ref if "%" in ref else quote(ref, safe="")

    def me(self) -> str:
        if self._me is not None:
            return self._me
        with self._wrap_errors():
            data = http.get_json(f"{self.api}/user", self._headers)
            self._me = str(data.get("username", "")) if isinstance(data, dict) else ""
        return self._me

    def find_open_pr(self, project: dict, source: str, target: str) -> flowmodel.PrState | None:
        pid = self._pid(project)
        query = f"?state=opened&source_branch={quote(source, safe='')}&target_branch={quote(target, safe='')}"
        with self._wrap_errors():
            data = http.get_json(f"{self.api}/projects/{pid}/merge_requests{query}", self._headers)
        if not isinstance(data, list) or not data:
            return None
        iid = data[0].get("iid")
        if iid is None:
            return None
        return self.get_pr(project, iid)

    def get_pr(self, project: dict, number: int) -> flowmodel.PrState:
        pid = self._pid(project)
        with self._wrap_errors():
            mr = http.get_json(f"{self.api}/projects/{pid}/merge_requests/{number}", self._headers)
            if not isinstance(mr, dict):
                raise ForgeError("unexpected response from merge request API")

            approvals = http.get_json(f"{self.api}/projects/{pid}/merge_requests/{number}/approvals", self._headers)
            if not isinstance(approvals, dict):
                approvals = {}

            discussions = http.get_json(f"{self.api}/projects/{pid}/merge_requests/{number}/discussions", self._headers)
            if not isinstance(discussions, list):
                discussions = []

        approvals_req = approvals.get("approvals_required", 0)
        if approvals_req == 0:
            review: Literal["approved", "changes_requested", "review_required", "none"] = "none"
        elif approvals.get("approved"):
            review = "approved"
        else:
            review = "review_required"

        unresolved_threads = 0
        for d in discussions:
            if isinstance(d, dict):
                notes = d.get("notes") or []
                if any(n.get("resolvable") and not n.get("resolved") for n in notes if isinstance(n, dict)):
                    unresolved_threads += 1

        comments = self._discussions_to_comments(discussions)
        comment_ids = [c["id"] for c in comments]

        state_str = mr.get("state")
        if state_str == "merged":
            state = "merged"
        elif state_str in ("closed", "locked"):
            state = "closed"
        else:
            state = "open"

        author = mr.get("author", {}).get("username", "")
        author_is_me = (author == self.me()) if self._token else False

        head_sha = mr.get("sha") or mr.get("diff_head_sha", "")

        return flowmodel.PrState(
            number=number,
            url=mr.get("web_url", ""),
            source=mr.get("source_branch", ""),
            target=mr.get("target_branch", ""),
            head_sha=head_sha,
            state=state,
            review=review,
            unresolved_threads=unresolved_threads,
            comment_ids=comment_ids,
            author_is_me=author_is_me,
        )

    @staticmethod
    def _discussions_to_comments(discussions: list[dict]) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        for d in discussions:
            if not isinstance(d, dict):
                continue
            notes = d.get("notes") or []
            valid_notes = [
                n
                for n in notes
                if isinstance(n, dict)
                and not n.get("system")
                and not _is_ignored_comment(n.get("body"))
            ]
            if not valid_notes:
                continue
            first = valid_notes[0]
            pos = first.get("position") or {}
            path = pos.get("new_path") or pos.get("old_path")
            line = pos.get("new_line") or pos.get("old_line")
            is_resolvable = any(n.get("resolvable") for n in valid_notes)
            is_resolved = (
                all(n.get("resolved") for n in valid_notes if n.get("resolvable"))
                if is_resolvable
                else None
            )

            comments.append({
                "id": f"d-{d.get('id')}",
                "kind": "thread" if is_resolvable else "issue",
                "author": first.get("author", {}).get("username", ""),
                "body": first.get("body", ""),
                "path": path,
                "line": line,
                "resolved": is_resolved,
                "url": first.get("web_url", ""),
            })
        return comments

    def list_comments(self, project: dict, number: int) -> list[dict[str, Any]]:
        pid = self._pid(project)
        with self._wrap_errors():
            discussions = http.get_json(f"{self.api}/projects/{pid}/merge_requests/{number}/discussions", self._headers)
        if not isinstance(discussions, list):
            return []
        return self._discussions_to_comments(discussions)

    def reply(self, project: dict, number: int, comment_id: str, body: str) -> str:
        pid = self._pid(project)
        disc_id = comment_id[2:] if comment_id.startswith("d-") else comment_id
        with self._wrap_errors(is_write=True):
            res = http.post_json(
                f"{self.api}/projects/{pid}/merge_requests/{number}/discussions/{disc_id}/notes",
                self._headers,
                {"body": body},
            )
        if isinstance(res, dict) and res.get("web_url"):
            return str(res["web_url"])
        return ""

    def get_commit_subject(self, project: dict, sha: str) -> str | None:
        pid = self._pid(project)
        try:
            with self._wrap_errors():
                res = http.get_json(
                    f"{self.api}/projects/{pid}/repository/commits/{sha}",
                    self._headers,
                )
                if isinstance(res, dict):
                    title = res.get("title")
                    if title:
                        return str(title).strip()
                    msg = res.get("message", "")
                    if msg:
                        first_line = msg.split("\n", 1)[0].strip()
                        if first_line:
                            return first_line
        except Exception:
            return None
        return None

    def create_pr(
        self,
        project: dict,
        source: str,
        target: str,
        title: str,
        reviewers: list[str] = (),
        *,
        body: str | None = None,
        **kwargs: Any,
    ) -> flowmodel.PrState:
        pid = self._pid(project)
        payload = {
            "title": title,
            "source_branch": source,
            "target_branch": target,
        }
        if body is not None:
            payload["description"] = body
        elif "body" in kwargs and kwargs["body"] is not None:
            payload["description"] = kwargs["body"]
        elif "description" in kwargs and kwargs["description"] is not None:
            payload["description"] = kwargs["description"]
        if "remove_source_branch" in kwargs:
            payload["remove_source_branch"] = kwargs["remove_source_branch"]
        with self._wrap_errors(is_write=True):
            res = http.post_json(
                f"{self.api}/projects/{pid}/merge_requests",
                self._headers,
                payload,
            )
            if not isinstance(res, dict) or "iid" not in res:
                raise ForgeError("failed to create merge request")
            iid = res["iid"]
        return self.get_pr(project, iid)

    def merge(
        self,
        project: dict,
        number: int,
        *,
        method: Literal["squash", "merge"],
        delete_branch: bool,
        expected_sha: str,
    ) -> None:
        pid = self._pid(project)
        payload = {
            "sha": expected_sha,
            "squash": (method == "squash"),
            "should_remove_source_branch": delete_branch,
            "remove_source_branch": delete_branch,
        }
        with self._wrap_errors(is_write=True):
            http.put_json(
                f"{self.api}/projects/{pid}/merge_requests/{number}/merge",
                self._headers,
                payload,
            )
