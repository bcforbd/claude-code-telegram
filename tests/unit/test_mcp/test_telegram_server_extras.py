"""Tests for the new Telegram MCP server tools."""

from pathlib import Path

from src.mcp.telegram_server import notify_user, send_document_to_user


class TestSendDocumentToUser:
    async def test_valid_document(self, tmp_path: Path) -> None:
        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"%PDF-1.4")
        result = await send_document_to_user(str(doc))
        assert "Document queued for delivery" in result
        assert "report.pdf" in result

    async def test_valid_document_with_caption(self, tmp_path: Path) -> None:
        doc = tmp_path / "data.csv"
        doc.write_text("a,b,c\n1,2,3")
        result = await send_document_to_user(str(doc), caption="Report")
        assert "Document queued for delivery" in result

    async def test_relative_path_rejected(self) -> None:
        result = await send_document_to_user("relative/report.pdf")
        assert "Error" in result
        assert "absolute" in result

    async def test_missing_file_rejected(self, tmp_path: Path) -> None:
        result = await send_document_to_user(str(tmp_path / "nope.pdf"))
        assert "Error" in result
        assert "not found" in result

    async def test_unsupported_extension_rejected(self, tmp_path: Path) -> None:
        weird = tmp_path / "file.exe"
        weird.write_bytes(b"MZ")
        result = await send_document_to_user(str(weird))
        assert "Error" in result
        assert "unsupported" in result

    async def test_supported_extensions(self, tmp_path: Path) -> None:
        for ext in [".pdf", ".docx", ".xlsx", ".zip", ".py", ".md", ".txt"]:
            f = tmp_path / f"file{ext}"
            f.write_bytes(b"\x00" * 10)
            result = await send_document_to_user(str(f))
            assert "queued" in result, ext


class TestNotifyUser:
    async def test_valid_message(self) -> None:
        result = await notify_user("Task complete!")
        assert "Notification queued for delivery" in result
        assert "Task complete!" in result

    async def test_empty_message_rejected(self) -> None:
        result = await notify_user("   ")
        assert "Error" in result
        assert "empty" in result

    async def test_too_long_rejected(self) -> None:
        result = await notify_user("x" * 5000)
        assert "Error" in result
        assert "too long" in result

    async def test_trimmed_message(self) -> None:
        result = await notify_user("  hello  ")
        assert "hello" in result
