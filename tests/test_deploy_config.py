from pathlib import Path

import yaml


def test_compose_mounts_shared_repo_cache_for_api_and_worker():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))

    assert "repo_cache" in compose["volumes"]
    for service_name in ("api", "worker"):
        mounts = compose["services"][service_name].get("volumes", [])
        assert "repo_cache:/app/repo_cache" in mounts


def test_compose_runtime_commands_do_not_install_dev_dependencies():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))

    api_command = compose["services"]["api"]["command"]
    worker_command = compose["services"]["worker"]["command"]

    assert "uv run --no-dev alembic upgrade head" in api_command[-1]
    assert "uv run --no-dev uvicorn brain.main:app" in api_command[-1]
    assert worker_command[:3] == ["uv", "run", "--no-dev"]


def test_compose_api_healthcheck_and_worker_waits_for_api():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))

    api = compose["services"]["api"]
    worker = compose["services"]["worker"]

    assert "/health" in " ".join(api["healthcheck"]["test"])
    assert worker["depends_on"]["api"]["condition"] == "service_healthy"


def test_compose_has_caddy_tls_proxy_profile():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))

    caddy = compose["services"]["caddy"]

    assert caddy["image"].startswith("caddy:")
    assert "proxy" in caddy["profiles"]
    assert caddy["depends_on"]["api"]["condition"] == "service_healthy"
    assert "${BRAIN_HTTP_PORT:-80}:80" in caddy["ports"]
    assert "${BRAIN_HTTPS_PORT:-443}:443" in caddy["ports"]
    assert "./Caddyfile:/etc/caddy/Caddyfile:ro" in caddy["volumes"]
    assert "caddy_data:/data" in caddy["volumes"]
    assert "caddy_config:/config" in caddy["volumes"]
    assert "caddy_data" in compose["volumes"]
    assert "caddy_config" in compose["volumes"]


def test_caddyfile_proxies_public_routes_to_api():
    caddyfile = Path("Caddyfile").read_text(encoding="utf-8")

    assert "{$BRAIN_DOMAIN}" in caddyfile
    assert "reverse_proxy api:8000" in caddyfile
    assert "encode zstd gzip" in caddyfile


def test_env_example_documents_public_domain():
    env_example = Path(".env.example").read_text(encoding="utf-8")

    assert "BRAIN_DOMAIN=brain.seu-dominio.com" in env_example
    assert "BRAIN_HTTP_PORT=80" in env_example
    assert "BRAIN_HTTPS_PORT=443" in env_example


def test_compose_has_postgres_backup_profile():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))

    backup = compose["services"]["backup"]

    assert backup["image"].startswith("postgres:")
    assert "backup" in backup["profiles"]
    assert backup["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert "backups:/backups" in backup["volumes"]
    assert "./docker/backup/backup.sh:/usr/local/bin/brain-backup:ro" in backup["volumes"]
    assert backup["command"] == ["sh", "/usr/local/bin/brain-backup"]
    assert "backups" in compose["volumes"]


def test_backup_script_uses_pg_dump_and_retention():
    script = Path("docker/backup/backup.sh").read_text(encoding="utf-8")

    assert "pg_dump" in script
    assert "-Fc" in script
    assert "BRAIN_BACKUP_INTERVAL_SECONDS" in script
    assert "BRAIN_BACKUP_RETENTION_DAYS" in script
    assert "BRAIN_BACKUP_ONCE" in script
    assert "find /backups" in script


def test_env_example_documents_backup_settings():
    env_example = Path(".env.example").read_text(encoding="utf-8")

    assert "BRAIN_BACKUP_INTERVAL_SECONDS=86400" in env_example
    assert "BRAIN_BACKUP_RETENTION_DAYS=7" in env_example


def test_dockerfile_copies_lockfile_and_syncs_locked_without_dev_dependencies():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert "RUN uv sync --locked --no-dev" in dockerfile
