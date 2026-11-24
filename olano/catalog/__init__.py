"""Cloud catalog sync — providers/models (and the MCP directory) from cloud.olano.ai.

Published catalogs live on the cloud side behind public read APIs and are kept
fresh on the instance by one mechanism:

    bundled snapshot (instant, offline)  →  disk cache (<home>/catalog/)  →
    background :class:`~olano.catalog.sync.CatalogSyncService`

The sync service polls the tiny ``GET {CLOUD}/api/v1/catalog/manifest``
(per-catalog revisions) on a jittered schedule and re-fetches only the
catalogs whose revision moved, so browsing providers/models/MCPs in the UI
never waits on the network and quiet polls cost ~200 bytes.

Modules:
    store           on-disk payload + sync-state cache under <home>/catalog/
    models_registry client for the provider/model catalog (GET /api/v1/models)
    bundled         generated offline snapshot of the model catalog
    sync            CatalogSyncService — the manifest poller
"""

from olano.catalog.models_registry import (
    get_model_catalog,
    model_context_window,
    model_entry,
    model_pricing,
)
from olano.catalog.sync import CatalogSyncService, sync_once

__all__ = [
    "CatalogSyncService",
    "get_model_catalog",
    "model_context_window",
    "model_entry",
    "model_pricing",
    "sync_once",
]
