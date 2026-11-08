"""The provider ``error`` values the OAuth callback logs by name (#247). Moved out of
``oauth_routes`` unchanged (#249)."""

# RFC 6749 4.1.2.1 and OIDC Core 3.1.2.6: the ``error`` values logged by name; any other is
# logged as ``unrecognized`` (the query is the browser's, so its text is never logged).
OAUTH_ERRORS = frozenset(
    {
        "invalid_request",
        "unauthorized_client",
        "access_denied",
        "unsupported_response_type",
        "invalid_scope",
        "server_error",
        "temporarily_unavailable",
        "interaction_required",
        "login_required",
        "account_selection_required",
        "consent_required",
        "invalid_request_uri",
        "invalid_request_object",
        "request_not_supported",
        "request_uri_not_supported",
        "registration_not_supported",
    }
)
