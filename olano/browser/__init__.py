"""Browser control for agents: policy, sessions, the live sidecar and its guards.

``config.yaml → browser:`` decides whether agents get the ``browser_*`` tools
at all (:mod:`olano.browser.policy`), and where the browser runs:

- ``headless``: one Chromium process inside the runtime, one isolated
  ``BrowserContext`` per agent (:mod:`olano.browser.sessions`);
- ``live``: one ``olano-browser`` container per agent, reached over a
  ``docker exec`` DevTools pipe and streamed into the dashboard
  (:mod:`olano.browser.sidecar`, :mod:`olano.browser.live`);
- ``provider: remote``: a hosted browser over the DevTools protocol.

Every request any of them makes passes :mod:`olano.browser.guard` (the agent's
egress allowlist plus the private-network block), and every action that would
submit, buy or send stops for a human first (:mod:`olano.browser.consequence`).
"""
