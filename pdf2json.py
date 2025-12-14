#!/usr/bin/env python3
"""
Convert folder of PDFs to JSON with extracted text.
Usage: python pdf2json.py ./downloads/А60-21280-2023/ -o case_texts.json
"""

import argparse
import json
import sys
from pathlib import Path

try:
    import fitz  # pymupdf
except ImportError:
    print("pymupdf not installed. Run: pip install pymupdf")
    sys.exit(1)


def extract_text_from_pdf(pdf_path: Path) -> str:
    """Extract all text from PDF file."""
    try:
        doc = fitz.open(pdf_path)
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text())
        doc.close()
        return "\n".join(text_parts).strip()
    except Exception as e:
        print(f"  ⚠️  Error reading {pdf_path.name}: {e}")
        return ""


def main():
    parser = argparse.ArgumentParser(description="Convert PDFs to JSON")
    parser.add_argument("folder", type=Path, help="Folder with PDF files")
    parser.add_argument("-o", "--output", type=Path, default=None, help="Output JSON file")
    args = parser.parse_args()

    folder = args.folder
    if not folder.exists():
        print(f"❌ Folder not found: {folder}")
        sys.exit(1)

    # Find all PDFs
    pdfs = sorted(folder.glob("*.pdf"))
    if not pdfs:
        print(f"❌ No PDF files found in {folder}")
        sys.exit(1)

    print(f"📁 Found {len(pdfs)} PDF(s) in {folder}")

    # Extract text
    result = {}
    total_chars = 0

    for i, pdf_path in enumerate(pdfs, 1):
        print(f"[{i}/{len(pdfs)}] {pdf_path.name[:50]}...", end=" ")
        text = extract_text_from_pdf(pdf_path)
        result[pdf_path.name] = text
        chars = len(text)
        total_chars += chars
        print(f"({chars:,} chars)")

    # Output path
    output_path = args.output or folder / "texts.json"

    # Save JSON
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # Stats
    json_size = output_path.stat().st_size
    pdf_total = sum(p.stat().st_size for p in pdfs)

    print(f"\n{'=' * 50}")
    print(f"✅ Saved: {output_path}")
    print(f"📊 Stats:")
    print(f"   PDFs total:  {pdf_total / 1024 / 1024:.2f} MB")
    print(f"   JSON size:   {json_size / 1024 / 1024:.2f} MB")
    print(f"   Compression: {pdf_total / json_size:.1f}x smaller")
    print(f"   Total chars: {total_chars:,}")
    print(f"   ~Tokens:     {total_chars // 4:,} (rough estimate)")


if __name__ == "__main__":
    main()