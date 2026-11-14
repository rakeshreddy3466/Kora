"""Test fixtures.

`ingested` runs the full pipeline over the sample corpus ONCE per session
(inline sandbox for speed); `world` gives every test its own copy of that
state - database, blob store and source folders - so tests can delete,
restore and tamper freely without affecting each other.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _configure(data_dir: Path, **env):
    os.environ["KORA_DATA_DIR"] = str(data_dir)
    os.environ["KORA_SANDBOX"] = env.get("sandbox", "inline")
    from kora import clock
    from kora.config import configure, load_settings
    clock.reset()
    configure(load_settings())
    from kora import bootstrap
    from kora.governance.policy import _load
    _load.cache_clear()
    return bootstrap.init(json_logs=False)


@pytest.fixture(scope="session")
def samples(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("samples")
    src = ROOT / "samples"
    if (src / "principals.json").exists() and os.environ.get("KORA_REGEN_SAMPLES") != "1":
        shutil.copytree(src, d, dirs_exist_ok=True)
    else:
        subprocess.run([sys.executable, str(ROOT / "scripts" / "make_samples.py"), str(d)], check=True)
    return d


@pytest.fixture(scope="session")
def ingested(tmp_path_factory, samples) -> Path:
    base = tmp_path_factory.mktemp("base")
    shutil.copytree(samples, base / "sources")
    _configure(base)
    from kora import bootstrap, db
    from kora.jobs import Worker
    with db.session_scope() as s:
        bootstrap.load_samples(s, base / "sources")
        bootstrap.scan_all(s)
    Worker(lease_s=600).drain()
    db.engine().dispose()
    return base


@pytest.fixture
def world(tmp_path, ingested):
    """Isolated copy of the ingested state."""
    d = tmp_path / "w"
    shutil.copytree(ingested, d)
    con = sqlite3.connect(d / "kora.db")
    for name, cfg in con.execute("select name, config from sources").fetchall():
        c = json.loads(cfg)
        c["root"] = c["root"].replace(str(ingested), str(d))
        con.execute("update sources set config=? where name=?", (json.dumps(c), name))
    con.commit()
    con.close()
    _configure(d)
    yield d
    from kora import db
    db.engine().dispose()


@pytest.fixture
def no_saving_rule(world):
    """The synthetic decks are a few KB of text: page packs can be as large as the file, so the
    net-saving guard (correctly) refuses. Tests of the recovery proof itself switch it off."""
    from kora.governance.policy import override_policy
    override_policy(revisions={"min_net_saving_ratio": -10.0})


@pytest.fixture
def ids():
    from kora.security.auth import Identity
    return {
        "carol": Identity("carol", frozenset({"analyst", "viewer"}), ("network-team", "core-team")),
        "alice": Identity("alice", frozenset({"viewer"}), ("network-team",)),
        "frank": Identity("frank", frozenset({"viewer"}), ()),
        "dave": Identity("dave", frozenset({"approver"}), ("governance",)),
        "gina": Identity("gina", frozenset({"approver", "auditor"}), ("governance",)),
        "erin": Identity("erin", frozenset({"operator"}), ("governance",)),
    }


@pytest.fixture
def drain():
    from kora.jobs import Worker
    return lambda: Worker(lease_s=600).drain()


@pytest.fixture
def fresh_db(tmp_path):
    """Empty KORA instance (no samples) for unit-level tests of queue/audit/blobs."""
    _configure(tmp_path / "e")
    yield tmp_path / "e"
    from kora import db
    db.engine().dispose()
