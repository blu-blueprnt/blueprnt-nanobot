"""Email tools that proxy through the Blueprnt API."""

import json
import os
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from nanobot.agent.tools.base import Tool

BLUEPRNT_API_URL = os.environ.get("BLUEPRNT_API_URL", "https://api-production-0610.up.railway.app")
AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "")


def _api_call(endpoint: str, payload: dict) -> dict:
    """Make an authenticated call to the Blueprnt API."""
    url = f"{BLUEPRNT_API_URL}{endpoint}"
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Agent-Token", AGENT_TOKEN)

    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"error": f"HTTP {e.code}: {body[:500]}"}


class EmailReadTool(Tool):
    """Read emails from the user's connected email account."""

    @property
    def name(self) -> str:
        return "email_read"

    @property
    def description(self) -> str:
        return (
            "Read emails from the user's connected Gmail account. "
            "Can list recent emails or search by query. "
            "Returns subject, sender, date, and body preview."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Gmail search query (e.g. 'from:john@example.com', 'is:unread', 'subject:invoice'). Leave empty for recent emails.",
                },
                "maxResults": {
                    "type": "integer",
                    "description": "Maximum number of emails to return (default 10, max 50).",
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            "required": [],
        }

    async def execute(self, query: str = "", maxResults: int = 10, **kwargs) -> str:
        result = _api_call("/api/email/read", {
            "query": query,
            "maxResults": maxResults,
        })

        if "error" in result:
            return f"Error: {result['error']}"

        return json.dumps(result, indent=2)


class EmailSendTool(Tool):
    """Send an email from the user's connected email account."""

    @property
    def name(self) -> str:
        return "email_send"

    @property
    def description(self) -> str:
        return (
            "Send an email from the user's connected Gmail account. "
            "Always confirm with the user before sending."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient email address.",
                },
                "subject": {
                    "type": "string",
                    "description": "Email subject line.",
                },
                "body": {
                    "type": "string",
                    "description": "Email body (plain text).",
                },
                "replyTo": {
                    "type": "string",
                    "description": "Message ID to reply to (optional, for threading).",
                },
            },
            "required": ["to", "subject", "body"],
        }

    async def execute(self, to: str = "", subject: str = "", body: str = "", replyTo: str = "", **kwargs) -> str:
        payload: dict[str, Any] = {"to": to, "subject": subject, "body": body}
        if replyTo:
            payload["replyTo"] = replyTo

        result = _api_call("/api/email/send", payload)

        if "error" in result:
            return f"Error: {result['error']}"

        return json.dumps(result, indent=2)


class EmailIntegrationsTool(Tool):
    """Check what integrations and permissions the user has enabled."""

    @property
    def name(self) -> str:
        return "check_integrations"

    @property
    def description(self) -> str:
        return (
            "Check what integrations the user has connected and what permissions are enabled. "
            "Use this before attempting email operations to verify access."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(self, **kwargs) -> str:
        result = _api_call("/api/integrations", {})
        # GET endpoint, but we're using POST — let me use GET
        url = f"{BLUEPRNT_API_URL}/api/integrations"
        req = Request(url)
        req.add_header("X-Agent-Token", AGENT_TOKEN)

        try:
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
                return json.dumps(data, indent=2)
        except HTTPError as e:
            body = e.read().decode()
            return f"Error: HTTP {e.code}: {body[:500]}"
