# Self-Healing v2 - Web UI

## Overview
New "Self-Healing" tab in ProcClaw Web UI with suggestion management.

## New Tab: Self-Healing

### Location
Add as new tab after "Workflows":
`Jobs | Runs | Dependencies | Workflows | Self-Healing | Logs | Config`

### Tab Structure

```
┌─────────────────────────────────────────────────────────────────┐
│  📊 Overview Cards                                               │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐               │
│  │ Pending │ │Approved │ │ Applied │ │Rejected │               │
│  │   12    │ │    3    │ │   47    │ │    8    │               │
│  └─────────┘ └─────────┘ └─────────┘ └─────────┘               │
├─────────────────────────────────────────────────────────────────┤
│  🔧 Pending Suggestions                            [🔄 Refresh] │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │ Filters: [All Jobs ▼] [All Categories ▼] [All Severities ▼]││
│  ├─────────────────────────────────────────────────────────────┤│
│  │ List of suggestion cards...                                 ││
│  └─────────────────────────────────────────────────────────────┘│
├─────────────────────────────────────────────────────────────────┤
│  📜 Recent Actions                                 [View All →] │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │ Compact list of recent actions...                           ││
│  └─────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
```

## Components

### Overview Cards
Four stat cards showing counts:
- **Pending** (yellow/orange): Suggestions awaiting review
- **Approved** (blue): Approved but not yet applied
- **Applied** (green): Successfully applied
- **Rejected** (gray): Rejected suggestions

Click on card filters the list below.

### Suggestion Card
```
┌─ 🔴 HIGH ─────────────────────────────────────────────────────┐
│  oc-idea-hunter                              2026-02-11 08:30 │
│  "Prompt usando API deprecated do Twitter"                    │
│  Category: prompt                                             │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │ [👁 View]  [✓ Approve]  [✗ Reject]                      │ │
│  └─────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────┘
```

Severity colors:
- 🔴 CRITICAL/HIGH - Red border
- 🟡 MEDIUM - Yellow border
- 🟢 LOW - Green border

### Action Row
```
✅ oc-stock-hunter │ edit_prompt │ success │ 10min ago │ [View]
```

Status icons:
- ✅ success (green)
- ❌ failed (red)
- ↩️ rolled_back (blue)

## Modals

### Suggestion Detail Modal
Full-screen modal with:
- Header: Job name, category badge, severity badge, date
- Section: Title
- Section: Description (markdown rendered)
- Section: Current State (code block, scrollable)
- Section: Suggested Change (code block, scrollable)
- Section: Expected Impact
- Section: Affected Files (list)
- Footer: [Cancel] [✗ Reject] [✓ Approve & Apply]

If rejecting, show textarea for reason.

### Action Detail Modal
Full-screen modal with:
- Header: Job name, action type, status badge, date
- Section: File Modified (path)
- Section: Changes (diff view with syntax highlighting)
- Section: Execution Details (duration, AI session, tokens)
- Section: Rollback (if available)
- Footer: [Close] [↩️ Rollback] (if can_rollback=true)

### Reject Reason Modal
Small modal with:
- Textarea for rejection reason
- [Cancel] [Confirm Reject]

## Filters

### Job Filter
Dropdown with all jobs that have self-healing enabled.
Option "All Jobs" at top.

### Category Filter
- All Categories
- performance
- cost
- reliability
- security
- config
- prompt
- script

### Severity Filter
- All Severities
- critical
- high
- medium
- low

## Data Fetching

### On Tab Open
1. GET /api/v1/healing/stats (for overview cards)
2. GET /api/v1/healing/suggestions?status=pending (for list)
3. GET /api/v1/healing/actions?limit=5 (for recent actions)

### Auto-Refresh
Poll every 30 seconds for pending count.
Show badge on tab if pending > 0: `Self-Healing (5)`

### Actions
- Approve: POST /api/v1/healing/suggestions/{id}/approve
- Reject: POST /api/v1/healing/suggestions/{id}/reject
- Rollback: POST /api/v1/healing/actions/{id}/rollback

## Edit Job Modal Updates

Add new section "Self-Healing v2" in job edit modal:
- Toggle: Enable Self-Healing
- Radio: Mode (Reactive / Proactive)
- If Proactive:
  - Select: Review Frequency (hourly/daily/weekly/on_failure/on_sla_breach/manual)
  - Time picker: Review Time (for daily/weekly)
  - Number: Min Runs Before Review
- Checkboxes: Review Scope (logs, runs, AI sessions, SLA, workflows, script, prompt, config)
- Toggle: Auto-Apply Suggestions
- If not auto-apply:
  - Checkboxes: Categories to auto-apply anyway
  - Select: Min severity requiring approval

## Acceptance Criteria
- [ ] New tab added to navigation
- [ ] Overview cards show correct counts
- [ ] Suggestion list loads and displays correctly
- [ ] Filters work (job, category, severity)
- [ ] Suggestion detail modal opens and shows all fields
- [ ] Approve action works and updates list
- [ ] Reject action works with reason
- [ ] Action list shows recent actions
- [ ] Action detail modal shows diff
- [ ] Rollback action works
- [ ] Edit job modal has new self-healing fields
- [ ] Badge shows pending count on tab
- [ ] Responsive design (mobile friendly)
