"""Session export functionality for exporting chat history in various formats."""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Dict, List

from src.storage.facade import Storage
from src.utils.constants import MAX_SESSION_LENGTH


class ExportFormat(Enum):
    """Supported export formats."""

    MARKDOWN = "markdown"
    JSON = "json"
    HTML = "html"


@dataclass
class ExportedSession:
    """Exported session data."""

    format: ExportFormat
    content: str
    filename: str
    mime_type: str
    size_bytes: int
    created_at: datetime


def _normalize_messages(history: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalize stored messages into (role, content, timestamp) dicts.

    Stored ``MessageModel`` dicts use ``prompt`` (user) / ``response``
    (assistant) / ``timestamp``; the exporter renders them as a flat
    chronological list with a ``role`` field.
    """
    messages: List[Dict[str, Any]] = []
    for msg in history.get("messages", []) or []:
        timestamp = msg.get("timestamp") or ""
        if msg.get("prompt"):
            messages.append(
                {"role": "user", "content": msg["prompt"], "created_at": timestamp}
            )
        if msg.get("response"):
            messages.append(
                {
                    "role": "assistant",
                    "content": msg["response"],
                    "created_at": timestamp,
                }
            )
    return messages


class SessionExporter:
    """Handles exporting chat sessions in various formats."""

    def __init__(self, storage: Storage):
        """Initialize exporter with storage dependency.

        Args:
            storage: Storage facade for session data access
        """
        self.storage = storage

    async def export_session(
        self,
        user_id: int,
        session_id: str,
        format: ExportFormat = ExportFormat.MARKDOWN,
    ) -> ExportedSession:
        """Export a session in the specified format.

        Args:
            user_id: User ID
            session_id: Session ID to export
            format: Export format (markdown, json, html)

        Returns:
            ExportedSession with exported content

        Raises:
            ValueError: If session not found or invalid format
        """
        # Get session data (session + messages + tool usage)
        history = await self.storage.get_session_history(
            session_id, limit=MAX_SESSION_LENGTH
        )
        if not history or not history.get("session"):
            raise ValueError(f"Session {session_id} not found")

        session: Dict[str, Any] = history["session"]
        # Enforce ownership: only the session owner may export it
        if session.get("user_id") not in (None, user_id):
            raise ValueError("Session not found")

        messages = _normalize_messages(history)

        # Export based on format
        if format == ExportFormat.MARKDOWN:
            content = self._export_markdown(session, messages)
            mime_type = "text/markdown"
            extension = "md"
        elif format == ExportFormat.JSON:
            content = self._export_json(session, messages)
            mime_type = "application/json"
            extension = "json"
        elif format == ExportFormat.HTML:
            content = self._export_html(session, messages)
            mime_type = "text/html"
            extension = "html"
        else:
            raise ValueError(f"Unsupported export format: {format}")

        # Create filename
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        session_id_str = str(session.get("session_id") or session_id)
        filename = f"session_{session_id_str[:8]}_{timestamp}.{extension}"

        return ExportedSession(
            format=format,
            content=content,
            filename=filename,
            mime_type=mime_type,
            size_bytes=len(content.encode()),
            created_at=datetime.now(UTC),
        )

    def _session_id(self, session: Dict[str, Any]) -> str:
        return str(session.get("session_id") or "")

    def _session_created(self, session: Dict[str, Any]) -> str:
        return str(session.get("created_at") or "")

    def _session_updated(self, session: Dict[str, Any]) -> str:
        return str(session.get("last_used") or session.get("updated_at") or "")

    def _export_markdown(self, session: dict, messages: list) -> str:
        """Export session as Markdown.

        Args:
            session: Session metadata
            messages: List of normalized messages

        Returns:
            Markdown formatted content
        """
        lines = []

        # Header
        lines.append("# Claude Code Session Export")
        lines.append(f"\n**Session ID:** `{self._session_id(session)}`")
        lines.append(f"**Created:** {self._session_created(session)}")
        if self._session_updated(session):
            lines.append(f"**Last Updated:** {self._session_updated(session)}")
        lines.append(f"**Message Count:** {len(messages)}")
        lines.append("\n---\n")

        # Messages
        for msg in messages:
            timestamp = msg.get("created_at") or ""
            role = "You" if msg["role"] == "user" else "Claude"
            content = msg["content"]

            lines.append(f"### {role} - {timestamp}")
            lines.append(f"\n{content}\n")
            lines.append("---\n")

        return "\n".join(lines)

    def _export_json(self, session: dict, messages: list) -> str:
        """Export session as JSON.

        Args:
            session: Session metadata
            messages: List of normalized messages

        Returns:
            JSON formatted content
        """
        export_data = {
            "session": {
                "id": self._session_id(session),
                "user_id": session.get("user_id"),
                "project_path": session.get("project_path"),
                "created_at": self._session_created(session),
                "last_used": self._session_updated(session),
                "total_cost": session.get("total_cost", 0.0),
                "total_turns": session.get("total_turns", 0),
                "message_count": session.get("message_count", 0),
            },
            "messages": [
                {
                    "role": msg["role"],
                    "content": msg["content"],
                    "created_at": msg.get("created_at"),
                }
                for msg in messages
            ],
        }

        return json.dumps(export_data, indent=2, ensure_ascii=False)

    def _export_html(self, session: dict, messages: list) -> str:
        """Export session as HTML.

        Args:
            session: Session metadata
            messages: List of normalized messages

        Returns:
            HTML formatted content
        """
        # Convert markdown content to HTML-safe format
        markdown_content = self._export_markdown(session, messages)
        html_content = self._markdown_to_html(markdown_content)
        # HTML template
        template = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Claude Code Session - {self._session_id(session)[:8]}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .container {{
            background-color: white;
            padding: 30px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        h1 {{
            color: #2c3e50;
            border-bottom: 3px solid #3498db;
            padding-bottom: 10px;
        }}
        h3 {{
            color: #34495e;
            margin-top: 20px;
        }}
        code {{
            background-color: #f8f8f8;
            padding: 2px 6px;
            border-radius: 3px;
            font-family: 'Courier New', monospace;
        }}
        pre {{
            background-color: #f8f8f8;
            padding: 15px;
            border-radius: 5px;
            overflow-x: auto;
            border: 1px solid #e1e4e8;
        }}
        .metadata {{
            background-color: #f0f7ff;
            padding: 15px;
            border-radius: 5px;
            margin-bottom: 20px;
        }}
        .message {{
            margin: 20px 0;
            padding: 15px;
            border-left: 4px solid #3498db;
            background-color: #f9f9f9;
        }}
        .message.claude {{
            border-left-color: #2ecc71;
        }}
        .timestamp {{
            color: #7f8c8d;
            font-size: 0.9em;
        }}
        hr {{
            border: none;
            border-top: 1px solid #e1e4e8;
            margin: 30px 0;
        }}
    </style>
</head>
<body>
    <div class="container">
        {html_content}
    </div>
</body>
</html>"""

        return template

    def _markdown_to_html(self, markdown: str) -> str:
        """Convert markdown to HTML.

        Simple conversion for basic markdown elements.

        Args:
            markdown: Markdown content

        Returns:
            HTML content
        """
        html = markdown

        # Headers
        html = html.replace("# ", "<h1>").replace("\n\n", "</h1>\n\n", 1)
        html = html.replace("### ", "<h3>").replace("\n", "</h3>\n", 3)

        # Bold
        html = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", html)

        # Code blocks
        html = re.sub(r"`([^`]+)`", r"<code>\1</code>", html)

        # Line breaks and paragraphs
        html = html.replace("\n\n", "</p>\n<p>")
        html = f"<p>{html}</p>"

        # Clean up empty paragraphs
        html = html.replace("<p></p>", "")
        html = html.replace("<p><h", "<h")
        html = html.replace("</h1></p>", "</h1>")
        html = html.replace("</h3></p>", "</h3>")

        # Horizontal rules
        html = html.replace("<p>---</p>", "<hr>")

        return html
