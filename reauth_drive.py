"""Re-mint secrets/token.json by running the Google OAuth consent flow once.

Use this when Drive stops working with `invalid_grant` (the refresh token expired
or was revoked). Run it locally on a machine with a browser:

    python reauth_drive.py

It opens a browser, you approve, and a fresh token.json is written. NOTE: if your
OAuth consent screen is still in "Testing" mode, the new token also expires in
7 days — set up a service account (secrets/service_account.json) for a permanent
fix, or publish the OAuth app.
"""
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SECRETS = Path(__file__).parent / "secrets"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

flow = InstalledAppFlow.from_client_secrets_file(str(SECRETS / "credentials.json"), SCOPES)
creds = flow.run_local_server(port=0)
(SECRETS / "token.json").write_text(creds.to_json())
print("Wrote fresh", SECRETS / "token.json")
