# Multi-Keyword ArXiv Scraper — 100-Paper Run Results

> *Automatically harvest, filter, and enrich quantum computing research papers from arXiv, complete with extracted figures and figure captions, ready for downstream AI/RAG pipelines.*

---

## 👤 Author

**Velankani Joise Divya Gorla Christuraj**
[GitHub](https://github.com/divya-jd) · [LinkedIn](https://www.linkedin.com/in/velankani-joise-divya/)

---

## Problem Statement

Building a high-quality **knowledge corpus** for a distributed Retrieval-Augmented Generation (RAG) system on quantum computing requires:

- **Thousands of curated papers** from the latest research
- **Rich multimodal content** — not just text, but figures, circuit diagrams, and benchmark plots
- **Structured metadata** — authors, abstracts, categories, figure captions — all in one place
- **Reproducibility** — able to resume interrupted runs and avoid duplicate downloads

Manually curating even 100 papers is time-consuming. Scaling to 5,000–6,000 papers is practically impossible without automation.

There was **no single existing tool** that could:
1. Search arXiv across a broad, multi-keyword quantum computing taxonomy
2. Download the PDFs
3. Extract only high-quality figures (no blank pages, no decorative icons)
4. Capture the matching figure caption text from the paper
5. Output everything to structured CSVs, checkpoint-safe for long runs

---

## Solution

A fully automated Python pipeline built around the **arXiv API**, **PyMuPDF**, and **Pillow**:

```
Keywords → arXiv API → PDF Download → Image Extraction → Caption Matching → CSV Output
```

### Key Design Decisions

| Feature | Detail |
|---|---|
| **150+ quantum keywords** | Batched into arXiv-safe URL-length queries |
| **Cross-batch deduplication** | Same paper from 2 keyword matches → stored once |
| **Quality filtering** | Images < 200px, < 5KB, or > 95% white pixels are discarded |
| **Caption extraction** | Regex matches `Fig. N:` / `Figure N:` text blocks from PDF |
| **Parallel extraction** | `ThreadPoolExecutor` with 8 workers for image processing |
| **Checkpoint/resume** | `progress.json` tracks every paper; safe to Ctrl+C and restart |
| **Streaming CSV writes** | No large in-memory accumulation; writes row-by-row |

---

## Skills Used

| Category | Technologies |
|---|---|
| **Language** | Python 3.10+ |
| **API Integration** | `arxiv` Python SDK (arXiv REST API) |
| **PDF Processing** | PyMuPDF (`fitz`) — image extraction, text block parsing |
| **Image Processing** | Pillow — format validation, blank detection |
| **Concurrency** | `ThreadPoolExecutor`, `threading.Lock` |
| **Data Engineering** | Streaming CSV writers, JSON checkpointing |
| **CLI Design** | `argparse` — `--test`, `--max-papers`, `--fresh` flags |
| **Rate Limiting** | Exponential backoff, configurable download delay |
| **Progress UX** | `tqdm` progress bars with live stats |
| **Corpus Design** | Multimodal (text + image) structured output for RAG pipelines |

---

## Requirements

### System
- Python **3.10 or higher**
- macOS / Linux (Windows: untested)
- ~10 GB free disk space for a 6,000-paper run

### Python Dependencies

```
arxiv>=2.0.0
PyMuPDF>=1.23.0
tqdm
requests
Pillow
```

Install everything with:

```bash
pip install -r requirements.txt
```

---

## How to Run

### 1. Clone / navigate to the project

```bash
cd paper_image_extractor
```

### 2. (Recommended) Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate      # macOS/Linux
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run modes

```bash
# Quick test — 10 papers only (great for verifying setup)
python arxiv_quantum_scraper.py --test

# Custom paper count
python arxiv_quantum_scraper.py --max-papers 100

# Full run — 6,000 papers (default)
python arxiv_quantum_scraper.py

# Fresh start — wipe previous progress and re-run from scratch
python arxiv_quantum_scraper.py --fresh
```

>  **You can safely Ctrl+C at any time.** The next run will resume exactly where it left off.

---

## Output Files

After a run, the following are generated automatically:

```
paper_image_extractor/
├── arxiv_quantum_scraper.py        # Source code
├── requirements.txt                # Dependencies
│
├── quantum_arxiv_papers/           # Downloaded PDFs
│   ├── 2411.11822v3.pdf
│   ├── 2407.21641v4.pdf
│   └── ...
│
├── extracted_images/               # Extracted figures, per paper
│   ├── 2411.11822v3/
│   │   ├── 2411.11822v3__page2_fig1.png
│   │   └── 2411.11822v3__page6_fig1.png
│   └── ...
│
├── metadata.csv                    # Paper-level metadata
├── image_descriptions.csv          # Per-figure metadata + captions
└── progress.json                   # Checkpoint (auto-managed)
```

---

## Expected Output — Screenshots

### Terminal during a run

```
╔══════════════════════════════════════════════════════════╗
║   ArXiv Multi-Keyword Quantum Paper Scraper             ║
║   with Image Extraction & Captions                      ║
╚══════════════════════════════════════════════════════════╝
  Mode:          FULL (6000 papers)
  Keywords:      150
  PDF dir:       /Users/.../quantum_arxiv_papers
  Workers:       8 threads

🔑 150 unique keywords → 10 query batches

📦 Batch 1/10 — Adiabatic Theorem, Bosonic Creutz Ladder…
   (0/6000 unique papers so far)
  Batch 1: 100%|████████████████████| 500/500 [01:12<00:00]
   → 487 new, 13 duplicates

Processing papers: 100%|████████| 487/487 [dl=102, skip=0, imgs=843]
```

---

### `metadata.csv` — one row per paper

| arxiv_id | title | authors | abstract | published | categories | images_extracted |
|---|---|---|---|---|---|---|
| 2411.11822v3 | Fault-tolerant quantum computation... | Ben W. Reichardt, Adam Paetznick... | Quantum computing experiments are transitioning... | 2024-11-18 | quant-ph | 3 |
| 2409.08229v1 | Photonic Quantum Computers | M. AbuGhanem | In the pursuit of scalable and fault-tolerant... | 2024-09-12 | quant-ph, cs.AI | 17 |

---

### `image_descriptions.csv` — one row per extracted figure

| arxiv_id | image_filename | page | figure_number | caption | image_width | image_height |
|---|---|---|---|---|---|---|
| 2411.11822v3 | 2411.11822v3__page2_fig1.png | 2 | 1 | FIG. 1. Experimental architecture. a, b, 171Yb atoms... | 2144 | 670 |
| 2409.08229v1 | 2409.08229v1__page4_fig1.png | 4 | 1 | FIG. 1. The hexagonal waveguide mesh chip... | 675 | 391 |

---

### Summary Report (end of run)

```
============================================================
📊  SUMMARY REPORT
============================================================
  Papers requested:        100
  Papers found (API):      101
  PDFs downloaded (new):   101
  PDFs already on disk:    0
  Skipped (prev. run):     0
  Failed downloads:        0
  ─────────────────────────────
  Images found in PDFs:    1,847
  Images kept (quality):   1,203
  Images filtered out:     644
  Papers with images:      78
  Avg images / paper:      11.9
============================================================
```

---

## 🔧 Configuration Reference

All tunable constants are at the top of `arxiv_quantum_scraper.py`:

```python
DOWNLOAD_DELAY_SEC  = 1      # Delay between PDF downloads (arXiv-safe)
API_PAGE_SIZE       = 50     # Results per arXiv API page
MIN_IMAGE_WIDTH     = 200    # px — minimum image dimension
MIN_IMAGE_HEIGHT    = 200    # px
MIN_IMAGE_SIZE_BYTES= 5_000  # 5 KB minimum
MAX_WHITE_RATIO     = 0.95   # Discard images >95% white pixels
EXTRACTION_WORKERS  = 8      # Threads for parallel image extraction
RESULTS_PER_BATCH   = 500    # API results per keyword batch
```

---

## 📌 Notes

- arXiv enforces a **rate limit**; the built-in delays keep the scraper compliant.
- A full 6,000-paper run takes approximately **4–5 hours** (mostly PDF download time).
- The `progress.json` checkpoint allows splitting a large run across multiple sessions.
- Papers matched by multiple keywords are **deduplicated** — each paper appears exactly once in the output.

---


