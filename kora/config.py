"""Runtime settings, loaded from KORA_* environment variables.

Secrets are never given defaults in production mode. In dev mode (the default
when KORA_ENV != "production") missing keys are generated once and persisted
under <data_dir>/secrets with 0600 permissions so restarts keep working.
"""
from __future__ import annotations

import base64
import logging
import os
import secrets
from dataclasses import dataclass, field, replace
from pathlib import Path

log = logging.getLogger(__name__)


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(f"KORA_{name}", default)


def _env_bool(name: str, default: bool) -> bool:
    v = _env(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    v = _env(name)
    return int(v) if v else default


@dataclass(frozen=True)
class Settings:
    env: str = "dev"
    data_dir: Path = Path("./.kora")
    database_url: str = ""
    blob_backend: str = "local"            # local | s3
    s3_endpoint: str | None = None
    s3_bucket_prefix: str = "kora"
    s3_region: str = "us-east-1"
    sandbox: str = "subprocess"            # subprocess | inline
    sandbox_timeout_s: int = 900
    sandbox_mem_mb: int = 4096
    media_timeout_s: int = 7200            # recordings: speech recognition on a long meeting takes a while
    max_upload_mb: int = 4096
    max_pages: int = 3000
    max_zip_uncompressed_mb: int = 2048
    ocr: str = "auto"                      # auto | off
    asr: str = "auto"                      # auto | off  (offline speech recognition when no transcript exists)
    whisper_model: str = "small"           # faster-whisper size name, or a local model directory
    model_cache_dir: str = ""              # where downloaded models live; sandboxed runs are offline
    diarization: str = "anonymous"         # anonymous | off  (who-spoke-when, labels only, no voiceprints kept)
    allow_local_ingest: bool = False       # dashboard may add a folder on THIS machine (set by `kora dashboard`)
    pptx_renderer: str = "none"            # none | libreoffice
    slide_store: str = "lazy"              # lazy (pages stored only when a compaction needs them) | eager
    capsule_audio: bool = True
    policy_path: Path | None = None
    worker_lease_s: int = 120
    log_level: str = "INFO"
    retain_originals: bool = False         # KORA is not a second copy of the source systems
    # secrets (bytes); populated by load_settings()
    capsule_signing_key: bytes = field(default=b"", repr=False)
    audit_mac_key: bytes = field(default=b"", repr=False)
    blob_encryption_key: bytes | None = field(default=None, repr=False)

    @property
    def is_production(self) -> bool:
        return self.env == "production"


def _load_secret(data_dir: Path, name: str, env_name: str, required_in_prod: bool, env: str,
                 generate: bool = True) -> bytes | None:
    raw = _env(env_name)
    if raw:
        key = base64.b64decode(raw)
        if len(key) < 32:
            raise ValueError(f"KORA_{env_name} must decode to at least 32 bytes")
        return key
    if env == "production" and required_in_prod:
        raise RuntimeError(f"KORA_{env_name} is required in production")
    if not generate:
        return None
    sdir = data_dir / "secrets"
    sdir.mkdir(parents=True, exist_ok=True)
    os.chmod(sdir, 0o700)
    path = sdir / f"{name}.key"
    if path.exists():
        return path.read_bytes()
    key = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    log.warning("generated dev secret %s (set KORA_%s in production)", path, env_name)
    return key


def load_settings() -> Settings:
    env = _env("ENV", "dev") or "dev"
    data_dir = Path(_env("DATA_DIR", "./.kora")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    db_url = _env("DATABASE_URL") or f"sqlite:///{data_dir / 'kora.db'}"
    policy = _env("POLICY_PATH")
    s = Settings(
        env=env,
        data_dir=data_dir,
        database_url=db_url,
        blob_backend=_env("BLOB_BACKEND", "local"),
        s3_endpoint=_env("S3_ENDPOINT"),
        s3_bucket_prefix=_env("S3_BUCKET_PREFIX", "kora"),
        s3_region=_env("S3_REGION", "us-east-1"),
        sandbox=_env("SANDBOX", "subprocess"),
        sandbox_timeout_s=_env_int("SANDBOX_TIMEOUT_S", 900),
        sandbox_mem_mb=_env_int("SANDBOX_MEM_MB", 4096),
        media_timeout_s=_env_int("MEDIA_TIMEOUT_S", 7200),
        max_upload_mb=_env_int("MAX_UPLOAD_MB", 4096),
        max_pages=_env_int("MAX_PAGES", 3000),
        max_zip_uncompressed_mb=_env_int("MAX_ZIP_UNCOMPRESSED_MB", 2048),
        ocr=_env("OCR", "auto"),
        asr=_env("ASR", "auto"),
        whisper_model=_env("WHISPER_MODEL", "small"),
        model_cache_dir=_env("MODEL_CACHE_DIR", ""),
        diarization=_env("DIARIZATION", "anonymous"),
        allow_local_ingest=_env("ALLOW_LOCAL_INGEST", "0") in ("1", "true", "yes"),
        pptx_renderer=_env("PPTX_RENDERER", "none"),
        slide_store=_env("SLIDE_STORE", "lazy"),
        capsule_audio=_env_bool("CAPSULE_AUDIO", True),
        policy_path=Path(policy) if policy else None,
        worker_lease_s=_env_int("WORKER_LEASE_S", 120),
        log_level=_env("LOG_LEVEL", "INFO"),
        retain_originals=_env_bool("RETAIN_ORIGINALS", False),
    )
    return replace(
        s,
        capsule_signing_key=_load_secret(data_dir, "capsule", "CAPSULE_SIGNING_KEY", True, env),
        audit_mac_key=_load_secret(data_dir, "audit", "AUDIT_MAC_KEY", True, env),
        blob_encryption_key=_load_secret(data_dir, "blob", "BLOB_ENCRYPTION_KEY", False, env,
                                         generate=_env_bool("ENCRYPT_BLOBS", False)),
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def configure(settings: Settings) -> None:
    """Override settings (tests, CLI)."""
    global _settings
    _settings = settings
