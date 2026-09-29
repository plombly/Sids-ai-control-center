#!/usr/bin/env python3
from pathlib import Path

root = Path(__file__).resolve().parents[1]
worker = (root / 'services/worker/worker.py').read_text()
orch = (root / 'services/orchestrator/orchestrator.py').read_text()

checks = {
    'role token budgets': 'ROLE_TOKEN_DEFAULTS' in worker and 'configured ceiling' in worker,
    'role runtime budgets': 'ROLE_RUNTIME_DEFAULTS' in worker and 'TIMEOUT_SECONDS' in worker,
    'effective usage guard': 'parse_codex_log(log_path)' in worker and 'live_effective >= enforcement_budget' in worker and 'cached input excluded' in worker,
    'effective budget headroom': 'ROLE_ENFORCEMENT_DEFAULTS' in worker and '"builder": 100000' in worker and '"reviewer": 60000' in worker,
    'narrow execution contract': 'Do not inventory or read the whole repository' in worker,
    'compact planner manifest': 'repository_manifest()' in orch and 'git", "ls-files' in orch,
    'planner scope contract': '"scope": ["likely/relevant/path"]' in orch and 'scoped_builder_prompt' in orch,
    'bounded repair findings': 'findings[-6000:]' in orch,
    'terminal repair exhaustion': '"status": "repair_exhausted"' in orch,
    'final repair in-flight safety': (
        'existing = builder.get("repair_job_id")' in orch
        and 'if attempts >= MAX_REPAIR_ATTEMPTS:' in orch
        and orch.index('existing = builder.get("repair_job_id")')
        < orch.index('if attempts >= MAX_REPAIR_ATTEMPTS:')
    ),
    'bounded builder context packet': 'scoped_context_packet' in orch and 'CONTEXT_TOTAL_CHARS' in orch and '"6000"' in orch,
    'exact-file context only': 'candidates = [path] if path.is_file() else []' in orch,
    'atomic goal enforcement': 'atomic goal requires exactly one job' in orch and '--atomic' in (root / 'scripts/submit-goal.py').read_text(),
    'review candidate diff packet': 'candidate_diff' in worker and 'at 12000 chars' in worker,
    'efficiency telemetry': '"prompt_chars"' in worker and '"effective_tokens"' in worker and '"command_count"' in worker,
    'completed-turn safety': 'turn_completed' in worker and 'Never' in worker and 'successfully completed turn' in worker,
    'known validation environment': '/tmp/sid-agent-venv/bin/python -m pytest' in worker,
}
failed = [name for name, ok in checks.items() if not ok]
for name, ok in checks.items():
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
raise SystemExit(1 if failed else 0)
