"""The settings keep one field (and one ``OWNVITALS_`` variable) per name after the group split."""

from pydantic_settings import EnvSettingsSource

from ownvitals.config import Settings

# The field names before the settings groups moved into ``config_*.py`` modules (issue 245).
FIELDS = frozenset(
    {
        "admin_emails",
        "ai_global_monthly_cap_microusd",
        "ai_insights",
        "ai_user_monthly_cap_microusd",
        "app_secret_key",
        "cf_connecting_ip_proxies",
        "connection_kek_id",
        "database_maint_password",
        "database_owner_password",
        "database_password",
        "database_url",
        "environment",
        "experimental_findings",
        "geo_source",
        "job_busy_requeue_limit",
        "job_lease_seconds",
        "jobs_per_user",
        "log_level",
        "maint_database_url",
        "migrations_database_url",
        "oauth_google_client_id",
        "oauth_google_client_secret",
        "passkey_satisfies_second_factor",
        "passkeys_per_account",
        "password_hash_workers",
        "public_base_url",
        "purge_interval_seconds",
        "qr_login_enabled",
        "qr_streams_per_ip",
        "qr_streams_per_process",
        "session_absolute_days",
        "session_idle_hours",
        "signup_mode",
        "smtp_host",
        "smtp_password",
        "smtp_port",
        "smtp_sender",
        "smtp_tls",
        "smtp_total_seconds",
        "smtp_username",
        "static_dir",
        "stepup_window_minutes",
        "trusted_proxies",
        "webauthn_rp_id",
        "webhook_pepper_id",
        "webhook_seed_id",
        "worker_concurrency",
        "worker_maintenance_seconds",
        "worker_poll_seconds",
        "worker_shutdown_wait_seconds",
    }
)


def test_the_field_set_is_unchanged_by_the_split() -> None:
    assert set(Settings.model_fields) == FIELDS


def test_every_field_reads_its_prefixed_environment_variable() -> None:
    # Ask Settings' own environment source which variable each field reads.
    source = EnvSettingsSource(Settings)
    for name, field in Settings.model_fields.items():
        env_names = [env_name for _, env_name, _ in source._extract_field_info(field, name)]  # pyright: ignore[reportPrivateUsage]
        assert env_names == [f"ownvitals_{name}"], name
