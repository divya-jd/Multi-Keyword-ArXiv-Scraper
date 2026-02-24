#!/usr/bin/env python3
"""
ArXiv Multi-Keyword Quantum Paper Scraper with Image Extraction & Captions
(Optimized for 5000–6000 papers)

Downloads latest quantum-related papers from arXiv across ~200 keyword
queries, extracts well-formatted images from PDFs, and captures figure
captions from the surrounding text.

Optimizations:
  • Keyword batching to stay within arXiv URL limits
  • Cross-batch deduplication by arXiv ID
  • Concurrent image extraction via ThreadPoolExecutor
  • Streaming CSV metadata (no large in-memory lists)
  • Checkpoint/resume via progress file — safe to interrupt & restart
  • Sampled blank-detection for speed on large images
  • Image filenames include paper ID for full traceability
  • Figure caption extraction from PDF text blocks
"""

import argparse
import csv
import io
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import arxiv
import fitz  # PyMuPDF
import requests
from PIL import Image
from tqdm import tqdm


# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────
PDF_DIR = "quantum_arxiv_papers"
IMAGE_DIR = "extracted_images"
METADATA_FILE = "metadata.csv"
IMAGE_DESC_FILE = "image_descriptions.csv"
PROGRESS_FILE = "progress.json"

# ArXiv rate limiting
DOWNLOAD_DELAY_SEC = 1        # seconds between PDF downloads (1s is arXiv-safe)
API_PAGE_SIZE = 50             # results per API page (arXiv rate-limit friendly)

# Image quality filters
MIN_IMAGE_WIDTH = 200          # pixels
MIN_IMAGE_HEIGHT = 200         
MIN_IMAGE_SIZE_BYTES = 5_000   # 5 KB
MAX_WHITE_RATIO = 0.95         # skip images >95% white
ACCEPTED_EXTENSIONS = {"png", "jpeg", "jpg", "tiff", "tif"}

# Performance
EXTRACTION_WORKERS = 8         # threads for parallel image extraction
BLANK_SAMPLE_SIZE = 2000       # pixels to sample for blank detection (vs all)

# Retry settings
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 5         # seconds

# Keyword batching — arXiv limits URL length; ~15 keywords per batch
KEYWORDS_PER_BATCH = 15
RESULTS_PER_BATCH = 500        # papers to request per keyword batch


# ──────────────────────────────────────────────────────────────────────
# Keywords
# ──────────────────────────────────────────────────────────────────────
QUANTUM_KEYWORDS = [
    "Adiabatic Theorem", "Bosonic Creutz Ladder", "Dicke Model",
    "Distributed quantum computation", "Fault-tolerant quantum computation",
    "gray zone assault", "Hadamard Gate", "Harrow Hassidim Lloyd",
    "HHL algorithm", "ion traps", "Josephson junctions", "neutral atoms",
    "Noisy Intermediate-Scale Quantum era", "Open Quantum Systems",
    "Photonic Quantum Computing", "QAOA", "qbits", "qbytes", "QEC",
    "QNLP", "QSVM", "qtrits", "Quantum accelerators", "quantum annealing",
    "Quantum Advantage", "quantum algorithms", "Quantum applications",
    "quantum approaches", "quantum approximate optimization",
    "quantum approximate optimization algorithms", "quantum arithmetic",
    "Quantum artificial intelligence", "quantum backtracking",
    "quantum bits", "Quantum Bosonic Systems", "quantum bytes",
    "quantum chaos", "quantum chemistry", "Quantum circuits",
    "quantum classifier", "quantum communication", "quantum compiler",
    "Quantum complexity", "Quantum component", "Quantum computation",
    "Quantum computational", "Quantum computer",
    "Quantum Computing Architectures", "Quantum Control",
    "quantum correlation", "Quantum cryptanalysis",
    "Quantum cryptoalgorithm", "Quantum cryptog",
    "Quantum cryptographic", "Quantum cryptology",
    "Quantum cryptosystem", "Quantum decoding",
    "quantum devices", "quantum distillation", "quantum dots",
    "quantum dynamics", "quantum eigensolver", "quantum encryption",
    "Quantum Entanglement", "quantum entanglement distillation",
    "quantum error correction", "Quantum Error Detection",
    "Quantum Field theory", "quantum Fourier transform",
    "Quantum gas sensors", "Quantum gate", "Quantum gate fidelity",
    "Quantum gate-based", "Quantum gates", "quantum Grover",
    "Quantum hardware", "Quantum hardware security",
    "quantum image sensor", "quantum imager array",
    "quantum information", "Quantum information processing",
    "Quantum information science", "Quantum information systems",
    "Quantum information theory", "Quantum interference",
    "Quantum ions", "quantum Josza", "Quantum Kernel",
    "Quantum key", "Quantum key distribution",
    "Quantum key distribution network",
    "Quantum key distribution protocol",
    "Quantum key distribution systems", "quantum key exchange",
    "Quantum LDPC Codes", "Quantum Linear Optics",
    "quantum logic", "quantum machine learning",
    "quantum machines", "quantum magic states", "Quantum Maps",
    "Quantum Measurement", "Quantum Memristors",
    "Quantum metrological", "Quantum metrology",
    "Quantum metrology standards", "Quantum Monte Carlo",
    "Quantum Natural Language Processing", "Quantum Networks",
    "quantum neural networks", "Quantum Oscillator",
    "quantum phase amplifiers",
    "Quantum precision measurement sensors", "Quantum process",
    "Quantum processing", "Quantum Programs", "Quantum proof",
    "quantum public key", "Quantum Quantile Mechanics",
    "Quantum Quantizer", "Quantum random number",
    "Quantum random number generation",
    "Quantum random number generation device",
    "Quantum random number generator",
    "Quantum random number sequences", "Quantum safe network",
    "Quantum sensing", "Quantum sensing technology",
    "Quantum sensor", "Quantum sensor networks",
    "quantum sensors", "Quantum shared key", "quantum Shor",
    "Quantum Signal Processing", "quantum simulation",
    "Quantum Simulator", "quantum single photon",
    "quantum software", "Quantum Speedup", "quantum spin",
    "quantum spintronics", "quantum state", "Quantum Subroutines",
    "Quantum superposition", "quantum supremacy",
    "quantum switches", "quantum systems", "Quantum Technologies",
    "quantum teleportation", "quantum tensor",
    "quantum toffoli gate", "Quantum transmons",
    "quantum variational", "quantum video", "Quantum Week",
    "Quantum-enhanced", "Quantum-resistance", "qubits", "qubytes",
    "qudits", "qumodes", "qutrits", "silicon qubits", "VQE",
]


# ──────────────────────────────────────────────────────────────────────
# Progress tracker  (checkpoint / resume)
# ──────────────────────────────────────────────────────────────────────
class ProgressTracker:
    """Thread-safe progress tracker persisted to a JSON file."""

    def __init__(self, filepath: str):
        self._path = filepath
        self._lock = Lock()
        if os.path.exists(filepath):
            with open(filepath, "r") as f:
                self._data = json.load(f)
        else:
            self._data = {"completed": {}, "failed": []}

    def is_done(self, paper_id: str) -> bool:
        return paper_id in self._data["completed"]

    def mark_done(self, paper_id: str, images_kept: int):
        with self._lock:
            self._data["completed"][paper_id] = images_kept
            self._flush()

    def mark_failed(self, paper_id: str):
        with self._lock:
            if paper_id not in self._data["failed"]:
                self._data["failed"].append(paper_id)
            self._flush()

    def _flush(self):
        with open(self._path, "w") as f:
            json.dump(self._data, f)

    @property
    def completed_count(self) -> int:
        return len(self._data["completed"])

    @property
    def failed_count(self) -> int:
        return len(self._data["failed"])

    @property
    def total_images(self) -> int:
        return sum(self._data["completed"].values())


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
def is_mostly_blank(image_bytes: bytes, threshold: float = MAX_WHITE_RATIO) -> bool:
    """Fast sampled check — are >95% of sampled pixels white?"""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        all_pixels = list(
            img.get_flattened_data() if hasattr(img, "get_flattened_data")
            else img.getdata()
        )
        if not all_pixels:
            return True
        if len(all_pixels) > BLANK_SAMPLE_SIZE:
            pixels = random.sample(all_pixels, BLANK_SAMPLE_SIZE)
        else:
            pixels = all_pixels
        white = sum(1 for p in pixels if p > 240)
        return (white / len(pixels)) > threshold
    except Exception:
        return False


def download_pdf(url: str, filepath: str) -> bool:
    """Download a PDF with retry + exponential backoff."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=60, stream=True)
            resp.raise_for_status()

            tmp = filepath + ".tmp"
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)

            
            with open(tmp, "rb") as f:
                if f.read(5) != b"%PDF-":
                    os.remove(tmp)
                    raise ValueError("Not a valid PDF")

            os.replace(tmp, filepath)  
            return True

        except Exception as e:
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                tqdm.write(f"  ⚠ Attempt {attempt}/{MAX_RETRIES}: {e}. Retry in {wait}s…")
                time.sleep(wait)
            else:
                tqdm.write(f"  ✗ Failed after {MAX_RETRIES} attempts: {e}")
                for f in [filepath, filepath + ".tmp"]:
                    if os.path.exists(f):
                        os.remove(f)
                return False


# ──────────────────────────────────────────────────────────────────────
# Figure caption extraction
# ──────────────────────────────────────────────────────────────────────
# Regex matching "Figure 1:", "Fig. 2:", "FIG. 3.", "FIGURE 4 –" etc.

_CAPTION_RE = re.compile(
    r"^(Fig(?:ure|\.)\s*\d+|FIG(?:URE)?\.?\s*\d+)\s*[.:–—\-]?\s*",
    re.IGNORECASE,
)


def extract_figure_captions(pdf_path: str) -> dict:
    captions: dict = {}
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return captions

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        blocks = page.get_text("blocks")  # (x0, y0, x1, y1, text, block_no, block_type)
        page_caption_idx = 0
        for block in blocks:
            if block[6] != 0:  # skip image blocks, keep only text
                continue
            text = block[4].strip().replace("\n", " ")
            if _CAPTION_RE.match(text):
                page_caption_idx += 1
                # Clean up: collapse whitespace, limit length
                caption = " ".join(text.split())
                if len(caption) > 1000:
                    caption = caption[:1000] + "…"
                captions[(page_idx + 1, page_caption_idx)] = caption

    doc.close()
    return captions


def match_captions_to_images(
    page_images: dict, captions: dict
) -> dict:
    """
    Best-effort matching of captions to extracted images.

    page_images:  dict mapping (page_num, fig_num_on_page) -> image filename
    captions:     dict mapping (page_num, caption_idx)  -> caption text

    Returns dict mapping image_filename -> caption_text (or "" if unmatched).
    """
    
    result = {}
    for (img_page, img_fig), filename in page_images.items():
        # Try exact match on (page, index)
        caption = captions.get((img_page, img_fig), "")
        if not caption:
            # Fall back: any caption on the same page
            same_page = {k: v for k, v in captions.items() if k[0] == img_page}
            if same_page:
                # Pick the one with closest index
                closest_key = min(same_page.keys(), key=lambda k: abs(k[1] - img_fig))
                caption = same_page[closest_key]
        result[filename] = caption
    return result


# ──────────────────────────────────────────────────────────────────────
# Image extraction
# ──────────────────────────────────────────────────────────────────────
def extract_images(paper_id: str, pdf_path: str, output_root: str) -> dict:
    """
    Extract quality images from a single PDF.
    
    """
    stats = {"found": 0, "kept": 0, "filtered": 0, "image_files": {}}
    paper_dir = os.path.join(output_root, paper_id)

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        tqdm.write(f"  Cannot open {pdf_path}: {e}")
        return stats

    seen_xrefs = set()
    kept_images = []
    page_fig_counter = {}  

    for page_num in range(len(doc)):
        page = doc[page_num]
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            stats["found"] += 1

            try:
                base = doc.extract_image(xref)
                if not base:
                    stats["filtered"] += 1
                    continue

                img_bytes = base["image"]
                ext = base["ext"].lower()
                w = base.get("width", 0)
                h = base.get("height", 0)

                # Quality filters
                if ext not in ACCEPTED_EXTENSIONS:
                    stats["filtered"] += 1; continue
                if w < MIN_IMAGE_WIDTH or h < MIN_IMAGE_HEIGHT:
                    stats["filtered"] += 1; continue
                if len(img_bytes) < MIN_IMAGE_SIZE_BYTES:
                    stats["filtered"] += 1; continue
                if is_mostly_blank(img_bytes):
                    stats["filtered"] += 1; continue

                # Track per-page figure index
                p1 = page_num + 1
                page_fig_counter[p1] = page_fig_counter.get(p1, 0) + 1
                fig_on_page = page_fig_counter[p1]

                filename = f"{paper_id}__page{p1}_fig{fig_on_page}.{ext}"
                kept_images.append((filename, img_bytes, w, h))
                stats["image_files"][(p1, fig_on_page)] = filename
                stats["kept"] += 1

            except Exception:
                stats["filtered"] += 1

    doc.close()

    # Write images to disk only if we have some
    if kept_images:
        os.makedirs(paper_dir, exist_ok=True)
        for fname, data, _, _ in kept_images:
            with open(os.path.join(paper_dir, fname), "wb") as f:
                f.write(data)

    # Attach dimension info for CSV
    stats["_dims"] = {fname: (w, h) for fname, _, w, h in kept_images}

    return stats


# ──────────────────────────────────────────────────────────────────────
# Streaming metadata writers
# ──────────────────────────────────────────────────────────────────────
class MetadataWriter:
    """Append rows to the CSV incrementally (no large in-memory list)."""

    FIELDS = [
        "arxiv_id", "title", "authors", "abstract",
        "published", "updated", "pdf_url", "categories",
        "matched_keywords", "images_extracted",
    ]

    def __init__(self, filepath: str):
        self._path = filepath
        self._lock = Lock()
        write_header = not os.path.exists(filepath) or os.path.getsize(filepath) == 0
        self._file = open(filepath, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=self.FIELDS)
        if write_header:
            self._writer.writeheader()
            self._file.flush()

    def write(self, row: dict):
        with self._lock:
            self._writer.writerow(row)
            self._file.flush()

    def close(self):
        self._file.close()


class ImageDescWriter:
    """Per-image description CSV writer."""

    FIELDS = [
        "arxiv_id", "image_filename", "page", "figure_number",
        "caption", "image_width", "image_height",
    ]

    def __init__(self, filepath: str):
        self._path = filepath
        self._lock = Lock()
        write_header = not os.path.exists(filepath) or os.path.getsize(filepath) == 0
        self._file = open(filepath, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=self.FIELDS)
        if write_header:
            self._writer.writeheader()
            self._file.flush()

    def write(self, row: dict):
        with self._lock:
            self._writer.writerow(row)
            self._file.flush()

    def close(self):
        self._file.close()


# ──────────────────────────────────────────────────────────────────────
# Query builder
# ──────────────────────────────────────────────────────────────────────
def build_keyword_batches(keywords: list, batch_size: int = KEYWORDS_PER_BATCH) -> list:
    """
    Split keywords into batches and build arXiv query strings.

    Each batch produces a query like:
      ti:"keyword1" OR abs:"keyword1" OR ti:"keyword2" OR abs:"keyword2" ...

    Searching both title and abstract casts a wide net.
    """
    # De-duplicate keywords (case-insensitive)
    seen = set()
    unique = []
    for kw in keywords:
        lower = kw.strip().lower()
        if lower and lower not in seen:
            seen.add(lower)
            unique.append(kw.strip())

    batches = []
    for i in range(0, len(unique), batch_size):
        chunk = unique[i : i + batch_size]
        parts = []
        for kw in chunk:
            # Quote multi-word terms
            escaped = kw.replace('"', "")
            parts.append(f'ti:"{escaped}" OR abs:"{escaped}"')
        query = " OR ".join(parts)
        batches.append((query, chunk))  # (query_string, keyword_list)
    return batches


# ──────────────────────────────────────────────────────────────────────
# Pipeline
# ──────────────────────────────────────────────────────────────────────
def _extract_and_record(arxiv_id, pdf_path, paper_meta, progress,
                        csv_writer, img_desc_writer):
    """Worker function: extract images, captions, & write metadata rows."""
    stats = extract_images(arxiv_id, pdf_path, IMAGE_DIR)

    # Extract captions
    captions = extract_figure_captions(pdf_path)
    caption_map = match_captions_to_images(stats["image_files"], captions)
    dims = stats.get("_dims", {})

    # Write per-image description rows
    for (page, fig_num), filename in stats["image_files"].items():
        caption = caption_map.get(filename, "")
        w, h = dims.get(filename, (0, 0))
        img_desc_writer.write({
            "arxiv_id": paper_meta["arxiv_id"],
            "image_filename": filename,
            "page": page,
            "figure_number": fig_num,
            "caption": caption,
            "image_width": w,
            "image_height": h,
        })

    # Write paper metadata row
    csv_writer.write({
        "arxiv_id": paper_meta["arxiv_id"],
        "title": paper_meta["title"],
        "authors": paper_meta["authors"],
        "abstract": paper_meta["abstract"],
        "published": paper_meta["published"],
        "updated": paper_meta["updated"],
        "pdf_url": paper_meta["pdf_url"],
        "categories": paper_meta["categories"],
        "matched_keywords": paper_meta.get("matched_keywords", ""),
        "images_extracted": stats["kept"],
    })

    progress.mark_done(arxiv_id, stats["kept"])
    return stats


def fetch_papers_multi_keyword(max_papers: int, test_mode: bool = False):
    """
    Fetch paper metadata across all keyword batches, deduplicating by arXiv ID.
    Stops once max_papers unique papers are collected.
    """
    batches = build_keyword_batches(QUANTUM_KEYWORDS)
    print(f"\n🔑 {len(QUANTUM_KEYWORDS)} unique keywords → {len(batches)} query batches")

    client = arxiv.Client(
        page_size=API_PAGE_SIZE,
        delay_seconds=10.0,
        num_retries=10,
    )
    
    # How many results per batch?
    if test_mode:
        per_batch = max(2, max_papers // len(batches) + 1)
    else:
        per_batch = RESULTS_PER_BATCH

    papers = {}   
    keyword_map = {}  

    for batch_idx, (query, kw_list) in enumerate(batches):
        if len(papers) >= max_papers:
            break

        kw_label = ", ".join(kw_list[:3]) + ("…" if len(kw_list) > 3 else "")
        print(f"\n📦 Batch {batch_idx + 1}/{len(batches)} — {kw_label}")
        print(f"   ({len(papers)}/{max_papers} unique papers so far)")

        search = arxiv.Search(
            query=query,
            max_results=per_batch,
            sort_by=arxiv.SortCriterion.Relevance,
        )

        batch_new = 0
        batch_dup = 0
        try:
            for result in tqdm(client.results(search), total=per_batch,
                               desc=f"  Batch {batch_idx + 1}", leave=False):
                aid = result.get_short_id()
                if aid in papers:
                    batch_dup += 1
                    # Still record which keywords matched
                    keyword_map.setdefault(aid, set()).update(kw_list)
                    continue

                papers[aid] = {
                    "arxiv_id": aid,
                    "title": result.title,
                    "authors": ", ".join(a.name for a in result.authors[:5]),
                    "abstract": result.summary.replace("\n", " ")[:500],
                    "published": result.published.strftime("%Y-%m-%d"),
                    "updated": (result.updated.strftime("%Y-%m-%d")
                                if result.updated else ""),
                    "pdf_url": result.pdf_url,
                    "categories": ", ".join(result.categories),
                }
                keyword_map.setdefault(aid, set()).update(kw_list)
                batch_new += 1

                if len(papers) >= max_papers:
                    break
        except Exception as e:
            tqdm.write(f"  ⚠ Batch error: {e}")

        print(f"   → {batch_new} new, {batch_dup} duplicates")

    # Attach matched keywords to each paper
    for aid, meta in papers.items():
        meta["matched_keywords"] = "; ".join(sorted(keyword_map.get(aid, set())))

    return list(papers.values())


def run_pipeline(max_papers: int, test_mode: bool = False):
    """Main pipeline — fetch metadata, download PDFs, extract images + captions."""
    os.makedirs(PDF_DIR, exist_ok=True)
    os.makedirs(IMAGE_DIR, exist_ok=True)

    progress = ProgressTracker(PROGRESS_FILE)

    # Step 1: Need to Fetch paper metadata from arXiv API
    print(f"\n🔍 Searching arXiv for {max_papers} quantum computing papers "
          f"across {len(QUANTUM_KEYWORDS)} keywords…")

    papers = fetch_papers_multi_keyword(max_papers, test_mode=test_mode)

    print(f"\n✓ Collected {len(papers)} unique papers  "
          f"({progress.completed_count} already completed from previous runs)")

    if len(papers) < max_papers:
        shortfall = max_papers - len(papers)
        print(f"  ⚠ Shortfall of {shortfall} papers — consider broadening keywords "
              f"or increasing RESULTS_PER_BATCH")

    # Step 2: Next, need to Process papers
    csv_writer = MetadataWriter(METADATA_FILE)
    img_desc_writer = ImageDescWriter(IMAGE_DESC_FILE)

    counters = {
        "downloaded": 0, "skipped": 0, "existed": 0,
        "failed": 0, "images_found": 0, "images_kept": 0,
        "papers_with_images": 0, "captions_found": 0,
    }

    with ThreadPoolExecutor(max_workers=EXTRACTION_WORKERS) as pool:
        futures = {}

        pbar = tqdm(papers, desc="Processing papers", unit="paper")
        for paper_meta in pbar:
            arxiv_id = paper_meta["arxiv_id"].replace("/", "_")

            if progress.is_done(arxiv_id):
                counters["skipped"] += 1
                pbar.set_postfix(dl=counters["downloaded"],
                                 skip=counters["skipped"],
                                 imgs=counters["images_kept"])
                continue

            pdf_path = os.path.join(PDF_DIR, f"{arxiv_id}.pdf")

            # Download PDF (sequential — rate limit)
            need_download = not os.path.exists(pdf_path)
            if need_download:
                ok = download_pdf(paper_meta["pdf_url"], pdf_path)
                if not ok:
                    progress.mark_failed(arxiv_id)
                    counters["failed"] += 1
                    continue
                counters["downloaded"] += 1
                time.sleep(DOWNLOAD_DELAY_SEC)
            else:
                counters["existed"] += 1

            # Submit extraction to thread pool
            fut = pool.submit(
                _extract_and_record,
                arxiv_id, pdf_path, paper_meta, progress,
                csv_writer, img_desc_writer,
            )
            futures[fut] = arxiv_id

            # Harvest completed futures (non-blocking)
            done_futs = [f for f in futures if f.done()]
            for f in done_futs:
                try:
                    stats = f.result()
                    counters["images_found"] += stats["found"]
                    counters["images_kept"] += stats["kept"]
                    if stats["kept"] > 0:
                        counters["papers_with_images"] += 1
                except Exception as exc:
                    tqdm.write(f"  ✗ Extraction error: {exc}")
                del futures[f]

            pbar.set_postfix(dl=counters["downloaded"],
                             skip=counters["skipped"],
                             imgs=counters["images_kept"])

        # Wait for remaining extraction futures
        for fut in as_completed(futures):
            try:
                stats = fut.result()
                counters["images_found"] += stats["found"]
                counters["images_kept"] += stats["kept"]
                if stats["kept"] > 0:
                    counters["papers_with_images"] += 1
            except Exception as exc:
                tqdm.write(f"  ✗ Extraction error: {exc}")

    csv_writer.close()
    img_desc_writer.close()

    # Step 3: Summary 
    total_processed = (counters["downloaded"] + counters["existed"]
                       + counters["skipped"])
    print("\n" + "=" * 60)
    print("📊  SUMMARY REPORT")
    print("=" * 60)
    print(f"  Papers requested:        {max_papers}")
    print(f"  Papers found (API):      {len(papers)}")
    print(f"  PDFs downloaded (new):   {counters['downloaded']}")
    print(f"  PDFs already on disk:    {counters['existed']}")
    print(f"  Skipped (prev. run):     {counters['skipped']}")
    print(f"  Failed downloads:        {counters['failed']}")
    print(f"  ─────────────────────────────")
    print(f"  Images found in PDFs:    {counters['images_found']}")
    print(f"  Images kept (quality):   {counters['images_kept']}")
    print(f"  Images filtered out:     {counters['images_found'] - counters['images_kept']}")
    print(f"  Papers with images:      {counters['papers_with_images']}")
    if total_processed > 0:
        print(f"  Avg images / paper:      "
              f"{counters['images_kept'] / max(1, total_processed):.1f}")
    print("=" * 60)
    print(f"\n📁  PDFs:              ./{PDF_DIR}/")
    print(f"📁  Images:            ./{IMAGE_DIR}/<paper_id>/<paper_id>__page<P>_fig<N>.<ext>")
    print(f"📄  Metadata:          ./{METADATA_FILE}")
    print(f"📄  Image descriptions: ./{IMAGE_DESC_FILE}")
    print(f"📄  Progress:          ./{PROGRESS_FILE}  (delete to re-process all)")
    print("✅  Done!\n")


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Download arXiv quantum papers (multi-keyword) & extract images + captions"
    )
    parser.add_argument("--test", action="store_true",
                        help="Test mode: 10 papers only")
    parser.add_argument("--max-papers", type=int, default=6000,
                        help="Max unique papers to fetch (default: 6000)")
    parser.add_argument("--fresh", action="store_true",
                        help="Wipe previous progress and start fresh")
    args = parser.parse_args()

    n = 10 if args.test else args.max_papers

    # Fresh start: clear state files
    if args.fresh:
        for f in [PROGRESS_FILE, METADATA_FILE, IMAGE_DESC_FILE]:
            if os.path.exists(f):
                os.remove(f)
                print(f"  🗑  Removed {f}")

    print("==========================================================")
    print("   ArXiv Multi-Keyword Quantum Paper Scraper              ")
    print("    with Image Extraction & Captions                      ")
    print("==========================================================")
    
    print(f"  Mode:          {'TEST (10 papers)' if args.test else f'FULL ({n} papers)'}")
    print(f"  Keywords:      {len(QUANTUM_KEYWORDS)}")
    print(f"  PDF dir:       {os.path.abspath(PDF_DIR)}")
    print(f"  Image dir:     {os.path.abspath(IMAGE_DIR)}")
    print(f"  Workers:       {EXTRACTION_WORKERS} threads")

    run_pipeline(n, test_mode=args.test)


if __name__ == "__main__":
    main()
