"""Document reading and text chunking for the extraction pipeline."""

from __future__ import annotations

from pathlib import Path

SUPPORTED_EXTENSIONS = {
    ".txt", ".md", ".pdf", ".docx", ".csv", ".json", ".html", ".py",
}

TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".html", ".py"}


def discover_files(data_dir: Path, extensions: set[str] | None = None) -> list[Path]:
    """Recursively find all files with supported extensions."""
    exts = extensions or SUPPORTED_EXTENSIONS
    return sorted(
        f for f in data_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in exts
    )


def read_document(path: Path) -> str:
    """Read a document into plain text.

    Uses ``unstructured.partition`` for binary formats (PDF, DOCX),
    plain file read for text-based formats.
    """
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return path.read_text(encoding="utf-8", errors="replace")

    try:
        from unstructured.partition.auto import partition

        elements = partition(filename=str(path))
        return "\n\n".join(str(el) for el in elements)
    except ImportError:
        raise ImportError(
            f"Cannot read {path.suffix} files without the 'unstructured' package. "
            "Install it with: pip install 'unstructured[all-docs]'"
        )


def chunk_text(
    text: str,
    chunk_size: int = 4000,
    overlap: int = 200,
) -> list[str]:
    """Split text into overlapping chunks using paragraph-aware splitting.

    Splits on double-newline boundaries first, then merges paragraphs
    into chunks up to ``chunk_size`` characters with ``overlap`` characters
    carried forward between chunks.
    """
    if not text.strip():
        return []

    paragraphs = text.split("\n\n")
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para)

        if current_len + para_len + 2 > chunk_size and current:
            chunk_text_str = "\n\n".join(current)
            chunks.append(chunk_text_str)

            # Build overlap from the tail of the current chunk
            overlap_parts: list[str] = []
            overlap_len = 0
            for p in reversed(current):
                if overlap_len + len(p) > overlap:
                    break
                overlap_parts.insert(0, p)
                overlap_len += len(p) + 2

            current = overlap_parts
            current_len = overlap_len

        current.append(para)
        current_len += para_len + 2

    if current:
        chunks.append("\n\n".join(current))

    return chunks


def load_and_chunk(
    data_dir: Path,
    chunk_size: int = 4000,
    overlap: int = 200,
) -> list[dict]:
    """Discover, read, and chunk all documents in a directory.

    Returns a list of ``{source: str, chunk_index: int, text: str}`` dicts.
    """
    files = discover_files(data_dir)
    all_chunks: list[dict] = []

    for f in files:
        try:
            text = read_document(f)
        except Exception as exc:
            print(f"[!] Failed to read {f}: {exc}")
            continue

        chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
        for i, chunk in enumerate(chunks):
            all_chunks.append({
                "source": f.name,
                "chunk_index": i,
                "text": chunk,
            })

    return all_chunks
