# Block 10 — Local Knowledge / RAG

## Role

RAG makes the workbench useful for real organizations because the model can ground its output in internal SOPs, manuals, reports and correspondence.

## Pipeline

```text
Files
 ↓
Ingestion
 ↓
OCR / structure extraction
 ↓
Metadata + ACL tagging
 ↓
Chunking
 ↓
Embedding
 ↓
Vector DB
 ↓
ACL-aware retrieval
 ↓
Rerank
 ↓
Evidence package
 ↓
Agent
```

## Supported inputs

Prototype:

- PDF
- DOCX
- TXT
- scanned images

Later:

- PPTX
- XLSX
- CAD metadata
- email exports
- scanned drawings

## Document metadata

```json
{
  "document_id": "doc_17",
  "version": 4,
  "department": "maintenance",
  "sector": "sector-1",
  "classification": "CONFIDENTIAL",
  "acl": ["maintenance", "inspection"],
  "source_path": "..."
}
```

## OCR

For scans:

```text
scan
 ↓
vision/OCR
 ↓
text
 ↓
layout/bounding boxes
 ↓
document representation
```

Keep OCR output linked to the original scan.

## Chunking

Chunks should preserve:
- document ID
- section
- page
- version
- classification
- ACL

Do not create anonymous text chunks.

## Retrieval

Example query:

> What was the previous repair cost for the sector-1 lathe?

Search should apply:

```text
classification <= user clearance
department allowed
sector == 1
document status == approved/current
```

## Evidence package

Return:

```json
{
  "sources": [
    {
      "document_id": "doc_17",
      "page": 23,
      "text": "...",
      "version": 4
    }
  ]
}
```

The agent should receive evidence with provenance instead of a giant undifferentiated context dump.

## RAG quality checks

Measure:

```text
retrieval precision
source coverage
citation correctness
stale document usage
ACL violations
```

## Recommended prototype stack

- Qdrant
- sentence-transformer/open-weight embeddings
- optional local reranker
- local parsing/OCR
- Postgres for metadata/ACL

Everything stays within the organization's infrastructure.
