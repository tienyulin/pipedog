"""GitHub Forge implementation using REST and GraphQL APIs."""

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


class GitHubForge(Forge):
    def __init__(self, base_url: str, token: str | None) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token or None
        self._headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            self._headers["Authorization"] = f"Bearer {self._token}"
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
    def _repo(project: dict) -> str:
        repo = str(project.get("repo") or project.get("project_ref") or "")
        if repo.count("/") != 1:
            raise ForgeError(f"invalid repo: {repo}")
        return repo

    def me(self) -> str:
        if self._me is not None:
            return self._me
        with self._wrap_errors():
            data = http.get_json(f"{self.base_url}/user", self._headers)
            self._me = str(data.get("login", "")) if isinstance(data, dict) else ""
        return self._me

    def find_open_pr(self, project: dict, source: str, target: str) -> flowmodel.PrState | None:
        repo = self._repo(project)
        owner = repo.split("/")[0]
        head_param = f"{owner}:{source}" if ":" not in source else source
        query = f"?state=open&head={quote(head_param, safe='')}&base={quote(target, safe='')}"
        with self._wrap_errors():
            data = http.get_json(f"{self.base_url}/repos/{repo}/pulls{query}", self._headers)
        if not isinstance(data, list) or not data:
            fallback_query = f"?state=open&head={quote(source, safe='')}&base={quote(target, safe='')}"
            if fallback_query != query:
                with self._wrap_errors():
                    data = http.get_json(f"{self.base_url}/repos/{repo}/pulls{fallback_query}", self._headers)
        if not isinstance(data, list) or not data:
            return None
        pr_num = data[0].get("number")
        if pr_num is None:
            return None
        return self.get_pr(project, pr_num)

    def get_pr(self, project: dict, number: int) -> flowmodel.PrState:
        repo = self._repo(project)
        owner, name = repo.split("/")
        with self._wrap_errors():
            rest_pr = http.get_json(f"{self.base_url}/repos/{repo}/pulls/{number}", self._headers)
            if not isinstance(rest_pr, dict):
                raise ForgeError("unexpected response from pull request API")

            query = """
            query($owner: String!, $name: String!, $number: Int!) {
              repository(owner: $owner, name: $name) {
                pullRequest(number: $number) {
                  reviewDecision
                  headRefOid
                  state
                  reviewThreads(first: 100) {
                    nodes {
                      isResolved
                    }
                  }
                }
              }
            }
            """
            gql = http.post_graphql(
                f"{self.base_url}/graphql",
                self._headers,
                query,
                {"owner": owner, "name": name, "number": number},
            )

        pr_node = gql.get("repository", {}).get("pullRequest") if isinstance(gql, dict) else {}
        if not isinstance(pr_node, dict):
            pr_node = {}

        raw_decision = pr_node.get("reviewDecision")
        review_map = {
            "APPROVED": "approved",
            "CHANGES_REQUESTED": "changes_requested",
            "REVIEW_REQUIRED": "review_required",
        }
        review: Literal["approved", "changes_requested", "review_required", "none"] = (
            review_map.get(raw_decision, "none") if raw_decision else "none"
        )

        threads = pr_node.get("reviewThreads", {}).get("nodes") or []
        unresolved_threads = sum(1 for t in threads if isinstance(t, dict) and not t.get("isResolved"))

        comments = self.list_comments(project, number)
        comment_ids = [c["id"] for c in comments]

        state_str = rest_pr.get("state")
        if rest_pr.get("merged"):
            state = "merged"
        elif state_str == "closed":
            state = "closed"
        else:
            state = "open"

        author = rest_pr.get("user", {}).get("login", "")
        author_is_me = (author == self.me()) if self._token else False

        head_sha = pr_node.get("headRefOid") or rest_pr.get("head", {}).get("sha", "")

        return flowmodel.PrState(
            number=number,
            url=rest_pr.get("html_url", ""),
            source=rest_pr.get("head", {}).get("ref", ""),
            target=rest_pr.get("base", {}).get("ref", ""),
            head_sha=head_sha,
            state=state,
            review=review,
            unresolved_threads=unresolved_threads,
            comment_ids=comment_ids,
            author_is_me=author_is_me,
        )

    def get_commit_subject(self, project: dict, sha: str) -> str | None:
        repo = self._repo(project)
        try:
            with self._wrap_errors():
                res = http.get_json(f"{self.base_url}/repos/{repo}/commits/{sha}", self._headers)
                if isinstance(res, dict):
                    commit = res.get("commit")
                    if isinstance(commit, dict):
                        msg = commit.get("message", "")
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
        repo = self._repo(project)
        payload: dict[str, Any] = {"title": title, "head": source, "base": target}
        if body is not None:
            payload["body"] = body
        elif "body" in kwargs and kwargs["body"] is not None:
            payload["body"] = kwargs["body"]
        with self._wrap_errors(is_write=True):
            res = http.post_json(f"{self.base_url}/repos/{repo}/pulls", self._headers, payload)
            if not isinstance(res, dict) or "number" not in res:
                raise ForgeError("failed to create pull request")
            number = res["number"]
            if reviewers:
                http.post_json(
                    f"{self.base_url}/repos/{repo}/pulls/{number}/requested_reviewers",
                    self._headers,
                    {"reviewers": list(reviewers)},
                )
        return self.get_pr(project, number)

    def list_comments(self, project: dict, number: int) -> list[dict[str, Any]]:
        repo = self._repo(project)
        comments: list[dict[str, Any]] = []
        with self._wrap_errors():
            rcs = http.get_json(f"{self.base_url}/repos/{repo}/pulls/{number}/comments", self._headers)
            if isinstance(rcs, list):
                for rc in rcs:
                    if isinstance(rc, dict) and not _is_ignored_comment(rc.get("body")):
                        comments.append({
                            "id": f"rc-{rc.get('id')}",
                            "kind": "review",
                            "author": rc.get("user", {}).get("login", ""),
                            "body": rc.get("body", ""),
                            "path": rc.get("path"),
                            "line": rc.get("line") or rc.get("original_line"),
                            "resolved": None,
                            "url": rc.get("html_url", ""),
                        })

            ics = http.get_json(f"{self.base_url}/repos/{repo}/issues/{number}/comments", self._headers)
            if isinstance(ics, list):
                for ic in ics:
                    if isinstance(ic, dict) and not _is_ignored_comment(ic.get("body")):
                        comments.append({
                            "id": f"ic-{ic.get('id')}",
                            "kind": "issue",
                            "author": ic.get("user", {}).get("login", ""),
                            "body": ic.get("body", ""),
                            "path": None,
                            "line": None,
                            "resolved": None,
                            "url": ic.get("html_url", ""),
                        })

            rvs = http.get_json(f"{self.base_url}/repos/{repo}/pulls/{number}/reviews", self._headers)
            if isinstance(rvs, list):
                for rv in rvs:
                    if isinstance(rv, dict) and rv.get("body") and not _is_ignored_comment(rv.get("body")):
                        comments.append({
                            "id": f"rv-{rv.get('id')}",
                            "kind": "review",
                            "author": rv.get("user", {}).get("login", ""),
                            "body": rv.get("body", ""),
                            "path": None,
                            "line": None,
                            "resolved": None,
                            "url": rv.get("html_url", ""),
                        })

        return comments

    def reply(self, project: dict, number: int, comment_id: str, body: str) -> str:
        repo = self._repo(project)
        with self._wrap_errors(is_write=True):
            if comment_id.startswith("rc-"):
                raw_id = comment_id[3:] if comment_id.startswith("rc-") else comment_id
                res = http.post_json(
                    f"{self.base_url}/repos/{repo}/pulls/{number}/comments/{raw_id}/replies",
                    self._headers,
                    {"body": body},
                )
            elif comment_id.startswith("ic-") or comment_id.startswith("rv-"):
                res = http.post_json(
                    f"{self.base_url}/repos/{repo}/issues/{number}/comments",
                    self._headers,
                    {"body": body},
                )
            else:
                raise ForgeError(f"unknown comment id format: {comment_id}")

            if isinstance(res, dict) and res.get("html_url"):
                return str(res["html_url"])
            return ""

    def merge(
        self,
        project: dict,
        number: int,
        *,
        method: Literal["squash", "merge"],
        delete_branch: bool,
        expected_sha: str,
    ) -> None:
        repo = self._repo(project)
        source_branch: str | None = None
        if delete_branch:
            try:
                pr_info = http.get_json(f"{self.base_url}/repos/{repo}/pulls/{number}", self._headers)
                if isinstance(pr_info, dict):
                    source_branch = pr_info.get("head", {}).get("ref")
            except Exception:
                try:
                    pr = self.get_pr(project, number)
                    source_branch = pr.source
                except Exception:
                    pass

        payload = {"sha": expected_sha, "merge_method": method}
        with self._wrap_errors(is_write=True):
            http.put_json(
                f"{self.base_url}/repos/{repo}/pulls/{number}/merge",
                self._headers,
                payload,
            )

        if delete_branch and source_branch:
            try:
                http.delete(
                    f"{self.base_url}/repos/{repo}/git/refs/heads/{quote(source_branch, safe='/')}",
                    self._headers,
                )
            except Exception:
                pass
