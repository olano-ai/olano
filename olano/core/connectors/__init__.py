"""Generic OAuth connector broker behind the dashboard "Connections" tab.

Importing this package loads every provider module (registration side
effect). Public surface re-exported here; see ``base.py`` for the flow
machinery and ``registry.py`` for provider lookup / credential exposure.
"""

from olano.core.connectors import providers as _providers  # noqa: F401  (registers all)
from olano.core.connectors.base import (
    ConnectorProvider,
    DeviceAuth,
    OAuthSession,
    auth_failure,
    auth_methods,
    byo_client_available,
    connect_methods,
    connector_auth_status,
    disconnect_connector,
    extract_exposed_field,
    get_live_record,
    is_connector_shared,
    mark_auth_failed,
    poll_connector_auth,
    refresh_record_now,
    relay_capable,
    resolve_connector_callback,
    set_connector_shared,
    start_connector_auth,
)
from olano.core.connectors.device import (
    cancel_device_auth,
    poll_device_auth,
    start_device_auth,
)
from olano.core.connectors.registry import (
    all_statuses,
    exposed_credential_names,
    exposing_provider,
    get_provider,
    provider_ids,
    register,
    resolve_exposed_credential,
)

__all__ = [
    "ConnectorProvider",
    "DeviceAuth",
    "OAuthSession",
    "all_statuses",
    "auth_failure",
    "auth_methods",
    "byo_client_available",
    "cancel_device_auth",
    "connect_methods",
    "connector_auth_status",
    "disconnect_connector",
    "exposed_credential_names",
    "exposing_provider",
    "extract_exposed_field",
    "get_live_record",
    "get_provider",
    "is_connector_shared",
    "mark_auth_failed",
    "poll_connector_auth",
    "poll_device_auth",
    "provider_ids",
    "refresh_record_now",
    "register",
    "relay_capable",
    "resolve_connector_callback",
    "resolve_exposed_credential",
    "set_connector_shared",
    "start_connector_auth",
    "start_device_auth",
]
