# ProcClaw Skill Example

This is an example [OpenClaw](https://github.com/openclaw/openclaw) skill for controlling ProcClaw via chat.

## What is a Skill?

Skills are markdown files that teach AI agents how to use tools. When a user asks about jobs, processes, or ProcClaw, the agent loads `SKILL.md` and follows the instructions.

## Installation

Copy `SKILL.md` to your OpenClaw workspace skills folder:

```bash
# Create skill folder
mkdir -p ~/.openclaw/workspace/skills/procclaw

# Copy skill
cp SKILL.md ~/.openclaw/workspace/skills/procclaw/
```

## What's Included

- **Basic Commands**: List, start, stop, restart jobs
- **Enterprise Features**: DLQ, concurrency, webhooks, priorities
- **OpenClaw Integration**: Running AI cron jobs with proper timeout sync
- **Troubleshooting**: Diagnostics, common issues, recovery actions
- **Workflows**: Chains, groups, chords for task composition

## OpenClaw + ProcClaw Integration

When running OpenClaw cron jobs via ProcClaw, always sync timeouts:

```yaml
# OpenClaw cron: timeoutSeconds: 600
# ProcClaw job:
oc-my-job:
  cmd: python3 oc-runner.py <job_id> 660000  # 600s + 60s buffer
  retry:
    enabled: false  # Always false for OpenClaw jobs
```

See `SKILL.md` for the full integration guide.
