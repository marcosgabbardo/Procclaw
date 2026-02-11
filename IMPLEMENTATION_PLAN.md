# Implementation Plan - Self-Healing v2

> **Status:** In Progress  
> **Started:** 2026-02-11  
> **Target:** Complete implementation with full test coverage

## Validation Commands

```bash
# Run all tests
cd ~/.openclaw/workspace/projects/procclaw && uv run pytest tests/ -v

# Run specific test file
uv run pytest tests/test_healing_v2.py -v

# Type check
uv run mypy src/procclaw/

# Lint
uv run ruff check src/procclaw/

# Full validation (all must pass before commit)
uv run pytest tests/ -v && uv run mypy src/procclaw/ && uv run ruff check src/procclaw/
```

## Phase 1: Database & Models

### In Progress
- [ ] Task 1.2: Create new config models (iteration 2)

### Backlog
- [ ] Task 1.2: Create new config models (ReviewScheduleConfig, ReviewScopeConfig, SuggestionBehaviorConfig)
- [ ] Task 1.3: Update SelfHealingConfig with new fields (backwards compatible)
- [ ] Task 1.4: Create data models (HealingReview, HealingSuggestion, HealingAction)
- [ ] Task 1.5: Create DB migration v8 with new tables
- [ ] Task 1.6: Add DB methods for healing_reviews CRUD
- [ ] Task 1.7: Add DB methods for healing_suggestions CRUD
- [ ] Task 1.8: Add DB methods for healing_actions CRUD
- [ ] Task 1.9: Write tests for all new models
- [ ] Task 1.10: Write tests for all DB operations

## Phase 2: API Endpoints

### Backlog
- [ ] Task 2.1: Add GET /api/v1/healing/reviews endpoint
- [ ] Task 2.2: Add GET /api/v1/healing/reviews/{id} endpoint
- [ ] Task 2.3: Add POST /api/v1/healing/reviews/{job_id}/trigger endpoint
- [ ] Task 2.4: Add GET /api/v1/healing/suggestions endpoint with filters
- [ ] Task 2.5: Add GET /api/v1/healing/suggestions/{id} endpoint
- [ ] Task 2.6: Add POST /api/v1/healing/suggestions/{id}/approve endpoint
- [ ] Task 2.7: Add POST /api/v1/healing/suggestions/{id}/reject endpoint
- [ ] Task 2.8: Add GET /api/v1/healing/suggestions/pending/count endpoint
- [ ] Task 2.9: Add GET /api/v1/healing/actions endpoint
- [ ] Task 2.10: Add GET /api/v1/healing/actions/{id} endpoint
- [ ] Task 2.11: Add POST /api/v1/healing/actions/{id}/rollback endpoint
- [ ] Task 2.12: Add GET /api/v1/healing/stats endpoint
- [ ] Task 2.13: Add GET /api/v1/healing/stats/{job_id} endpoint
- [ ] Task 2.14: Write API tests for all endpoints

## Phase 3: Review Engine

### Backlog
- [ ] Task 3.1: Create HealingReviewScheduler class
- [ ] Task 3.2: Integrate scheduler with main supervisor
- [ ] Task 3.3: Create InsumoCollector class (gathers logs, runs, SLA, etc)
- [ ] Task 3.4: Create AIAnalyzer class (calls OpenClaw for analysis)
- [ ] Task 3.5: Create SuggestionGenerator class
- [ ] Task 3.6: Implement periodic review triggering
- [ ] Task 3.7: Implement manual review triggering
- [ ] Task 3.8: Write tests for review engine

## Phase 4: Action Executor

### Backlog
- [ ] Task 4.1: Create ActionExecutor class
- [ ] Task 4.2: Implement file backup before changes
- [ ] Task 4.3: Implement edit_prompt action
- [ ] Task 4.4: Implement edit_script action
- [ ] Task 4.5: Implement edit_config action
- [ ] Task 4.6: Implement rollback mechanism
- [ ] Task 4.7: Write tests for action executor

## Phase 5: Web UI - Self-Healing Tab

### Backlog
- [ ] Task 5.1: Add Self-Healing tab to navigation
- [ ] Task 5.2: Create overview cards component
- [ ] Task 5.3: Create suggestions list component
- [ ] Task 5.4: Create suggestion card component
- [ ] Task 5.5: Create suggestion detail modal
- [ ] Task 5.6: Create actions list component
- [ ] Task 5.7: Create action detail modal
- [ ] Task 5.8: Implement filters (job, category, severity)
- [ ] Task 5.9: Implement approve action
- [ ] Task 5.10: Implement reject action with reason
- [ ] Task 5.11: Implement rollback action
- [ ] Task 5.12: Add pending badge to tab

## Phase 6: Web UI - Edit Job Modal

### Backlog
- [ ] Task 6.1: Add Self-Healing v2 section to edit modal
- [ ] Task 6.2: Add mode selector (reactive/proactive)
- [ ] Task 6.3: Add review schedule fields
- [ ] Task 6.4: Add review scope checkboxes
- [ ] Task 6.5: Add suggestion behavior fields
- [ ] Task 6.6: Ensure saveJob sends all new fields
- [ ] Task 6.7: Ensure openEditModal loads all new fields

## Phase 7: Integration & Polish

### Backlog
- [ ] Task 7.1: End-to-end test: create review, generate suggestion, approve, apply
- [ ] Task 7.2: End-to-end test: reject suggestion flow
- [ ] Task 7.3: End-to-end test: rollback action flow
- [ ] Task 7.4: Test with real jobs (oc-idea-hunter, backup-procclaw)
- [ ] Task 7.5: Performance testing (many suggestions)
- [ ] Task 7.6: Update documentation
- [ ] Task 7.7: Final code review and cleanup

## Completed
(none yet)

---

## Notes & Discoveries
- Iteration 1: (pending)
