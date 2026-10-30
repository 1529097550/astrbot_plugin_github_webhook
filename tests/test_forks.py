"""Fork notifications, repository tracking and the /github_forks query."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

from src.core.plugin import GitHubWebhookPlugin
from src.formatters.fork_formatter import format_fork_message
from src.handlers.fork_handler import handle_fork_event


def make_payload(source="owner/source-repo", fork="user/forked-repo"):
    return {
        "sender": {"login": "user"},
        "repository": {"full_name": source},
        "forkee": {
            "full_name": fork,
            "html_url": f"https://github.com/{fork}",
        },
    }


def test_fork_message_contains_all_fields():
    message = format_fork_message(
        author_name="user",
        source_repo="owner/source-repo",
        fork_repo="user/forked-repo",
        fork_url="https://github.com/user/forked-repo",
    )
    assert "👤 user forked owner/source-repo" in message
    assert "📦 New repo: user/forked-repo" in message
    assert "📎 https://github.com/user/forked-repo" in message


def test_fork_handler_formats_event():
    message = asyncio.run(handle_fork_event(make_payload(), None))
    assert "GitHub Fork Event" in message
    assert "👤 user forked owner/source-repo" in message
    assert "📦 New repo: user/forked-repo" in message


def test_fork_handler_falls_back_to_unknown_fields():
    message = asyncio.run(handle_fork_event({"sender": {}, "forkee": {}}, None))
    assert "👤 Unknown forked Unknown" in message
    assert "📦 New repo: Unknown" in message


def test_fork_handler_ignores_bad_payload():
    message = asyncio.run(handle_fork_event({"sender": None, "forkee": 42}, None))
    assert message is None


def build_plugin(enable_agent=False):
    context = SimpleNamespace(
        send_message=AsyncMock(return_value=True),
        llm_generate=AsyncMock(return_value=SimpleNamespace(
            completion_text="Fork notification from LLM"
        )),
    )
    plugin = GitHubWebhookPlugin(context, {
        "port": 8080,
        "target_umo": "test:GroupMessage:123456",
        "webhook_secret": "",
        "rate_limit": 0,
        "enable_agent": enable_agent,
        "llm_provider_id": "test-provider",
        "agent_timeout": 60,
        "agent_system_prompt": "",
    })
    return plugin, context


def test_fork_webhook_dispatches_and_tracks_source_repo():
    async def run():
        plugin, context = build_plugin()
        payload = make_payload()

        async with TestClient(TestServer(plugin.app)) as client:
            response = await client.post(
                "/webhook", json=payload, headers={"X-GitHub-Event": "fork"}
            )
            assert response.status == 200
            assert await response.text() == "OK"

        assert list(plugin.source_repos) == ["owner/source-repo"]
        context.send_message.assert_awaited_once()
        target, chain = context.send_message.await_args.args
        assert target == "test:GroupMessage:123456"
        assert "👤 user forked owner/source-repo" in chain[0].text

    asyncio.run(run())


def test_fork_webhook_keeps_insertion_order_without_duplicates():
    async def run():
        plugin, _ = build_plugin()

        async with TestClient(TestServer(plugin.app)) as client:
            for repo in ("a/repo", "b/repo", "a/repo"):
                payload = make_payload(source=repo, fork="user/forked-repo")
                response = await client.post(
                    "/webhook", json=payload, headers={"X-GitHub-Event": "fork"}
                )
                assert response.status == 200

        assert list(plugin.source_repos) == ["a/repo", "b/repo"]

    asyncio.run(run())


def fake_event():
    captured = {}
    def plain_result(text):
        captured["text"] = text
        return text
    return SimpleNamespace(plain_result=plain_result), captured


def test_query_forks_prompts_when_no_repos_tracked():
    plugin, _ = build_plugin()
    event, captured = fake_event()

    result = asyncio.run(plugin.query_forks(event))

    assert "尚未收到 GitHub 事件" in captured["text"]
    assert result == captured["text"]


def test_query_forks_lists_all_tracked_repos():
    plugin, _ = build_plugin()
    plugin.source_repos["owner/a"] = None
    plugin.source_repos["owner/b"] = None
    plugin.github_api.get_repo = AsyncMock(side_effect=lambda repo: {
        "owner/a": {"forks_count": 12},
        "owner/b": {"forks_count": 3},
    }[repo])
    event, captured = fake_event()

    asyncio.run(plugin.query_forks(event))

    lines = captured["text"].splitlines()
    assert lines[0] == "📊 GitHub Fork Stats"
    assert "🍴 owner/a: 12 forks" in lines
    assert "🍴 owner/b: 3 forks" in lines


def test_query_forks_marks_failed_queries():
    plugin, _ = build_plugin()
    plugin.source_repos["owner/a"] = None
    plugin.github_api.get_repo = AsyncMock(return_value=None)
    event, captured = fake_event()

    asyncio.run(plugin.query_forks(event))

    assert "🍴 owner/a: ? forks" in captured["text"]
