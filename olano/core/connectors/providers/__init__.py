"""Provider definitions — one module per service, registered at import time.

Adding a connector = one module here (a ``ConnectorProvider`` passed to
``registry.register``) + a frontend card in
``dashboard_2.0/src/views/connections/types.ts``.
"""

from olano.core.connectors.providers import (  # noqa: F401
    buffer,
    docusign,
    fitbit,
    github,
    hubspot,
    linkedin,
    meta,
    microsoft365,
    notion,
    quickbooks,
    reddit,
    salesforce,
    shopify,
    slack,
    x_twitter,
)
