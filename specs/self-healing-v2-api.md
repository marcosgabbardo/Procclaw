# Self-Healing v2 - API Endpoints

## Overview
New REST API endpoints for Self-Healing v2 functionality.

## Base Path
`/api/v1/healing`

## Endpoints

### Reviews

#### GET /api/v1/healing/reviews
List all healing reviews with pagination and filters.

**Query Parameters:**
- `job_id` (optional): Filter by job
- `status` (optional): Filter by status
- `limit` (default: 50): Max results
- `offset` (default: 0): Pagination offset

**Response:**
```json
{
  "reviews": [...],
  "total": 123,
  "limit": 50,
  "offset": 0
}
```

#### GET /api/v1/healing/reviews/{id}
Get details of a specific review.

**Response:**
```json
{
  "id": 1,
  "job_id": "oc-idea-hunter",
  "started_at": "2026-02-11T08:00:00",
  "finished_at": "2026-02-11T08:01:30",
  "status": "completed",
  "runs_analyzed": 10,
  "suggestions_count": 2,
  ...
}
```

#### POST /api/v1/healing/reviews/{job_id}/trigger
Manually trigger a healing review for a job.

**Response:**
```json
{
  "success": true,
  "review_id": 123,
  "message": "Review started for job 'oc-idea-hunter'"
}
```

### Suggestions

#### GET /api/v1/healing/suggestions
List suggestions with filters.

**Query Parameters:**
- `job_id` (optional): Filter by job
- `status` (optional): 'pending', 'approved', 'rejected', 'applied', 'failed'
- `category` (optional): Filter by category
- `severity` (optional): Filter by severity
- `limit` (default: 50): Max results
- `offset` (default: 0): Pagination offset

**Response:**
```json
{
  "suggestions": [...],
  "total": 45,
  "limit": 50,
  "offset": 0
}
```

#### GET /api/v1/healing/suggestions/{id}
Get full details of a suggestion.

**Response:**
```json
{
  "id": 1,
  "review_id": 5,
  "job_id": "oc-idea-hunter",
  "category": "prompt",
  "severity": "high",
  "title": "Deprecated API usage",
  "description": "Full explanation...",
  "current_state": "...",
  "suggested_change": "...",
  "expected_impact": "...",
  "affected_files": ["~/.procclaw/prompts/idea-hunter.md"],
  "status": "pending",
  ...
}
```

#### POST /api/v1/healing/suggestions/{id}/approve
Approve a suggestion and execute the change.

**Request Body (optional):**
```json
{
  "comment": "Looks good, apply it"
}
```

**Response:**
```json
{
  "success": true,
  "action_id": 42,
  "message": "Suggestion approved and applied"
}
```

#### POST /api/v1/healing/suggestions/{id}/reject
Reject a suggestion.

**Request Body:**
```json
{
  "reason": "Not applicable for our use case"
}
```

**Response:**
```json
{
  "success": true,
  "message": "Suggestion rejected"
}
```

#### GET /api/v1/healing/suggestions/pending/count
Get count of pending suggestions (for badge).

**Response:**
```json
{
  "count": 5
}
```

### Actions

#### GET /api/v1/healing/actions
List executed actions.

**Query Parameters:**
- `job_id` (optional): Filter by job
- `status` (optional): Filter by status
- `limit` (default: 50): Max results
- `offset` (default: 0): Pagination offset

**Response:**
```json
{
  "actions": [...],
  "total": 89,
  "limit": 50,
  "offset": 0
}
```

#### GET /api/v1/healing/actions/{id}
Get full details of an action including diff.

**Response:**
```json
{
  "id": 42,
  "suggestion_id": 1,
  "job_id": "oc-idea-hunter",
  "action_type": "edit_prompt",
  "file_path": "~/.procclaw/prompts/idea-hunter.md",
  "original_content": "...",
  "new_content": "...",
  "status": "success",
  "can_rollback": true,
  "execution_duration_ms": 1200,
  ...
}
```

#### POST /api/v1/healing/actions/{id}/rollback
Rollback an action to original state.

**Response:**
```json
{
  "success": true,
  "message": "Action rolled back successfully"
}
```

### Stats

#### GET /api/v1/healing/stats
Get overall healing statistics.

**Response:**
```json
{
  "pending_suggestions": 5,
  "approved_suggestions": 12,
  "rejected_suggestions": 3,
  "applied_suggestions": 47,
  "total_reviews": 89,
  "total_actions": 47,
  "success_rate": 94.5,
  "jobs_with_healing": 8
}
```

#### GET /api/v1/healing/stats/{job_id}
Get healing statistics for a specific job.

**Response:**
```json
{
  "job_id": "oc-idea-hunter",
  "total_reviews": 15,
  "last_review": "2026-02-11T08:00:00",
  "next_scheduled_review": "2026-02-12T03:00:00",
  "pending_suggestions": 1,
  "applied_suggestions": 8,
  "improvement_metrics": {
    "sla_before": 72.5,
    "sla_after": 95.2
  }
}
```

## Error Responses

All endpoints return standard error format:
```json
{
  "success": false,
  "error": "Error message",
  "detail": "Detailed explanation"
}
```

## Acceptance Criteria
- [ ] All endpoints implemented
- [ ] Proper authentication (existing token system)
- [ ] Pagination works correctly
- [ ] Filters work correctly
- [ ] Error handling consistent
- [ ] Response models documented
- [ ] OpenAPI spec updated
