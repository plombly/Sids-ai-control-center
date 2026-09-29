#!/usr/bin/env python3
from pathlib import Path

root = Path(__file__).resolve().parents[1]
worker = (root / 'services/worker/worker.py').read_text()
orch = (root / 'services/orchestrator/orchestrator.py').read_text()

checks = {
    'role token budgets': 'ROLE_TOKEN_DEFAULTS' in worker and 'Codex token budget exceeded' in worker,
    'role runtime budgets': 'ROLE_RUNTIME_DEFAULTS' in worker and 'TIMEOUT_SECONDS' in worker,
    'live usage guard': 'parse_codex_log(log_path)' in worker and 'live_total >= token_budget' in worker,
    'narrow execution contract': 'Do not inventory or read the whole repository' in worker,
    'compact planner manifest': 'repository_manifest()' in orch and 'git", "ls-files' in orch,
    'planner scope contract': '"scope": ["likely/relevant/path"]' in orch and 'scoped_builder_prompt' in orch,
    'bounded repair findings': 'findings[-6000:]' in orch,
    'terminal repair exhaustion': '"status": "repair_exhausted"' in orch,
    'bounded builder context packet': 'scoped_context_packet' in orch and 'CONTEXT_TOTAL_CHARS' in orch,
    'review candidate diff packet': 'candidate_diff' in worker and 'diff truncated by SID' in worker,
}
failed = [name for name, ok in checks.items() if not ok]
for name, ok in checks.items():
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
raise SystemExit(1 if failed else 0)
