"""Fork event formatter."""


def format_fork_message(
    author_name: str,
    source_repo: str,
    fork_repo: str,
    fork_url: str,
    vip_line: str = "",
    secret_line: str = "",
) -> str:
    """Format fork event message."""
    lines = [
        "🍴 GitHub Fork Event",
        f"👤 {author_name} forked {source_repo}",
        f"📦 New repo: {fork_repo}",
        f"📎 {fork_url}",
    ]
    if vip_line:
        lines.append(vip_line)
    if secret_line:
        lines.append(secret_line)
    return "\n".join(lines)
