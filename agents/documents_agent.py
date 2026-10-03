"""Document-folder MCP server for QueryCraft."""

import json
import os
import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP

DOCUMENTS_ROOT = Path(
    os.getenv("DOCUMENTS_ROOT", str(Path.home() / "Documents"))
).resolve()
READ_MAX_BYTES = int(os.getenv("DOC_READ_MAX_BYTES", str(1024 * 512)))
SEARCH_MAX_HITS = int(os.getenv("DOC_SEARCH_MAX_HITS", "20"))
ALLOWED_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst",
    ".py", ".js", ".ts", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".csv", ".log",
}


def _safe_path(filename: str) -> Path | None:
    clean = re.sub(r"[^\w\s\-\./]", "", filename).strip().lstrip("/")
    if not clean:
        return None
    resolved = (DOCUMENTS_ROOT / clean).resolve()
    try:
        resolved.relative_to(DOCUMENTS_ROOT)
    except ValueError:
        return None
    return resolved


mcp = FastMCP(
    "documents",
    instructions=(
        "Manages the user's Documents folder. Use list_documents to browse, "
        "read_document to read, search_documents to search, and "
        "write_document to create or update text files."
    ),
)


@mcp.tool()
def list_documents(subfolder: str = "") -> str:
    """List files in the configured documents folder."""
    target = _safe_path(subfolder) if subfolder else DOCUMENTS_ROOT
    if target is None:
        return json.dumps({"error": "Invalid path"})
    if not target.exists():
        return json.dumps({"error": f"Folder not found: {subfolder!r}"})
    if not target.is_dir():
        return json.dumps({"error": f"Not a directory: {subfolder!r}"})

    entries = []
    for item in sorted(target.iterdir()):
        if item.name.startswith("."):
            continue
        entries.append({
            "name": item.name,
            "type": "dir" if item.is_dir() else "file",
            "size": item.stat().st_size if item.is_file() else None,
            "extension": item.suffix if item.is_file() else None,
        })
    return json.dumps({"folder": str(target), "entries": entries}, ensure_ascii=False)


@mcp.tool()
def read_document(filename: str) -> str:
    """Read an allowed text document relative to DOCUMENTS_ROOT."""
    path = _safe_path(filename)
    if path is None:
        return json.dumps({"error": "Invalid or unsafe path"})
    if not path.exists():
        return json.dumps({"error": f"File not found: {filename!r}"})
    if not path.is_file():
        return json.dumps({"error": f"Not a file: {filename!r}"})
    if path.suffix.lower() not in ALLOWED_EXTENSIONS:
        return json.dumps({"error": f"File type not allowed: {path.suffix!r}"})

    size = path.stat().st_size
    if size > READ_MAX_BYTES:
        return json.dumps({
            "error": (
                f"File too large ({size} bytes, max {READ_MAX_BYTES}). "
                "Use search_documents to find specific content."
            )
        })

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        return json.dumps({
            "filename": filename,
            "size_bytes": size,
            "content": content,
        }, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": f"Read error: {type(exc).__name__}"})


@mcp.tool()
def search_documents(term: str, subfolder: str = "", file_extension: str = "") -> str:
    """Search text files under DOCUMENTS_ROOT for a plain-text term."""
    if not term or not term.strip():
        return json.dumps({"error": "Search term cannot be empty"})

    target = _safe_path(subfolder) if subfolder else DOCUMENTS_ROOT
    if target is None:
        return json.dumps({"error": "Invalid path"})
    if not target.exists() or not target.is_dir():
        return json.dumps({"error": "Search folder not found"})

    pattern = re.compile(re.escape(term.strip()[:200]), re.IGNORECASE)
    extension = file_extension.lower() if file_extension else None
    hits = []

    for path in sorted(target.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        if extension and path.suffix.lower() != extension:
            continue
        if path.stat().st_size > READ_MAX_BYTES:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        for line_number, line in enumerate(content.splitlines(), start=1):
            if pattern.search(line):
                hits.append({
                    "file": str(path.relative_to(DOCUMENTS_ROOT)),
                    "line": line_number,
                    "preview": line.strip()[:200],
                })
                if len(hits) >= SEARCH_MAX_HITS:
                    return json.dumps({
                        "term": term,
                        "hits": hits,
                        "truncated": True,
                    }, ensure_ascii=False)

    return json.dumps({"term": term, "hits": hits, "truncated": False}, ensure_ascii=False)


@mcp.tool()
def write_document(filename: str, content: str, confirm: bool = False) -> str:
    """Create or overwrite an allowed text document after confirm=True."""
    path = _safe_path(filename)
    if path is None:
        return json.dumps({"error": "Invalid or unsafe path"})
    if path.suffix.lower() not in ALLOWED_EXTENSIONS:
        return json.dumps({"error": f"File type not allowed: {path.suffix!r}"})
    if len(content.encode("utf-8")) > READ_MAX_BYTES:
        return json.dumps({"error": f"Content too large (max {READ_MAX_BYTES} bytes)"})

    exists = path.exists()
    if not confirm:
        return json.dumps({
            "preview": True,
            "filename": filename,
            "action": "overwrite" if exists else "create",
            "content_length": len(content),
            "message": "Call again with confirm=True to proceed.",
        })

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return json.dumps({
            "success": True,
            "filename": filename,
            "action": "overwritten" if exists else "created",
            "bytes_written": len(content.encode("utf-8")),
        })
    except Exception as exc:
        return json.dumps({"error": f"Write error: {type(exc).__name__}"})


if __name__ == "__main__":
    import logging
    import sys

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    mcp.run(transport="stdio")
