"""Consent-based customer↔customer connections for multi-user agents.

The invite-code flow: customer A asks the agent for an invite, the platform
mints a random code and a ready-to-forward message, A shares it out-of-band,
and customer B accepts by sending the pasted ``connect <CODE>`` line to the
agent in B's OWN authenticated chat — redemption is the consent. Nobody ever
addresses anybody by platform id, so the no-misroutable-recipient guarantee
of multi-user mode survives: ``send_to_connection`` can only reach peers who
explicitly connected, and delivery still goes through the platform router
(which re-checks mute/block).
"""

from olano.connections.store import ConnectionsStore, erase_customer_connections
from olano.connections.tools import make_customer_connection_tools

__all__ = [
    "ConnectionsStore",
    "erase_customer_connections",
    "make_customer_connection_tools",
]
