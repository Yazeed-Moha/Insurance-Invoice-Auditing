"""General Markdown-aware contract chunking.

Chunks preserve document and section context. Tables are split only between
rows and repeat their header, while prose is split on paragraph/sentence
boundaries. The implementation contains no hospital-specific rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re


@dataclass(frozen=True)
class ContractChunk:
    chunk_id: str
    source_file: str
    sections: tuple[str, ...]
    content_types: tuple[str, ...]
    content: str

    @property
    def char_count(self) -> int:
        return len(self.render())

    def render(self) -> str:
        section_text = " > ".join(self.sections) if self.sections else "document preamble"
        return (
            f"CHUNK ID: {self.chunk_id}\n"
            f"SOURCE FILE: {self.source_file}\n"
            f"SECTIONS: {section_text}\n"
            f"CONTENT TYPES: {', '.join(self.content_types)}\n\n"
            f"{self.content}"
        )


@dataclass(frozen=True)
class _Unit:
    source_file: str
    sections: tuple[str, ...]
    content_type: str
    content: str


def _heading_path(headings: dict[int, str]) -> tuple[str, ...]:
    return tuple(headings[level] for level in sorted(headings))


def _logical_units(path: Path) -> list[_Unit]:
    lines = path.read_text(encoding="utf-8").splitlines()
    headings: dict[int, str] = {}
    units: list[_Unit] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            content = "\n".join(paragraph).strip()
            if content:
                units.append(_Unit(path.name, _heading_path(headings), "prose", content))
            paragraph.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            headings[level] = heading.group(2)
            for deeper in [key for key in headings if key > level]:
                del headings[deeper]
            index += 1
            continue
        if line.lstrip().startswith("|"):
            flush_paragraph()
            table: list[str] = []
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                table.append(lines[index])
                index += 1
            units.append(_Unit(path.name, _heading_path(headings), "table", "\n".join(table)))
            continue
        if not line.strip():
            flush_paragraph()
        else:
            paragraph.append(line)
        index += 1
    flush_paragraph()
    return units


def _context_prefix(unit: _Unit) -> str:
    if not unit.sections:
        return ""
    return "\n".join(f"{'#' * (index + 1)} {heading}" for index, heading in enumerate(unit.sections)) + "\n\n"


def _split_table(unit: _Unit, max_chars: int, max_table_rows: int) -> list[_Unit]:
    lines = unit.content.splitlines()
    if len(lines) <= 2:
        return [unit]
    header = lines[:2]
    rows = lines[2:]
    prefix = _context_prefix(unit)
    batches: list[_Unit] = []
    current: list[str] = []
    for row in rows:
        candidate = "\n".join([*header, *current, row])
        rendered_size = len(prefix) + len(candidate) + 220
        if current and (len(current) >= max_table_rows or rendered_size > max_chars):
            batches.append(_Unit(unit.source_file, unit.sections, "table", prefix + "\n".join([*header, *current])))
            current = [row]
        else:
            current.append(row)
    if current:
        batches.append(_Unit(unit.source_file, unit.sections, "table", prefix + "\n".join([*header, *current])))
    return batches


def _split_prose(unit: _Unit, max_chars: int) -> list[_Unit]:
    prefix = _context_prefix(unit)
    available = max(500, max_chars - len(prefix) - 220)
    if len(unit.content) <= available:
        return [_Unit(unit.source_file, unit.sections, unit.content_type, prefix + unit.content)]
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", unit.content)
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > available:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(sentence[start:start + available] for start in range(0, len(sentence), available))
            continue
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > available:
            pieces.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        pieces.append(current)
    return [_Unit(unit.source_file, unit.sections, unit.content_type, prefix + piece) for piece in pieces]


def chunk_contract(paths: list[Path], max_chars: int = 12_000,
                   max_table_rows: int = 20) -> list[ContractChunk]:
    """Create bounded, stable chunks from any Markdown contract documents."""
    split_units: list[_Unit] = []
    for path in sorted(paths):
        for unit in _logical_units(path):
            if unit.content_type == "table":
                split_units.extend(_split_table(unit, max_chars, max_table_rows))
            else:
                split_units.extend(_split_prose(unit, max_chars))

    groups: list[list[_Unit]] = []
    for unit in split_units:
        if not groups or groups[-1][-1].source_file != unit.source_file:
            groups.append([unit])
            continue
        # Keep table batches separate so the response-size limit expressed by
        # max_table_rows is preserved after packing.
        if unit.content_type == "table" and groups[-1][-1].content_type == "table":
            groups.append([unit])
            continue
        candidate = "\n\n".join(item.content for item in [*groups[-1], unit])
        # Reserve space for chunk id, source, section path, and type labels.
        if len(candidate) + 600 <= max_chars:
            groups[-1].append(unit)
        else:
            groups.append([unit])

    chunks: list[ContractChunk] = []
    for index, group in enumerate(groups, start=1):
        content = "\n\n".join(unit.content for unit in group)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:10]
        sections = tuple(dict.fromkeys(section for unit in group for section in unit.sections))
        content_types = tuple(dict.fromkeys(unit.content_type for unit in group))
        stem = Path(group[0].source_file).stem
        chunks.append(ContractChunk(
            chunk_id=f"{stem}-{index:03d}-{digest}", source_file=group[0].source_file,
            sections=sections, content_types=content_types, content=content,
        ))
    return chunks


def save_chunks(chunks: list[ContractChunk], output_root: Path, hospital_id: str,
                source_hash: str, max_chars: int, max_table_rows: int) -> Path:
    """Persist the exact model inputs and a machine-readable manifest."""
    version_dir = output_root / f"hospital_{hospital_id.removeprefix('H')}" / source_hash[:12]
    version_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for index, chunk in enumerate(chunks, start=1):
        filename = f"{index:03d}-{chunk.chunk_id}.md"
        path = version_dir / filename
        path.write_text(chunk.render() + "\n", encoding="utf-8")
        records.append({
            "index": index,
            "chunk_id": chunk.chunk_id,
            "file": filename,
            "source_file": chunk.source_file,
            "sections": list(chunk.sections),
            "content_types": list(chunk.content_types),
            "char_count": chunk.char_count,
            "content_sha256": hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
        })
    manifest = {
        "hospital_id": hospital_id,
        "source_hash": source_hash,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "max_chars": max_chars,
        "max_table_rows": max_table_rows,
        "chunk_count": len(chunks),
        "chunks": records,
    }
    temporary = version_dir / "manifest.tmp"
    temporary.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(version_dir / "manifest.json")
    return version_dir
