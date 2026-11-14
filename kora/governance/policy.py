"""Policy-as-code. The policy file is versioned and hashed; every deletion plan
stores (version, hash) so an auditor can tell exactly which rules approved it."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..config import get_settings
from ..util import canonical_json, sha256_bytes

DEFAULT = Path(__file__).resolve().parent.parent / "policies" / "default.json"


@dataclass(frozen=True)
class Policy:
    raw: dict
    hash: str

    @property
    def version(self) -> str:
        return self.raw["version"]

    def cls(self, name: str) -> dict:
        return self.raw["classes"].get(name, {"deletable": False, "approvals": 99, "action": "NONE", "recovery": "n/a"})

    def __getitem__(self, k):
        return self.raw[k]

    def get(self, k, default=None):
        return self.raw.get(k, default)

    def label(self, name: str) -> dict:
        return self.raw["retention_labels"].get(name, {"immutable": True, "min_age_days": 36500})

    def weight(self, check: str) -> float:
        return float(self.raw.get("check_weights", {}).get(check, 1))


@lru_cache(maxsize=4)
def _load(path: str) -> Policy:
    raw = json.loads(Path(path).read_text())
    for k in ("version", "classes", "retention_labels", "system_precedence"):
        if k not in raw:
            raise ValueError(f"policy missing '{k}'")
    return Policy(raw, sha256_bytes(canonical_json(raw)))


def get_policy() -> Policy:
    p = get_settings().policy_path or DEFAULT
    return _load(str(p))


def override_policy(**changes) -> Policy:
    """Test/demo helper: returns a modified copy and installs it."""
    import copy
    import tempfile
    base = copy.deepcopy(get_policy().raw)
    for k, v in changes.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k].update(v)
        else:
            base[k] = v
    tmp = Path(tempfile.mkdtemp()) / "policy.json"
    tmp.write_text(json.dumps(base))
    from dataclasses import replace
    from ..config import configure
    configure(replace(get_settings(), policy_path=tmp))
    return get_policy()
