"""Starts CPM on a throwaway data dir with an in-memory secret store, for tests/ui/e2e.mjs.

Usage: python tests/ui/serve_for_e2e.py <data_dir> <port> <worktree_root>
No real key, no network: runs are MockProvider scenarios only.
"""

import sys
from pathlib import Path

import uvicorn

from cpm.api.app import create_app
from cpm.config import Settings
from cpm.security.secrets_store import MemorySecretStore

data, port, wt = Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3])
settings = Settings(
    host="127.0.0.1", port=port, data_dir=data, log_level="WARNING", worktree_root=wt
)
uvicorn.run(
    create_app(settings, secret_store=MemorySecretStore()),
    host="127.0.0.1",
    port=port,
    log_config=None,
)
