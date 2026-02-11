# Self-Healing v2 - Proactive Behavior Analysis

Self-Healing v2 extends ProcClaw's reactive failure handling with **proactive job analysis**. Instead of only responding to failures, the system periodically reviews job behavior and suggests improvements.

## Features

### 🧬 Proactive Reviews
- **Scheduled analysis**: hourly, daily, or weekly reviews
- **Event-triggered**: analyze on failure or SLA breach
- **Context collection**: runs, logs, AI sessions, prompts, scripts
- **AI-powered**: uses OpenClaw to analyze patterns and suggest improvements

### 💡 Suggestions
- **Categories**: performance, cost, reliability, security, config, prompt, script
- **Severities**: low, medium, high, critical
- **Human review**: approve, reject, or apply changes
- **Auto-apply**: low-risk changes can be applied automatically

### ⚡ Actions
- **File modifications**: edit prompts, scripts, configs
- **Rollback support**: restore original content anytime
- **AI-assisted**: uses OpenClaw to make precise changes

### 📊 Dashboard
- **Overview panel**: pending suggestions, recent reviews
- **Sub-tabs**: Suggestions, Reviews, Actions
- **Stats**: all-time statistics, category breakdown
- **Modal details**: full suggestion information

## Configuration

Enable proactive healing in your job config:

```yaml
jobs:
  my-job:
    name: "My Job"
    cmd: "python script.py"
    
    self_healing:
      enabled: true
      mode: proactive  # 'reactive' or 'proactive'
      
      # Schedule for proactive reviews
      review_schedule:
        frequency: daily  # hourly, daily, weekly, on_failure, on_sla_breach, manual
        time: "03:00"     # HH:MM for daily/weekly
        day: 1            # 0-6 for weekly (0=Sunday)
        min_runs: 5       # Minimum runs before first review
      
      # What to analyze
      review_scope:
        analyze_logs: true
        analyze_runs: true
        analyze_ai_sessions: true  # For OpenClaw jobs
        analyze_sla: true
        analyze_workflows: true
        analyze_script: true
        analyze_prompt: true       # For OpenClaw jobs
        analyze_config: true
      
      # How to handle suggestions
      suggestions:
        auto_apply: false          # Auto-apply low-risk changes
        auto_apply_categories: []  # Categories to always auto-apply
        min_severity_for_approval: medium  # Below this = auto-apply
        notify_on_suggestion: true
        notify_channel: whatsapp
```

## API Endpoints

### Reviews

```
GET  /api/v1/healing/reviews              # List reviews
GET  /api/v1/healing/reviews/{id}         # Get review details
POST /api/v1/healing/trigger              # Trigger manual review
```

### Suggestions

```
GET  /api/v1/healing/suggestions          # List suggestions
GET  /api/v1/healing/suggestions/{id}     # Get suggestion details
POST /api/v1/healing/suggestions/{id}/approve  # Approve suggestion
POST /api/v1/healing/suggestions/{id}/reject   # Reject suggestion
POST /api/v1/healing/suggestions/{id}/apply    # Apply approved suggestion
```

### Actions

```
GET  /api/v1/healing/actions              # List actions
GET  /api/v1/healing/actions/{id}         # Get action details
POST /api/v1/healing/actions/{id}/rollback  # Rollback action
```

### Stats

```
GET  /api/v1/healing/stats                # Global statistics
GET  /api/v1/jobs/{id}/healing/summary    # Job-specific summary
```

## Database Schema

Three new tables store healing data:

- **healing_reviews**: Analysis runs with status, duration, counts
- **healing_suggestions**: Generated suggestions with category, severity, content
- **healing_actions**: Applied changes with rollback support

Data older than 90 days is automatically cleaned up.

## How It Works

1. **Trigger**: Review is triggered by schedule, event, or manual request
2. **Collect**: Engine collects context (runs, logs, sessions, files)
3. **Analyze**: OpenClaw analyzes patterns and generates suggestions
4. **Store**: Suggestions are saved with details and severity
5. **Notify**: Alert sent via configured channel (WhatsApp, etc.)
6. **Review**: Human approves or rejects suggestions in UI
7. **Apply**: Approved changes are applied with AI assistance
8. **Rollback**: If needed, changes can be reverted

## Best Practices

### Frequency Selection
- **hourly**: High-frequency jobs with strict SLA
- **daily**: Standard jobs, good balance
- **weekly**: Low-activity jobs
- **on_failure**: Only analyze when problems occur
- **on_sla_breach**: Focus on SLA-critical jobs

### Auto-Apply
Start conservative:
```yaml
suggestions:
  auto_apply: false
  min_severity_for_approval: low  # Only auto-apply 'low' severity
```

Once confident:
```yaml
suggestions:
  auto_apply: true
  auto_apply_categories: [config, prompt]  # Safe categories
```

### Notifications
Enable notifications to stay informed:
```yaml
suggestions:
  notify_on_suggestion: true
  notify_channel: whatsapp  # or telegram, discord
```

## Troubleshooting

### Review not triggering
- Check `enabled: true` and `mode: proactive`
- Verify `min_runs` threshold is met
- Check scheduler is running (`/health` endpoint)

### Suggestions not generated
- Ensure OpenClaw CLI is available
- Check logs for analysis errors
- Verify job has enough run history

### Apply failing
- Check file permissions
- Verify paths in `affected_files`
- Review action error message in UI

## Metrics

Monitor healing effectiveness:
- **Suggestion acceptance rate**: approved / total
- **Apply success rate**: successful / attempted
- **Time to resolution**: suggestion created → applied
- **Rollback rate**: rolled_back / applied
