"""Continuous delivery polling mixin for pipedog poller."""

from __future__ import annotations

import fnmatch
from typing import Any

import cd_exec
import cdflow
import deploymodel
import deploystore
import flow
import flowmodel
import gate
import model
import store
import verify


class CdPollerMixin:
    def _tracked_refs_for_project(self, project: dict) -> list[flowmodel.TrackedRef]:
        flow_cfg = project.get("flow") or {}
        chain: list[str] = list(flow_cfg.get("chain") or [])
        proj_name = str(project.get("name") or "")

        try:
            checkout = (
                str(project["mock_branch"])
                if self.mock and project.get("mock_branch")
                else self.resolve_branch(project)
            )
        except Exception:  # noqa: BLE001
            checkout = None

        owned_all = store.load_owned(root=self.state_root)
        owned_proj = [r for r in owned_all if r.project == proj_name]
        owned_by_branch = {r.branch: r for r in owned_proj}

        refs: list[flowmodel.TrackedRef] = []
        if checkout and flow_cfg and flow.is_entry_branch(flow_cfg, checkout):
            if checkout in owned_by_branch:
                refs.append(owned_by_branch[checkout])
            else:
                mem = self._ref_state.get((proj_name, checkout))
                refs.append(
                    mem
                    or flowmodel.TrackedRef(
                        project=proj_name,
                        branch=checkout,
                        target=flow.target_for(flow_cfg, checkout),
                        pr=None,
                        owned=False,
                    )
                )

        for r in owned_proj:
            if not any(existing.branch == r.branch for existing in refs):
                mem = self._ref_state.get((proj_name, r.branch))
                if mem is not None:
                    r.ci_fix_rounds = mem.ci_fix_rounds
                    r.review_fix_rounds = mem.review_fix_rounds
                    r.notified = list(mem.notified)
                    if r.head_sha is None:
                        r.head_sha = mem.head_sha
                refs.append(r)

        return refs

    def _resolve_deploy_adapter(
        self, project: dict[str, Any], env_cfg: dict[str, Any]
    ) -> Any:
        adapter_name = str(env_cfg.get("adapter") or "github")
        if adapter_name == "github":
            base_url = self.github_api_url
            token = self.tokens.get("github")
        elif adapter_name == "gitlab":
            base_url = self.gitlab_url
            token = self.tokens.get("gitlab")
        else:
            base_url = ""
            token = None
        return self.adapter_factory(adapter_name, base_url=base_url, token=token)

    def _poll_cd_for_project(
        self,
        project: dict[str, Any],
        client: Any,
        pipelines: list[model.Pipeline],
        now: float,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        deploy_cfg = project.get("deploy")
        if not isinstance(deploy_cfg, dict):
            return [], []
        envs_cfg = deploy_cfg.get("environments")
        if not isinstance(envs_cfg, list) or not envs_cfg:
            return [], []

        proj_name = str(project.get("name") or "")
        flow_cfg = project.get("flow") or {}
        chain: list[str] = list(flow_cfg.get("chain") or [])
        after_br = str(
            deploy_cfg.get("after_branch")
            or (chain[-1] if chain else project.get("branch") or "main")
        )

        p_after = next((p for p in pipelines if p.branch == after_br), None)
        if p_after is None:
            p_after = self._fetch_branch(project, client, after_br)

        branch_green_sha: str | None = None
        if (
            p_after is not None
            and not p_after.fetch_error
            and gate.classify_ci(p_after).color == "green"
            and p_after.head_sha
        ):
            branch_green_sha = p_after.head_sha

        states_map = deploystore.load_states(root=self.state_root)
        prev_state: deploymodel.DeployState | None = None
        env_entries: list[dict[str, Any]] = []
        cd_warnings: list[str] = []

        for env_idx, raw_env_cfg in enumerate(envs_cfg):
            if not isinstance(raw_env_cfg, dict):
                continue
            env_name = str(raw_env_cfg.get("name") or "")
            is_prod = bool(
                raw_env_cfg.get("production")
                if "production" in raw_env_cfg
                else env_name.lower() in ("prod", "production")
            )
            st_key = deploymodel.state_key(proj_name, env_name)
            state = states_map.get(st_key) or deploymodel.DeployState(
                project=proj_name, env=env_name
            )
            cand = cdflow.candidate_sha(
                env_idx, branch_green_sha=branch_green_sha, prev_env=prev_state
            )
            env_state_obs: deploymodel.EnvState | None = None

            try:
                adapter = self._resolve_deploy_adapter(project, raw_env_cfg)
                env_state_obs = adapter.status(project, raw_env_cfg)
                gate_obs = adapter.pending_approval(
                    project, raw_env_cfg, state.sha or cand
                )
                # resolve_existing handles workflow_dispatch placeholders by time:
                # GitHub records the deployment on the ref HEAD, which differs from
                # the input sha once the branch has moved on.
                existing_obs = cd_exec.resolve_existing(
                    adapter, project, raw_env_cfg, state, candidate=cand, now=now
                )
                known_good_obs = deploystore.known_good(
                    proj_name, env_name, root=self.state_root
                )
                if known_good_obs is None:
                    for h_rec in adapter.history(project, raw_env_cfg, limit=10):
                        if h_rec.status == "success":
                            deploystore.record_good(
                                proj_name, env_name, h_rec, root=self.state_root
                            )
                            known_good_obs = h_rec
                            break

                check_obs: deploymodel.CheckResult | None = None
                verify_cfg = raw_env_cfg.get("verify")
                if (state.phase == "verifying" or state.verify is not None) and verify_cfg:
                    prog = state.verify or deploymodel.VerifyProgress(
                        sha=state.sha or cand or "", started_at=now
                    )
                    if verify.due(prog, now=now, cfg=verify_cfg):
                        check_obs = verify.check_once(
                            verify_cfg,
                            project=project,
                            env=raw_env_cfg,
                            sha=prog.sha or state.sha or "",
                        )

                env_decisions = [
                    d
                    for d in store.list_decisions(root=self.state_root)
                    if d.project == proj_name and d.target == env_name
                ]
                obs = cdflow.EnvObservation(
                    env_state=env_state_obs,
                    gate=gate_obs,
                    existing=existing_obs,
                    check=check_obs,
                    known_good=known_good_obs,
                    decisions=env_decisions,
                    now=now,
                )
                new_state, cd_actions = cdflow.step_env(
                    state, raw_env_cfg, obs, candidate=cand, project=project
                )
                deploystore.save_state(new_state, root=self.state_root)
                exec_warns: list[str] = []
                cd_exec.apply(
                    cd_actions,
                    adapter=adapter,
                    project=project,
                    env_cfg=raw_env_cfg,
                    state=new_state,
                    notify=self._deliver_notification,
                    root=self.state_root,
                    secrets=self.secrets,
                    now=now,
                    warnings=exec_warns,
                )
                cd_warnings.extend(exec_warns)
                state = (
                    deploystore.get_state(proj_name, env_name, root=self.state_root)
                    or new_state
                )
            except Exception as exc:  # noqa: BLE001 - isolate CD errors to this environment
                msg = (
                    self._redact(f"{proj_name}/{env_name}: {exc}")
                    or f"{proj_name}/{env_name}: deploy error"
                )
                cd_warnings.append(msg)
                if env_state_obs is None:
                    env_state_obs = deploymodel.EnvState(
                        env=env_name, sha=state.sha, status="unknown"
                    )

            prev_state = state

            disp_sha = (
                state.sha
                or (env_state_obs.sha if env_state_obs else None)
                or state.verified_sha
            )
            if state.phase == "stuck":
                disp_status = "stuck"
            elif env_state_obs is not None and env_state_obs.status == "waiting_approval":
                disp_status = "waiting_approval"
            elif state.phase in ("deploying", "rolling_back"):
                disp_status = "running"
            elif state.phase == "verifying":
                disp_status = "running"
            elif state.phase == "failed":
                disp_status = "failed"
            elif env_state_obs is not None:
                disp_status = env_state_obs.status
            else:
                disp_status = "none"

            is_verified = bool(
                state.verified_sha
                and disp_sha == state.verified_sha
                and state.phase
                not in ("deploying", "verifying", "rolling_back", "failed", "stuck")
            )
            env_entries.append(
                {
                    "project": proj_name,
                    "env": env_name,
                    "sha": disp_sha,
                    "status": disp_status,
                    "phase": state.phase,
                    "verified": is_verified,
                    "verified_sha": state.verified_sha,
                    "deployed_at": (
                        env_state_obs.deployed_at if env_state_obs else None
                    ),
                    "web_url": env_state_obs.web_url if env_state_obs else None,
                    "production": is_prod,
                }
            )

        return env_entries, cd_warnings
