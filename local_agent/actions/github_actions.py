"""GitHub actions for the agent loop.

Provides read-only and destructive actions for managing GitHub
repositories, issues, pull requests, branches, files, releases,
search, and notifications.

Risk levels:
* read-only (``github.status``, ``github.list_repos``, ``github.get_repo``,
  ``github.list_issues``, ``github.get_issue``, ``github.list_prs``,
  ``github.get_pr``, ``github.list_branches``, ``github.get_commits``,
  ``github.search_code``, ``github.search_issues``, ``github.list_notifications``,
  ``github.get_file``, ``github.list_files``, ``github.list_releases``) — Safe
* write (``github.create_issue``, ``github.close_issue``, ``github.create_pr``,
  ``github.merge_pr``, ``github.create_branch``, ``github.delete_branch``,
  ``github.add_comment``, ``github.add_labels``, ``github.assign_issue``,
  ``github.create_release``, ``github.update_file``, ``github.mark_read``) — Destructive
"""

from __future__ import annotations

from typing import Any

from ..core.errors import AssistantError, DependencyMissing
from .registry import ActionContext, ActionRegistry, Risk, risk


def register_github(registry: ActionRegistry, context: ActionContext) -> None:
    """Register all GitHub actions."""
    gh = context.extra.get("github")

    # ---- Safe / read-only -----------------------------------------------

    registry.decorator(
        name="github.status",
        description="وضعیت اتصال GitHub و اطلاعات حساب کاربری (نام، email، تعداد repos). SAFE.",
        parameters={},
    )(status)

    registry.decorator(
        name="github.list_repos",
        description=(
            "لیست repository های کاربر GitHub. فیلتر با query (نام/توضیحات)، "
            "مرتب‌سازی با sort (updated/stars/name) و visibility (all/public/private). SAFE."
        ),
        parameters={
            "sort": {"type": "string", "enum": ["updated", "stars", "name", "created"]},
            "visibility": {"type": "string", "enum": ["all", "public", "private"]},
            "per_page": {"type": "integer", "description": "حداکثر تعداد (پیش‌فرض 30)"},
            "query": {"type": "string", "description": "فیلتر نام یا توضیحات"},
        },
    )(list_repos)

    registry.decorator(
        name="github.get_repo",
        description=(
            "جزئیات یک repository: stars, forks, language, open issues, "
            "default branch, topics. ورودی: owner/name یا URL کامل. SAFE."
        ),
        parameters={
            "repo": {"type": "string", "description": "owner/name (مثلاً Alirezahjf/AI_Agent_OLLAMA)"},
        },
        required=("repo",),
    )(get_repo)

    registry.decorator(
        name="github.list_issues",
        description="لیست issue های یک repo با فیلتر state (open/closed/all) و labels. SAFE.",
        parameters={
            "repo": {"type": "string"},
            "state": {"type": "string", "enum": ["open", "closed", "all"]},
            "labels": {"type": "string", "description": "فیلتر label (comma-separated)"},
            "per_page": {"type": "integer"},
            "query": {"type": "string", "description": "فیلتر عنوان/متن"},
        },
        required=("repo",),
    )(list_issues)

    registry.decorator(
        name="github.get_issue",
        description="جزئیات یک issue شامل body, labels, assignees, comments. SAFE.",
        parameters={
            "repo": {"type": "string"},
            "number": {"type": "integer"},
        },
        required=("repo", "number"),
    )(get_issue)

    registry.decorator(
        name="github.list_prs",
        description="لیست Pull Request های یک repo (open/closed/all). SAFE.",
        parameters={
            "repo": {"type": "string"},
            "state": {"type": "string", "enum": ["open", "closed", "all"]},
            "per_page": {"type": "integer"},
        },
        required=("repo",),
    )(list_prs)

    registry.decorator(
        name="github.get_pr",
        description="جزئیات یک PR شامل diff stats, head/base branch, labels. SAFE.",
        parameters={
            "repo": {"type": "string"},
            "number": {"type": "integer"},
        },
        required=("repo", "number"),
    )(get_pr)

    registry.decorator(
        name="github.list_branches",
        description="لیست شاخه‌های یک repo. SAFE.",
        parameters={
            "repo": {"type": "string"},
            "per_page": {"type": "integer"},
        },
        required=("repo",),
    )(list_branches)

    registry.decorator(
        name="github.get_commits",
        description="آخرین کامیت‌های یک repo (اختیاری: شاخه یا مسیر فایل). SAFE.",
        parameters={
            "repo": {"type": "string"},
            "sha": {"type": "string", "description": "شاخه یا SHA (اختیاری)"},
            "per_page": {"type": "integer"},
            "path": {"type": "string", "description": "فیلتر مسیر فایل"},
        },
        required=("repo",),
    )(get_commits)

    registry.decorator(
        name="github.search_code",
        description="جست‌وجوی کد در کل GitHub (public repos). SAFE.",
        parameters={
            "query": {"type": "string", "description": "عبارت جست‌وجو (GitHub search syntax)"},
            "per_page": {"type": "integer"},
        },
        required=("query",),
    )(search_code)

    registry.decorator(
        name="github.search_issues",
        description="جست‌وجوی issue و PR در کل GitHub (GitHub search syntax). SAFE.",
        parameters={
            "query": {"type": "string"},
            "per_page": {"type": "integer"},
        },
        required=("query",),
    )(search_issues)

    registry.decorator(
        name="github.list_notifications",
        description="اعلان‌های GitHub (unread/all). SAFE.",
        parameters={
            "all": {"type": "boolean", "description": "اگر true باشد همه (شامل خوانده‌شده) برمی‌گرداند"},
            "per_page": {"type": "integer"},
        },
    )(list_notifications)

    registry.decorator(
        name="github.get_file",
        description="خواندن محتوای یک فایل از repository. SAFE.",
        parameters={
            "repo": {"type": "string"},
            "path": {"type": "string", "description": "مسیر فایل در repo"},
            "ref": {"type": "string", "description": "شاخه یا SHA (اختیاری)"},
        },
        required=("repo", "path"),
    )(get_file)

    registry.decorator(
        name="github.list_files",
        description="لیست فایل‌ها و پوشه‌های یک مسیر در repository. SAFE.",
        parameters={
            "repo": {"type": "string"},
            "path": {"type": "string", "description": "مسیر (اختیاری، پیش‌فرض root)"},
            "ref": {"type": "string", "description": "شاخه (اختیاری)"},
        },
        required=("repo",),
    )(list_files)

    registry.decorator(
        name="github.list_releases",
        description="لیست release های یک repo. SAFE.",
        parameters={
            "repo": {"type": "string"},
            "per_page": {"type": "integer"},
        },
        required=("repo",),
    )(list_releases)

    # ---- Destructive / write -----------------------------------------------

    registry.decorator(
        name="github.create_issue",
        description="ساخت issue جدید در یک repo با title, body, labels, assignees. DESTRUCTIVE.",
        parameters={
            "repo": {"type": "string"},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "labels": {"type": "array", "items": {"type": "string"}},
            "assignees": {"type": "array", "items": {"type": "string"}},
        },
        required=("repo", "title"),
        risk_level=Risk.DESTRUCTIVE,
    )(create_issue)

    registry.decorator(
        name="github.close_issue",
        description="بستن یک issue. DESTRUCTIVE.",
        parameters={
            "repo": {"type": "string"},
            "number": {"type": "integer"},
        },
        required=("repo", "number"),
        risk_level=Risk.DESTRUCTIVE,
    )(close_issue)

    registry.decorator(
        name="github.reopen_issue",
        description="باز کردن دوبارهٔ یک issue بسته‌شده. DESTRUCTIVE.",
        parameters={
            "repo": {"type": "string"},
            "number": {"type": "integer"},
        },
        required=("repo", "number"),
        risk_level=Risk.DESTRUCTIVE,
    )(reopen_issue)

    registry.decorator(
        name="github.add_comment",
        description="افزودن کامنت به یک issue یا PR. DESTRUCTIVE.",
        parameters={
            "repo": {"type": "string"},
            "number": {"type": "integer"},
            "body": {"type": "string"},
        },
        required=("repo", "number", "body"),
        risk_level=Risk.DESTRUCTIVE,
    )(add_comment)

    registry.decorator(
        name="github.add_labels",
        description="افزودن label به یک issue/PR. DESTRUCTIVE.",
        parameters={
            "repo": {"type": "string"},
            "number": {"type": "integer"},
            "labels": {"type": "array", "items": {"type": "string"}},
        },
        required=("repo", "number", "labels"),
        risk_level=Risk.DESTRUCTIVE,
    )(add_labels)

    registry.decorator(
        name="github.assign_issue",
        description="Assign کردن یک issue به کاربران. DESTRUCTIVE.",
        parameters={
            "repo": {"type": "string"},
            "number": {"type": "integer"},
            "assignees": {"type": "array", "items": {"type": "string"}},
        },
        required=("repo", "number", "assignees"),
        risk_level=Risk.DESTRUCTIVE,
    )(assign_issue)

    registry.decorator(
        name="github.create_pr",
        description="ساخت Pull Request جدید (head → base). DESTRUCTIVE.",
        parameters={
            "repo": {"type": "string"},
            "title": {"type": "string"},
            "head": {"type": "string", "description": "شاخهٔ مبدأ"},
            "base": {"type": "string", "description": "شاخهٔ مقصد (پیش‌فرض: default branch)"},
            "body": {"type": "string"},
            "draft": {"type": "boolean"},
        },
        required=("repo", "title", "head"),
        risk_level=Risk.DESTRUCTIVE,
    )(create_pr)

    registry.decorator(
        name="github.merge_pr",
        description="Merge کردن یک PR (method: merge/squash/rebase). DESTRUCTIVE.",
        parameters={
            "repo": {"type": "string"},
            "number": {"type": "integer"},
            "method": {"type": "string", "enum": ["merge", "squash", "rebase"]},
            "message": {"type": "string"},
        },
        required=("repo", "number"),
        risk_level=Risk.DESTRUCTIVE,
    )(merge_pr)

    registry.decorator(
        name="github.close_pr",
        description="بستن یک PR بدون merge. DESTRUCTIVE.",
        parameters={
            "repo": {"type": "string"},
            "number": {"type": "integer"},
        },
        required=("repo", "number"),
        risk_level=Risk.DESTRUCTIVE,
    )(close_pr)

    registry.decorator(
        name="github.create_branch",
        description="ساخت شاخهٔ جدید از یک شاخهٔ موجود. DESTRUCTIVE.",
        parameters={
            "repo": {"type": "string"},
            "branch_name": {"type": "string"},
            "from_ref": {"type": "string", "description": "شاخهٔ مبدأ (پیش‌فرض: default branch)"},
        },
        required=("repo", "branch_name"),
        risk_level=Risk.DESTRUCTIVE,
    )(create_branch)

    registry.decorator(
        name="github.delete_branch",
        description="حذف یک شاخه از repo. DESTRUCTIVE.",
        parameters={
            "repo": {"type": "string"},
            "branch_name": {"type": "string"},
        },
        required=("repo", "branch_name"),
        risk_level=Risk.DESTRUCTIVE,
    )(delete_branch)

    registry.decorator(
        name="github.create_release",
        description="ساخت release جدید (tag + name + body). DESTRUCTIVE.",
        parameters={
            "repo": {"type": "string"},
            "tag": {"type": "string"},
            "name": {"type": "string"},
            "body": {"type": "string"},
            "draft": {"type": "boolean"},
            "prerelease": {"type": "boolean"},
        },
        required=("repo", "tag"),
        risk_level=Risk.DESTRUCTIVE,
    )(create_release)

    registry.decorator(
        name="github.update_file",
        description="ایجاد یا به‌روزرسانی فایل در repo (commit مستقیم). DESTRUCTIVE.",
        parameters={
            "repo": {"type": "string"},
            "path": {"type": "string"},
            "content": {"type": "string"},
            "message": {"type": "string", "description": "commit message"},
            "branch": {"type": "string"},
        },
        required=("repo", "path", "content"),
        risk_level=Risk.DESTRUCTIVE,
    )(update_file)

    registry.decorator(
        name="github.mark_notifications_read",
        description="علامت‌گذاری همهٔ اعلان‌ها به‌عنوان خوانده‌شده. DESTRUCTIVE.",
        parameters={},
        risk_level=Risk.DESTRUCTIVE,
    )(mark_notifications_read)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _github(context: ActionContext):
    """Get the GitHub client from the context, raising a clear error if missing."""
    client = context.extra.get("github")
    if client is None:
        raise DependencyMissing(
            "GitHub client is not configured",
            install_hint=(
                "GitHub وصل نیست. ابتدا Personal Access Token را در config.json تنظیم کنید "
                "(فیلد github.token) یا از دکمهٔ «اتصال GitHub» در رابط وب استفاده کنید."
            ),
        )
    if not client.is_authenticated:
        raise DependencyMissing(
            "GitHub is not authenticated",
            install_hint="توکن GitHub تنظیم نشده است. از تنظیمات وصل شوید.",
        )
    return client


def _format_repos(repos: list) -> str:
    if not repos:
        return "repository ای یافت نشد."
    lines = []
    for r in repos:
        privacy = "🔒" if r.is_private else "🌐"
        parts = [f"  {privacy} {r.full_name}"]
        extras = []
        if r.language:
            extras.append(r.language)
        extras.append(f"⭐{r.stars}")
        extras.append(f"🍴{r.forks}")
        if r.open_issues:
            extras.append(f"📋{r.open_issues}")
        parts.append(" | " + " ".join(extras))
        if r.description:
            parts.append(f"\n     {r.description[:100]}")
        lines.append("".join(parts))
    return f"تعداد {len(repos)} repository:\n" + "\n".join(lines)


def _format_issues(issues: list) -> str:
    if not issues:
        return "issue ای یافت نشد."
    lines = []
    for i in issues:
        state_icon = "🟢" if i.state == "open" else "🔴"
        labels_str = ", ".join(i.labels[:3]) if i.labels else ""
        line = f"  {state_icon} #{i.number} {i.title}"
        if labels_str:
            line += f" [{labels_str}]"
        if i.user:
            line += f" (by @{i.user})"
        lines.append(line)
    return f"تعداد {len(issues)} issue:\n" + "\n".join(lines)


def _format_prs(prs: list) -> str:
    if not prs:
        return "Pull Request ای یافت نشد."
    lines = []
    for pr in prs:
        if pr.merged:
            icon = "🟣"
        elif pr.state == "open":
            icon = "🟢"
        else:
            icon = "🔴"
        line = f"  {icon} #{pr.number} {pr.title} ({pr.head} → {pr.base})"
        if pr.additions or pr.deletions:
            line += f" [+{pr.additions}/-{pr.deletions}]"
        lines.append(line)
    return f"تعداد {len(prs)} PR:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Safe action implementations
# ---------------------------------------------------------------------------


@risk(Risk.SAFE)
def status(*, context: ActionContext) -> str:
    info = _github(context).status()
    if not info.get("connected"):
        return f"❌ GitHub وصل نیست: {info.get('message', '')}"
    return (
        f"✅ GitHub وصل است\n"
        f"  کاربر: @{info.get('login', '?')}\n"
        f"  نام: {info.get('name', '')}\n"
        f"  ایمیل: {info.get('email', '')}\n"
        f"  Repos عمومی: {info.get('public_repos', 0)}\n"
        f"  Repos خصوصی: {info.get('private_repos', 0)}\n"
        f"  دنبال‌کننده: {info.get('followers', 0)}\n"
        f"  پروفایل: {info.get('url', '')}"
    )


@risk(Risk.SAFE)
def list_repos(*, sort: str = "updated", visibility: str = "all",
               per_page: int = 30, query: str = "", context: ActionContext) -> str:
    repos = _github(context).list_repos(
        sort=sort, visibility=visibility,
        per_page=max(1, int(per_page or 30)),
        query=query,
    )
    return _format_repos(repos)


@risk(Risk.SAFE)
def get_repo(*, repo: str, context: ActionContext) -> str:
    r = _github(context).get_repo(repo)
    topics = ", ".join(r.topics[:10]) if r.topics else "—"
    return (
        f"📦 {r.full_name}\n"
        f"  توضیحات: {r.description or '—'}\n"
        f"  زبان: {r.language or '—'}\n"
        f"  ⭐ {r.stars} | 🍴 {r.forks} | 📋 {r.open_issues}\n"
        f"  شاخهٔ پیش‌فرض: {r.default_branch}\n"
        f"  خصوصی: {'بله' if r.is_private else 'خیر'}\n"
        f"  Fork: {'بله' if r.is_fork else 'خیر'}\n"
        f"  Topics: {topics}\n"
        f"  آخرین به‌روزرسانی: {r.updated_at}\n"
        f"  URL: {r.url}"
    )


@risk(Risk.SAFE)
def list_issues(*, repo: str, state: str = "open", labels: str = "",
                per_page: int = 30, query: str = "", context: ActionContext) -> str:
    issues = _github(context).list_issues(
        repo, state=state, labels=labels,
        per_page=max(1, int(per_page or 30)),
        query=query,
    )
    return _format_issues(issues)


@risk(Risk.SAFE)
def get_issue(*, repo: str, number: int, context: ActionContext) -> str:
    i = _github(context).get_issue(repo, int(number))
    labels = ", ".join(i.labels) if i.labels else "—"
    assignees = ", ".join(f"@{a}" for a in i.assignees) if i.assignees else "—"
    return (
        f"📋 Issue #{i.number}: {i.title}\n"
        f"  وضعیت: {i.state}\n"
        f"  نویسنده: @{i.user}\n"
        f"  Labels: {labels}\n"
        f"  Assignees: {assignees}\n"
        f"  کامنت‌ها: {i.comments}\n"
        f"  ایجاد: {i.created_at}\n"
        f"  URL: {i.url}\n"
        f"\n{i.body[:2000]}"
    )


@risk(Risk.SAFE)
def list_prs(*, repo: str, state: str = "open",
             per_page: int = 30, context: ActionContext) -> str:
    prs = _github(context).list_prs(repo, state=state,
                                     per_page=max(1, int(per_page or 30)))
    return _format_prs(prs)


@risk(Risk.SAFE)
def get_pr(*, repo: str, number: int, context: ActionContext) -> str:
    pr = _github(context).get_pr(repo, int(number))
    labels = ", ".join(pr.labels) if pr.labels else "—"
    merged_str = "✅ Merged" if pr.merged else f"وضعیت: {pr.state}"
    return (
        f"🔀 PR #{pr.number}: {pr.title}\n"
        f"  {merged_str}\n"
        f"  نویسنده: @{pr.user}\n"
        f"  شاخه: {pr.head} → {pr.base}\n"
        f"  Labels: {labels}\n"
        f"  تغییرات: +{pr.additions}/-{pr.deletions} ({pr.changed_files} فایل)\n"
        f"  کامنت‌ها: {pr.comments} | Review: {pr.review_comments}\n"
        f"  ایجاد: {pr.created_at}\n"
        f"  URL: {pr.url}\n"
        f"\n{pr.body[:2000]}"
    )


@risk(Risk.SAFE)
def list_branches(*, repo: str, per_page: int = 30, context: ActionContext) -> str:
    branches = _github(context).list_branches(repo, per_page=max(1, int(per_page or 30)))
    if not branches:
        return "شاخه‌ای یافت نشد."
    lines = [f"  • {b['name']} (sha={b['sha']}){'  🛡️ protected' if b.get('protected') else ''}"
             for b in branches]
    return f"تعداد {len(branches)} شاخه در {repo}:\n" + "\n".join(lines)


@risk(Risk.SAFE)
def get_commits(*, repo: str, sha: str = "", per_page: int = 20,
                path: str = "", context: ActionContext) -> str:
    commits = _github(context).get_commits(
        repo, sha=sha, per_page=max(1, int(per_page or 20)), path=path
    )
    if not commits:
        return "کامیتی یافت نشد."
    lines = [f"  • {c['sha']} {c['message']} — {c['author']} ({c['date'][:10]})"
             for c in commits]
    return f"آخرین {len(commits)} کامیت در {repo}:\n" + "\n".join(lines)


@risk(Risk.SAFE)
def search_code(*, query: str, per_page: int = 20, context: ActionContext) -> str:
    results = _github(context).search_code(query, per_page=max(1, int(per_page or 20)))
    if not results:
        return "نتیجه‌ای یافت نشد."
    lines = [f"  • {r['repo']}/{r['path']} — {r['url']}" for r in results]
    return f"نتایج جست‌وجوی «{query}» ({len(results)} مورد):\n" + "\n".join(lines)


@risk(Risk.SAFE)
def search_issues(*, query: str, per_page: int = 20, context: ActionContext) -> str:
    results = _github(context).search_issues(query, per_page=max(1, int(per_page or 20)))
    return _format_issues(results) if results else "نتیجه‌ای یافت نشد."


@risk(Risk.SAFE)
def list_notifications(*, all: bool = False, per_page: int = 30, context: ActionContext) -> str:
    notifications = _github(context).list_notifications(
        all=all, per_page=max(1, int(per_page or 30))
    )
    if not notifications:
        return "اعلانی وجود ندارد."
    lines = []
    for n in notifications:
        unread = "🔵" if n["unread"] else "⚪"
        lines.append(f"  {unread} [{n['type']}] {n['title']}\n     {n['repo']} — {n['reason']}")
    return f"تعداد {len(notifications)} اعلان:\n" + "\n".join(lines)


@risk(Risk.SAFE)
def get_file(*, repo: str, path: str, ref: str = "", context: ActionContext) -> str:
    f = _github(context).get_file(repo, path, ref=ref)
    content = f["content"]
    if len(content) > 10000:
        content = content[:10000] + "\n… (کوتاه شد)"
    return (
        f"📄 {f['path']} ({f['size']} bytes)\n"
        f"SHA: {f['sha'][:12]}\n"
        f"URL: {f['url']}\n\n"
        f"{content}"
    )


@risk(Risk.SAFE)
def list_files(*, repo: str, path: str = "", ref: str = "", context: ActionContext) -> str:
    files = _github(context).list_files(repo, path=path, ref=ref)
    if not files:
        return "فایلی یافت نشد."
    lines = []
    for f in files:
        icon = "📁" if f["type"] == "dir" else "📄"
        size = f" ({f['size']}B)" if f["size"] else ""
        lines.append(f"  {icon} {f['name']}{size}")
    return f"محتوای {path or '/'} در {repo}:\n" + "\n".join(lines)


@risk(Risk.SAFE)
def list_releases(*, repo: str, per_page: int = 10, context: ActionContext) -> str:
    releases = _github(context).list_releases(repo, per_page=max(1, int(per_page or 10)))
    if not releases:
        return "release ای یافت نشد."
    lines = []
    for r in releases:
        badge = "📦"
        if r["prerelease"]:
            badge = "🧪"
        if r["draft"]:
            badge = "📝"
        lines.append(f"  {badge} {r['tag']} — {r['name']} ({r['published_at'][:10] if r['published_at'] else 'draft'})")
        if r["body"]:
            lines.append(f"     {r['body'][:100]}")
    return f"Release های {repo}:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Destructive action implementations
# ---------------------------------------------------------------------------


@risk(Risk.DESTRUCTIVE)
def create_issue(*, repo: str, title: str, body: str = "",
                 labels: list[str] | None = None,
                 assignees: list[str] | None = None,
                 context: ActionContext) -> str:
    issue = _github(context).create_issue(repo, title, body=body or "",
                                          labels=labels, assignees=assignees)
    return f"✅ Issue #{issue.number} ساخته شد: {issue.title}\n   {issue.url}"


@risk(Risk.DESTRUCTIVE)
def close_issue(*, repo: str, number: int, context: ActionContext) -> str:
    issue = _github(context).close_issue(repo, int(number))
    return f"✅ Issue #{issue.number} بسته شد: {issue.title}"


@risk(Risk.DESTRUCTIVE)
def reopen_issue(*, repo: str, number: int, context: ActionContext) -> str:
    issue = _github(context).reopen_issue(repo, int(number))
    return f"✅ Issue #{issue.number} باز شد: {issue.title}"


@risk(Risk.DESTRUCTIVE)
def add_comment(*, repo: str, number: int, body: str, context: ActionContext) -> str:
    _github(context).add_issue_comment(repo, int(number), body)
    return f"✅ کامنت به #{number} در {repo} اضافه شد."


@risk(Risk.DESTRUCTIVE)
def add_labels(*, repo: str, number: int, labels: list[str], context: ActionContext) -> str:
    _github(context).add_labels(repo, int(number), labels)
    return f"✅ Label های {labels} به #{number} اضافه شد."


@risk(Risk.DESTRUCTIVE)
def assign_issue(*, repo: str, number: int, assignees: list[str], context: ActionContext) -> str:
    _github(context).assign_issue(repo, int(number), assignees)
    return f"✅ Issue #{number} به {assignees} assign شد."


@risk(Risk.DESTRUCTIVE)
def create_pr(*, repo: str, title: str, head: str, base: str = "main",
              body: str = "", draft: bool = False, context: ActionContext) -> str:
    pr = _github(context).create_pr(repo, title, head, base=base or "main",
                                     body=body or "", draft=draft)
    return f"✅ PR #{pr.number} ساخته شد: {pr.title}\n   {pr.url}"


@risk(Risk.DESTRUCTIVE)
def merge_pr(*, repo: str, number: int, method: str = "merge",
             message: str = "", context: ActionContext) -> str:
    _github(context).merge_pr(repo, int(number), method=method, message=message)
    return f"✅ PR #{number} با روش {method} merge شد."


@risk(Risk.DESTRUCTIVE)
def close_pr(*, repo: str, number: int, context: ActionContext) -> str:
    pr = _github(context).close_pr(repo, int(number))
    return f"✅ PR #{pr.number} بسته شد: {pr.title}"


@risk(Risk.DESTRUCTIVE)
def create_branch(*, repo: str, branch_name: str, from_ref: str = "",
                  context: ActionContext) -> str:
    _github(context).create_branch(repo, branch_name, from_ref=from_ref)
    source = from_ref or "default branch"
    return f"✅ شاخهٔ «{branch_name}» از {source} ساخته شد."


@risk(Risk.DESTRUCTIVE)
def delete_branch(*, repo: str, branch_name: str, context: ActionContext) -> str:
    _github(context).delete_branch(repo, branch_name)
    return f"✅ شاخهٔ «{branch_name}» حذف شد."


@risk(Risk.DESTRUCTIVE)
def create_release(*, repo: str, tag: str, name: str = "", body: str = "",
                   draft: bool = False, prerelease: bool = False,
                   context: ActionContext) -> str:
    r = _github(context).create_release(repo, tag, name=name or tag,
                                         body=body, draft=draft, prerelease=prerelease)
    return f"✅ Release {tag} ساخته شد.\n   {r.get('html_url', '')}"


@risk(Risk.DESTRUCTIVE)
def update_file(*, repo: str, path: str, content: str,
                message: str = "", branch: str = "",
                context: ActionContext) -> str:
    _github(context).update_file(repo, path, content,
                                  message=message or f"Update {path}",
                                  branch=branch)
    return f"✅ فایل {path} در {repo} به‌روزرسانی شد."


@risk(Risk.DESTRUCTIVE)
def mark_notifications_read(*, context: ActionContext) -> str:
    _github(context).mark_notifications_read()
    return "✅ همهٔ اعلان‌ها خوانده‌شده علامت خوردند."
