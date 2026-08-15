"""MCP server exposing Telegram-specific tools to Claude.

Runs as a stdio transport server. Tools validate inputs and return success
strings; actual Telegram delivery is handled by the bot's stream callback,
which intercepts tool calls and performs the real send.
"""

from pathlib import Path

from mcp.server.fastmcp import FastMCP

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".doc",
    ".xlsx",
    ".xls",
    ".pptx",
    ".ppt",
    ".txt",
    ".csv",
    ".json",
    ".md",
    ".zip",
    ".py",
    ".js",
    ".ts",
}

mcp = FastMCP("telegram")


def _validate_file(file_path: str, allowed: set, kind: str) -> tuple:
    """Validate an absolute, existing file with an allowed extension.

    Returns (ok, error_or_path).
    """
    path = Path(file_path)
    if not path.is_absolute():
        return False, f"Error: path must be absolute, got '{file_path}'"
    if path.suffix.lower() not in allowed:
        return (
            False,
            f"Error: unsupported {kind} extension '{path.suffix}'. "
            f"Supported: {', '.join(sorted(allowed))}",
        )
    if not path.is_file():
        return False, f"Error: file not found: {file_path}"
    return True, path


@mcp.tool()
async def send_image_to_user(file_path: str, caption: str = "") -> str:
    """Send an image file to the Telegram user.

    Args:
        file_path: Absolute path to the image file.
        caption: Optional caption to display with the image.

    Returns:
        Confirmation string when the image is queued for delivery.
    """
    ok, result = _validate_file(file_path, IMAGE_EXTENSIONS, "image")
    if not ok:
        return result  # type: ignore[return-value]
    path = result  # type: ignore[assignment]
    return f"Image queued for delivery: {path.name}"


@mcp.tool()
async def send_document_to_user(file_path: str, caption: str = "") -> str:
    """Send a document file (PDF, DOCX, XLSX, ZIP, code, etc.) to the Telegram user.

    Args:
        file_path: Absolute path to the document file.
        caption: Optional caption to display with the document.

    Returns:
        Confirmation string when the document is queued for delivery.
    """
    ok, result = _validate_file(file_path, DOCUMENT_EXTENSIONS, "document")
    if not ok:
        return result  # type: ignore[return-value]
    path = result  # type: ignore[assignment]
    return f"Document queued for delivery: {path.name}"


@mcp.tool()
async def notify_user(message: str) -> str:
    """Send a short notification message to the Telegram user.

    Useful for alerting the user when a long-running task completes or when
    their input is needed.

    Args:
        message: The notification text to send.

    Returns:
        Confirmation string when the notification is queued for delivery.
    """
    text = message.strip()
    if not text:
        return "Error: message must not be empty"
    if len(text) > 4000:
        return "Error: message too long (max 4000 characters)"
    return f"Notification queued for delivery: {text[:80]}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
