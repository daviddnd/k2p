"""Serves the real vendor control plane on loopback, for the integration suites.

The command-channel test cannot import the vendor service — both deployables
name their package ``app``, and inside a backend test *this* deployable owns
the name. It cannot put the worker behind a subprocess either, because the
worker is part of this deployable. So the other side is the one that leaves:
this script imports the vendor app (from ``AICS_CP_DIR``), mounts the fake
Google identity provider beside it, binds a loopback port, and prints the base
URL the test should use. It runs until it is terminated.

The socket is the only way for one deployable to reach the other here, and it
stays local: ``127.0.0.1``, port chosen by the kernel, and the fake identity
provider rather than Google. Every credential — the vendor admin key above
all — comes from the environment this process was started with; the script
invents none and prints none (only the URL it bound).

Environment: ``AICS_CP_DIR`` (the ``control_plane`` directory) and the
vendor service's own ``AICS_CP_*`` configuration (its database URL and the
vendor admin key above all), which it reads exactly as it does in production.
"""

import json
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(os.environ["AICS_CP_DIR"]).resolve()))

import uvicorn
from fastapi.responses import RedirectResponse

from app.core.config import settings
from app.main import create_app
from tests.fake_google_idp import FakeGoogleIdP

SUBJECT = "command-channel-admin"
EMAIL = "admin@tcsp.example"


def main() -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"

    identity_provider = FakeGoogleIdP()
    settings.google_authorization_endpoint = f"{base_url}/fake-idp/authorize"
    settings.google_token_endpoint = f"{base_url}/fake-idp/token"
    settings.google_userinfo_endpoint = f"{base_url}/fake-idp/userinfo"
    settings.google_redirect_uri = f"{base_url}/api/v1/auth/google/callback"
    settings.google_client_id = "command-channel-client"
    settings.google_client_secret = "command-channel-secret"
    settings.state_secret = settings.state_secret or "command-channel-state-secret"

    app = create_app()

    @app.get("/fake-idp/authorize")
    async def authorize(redirect_uri: str = "", state: str = "") -> RedirectResponse:
        """The browser leg the fake identity provider leaves to its caller.

        The real Google would show a consent screen; the test just needs the
        redirect back to the control plane's callback with a code for the one
        identity this fake knows. Declared before the mount so it wins the
        path match.
        """
        code = identity_provider.issue_code(subject=SUBJECT, email=EMAIL)
        return RedirectResponse(f"{redirect_uri}?code={code}&state={state}", status_code=302)

    app.mount("/fake-idp", identity_provider.app)

    print(json.dumps({"base_url": base_url}), flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[listener])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
