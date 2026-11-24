"""Push notifications to the Olano mobile app.

The app (``mobile/`` at the repository root) is a Capacitor shell that loads this
box's own dashboard and registers the phone's push token here, per signed-in
human. When something happens that a person should hear about while the app is
closed, this package decides who that is and asks the Olano Cloud to deliver it.

Three rules shape it:

* **The box decides, the cloud delivers.** Apple's and Google's push credentials
  can never sit on a customer box, so delivery goes through the control plane's
  ``POST /api/v1/push`` relay, authenticated with the deployment token the box
  already uses for ``/api/v1/health``. A box without that channel (a self-hosted
  install) has no push, and says so on ``GET /api/push/status``.
* **Nothing the user wrote leaves the box by default.** A notification carries a
  title, a fixed sentence and the ids the app needs to open the right screen.
  Reply text and Cortex finding titles ride along only when the operator turns on
  ``push.previews`` in ``config.yaml``, because they then pass through Apple's or
  Google's servers.
* **A notification is for someone who is not looking.** A finished reply or a
  mid-turn approval is pushed only when no dashboard is attached to that turn's
  stream. The app drops its streams when it goes to the background, so a phone in
  a pocket gets the push and a person watching the reply on a laptop does not.

Producers call :mod:`olano.push.events`. Every entry point there is a no-op unless
the runtime server started the dispatcher (:func:`dispatcher.get_push_dispatcher`
with ``start=True``), so the CLI, tests and one-shot runs never send anything.
"""

from __future__ import annotations

#: The kinds of notification a device can opt in or out of. Order is the order
#: the app's settings sheet lists them in.
EVENT_KINDS: tuple[str, ...] = ("replies", "approvals", "cortex", "alerts")

__all__ = ["EVENT_KINDS"]
