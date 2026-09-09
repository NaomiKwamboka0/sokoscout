"""Verify the built PDF actually contains the text we think it does.

Chrome embeds subsetted Type0 fonts, so the bytes inside a content stream are
glyph ids, not characters. Reading them requires the /ToUnicode CMap that
travels with each font. This walks every CMap in the file, builds one combined
glyph to character map, then decodes the hex strings out of the page content
streams.

It is deliberately crude: it does not track which font is active, so it maps
every glyph id through the union of all CMaps. That is fine for the only
question being asked, which is whether given phrases survived into the PDF.

    python verify_pdf.py
"""

from __future__ import annotations

import re
import sys
import zlib
from pathlib import Path

PDF = Path(__file__).resolve().parent / "SokoScout_Proposal.pdf"

PHRASES = [
    "SokoScout",
    "Should you sell in Kenya",
    "Investment proposal",
    "Leonard Kinyera",
    "Kilimall",
    "Glovo",
    "Uber Eats",
    "Paystack",
    "Gikomba",
    "Ecart Services",
    "argon2id",
    "no model ever",
    "zero cost per answer",
    "36 Starter subscribers",
    "1,573",
    "194.20",
    "Data Protection Act 2019",
    "The ask",
]


def streams(data: bytes) -> list[bytes]:
    """Every stream in the file, inflated where it is Flate encoded."""
    out = []
    for m in re.finditer(rb"stream\r?\n", data):
        start = m.end()
        end = data.find(b"endstream", start)
        if end == -1:
            continue
        raw = data[start:end]
        try:
            out.append(zlib.decompress(raw))
        except zlib.error:
            out.append(raw)
    return out


def build_cmap(chunks: list[bytes]) -> dict[int, str]:
    """Combine every /ToUnicode CMap found into one glyph id to text map."""
    cmap: dict[int, str] = {}

    def decode_utf16be(h: str) -> str:
        try:
            return bytes.fromhex(h).decode("utf-16-be", errors="ignore")
        except ValueError:
            return ""

    for chunk in chunks:
        if b"beginbfchar" not in chunk and b"beginbfrange" not in chunk:
            continue
        text = chunk.decode("latin1")

        for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
            for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
                cmap[int(src, 16)] = decode_utf16be(dst)

        for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
            for lo, hi, dst in re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block
            ):
                start, stop, base = int(lo, 16), int(hi, 16), int(dst, 16)
                for i in range(stop - start + 1):
                    cmap[start + i] = chr(base + i)
    return cmap


def extract_text(chunks: list[bytes], cmap: dict[int, str]) -> str:
    """Pull hex string literals out of content streams and map them through."""
    pieces = []
    for chunk in chunks:
        if b"Tj" not in chunk and b"TJ" not in chunk:
            continue
        text = chunk.decode("latin1")
        for hexlit in re.findall(r"<([0-9A-Fa-f]{4,})>", text):
            glyphs = [int(hexlit[i : i + 4], 16) for i in range(0, len(hexlit) - 3, 4)]
            pieces.append("".join(cmap.get(g, "") for g in glyphs))
        pieces.append(" ")
    return " ".join(pieces)


def main() -> int:
    if not PDF.exists():
        print(f"Missing {PDF}", file=sys.stderr)
        return 1

    data = PDF.read_bytes()
    chunks = streams(data)
    cmap = build_cmap(chunks)
    text = extract_text(chunks, cmap)

    pages = len(re.findall(rb"/Type\s*/Page[^s]", data))
    squashed = re.sub(r"\s+", "", text)

    print(f"File      : {PDF.name}")
    print(f"Size      : {len(data) / 1024:,.0f} KB")
    print(f"Pages     : {pages}")
    print(f"CMap size : {len(cmap)} glyphs")
    print(f"Text      : {len(text):,} chars recovered")
    print()

    missing = []
    for phrase in PHRASES:
        needle = re.sub(r"\s+", "", phrase)
        if needle in squashed:
            print(f"  found    {phrase}")
        else:
            print(f"  MISSING  {phrase}")
            missing.append(phrase)

    print()
    if missing:
        print(f"{len(missing)} phrase(s) not found.")
        return 1
    print("All phrases present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
