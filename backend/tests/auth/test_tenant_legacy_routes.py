"""Unused legacy APIs cannot bypass tenant scope in multi-tenant mode."""

import os
import sqlite3
import subprocess
import sys


def test_legacy_routers_are_not_exposed_in_tenant_mode(tmp_path):
    target = tmp_path / "unified.db"
    with sqlite3.connect(target) as conn:
        conn.execute("CREATE TABLE unified_metadata (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO unified_metadata VALUES (1)")
    env = os.environ.copy()
    env["LEADHUNTER_MULTI_TENANT_ENABLED"] = "1"
    env["LEADHUNTER_UNIFIED_DB_PATH"] = str(target)
    script = """
from app.api.v1.router import api_router

def paths(router):
    for route in router.routes:
        if hasattr(route, 'original_router'):
            yield from paths(route.original_router)
        elif hasattr(route, 'path'):
            yield route.path

all_paths = set(paths(api_router))
for path in ('/database', '/companies/', '/contacts/', '/research/',
             '/leadership/', '/email/', '/discovery/companies',
             '/connectors/texas-procurement'):
    assert path not in all_paths, path
assert '/health' in all_paths
assert '/phones/stats' in all_paths
"""
    result = subprocess.run(
        [sys.executable, "-c", script], env=env,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
