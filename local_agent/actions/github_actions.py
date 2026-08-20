"""Risk-gated, allow-listed GitHub tools for the LLM agent."""

from __future__ import annotations

from typing import Any

from ..core.errors import AssistantError
from ..github.client import compact_json
from .registry import ActionContext, ActionRegistry, Risk

_READ_GROUPS: dict[str, set[str]] = {
    "github.repositories": {
        "repositories",
        "repository",
        "installations",
        "installation_repositories",
    },
    "github.code": {
        "contents",
        "file_text",
        "repository_tree",
        "commits",
        "commit",
        "compare",
        "languages",
        "contributors",
        "branches",
        "tags",
    },
    "github.governance": {
        "branch_protection",
        "branch_rules",
        "rulesets",
        "ruleset",
        "ruleset_history",
        "webhooks",
        "webhook",
        "webhook_deliveries",
    },
    "github.issues": {"issues", "issue", "issue_comments"},
    "github.pulls": {"pulls", "pull", "pull_files", "pull_reviews"},
    "github.discussions": {"discussion_categories", "discussions", "discussion"},
    "github.checks": {
        "check_runs",
        "check_run",
        "check_run_annotations",
        "check_suites",
        "check_suite",
        "check_suite_runs",
    },
    "github.actions": {
        "workflows",
        "workflow",
        "workflow_runs",
        "workflow_run",
        "workflow_run_jobs",
        "artifacts",
        "actions_secrets",
        "actions_variables",
        "organization_actions_secrets",
        "organization_actions_variables",
        "environment_actions_secrets",
        "environment_actions_variables",
        "actions_caches",
        "actions_cache_usage",
        "self_hosted_runners",
    },
    "github.releases": {"releases", "release"},
    "github.deployments": {"deployments", "deployment_statuses", "environments"},
    "github.organizations": {
        "organizations",
        "organization_repositories",
        "organization_members",
        "organization_runners",
        "organization_webhooks",
        "collaborators",
    },
    "github.notifications": {
        "notifications",
        "notification_thread",
        "notification_subscription",
    },
    "github.security": {
        "dependabot_alerts",
        "code_scanning_alerts",
        "secret_scanning_alerts",
        "security_advisories",
    },
    "github.cloud": {
        "repository_codespaces",
        "codespaces",
        "codespace",
        "codespace_machines",
        "codespace_secrets",
        "packages",
        "package_versions",
    },
    "github.search": {"search"},
    "github.projects": {"projects", "project"},
}

_READ_HELP = {
    "github.repositories": "repositories(limit)، repository(owner,repo)، installations، installation_repositories(installation_id)",
    "github.code": "contents(owner,repo,path?,ref?)، file_text(owner,repo,path,ref?,max_bytes?)، repository_tree(owner,repo,ref?,limit?)، commits/branches/tags/contributors(owner,repo,limit?)، commit(owner,repo,ref)، compare(owner,repo,base,head)، languages(owner,repo)",
    "github.governance": "branch_protection/branch_rules(owner,repo,branch)، ruleset(s)/history و webhook(s)/deliveries با شناسه‌های لازم",
    "github.issues": "issues(owner,repo,state?,labels?,limit?)، issue(owner,repo,number)، issue_comments(owner,repo,number,limit?)",
    "github.pulls": "pulls(owner,repo,state?,limit?)، pull/pull_files/pull_reviews(owner,repo,number)",
    "github.discussions": "discussion_categories/discussions(owner,repo)، discussion(owner,repo,number)",
    "github.checks": "owner/repo به‌همراه ref، check_run_id یا check_suite_id مطابق operation",
    "github.actions": "owner/repo یا org/environment؛ workflow_id، run_id، runner_id یا cache مطابق operation",
    "github.releases": "releases(owner,repo,limit)، release(owner,repo و یکی از release_id/tag/latest)",
    "github.deployments": "deployments/environments(owner,repo)، deployment_statuses(owner,repo,deployment_id)",
    "github.organizations": "organizations، organization_repositories/organization_members(org)، collaborators(owner,repo)",
    "github.notifications": "notifications(all?,participating?,limit?) و thread/subscription با thread_id",
    "github.security": "هشدارهای dependabot/code-scanning/secret-scanning و security advisories با owner/repo",
    "github.cloud": "codespaceها و machineها؛ packages/package_versions با owner/owner_type/package_type",
    "github.search": "search(type,query,sort?,order?,limit?)",
    "github.projects": "projects(owner,owner_type)، project(project_id,limit?,after?)",
}


_WRITE_GROUPS: dict[str, set[str]] = {
    "github.repository_manage": {
        "repository_create",
        "repository_update",
        "repository_delete",
        "repository_transfer",
        "repository_topics",
        "fork",
        "branch_create",
        "branch_delete",
        "branch_protection_update",
        "branch_protection_delete",
        "ruleset_create",
        "ruleset_update",
        "ruleset_delete",
    },
    "github.file_write": {"file_upsert", "file_delete"},
    "github.issue_manage": {
        "issue_create",
        "issue_update",
        "issue_comment",
        "issue_lock",
        "issue_unlock",
    },
    "github.pull_manage": {"pull_create", "pull_update", "pull_review", "pull_merge"},
    "github.discussion_manage": {
        "discussion_create",
        "discussion_update",
        "discussion_delete",
        "discussion_comment",
        "discussion_comment_update",
        "discussion_comment_delete",
        "discussion_close",
        "discussion_reopen",
    },
    "github.check_manage": {
        "check_run_create",
        "check_run_update",
        "check_run_rerequest",
        "check_suite_rerequest",
    },
    # Secret *values* are intentionally excluded from LLM-facing tools. They
    # remain available through the protected UI API and never enter a prompt.
    "github.actions_manage": {
        "workflow_dispatch",
        "workflow_enable",
        "workflow_disable",
        "workflow_run_rerun",
        "workflow_run_cancel",
        "workflow_run_delete",
        "artifact_delete",
        "actions_cache_delete",
        "runner_labels_set",
        "runner_remove",
        "actions_secret_delete",
        "actions_variable_set",
        "actions_variable_delete",
        "organization_actions_secret_repositories_set",
        "organization_actions_secret_delete",
        "organization_actions_variable_set",
        "organization_actions_variable_delete",
        "environment_actions_secret_delete",
        "environment_actions_variable_set",
        "environment_actions_variable_delete",
    },
    "github.release_manage": {
        "release_create",
        "release_update",
        "release_delete",
        "release_asset_update",
        "release_asset_delete",
    },
    "github.deployment_manage": {
        "deployment_create",
        "deployment_status",
        "environment_update",
        "environment_delete",
    },
    "github.access_manage": {
        "collaborator_add",
        "collaborator_remove",
        "organization_membership_set",
        "organization_membership_remove",
        "notification_mark",
        "notification_subscription_set",
        "notification_subscription_delete",
    },
    "github.webhook_manage": {
        "webhook_delete",
        "webhook_ping",
        "webhook_redeliver",
    },
    "github.cloud_manage": {
        "codespace_create",
        "codespace_start",
        "codespace_stop",
        "codespace_update",
        "codespace_delete",
        "codespace_secret_repositories_set",
        "codespace_secret_delete",
        "package_version_restore",
        "package_version_delete",
    },
    "github.security_manage": {
        "dependabot_alert_update",
        "code_scanning_alert_update",
        "secret_scanning_alert_update",
    },
    "github.project_manage": {
        "project_create",
        "project_update",
        "project_delete",
        "project_add_item",
        "project_archive_item",
        "project_unarchive_item",
        "project_delete_item",
        "project_add_draft_issue",
        "project_update_draft_issue",
        "project_update_item_field",
        "project_clear_item_field",
        "project_update_item_position",
    },
    "github.local_write": {
        "local_clone",
        "local_pull",
        "local_push",
        "local_branch_create",
        "local_branch_switch",
        "local_branch_delete",
        "local_commit",
        "local_tag",
    },
}

_WRITE_HELP = {
    "github.repository_manage": "repository_create(name,description?,private?,auto_init?,org?)؛ سایر عملیات با owner/repo و فیلدهای همان operation",
    "github.file_write": "file_upsert(owner,repo,path,content,message,sha?,branch?)؛ file_delete(owner,repo,path,message,sha,branch?)",
    "github.issue_manage": "issue_create(owner,repo,title,body?)؛ update/comment/lock/unlock با owner/repo/number",
    "github.pull_manage": "pull_create(owner,repo,title,head,base,body?)؛ update/review/merge با owner/repo/number",
    "github.discussion_manage": "discussionها و commentها با owner/repo و number/id؛ ساخت به category_id نیاز دارد",
    "github.check_manage": "check_run_create(owner,repo,name,head_sha) و update/rerequest با شناسه",
    "github.actions_manage": "owner/repo، org یا environment؛ workflow/run/cache/runner/variable/secret metadata مطابق operation",
    "github.release_manage": "release_create(owner,repo,tag_name,...)؛ update/delete با release_id",
    "github.deployment_manage": "deployment_create/deployment_status و environment_update/delete با owner/repo",
    "github.access_manage": "همکار/عضویت سازمان و notification subscription با شناسه‌های لازم",
    "github.webhook_manage": "حذف/ping/redeliver وب‌هوک مخزن یا سازمان؛ ساخت و تغییر از UI مستقیم انجام می‌شود",
    "github.cloud_manage": "مدیریت چرخهٔ Codespace، دسترسی secret metadata و نسخه‌های package؛ مقدار secret از LLM عبور نمی‌کند",
    "github.security_manage": "به‌روزرسانی alertهای Dependabot، code scanning و secret scanning با owner/repo و number",
    "github.project_manage": "چرخهٔ project/item/draft issue/field/position با node idهای GraphQL",
    "github.local_write": "local_clone(owner,repo,destination?)؛ pull/push(path,branch?)؛ commit(path,message,paths?,all_tracked?,author_name?,author_email?)؛ branch و tag با path",
}


def register_github(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="github.status",
        description="وضعیت اتصال امن GitHub، حساب، سهمیه و مخزن‌های انتخاب‌شده را می‌خواند. SAFE.",
        parameters={},
    )(status)
    registry.decorator(
        name="github.account",
        description="پروفایل حساب GitHub متصل را می‌خواند. SAFE.",
        parameters={"refresh": {"type": "boolean"}},
    )(account)

    for name, operations in _READ_GROUPS.items():
        registry.decorator(
            name=name,
            description=(
                f"عملیات فقط‌خواندنی GitHub. operation یکی از: {', '.join(sorted(operations))}. "
                f"پارامترها: {_READ_HELP[name]}. SAFE."
            ),
            parameters={
                "operation": {"type": "string", "enum": sorted(operations)},
                "params": {
                    "type": "object",
                    "description": "پارامترهای تایپ‌شدهٔ عملیات؛ owner/repo و limit در صورت نیاز",
                },
            },
            required=("operation",),
        )(_make_read(name, operations))

    registry.decorator(
        name="github.local_inspect",
        description=(
            "cloneهای محلی یا status، branch، log، diff و remote بدون credential را می‌خواند. "
            "local_repositories با params={}؛ بقیه با path و برای log: limit، برای diff: staged/ref. SAFE."
        ),
        parameters={
            "operation": {
                "type": "string",
                "enum": [
                    "local_repositories",
                    "local_status",
                    "local_branches",
                    "local_log",
                    "local_remotes",
                    "local_diff",
                ],
            },
            "params": {"type": "object"},
        },
        required=("operation", "params"),
    )(local_inspect)

    for name, operations in _WRITE_GROUPS.items():
        registry.decorator(
            name=name,
            description=(
                f"عملیات تغییردهندهٔ GitHub. operation یکی از: {', '.join(sorted(operations))}. "
                f"پارامترها: {_WRITE_HELP[name]}. "
                "همیشه و مستقل از تنظیمات، تأیید زندهٔ کاربر لازم است."
            ),
            parameters={
                "operation": {"type": "string", "enum": sorted(operations)},
                "params": {"type": "object", "description": "پارامترهای تایپ‌شدهٔ عملیات"},
            },
            required=("operation", "params"),
            risk_level=Risk.DESTRUCTIVE,
            force_human_confirmation=True,
        )(_make_write(name, operations))


def _service(context: ActionContext):
    service = context.extra.get("github")
    if service is None:
        raise AssistantError("سرویس GitHub در این اجرا در دسترس نیست")
    return service


def status(*, context: ActionContext) -> str:
    return compact_json(_service(context).status(verify=True))


def account(refresh: bool = False, *, context: ActionContext) -> str:
    return compact_json(_service(context).account(force=refresh))


def local_inspect(operation: str, params: dict[str, Any], *, context: ActionContext) -> str:
    return compact_json(_service(context).local_read(operation, params))


def _make_read(action_name: str, allowed: set[str]):
    def execute(
        operation: str, params: dict[str, Any] | None = None, *, context: ActionContext
    ) -> str:
        if operation not in allowed:
            raise AssistantError(f"عملیات {operation} برای ابزار {action_name} مجاز نیست")
        return compact_json(_service(context).read(operation, params or {}))

    execute.__name__ = action_name.replace(".", "_")
    return execute


def _make_write(action_name: str, allowed: set[str]):
    def execute(operation: str, params: dict[str, Any], *, context: ActionContext) -> str:
        if operation not in allowed:
            raise AssistantError(f"عملیات {operation} برای ابزار {action_name} مجاز نیست")
        return compact_json(_service(context).write(operation, params))

    execute.__name__ = action_name.replace(".", "_")
    return execute
