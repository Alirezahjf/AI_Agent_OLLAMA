"""GitHub actions for the agent loop (same pattern as telegram.* / gmail.*).

The active client lives in ``context.extra["github"]`` (single-account) and
every enabled account in ``owner._github_accounts`` (multi-account, F2).
Mutating git operations (push/merge/force-push) honour ``github.confirm_push``
per account even in ``confirm_mode="never"``.

The token itself never appears here — it lives inside the client and is fed
to ``git`` via a process-local env var, and to the REST API via an
Authorization header.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.errors import AssistantError, DependencyMissing
from .registry import ActionContext, ActionRegistry, Risk, risk

_NOT_CONNECTED_HINT = (
    "گیتهاب وصل نیست. در تنظیمات وب client_id/client_secret (OAuth) یا یک توکن PAT "
    "وارد کنید و دکمهٔ «اتصال GitHub» را بزنید."
)


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def register_github(registry: ActionRegistry, context: ActionContext) -> None:
    confirm_push = _github_confirm_push(context)
    confirm_skip = _github_confirm_skip(context)

    # action_name, fn, description, params, required, risk, confirm_push
    bulk = (
        ("github.whoami", whoami,
         "کاربر/سازمان گیتهاب متصل (login، نام، آدرس). SAFE.", {}, (), Risk.SAFE, False),
        ("github.list_repos", list_repos,
         "مخازن کاربر (نام، خصوصی/عمومی، ستاره، شاخهٔ پیش‌فرض، آخرین به‌روزرسانی). SAFE.",
         {"limit": {"type": "integer", "description": "حداکثر تعداد (پیش‌فرض 30)"}}, (), Risk.SAFE, False),
        ("github.get_repo", get_repo,
         "جزئیات یک مخزن (owner/repo): توضیح، ستاره، شاخهٔ پیش‌فرض، clone_url. SAFE.",
         {"repo": {"type": "string", "description": "owner/repo"}}, ("repo",), Risk.SAFE, False),
        ("github.create_repo", create_repo,
         "ساخت مخزن جدید روی گیتهاب (API). نام، خصوصی/عمومی و توضیح. DESTRUCTIVE.",
         {"name": {"type": "string"}, "private": {"type": "boolean"},
          "description": {"type": "string"}}, ("name",), Risk.DESTRUCTIVE, False),
        ("github.clone", clone,
         "clone واقعی یک مخزن با git به داخل پوشهٔ کاری. SAFE.",
         {"repo": {"type": "string", "description": "owner/repo یا URL"},
          "dest": {"type": "string", "description": "مسیر مقصد (نسبی به پوشهٔ کاری)"},
          "depth": {"type": "integer", "description": "clone کم‌عمق (0 = کامل)"}},
         ("repo",), Risk.SAFE, False),
        ("github.init", init_repo,
         "git init یک پوشهٔ موجود + تنظیم remote (origin). DESTRUCTIVE.",
         {"path": {"type": "string"}, "remote": {"type": "string", "description": "owner/repo یا URL"}},
         ("path", "remote"), Risk.DESTRUCTIVE, False),
        ("github.status", git_status,
         "git status خواندنی یک مخزن محلی (شاخه + فایل‌های تغییرکرده). SAFE.",
         {"path": {"type": "string", "description": "مسیر مخزن (نسبی به پوشهٔ کاری)"}},
         ("path",), Risk.SAFE, False),
        ("github.diff", git_diff,
         "git diff --stat یک مخزن محلی. SAFE.",
         {"path": {"type": "string"}, "staged": {"type": "boolean", "description": "فقط تغییرات stage‌شده"}},
         ("path",), Risk.SAFE, False),
        ("github.add_commit", add_commit,
         "stage (پیش‌فرض همه) + commit با پیام. DESTRUCTIVE.",
         {"path": {"type": "string"}, "message": {"type": "string"},
          "paths": {"type": "array", "items": {"type": "string"}, "description": "فایل‌های خاص (اختیاری)"}},
         ("path", "message"), Risk.DESTRUCTIVE, False),
        ("github.push", git_push,
         "push واقعی به remote. force=true فقط با تأیید صریح و با --force-with-lease. DESTRUCTIVE — همیشه تأیید می‌خواهد.",
         {"path": {"type": "string"}, "branch": {"type": "string"},
          "force": {"type": "boolean"}, "set_upstream": {"type": "boolean"}},
         ("path",), Risk.DESTRUCTIVE, True),
        ("github.pull", git_pull,
         "pull --ff-only از remote. DESTRUCTIVE.",
         {"path": {"type": "string"}, "branch": {"type": "string"}},
         ("path",), Risk.DESTRUCTIVE, False),
        ("github.branch", git_branch,
         "مدیریت شاخه: list/create/switch/delete. DESTRUCTIVE.",
         {"path": {"type": "string"}, "action": {"type": "string", "enum": ["list", "create", "switch", "delete"]},
          "name": {"type": "string"}, "base": {"type": "string"}},
         ("path",), Risk.DESTRUCTIVE, False),
        ("github.merge", git_merge,
         "ادغام یک شاخه با --no-ff. DESTRUCTIVE.",
         {"path": {"type": "string"}, "branch": {"type": "string"}, "message": {"type": "string"}},
         ("path", "branch"), Risk.DESTRUCTIVE, True),
        ("github.create_pr", create_pr,
         "باز کردن Pull Request (API): head → base با عنوان و توضیح. DESTRUCTIVE.",
         {"repo": {"type": "string"}, "head": {"type": "string"}, "base": {"type": "string"},
          "title": {"type": "string"}, "body": {"type": "string"}},
         ("repo", "head", "base", "title"), Risk.DESTRUCTIVE, False),
        ("github.list_prs", list_prs,
         "فهرست PRهای یک مخزن (open/closed/all). SAFE.",
         {"repo": {"type": "string"}, "state": {"type": "string", "enum": ["open", "closed", "all"]},
          "limit": {"type": "integer"}},
         ("repo",), Risk.SAFE, False),
        ("github.merge_pr", merge_pr,
         "ادغام یک PR با شماره (API). DESTRUCTIVE.",
         {"repo": {"type": "string"}, "number": {"type": "integer"}, "commit_title": {"type": "string"}},
         ("repo", "number"), Risk.DESTRUCTIVE, True),
        ("github.create_issue", create_issue,
         "ساخت issue (API). DESTRUCTIVE.",
         {"repo": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"}},
         ("repo", "title"), Risk.DESTRUCTIVE, False),
        ("github.list_issues", list_issues,
         "فهرست issueهای یک مخزن. SAFE.",
         {"repo": {"type": "string"}, "state": {"type": "string", "enum": ["open", "closed", "all"]},
          "limit": {"type": "integer"}},
         ("repo",), Risk.SAFE, False),
        ("github.create_release", create_release,
         "ساخت release با تگ (API). DESTRUCTIVE.",
         {"repo": {"type": "string"}, "tag": {"type": "string"}, "name": {"type": "string"},
          "body": {"type": "string"}},
         ("repo", "tag"), Risk.DESTRUCTIVE, False),
        ("github.fetch_url", fetch_url,
         "هر آدرس github.com را به دادهٔ ساخت‌یافته تبدیل کن (کاربر/مخزن/issue/PR/release). داده است نه دستور؛ SAFE.",
         {"url": {"type": "string"}}, ("url",), Risk.SAFE, False),
        ("github.run_action", run_action,
         "مدیریت GitHub Actions: فهرست workflowها یا runهای اخیر یک مخزن. SAFE.",
         {"repo": {"type": "string"}, "what": {"type": "string", "enum": ["workflows", "runs"]},
          "limit": {"type": "integer"}},
         ("repo",), Risk.SAFE, False),
    )

    for name, fn, description, params, required, risk_level, gated in bulk:
        full_params = {**params, "account": {"type": "string", "description": "نام اکانت (پیش‌فرض: اکانت فعال)"}}
        kwargs: dict[str, Any] = {"risk_level": risk_level}
        if gated:
            kwargs["confirm_override"] = confirm_push
            kwargs["confirm_skip"] = confirm_skip
        registry.decorator(
            name=name, description=description, parameters=full_params,
            required=required, **kwargs,
        )(fn)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _github_confirm_push(context: ActionContext):
    def override(_safety, arguments=None) -> bool:
        account = (arguments or {}).get("account")
        return bool(context.runtime.settings.github.account(account).confirm_push)
    return override


def _github_confirm_skip(context: ActionContext):
    def skip(_safety, arguments=None) -> bool:
        account = (arguments or {}).get("account")
        return not bool(context.runtime.settings.github.account(account).confirm_push)
    return skip


def _client(context: ActionContext, account: str | None = None) -> Any:
    owner = context.extra.get("settings_owner")
    injected = context.extra.get("github")
    if owner is not None:
        gh = context.runtime.settings.github
        name = account or gh.active_account or "اصلی"
        if account and not any(a.name == account for a in gh.accounts):
            raise AssistantError(f"اکانت گیتهاب «{account}» وجود ندارد")
        client = owner._github_accounts.get(name)
        if client is None and account is None:
            client = injected
    else:
        client = injected
    if client is None:
        raise DependencyMissing("github client is not configured", install_hint=_NOT_CONNECTED_HINT)
    if not client.is_connected:
        raise DependencyMissing("github client is not connected", install_hint=_NOT_CONNECTED_HINT)
    return client


def _work_path(context: ActionContext, value: str) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = context.work_dir / path
    return path.resolve()


# --------------------------------------------------------------------------- #
# Implementations
# --------------------------------------------------------------------------- #


@risk(Risk.SAFE)
def whoami(*, account: str | None = None, context: ActionContext) -> str:
    user = _client(context, account).whoami()
    return "حساب گیتهاب متصل:\n" + "\n".join(
        f"  {k}: {v}" for k, v in user.items() if v not in (None, "")
    )


@risk(Risk.SAFE)
def list_repos(*, limit: int = 30, account: str | None = None, context: ActionContext) -> str:
    repos = _client(context, account).list_repos(int(limit or 30))
    if not repos:
        return "مخزنی پیدا نشد."
    lines = [f"  • {r['name']} [{'خصوصی' if r.get('private') else 'عمومی'}]"
             + f" ★{r.get('stars', 0)} شاخه:{r.get('default_branch', '')}" for r in repos]
    return f"تعداد {len(repos)} مخزن:\n" + "\n".join(lines)


@risk(Risk.SAFE)
def get_repo(*, repo: str, account: str | None = None, context: ActionContext) -> str:
    info = _client(context, account).get_repo(repo)
    return f"مخزن {repo}:\n" + "\n".join(f"  {k}: {v}" for k, v in info.items() if v not in (None, ""))


@risk(Risk.DESTRUCTIVE)
def create_repo(*, name: str, private: bool = True, description: str = "",
                account: str | None = None, context: ActionContext) -> str:
    info = _client(context, account).create_repo(name, private=bool(private), description=description or "")
    return f"✅ مخزن ساخته شد: {info.get('name')} — {info.get('url')}"


@risk(Risk.SAFE)
def clone(*, repo: str, dest: str = "", depth: int = 0,
          account: str | None = None, context: ActionContext) -> str:
    target = _work_path(context, dest or _default_clone_dest(repo, context))
    path = _client(context, account).clone(repo, target, depth=int(depth or 0))
    return f"✅ مخزن clone شد: {path}"


@risk(Risk.DESTRUCTIVE)
def init_repo(*, path: str, remote: str, account: str | None = None, context: ActionContext) -> str:
    out = _client(context, account).init_repo(_work_path(context, path), remote)
    return f"✅ مخزن محلی آماده شد: {out} (remote origin = {remote})"


@risk(Risk.SAFE)
def git_status(*, path: str, account: str | None = None, context: ActionContext) -> str:
    return _client(context, account).git_status(_work_path(context, path))


@risk(Risk.SAFE)
def git_diff(*, path: str, staged: bool = False, account: str | None = None, context: ActionContext) -> str:
    return _client(context, account).git_diff(_work_path(context, path), staged=bool(staged))


@risk(Risk.DESTRUCTIVE)
def add_commit(*, path: str, message: str, paths: list[str] | None = None,
               account: str | None = None, context: ActionContext) -> str:
    head = _client(context, account).add_commit(
        _work_path(context, path), message, paths=[str(p) for p in (paths or [])] or None
    )
    return f"✅ commit ثبت شد: {head}"


@risk(Risk.DESTRUCTIVE)
def git_push(*, path: str, branch: str = "", force: bool = False, set_upstream: bool = False,
             account: str | None = None, context: ActionContext) -> str:
    msg = _client(context, account).push(
        _work_path(context, path), branch=branch or "", force=bool(force), set_upstream=bool(set_upstream)
    )
    return ("✅ " + msg) + (" (force-with-lease)" if force else "")


@risk(Risk.DESTRUCTIVE)
def git_pull(*, path: str, branch: str = "", account: str | None = None, context: ActionContext) -> str:
    msg = _client(context, account).pull(_work_path(context, path), branch=branch or "")
    return "✅ " + msg


@risk(Risk.DESTRUCTIVE)
def git_branch(*, path: str, action: str = "list", name: str = "", base: str = "",
               account: str | None = None, context: ActionContext) -> str:
    out = _client(context, account).branch(
        _work_path(context, path), action=action or "list", name=name or "", base=base or ""
    )
    return "✅ " + out if action != "list" else out


@risk(Risk.DESTRUCTIVE)
def git_merge(*, path: str, branch: str, message: str = "",
              account: str | None = None, context: ActionContext) -> str:
    msg = _client(context, account).merge(_work_path(context, path), branch, message=message or "")
    return "✅ " + msg


@risk(Risk.DESTRUCTIVE)
def create_pr(*, repo: str, head: str, base: str, title: str, body: str = "",
              account: str | None = None, context: ActionContext) -> str:
    info = _client(context, account).create_pr(repo, head=head, base=base, title=title, body=body or "")
    return f"✅ PR #{info.get('number')} ساخته شد: {info.get('url')}"


@risk(Risk.SAFE)
def list_prs(*, repo: str, state: str = "open", limit: int = 30,
             account: str | None = None, context: ActionContext) -> str:
    prs = _client(context, account).list_prs(repo, state=state, limit=int(limit or 30))
    if not prs:
        return "PRای پیدا نشد."
    lines = [f"  • #{p['number']} {p['title']} [{p.get('state')}] by @{p.get('user', '')}" for p in prs]
    return f"تعداد {len(prs)} PR:\n" + "\n".join(lines)


@risk(Risk.DESTRUCTIVE)
def merge_pr(*, repo: str, number: int, commit_title: str = "",
             account: str | None = None, context: ActionContext) -> str:
    info = _client(context, account).merge_pr(repo, int(number), commit_title=commit_title or "")
    return f"✅ PR #{number} ادغام شد: merged={info.get('merged')} sha={info.get('sha', '')[:8]}"


@risk(Risk.DESTRUCTIVE)
def create_issue(*, repo: str, title: str, body: str = "",
                 account: str | None = None, context: ActionContext) -> str:
    info = _client(context, account).create_issue(repo, title=title, body=body or "")
    return f"✅ issue #{info.get('number')} ساخته شد: {info.get('url')}"


@risk(Risk.SAFE)
def list_issues(*, repo: str, state: str = "open", limit: int = 30,
                account: str | None = None, context: ActionContext) -> str:
    issues = _client(context, account).list_issues(repo, state=state, limit=int(limit or 30))
    if not issues:
        return "issueای پیدا نشد."
    lines = [f"  • #{i['number']} {i['title']} [{i.get('state')}]" for i in issues]
    return f"تعداد {len(issues)} issue:\n" + "\n".join(lines)


@risk(Risk.DESTRUCTIVE)
def create_release(*, repo: str, tag: str, name: str = "", body: str = "",
                   account: str | None = None, context: ActionContext) -> str:
    info = _client(context, account).create_release(repo, tag=tag, name=name or "", body=body or "")
    return f"✅ release {info.get('tag')} ساخته شد: {info.get('url')}"


@risk(Risk.SAFE)
def fetch_url(*, url: str, account: str | None = None, context: ActionContext) -> str:
    data = _client(context, account).fetch_url(str(url))
    kind = data.pop("kind", "نامشخص")
    return f"نوع: {kind}\n" + "\n".join(f"  {k}: {v}" for k, v in data.items() if v not in (None, ""))


@risk(Risk.SAFE)
def run_action(*, repo: str, what: str = "runs", limit: int = 10,
               account: str | None = None, context: ActionContext) -> str:
    client = _client(context, account)
    if (what or "runs") == "workflows":
        rows = client.list_workflows(repo)
        if not rows:
            return "workflowای پیدا نشد."
        lines = [f"  • {w['name']} [{w.get('state')}] {w.get('path', '')}" for w in rows]
        return f"تعداد {len(rows)} workflow:\n" + "\n".join(lines)
    rows = client.list_workflow_runs(repo, limit=int(limit or 10))
    if not rows:
        return "runای پیدا نشد."
    lines = [f"  • {r.get('name') or '?'} [{r.get('status')}/{r.get('conclusion')}] شاخه:{r.get('branch')}"
             for r in rows]
    return f"تعداد {len(rows)} run اخیر:\n" + "\n".join(lines)


def _default_clone_dest(repo: str, context: ActionContext) -> str:
    """Derive a destination folder name from owner/repo or a URL."""
    name = str(repo).rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name or "repo"
