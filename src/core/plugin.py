"""GitHub Webhook Plugin core implementation."""

import asyncio
import json
from aiohttp import web

from astrbot.api import all as api
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.api import logger

from .config import PluginConfig
from .constants import EVENT_TYPE_FORK
from ..handlers.fork_handler import handle_fork_event
from ..handlers.issues_handler import IssueNotifier
from ..handlers.pull_request_handler import handle_pull_request_event
from ..handlers.push_handler import handle_push_event
from ..handlers.repository_handler import handle_repository_event
from ..services.github_api import GitHubAPI
from ..utils.rate_limiter import RateLimiter
from ..utils.verify_signature import verify_signature


class GitHubWebhookPlugin(Star):
    """GitHub Webhook receiver plugin."""

    def __init__(self, context: Context, config):
        super().__init__(context)
        self.app = web.Application()
        self.app.router.add_post("/webhook", self.handle_webhook)
        self.runner = None
        self.site = None
        self.cfg = PluginConfig(config)
        self.issue_notifier = IssueNotifier(self)
        self.source_repos: dict[str, None] = {}
        self.github_api = GitHubAPI(self.cfg.github_token)

        if self.cfg.rate_limit > 0:
            self.rate_limiter = RateLimiter(max_requests=self.cfg.rate_limit)
        else:
            self.rate_limiter = None

    async def start_server(self):
        # Clean up any existing server instance
        if self.site:
            await self.site.stop()
            logger.info("GitHub Webhook: Cleaned up existing server instance")
        if self.runner:
            await self.runner.cleanup()
            logger.info("GitHub Webhook: Cleaned up existing runner")

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "0.0.0.0", self.cfg.port)
        await self.site.start()
        logger.info(f"GitHub Webhook: Server started on port {self.cfg.port}")

    async def handle_webhook(self, request: web.Request):
        event_type = request.headers.get("X-GitHub-Event", "unknown")
        signature = request.headers.get("X-Hub-Signature-256", "")
        # 尽早记录事件类型，确保限流、读取或解析失败时也能追踪请求来源。
        logger.info(f"GitHub Webhook: Received event type: {event_type}")

        # Rate limiting check
        if self.rate_limiter:
            is_allowed, retry_after = await self.rate_limiter.is_allowed()
            if not is_allowed:
                current, max_req = self.rate_limiter.get_usage()
                logger.warning(
                    f"GitHub Webhook: Rate limit exceeded "
                    f"({current}/{max_req} requests/minute)"
                )
                return web.Response(
                    status=429,
                    text=f"Rate limit exceeded. Retry after {retry_after} seconds.",
                    headers={
                        "Retry-After": str(retry_after),
                        "X-RateLimit-Limit": str(max_req),
                        "X-RateLimit-Remaining": str(max_req - current),
                        "X-RateLimit-Reset": str(retry_after),
                    },
                )

        # 读取和解析分开处理，便于日志准确区分请求读取失败与数据格式错误。
        try:
            payload_bytes = await request.read()
        except Exception as e:
            logger.error(
                "GitHub Webhook: Failed to read request body for event "
                f"'{event_type}': {type(e).__name__}: {e}"
            )
            return web.Response(status=400, text="Failed to read request")

        try:
            data = json.loads(payload_bytes.decode("utf-8"))
        except UnicodeDecodeError as e:
            # 请求体不是合法 UTF-8 时，记录具体的解码原因。
            logger.error(
                "GitHub Webhook: Failed to decode payload for event "
                f"'{event_type}': {type(e).__name__}: {e}"
            )
            return web.Response(status=400, text="Invalid UTF-8 payload")
        except json.JSONDecodeError as e:
            # JSON 语法错误附带行列位置，方便定位 GitHub payload 问题。
            logger.error(
                "GitHub Webhook: Failed to parse JSON for event "
                f"'{event_type}': {e.msg} (line {e.lineno}, column {e.colno})"
            )
            return web.Response(status=400, text="Invalid JSON")
        except Exception as e:
            logger.error(
                "GitHub Webhook: Failed to parse payload for event "
                f"'{event_type}': {type(e).__name__}: {e}"
            )
            return web.Response(status=400, text="Invalid payload")

        # Signature verification
        if self.cfg.webhook_secret and signature:
            if not verify_signature(payload_bytes, signature, self.cfg.webhook_secret):
                logger.warning("GitHub Webhook: Invalid signature - request rejected")
                return web.Response(status=401, text="Invalid signature")

        if event_type == "ping":
            return web.Response(text="Pong")

        # Remember source repositories for the /github_forks command
        if isinstance(data, dict):
            repo_full_name = data.get("repository", {}).get("full_name")
            if repo_full_name:
                self.source_repos.setdefault(repo_full_name, None)

        message = None

        try:
            if event_type == "push":
                message = await handle_push_event(data, self.context)
            elif event_type == "issues":
                message = await self.issue_notifier.handle(data)
            elif event_type == "pull_request":
                message = await handle_pull_request_event(data, self.context)
            elif event_type == EVENT_TYPE_FORK:
                message = await handle_fork_event(data, self.context)
            elif event_type == "repository":
                message = await handle_repository_event(data, self.context)
            else:
                # 暂未支持的事件仍返回成功，但明确记录没有对应处理器。
                logger.warning(
                    "GitHub Webhook: No handler registered for event type "
                    f"'{event_type}' (event not handled)"
                )
        except Exception as e:
            logger.error(f"GitHub Webhook: Error processing event: {e}", exc_info=True)
            return web.Response(status=500, text="Internal server error")

        if message:
            await self.dispatch_message(message, data, event_type)

        return web.Response(status=200, text="OK")

    async def dispatch_message(self, message: str, data: dict, event_type: str):
        """Send a formatted message, optionally passing it through the LLM."""
        if self.cfg.enable_agent:
            from ..services.llm_service import send_with_agent

            await send_with_agent(self, message, data, event_type)
        else:
            await self.send_message(message)

    async def send_message(self, message: str):
        if not self.cfg.target_umo:
            logger.error(
                "GitHub Webhook: Cannot send message - target_umo not configured"
            )
            return

        try:
            message_chain = api.MessageChain([Plain(message)])
            result = await self.context.send_message(self.cfg.target_umo, message_chain)
            logger.info(
                f"GitHub Webhook: Message sent to {self.cfg.target_umo}, result: {result}"
            )
            if not result:
                logger.warning(
                    f"GitHub Webhook: Platform not found for {self.cfg.target_umo}"
                )
        except Exception as e:
            # 记录完整错误信息但不传播异常
            logger.error(f"GitHub Webhook: Failed to send message: {e}")
            logger.error(f"GitHub Webhook: Error type: {type(e).__name__}")
            logger.error(f"GitHub Webhook: Error details: {str(e)}")

    async def query_forks(self, event):
        """Handler for the /github_forks command: list all tracked repos with fork counts."""
        if not self.source_repos:
            return event.plain_result(
                "GitHub Webhook: 尚未收到 GitHub 事件，无法确定要查询的仓库"
            )

        async def _forks(repo: str) -> str:
            info = await self.github_api.get_repo(repo)
            forks = info.get("forks_count", "?") if info else "?"
            return f"🍴 {repo}: {forks} forks"

        lines = await asyncio.gather(
            *(_forks(repo) for repo in self.source_repos)
        )
        return event.plain_result(
            "📊 GitHub Fork Stats\n" + "\n".join(lines)
        )

    async def terminate(self):
        logger.info("GitHub Webhook: Shutting down server...")
        await self.issue_notifier.shutdown()
        await self.github_api.close()
        if self.site:
            await self.site.stop()
            logger.info("GitHub Webhook: Server stopped")
        if self.runner:
            await self.runner.cleanup()
            logger.info("GitHub Webhook: Runner cleaned up")
