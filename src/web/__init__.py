from __future__ import annotations

from src.web.demo_seed import seed_demo_data
from src.web.queries import get_snapshot_at_version, get_target_detail, list_targets
from src.web.runner import ActiveRunInfo, RunManager, next_run_no
from src.web.server import create_app

__all__ = [
    "ActiveRunInfo",
    "RunManager",
    "create_app",
    "get_snapshot_at_version",
    "get_target_detail",
    "list_targets",
    "next_run_no",
    "seed_demo_data",
]
