# Block 11 — Deliverable Generation / Verification / Approval

## Role

The system is deliverable-first.

The final product should be:

```text
Word / PPT / Excel / verified code
```

not simply a chat answer.

## Pipeline

```text
Evidence + agent result
        ↓
Template selection
        ↓
Artifact generation
        ↓
Automated verification
        ↓
Human approval
        ↓
Release
```

## Document generation

Suggested libraries:

```text
DOCX → python-docx
PPTX → python-pptx
XLSX → openpyxl
```

## Template model

Templates should contain:

```text
organization style
document metadata
approval fields
classification markings
standard sections
footer/header
revision history
```

## Verification

Every artifact should be checked for:

### File validity

```text
opens successfully
required sheets/slides/sections exist
no corrupted package
```

### Content

```text
required fields present
numbers consistent
citations attached
source coverage adequate
```

### Policy

```text
classification markings correct
restricted content handled correctly
no unauthorized source
```

### Business rules

Example:

```text
repair cost must match approved source
approval note must contain responsible engineer
date must be current task date
```

## Approval gate

Before release:

```text
Draft
 ↓
Automated verification
 ↓
Human reviewer
 ↓
Approve / Reject
```

## Release

Once approved:

```text
artifact version immutable
approval identity recorded
hash recorded
provenance recorded
```

## Example artifact metadata

```json
{
  "artifact_id": "art_55",
  "type": "DOCX",
  "version": 3,
  "status": "RELEASED",
  "sha256": "...",
  "created_by": "agent_2",
  "approved_by": "user_42",
  "sources": ["doc_17", "doc_32"]
}
```

## Why this is strategically important

The architecture can demonstrate:

```text
Confidential source
      ↓
AI reasoning
      ↓
verified business document
      ↓
human sign-off
      ↓
auditable released artifact
```

That is a stronger industrial workflow than a conventional chatbot.
