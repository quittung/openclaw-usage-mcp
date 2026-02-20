"""
OpenClaw device pairing and authentication for local WebSocket clients.

WHY THIS EXISTS
---------------
OpenClaw's gateway uses a device-identity model: every client has a stable
deviceId (SHA-256 of its Ed25519 public key), and the gateway issues scoped
device tokens tied to that identity. Simple shared-token auth (gateway.auth.token
in openclaw.json) is intended for the web control UI — it authenticates but
doesn't resolve per-device scopes, causing RPC calls to fail with scope errors.

The correct path for a backend client is:
  1. Generate an Ed25519 keypair → derive a stable deviceId
  2. Pair once using the gateway's shared auth token
  3. Receive and persist a device-specific token
  4. On each subsequent connection, present deviceId + device token

WHY ED25519 SIGNING IS REQUIRED EVEN FOR LOCAL CONNECTIONS
----------------------------------------------------------
The OpenClaw protocol skips the *nonce* requirement for loopback connections
(so signature replay from another session isn't a concern), but the Ed25519
signature over the payload IS still verified for all connections. The payload
is a pipe-delimited string:

  v1|{deviceId}|{clientId}|{clientMode}|{role}|{scopes}|{signedAtMs}|{token}

The token in the payload is what's being authenticated (gateway token on first
connect, device token thereafter), so the signature binds the key to the token
and the session parameters. A new timestamp is used on every connect, so there's
no static credential to replay.

HOW FIRST-RUN PAIRING WORKS
----------------------------
On first connect, the gateway's shared auth token (gateway.auth.token from
openclaw.json, or the OPENCLAW_GATEWAY_TOKEN env var) is used to authenticate.
For loopback connections from an unknown device, the gateway auto-pairs silently
and inline during the connect handshake — it creates and immediately approves
the pairing request before responding. The hello-ok response contains the issued
device token, which is stored and used for all subsequent connections.

There is no pending-approval window and no race condition: the auto-approval is
atomic within the gateway's connect handler.

HOW TOKEN ROTATION WORKS
-------------------------
The gateway may issue a fresh device token on every connect (rotation). The
returned token is stored in creds.token in-memory. Callers should persist
updated credentials to avoid unnecessary re-pairing if the token changes.

CLIENT ID CHOICE
----------------
We use clientId "gateway-client" with mode "backend" — the canonical values
for programmatic backend clients in the OpenClaw client ID registry. Using
"cli" would misrepresent this client in gateway audit logs.

USAGE
-----
    from device_auth import bootstrap, connect, CREDENTIALS_PATH

    # First run: reads gateway token from config, auto-pairs the device
    # Subsequent runs: loads stored credentials, connects with device token
    creds = await bootstrap("ws://127.0.0.1:18789")

    ws = await connect("ws://127.0.0.1:18789", creds)

Override credentials path with OPENCLAW_USAGE_MCP_CREDENTIALS env var.
Override gateway token with OPENCLAW_GATEWAY_TOKEN env var.
"""

import asyncio
import base64
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, PublicFormat, load_pem_private_key,
)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

CREDENTIALS_PATH = Path(
    os.environ.get(
        "OPENCLAW_USAGE_MCP_CREDENTIALS",
        Path.home() / ".config" / "openclaw-usage-mcp" / "device.json",
    )
)

# Minimal scopes for usage/cost queries — no write or admin access.
SCOPES = ["operator.read"]

# Canonical gateway registry values for a programmatic backend client.
CLIENT_ID = "gateway-client"
CLIENT_MODE = "backend"


# ---------------------------------------------------------------------------
# Gateway config helpers
# ---------------------------------------------------------------------------

def _openclaw_config_path() -> Path:
    return Path(os.environ.get("OPENCLAW_CONFIG_PATH", Path.home() / ".openclaw" / "openclaw.json"))


def _read_gateway_auth_token() -> str:
    """Read the gateway shared auth token from openclaw.json.

    Checks OPENCLAW_GATEWAY_TOKEN env var first (same convention used by the
    openclaw CLI --token flag), then falls back to reading the config file.
    """
    env_token = os.environ.get("OPENCLAW_GATEWAY_TOKEN")
    if env_token:
        return env_token
    config_path = _openclaw_config_path()
    if not config_path.exists():
        raise RuntimeError(
            f"[device_auth] openclaw config not found at {config_path}. "
            "Set OPENCLAW_GATEWAY_TOKEN env var to the gateway.auth.token value."
        )
    with open(config_path) as f:
        config = json.load(f)
    token = config.get("gateway", {}).get("auth", {}).get("token")
    if not token:
        raise RuntimeError(
            "[device_auth] gateway.auth.token not found in openclaw.json. "
            "Set OPENCLAW_GATEWAY_TOKEN env var, or configure gateway.auth.mode=token."
        )
    return token


# ---------------------------------------------------------------------------
# Credential model
# ---------------------------------------------------------------------------

@dataclass
class DeviceCredentials:
    device_id: str
    private_key_pem: str
    token: str | None = None  # device token (None before first successful pairing)

    @classmethod
    def generate(cls) -> "DeviceCredentials":
        """Generate a new Ed25519 keypair and derive the deviceId from it.

        deviceId = SHA-256(raw_public_key_bytes).hex(), matching the gateway's
        deriveDeviceIdFromPublicKey() implementation.
        """
        private_key = Ed25519PrivateKey.generate()
        raw_pub = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        device_id = hashlib.sha256(raw_pub).hexdigest()
        pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
        return cls(device_id=device_id, private_key_pem=pem)

    def _private_key(self) -> Ed25519PrivateKey:
        return load_pem_private_key(self.private_key_pem.encode(), password=None)

    def public_key_b64url(self) -> str:
        """Raw public key as URL-safe base64 without padding (gateway wire format)."""
        raw = self._private_key().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def sign(self, scopes: list[str], signed_at_ms: int, auth_token: str) -> str:
        """Build and sign the connect payload string, returning base64url signature.

        Payload format (v1, no nonce — for local/loopback connections):
          v1|{deviceId}|{clientId}|{clientMode}|{role}|{scopes}|{signedAtMs}|{token}

        auth_token is the gateway shared token on first connect, and the device
        token on subsequent connects. It's included in the signed payload so the
        signature binds the key to the specific auth credential being used.
        """
        scope_str = ",".join(scopes)
        payload = f"v1|{self.device_id}|{CLIENT_ID}|{CLIENT_MODE}|operator|{scope_str}|{signed_at_ms}|{auth_token}"
        sig_bytes = self._private_key().sign(payload.encode("utf-8"))
        return base64.urlsafe_b64encode(sig_bytes).rstrip(b"=").decode()

    def to_dict(self) -> dict:
        return {
            "deviceId": self.device_id,
            "privateKeyPem": self.private_key_pem,
            "token": self.token,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DeviceCredentials":
        return cls(
            device_id=d["deviceId"],
            private_key_pem=d["privateKeyPem"],
            token=d.get("token"),
        )


# ---------------------------------------------------------------------------
# Credential persistence
# ---------------------------------------------------------------------------

def load_credentials(path: Path = CREDENTIALS_PATH) -> DeviceCredentials | None:
    if not path.exists():
        return None
    with open(path) as f:
        return DeviceCredentials.from_dict(json.load(f))


def save_credentials(creds: DeviceCredentials, path: Path = CREDENTIALS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(creds.to_dict(), f, indent=2)


# ---------------------------------------------------------------------------
# WebSocket handshake
# ---------------------------------------------------------------------------

async def _handshake(
    gateway_url: str,
    creds: DeviceCredentials,
    scopes: list[str],
    auth_token: str,
) -> tuple[websockets.WebSocketClientProtocol, str | None]:
    """Run the OpenClaw connect handshake.

    auth_token is what goes in params.auth.token and in the signature payload:
    - First connect (pairing): the gateway's shared auth token
    - Subsequent connects: the device token

    Signs a fresh payload on every call (new timestamp), so there's no
    replayable static credential.

    Returns (websocket, issued_device_token_or_None).
    Raises RuntimeError on auth failure.
    """
    parsed = urlparse(gateway_url)
    origin = f"http://{parsed.hostname}:{parsed.port or 80}"
    ws = await websockets.connect(gateway_url, additional_headers={"Origin": origin})

    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        if msg.get("type") == "event" and msg.get("event") == "connect.challenge":
            break

    signed_at_ms = int(time.time() * 1000)
    signature = creds.sign(scopes, signed_at_ms, auth_token)

    req_id = str(uuid.uuid4())
    await ws.send(json.dumps({
        "type": "req", "id": req_id, "method": "connect",
        "params": {
            "minProtocol": 3, "maxProtocol": 3,
            "client": {
                "id": CLIENT_ID,
                "version": "1.0",
                "platform": "linux",
                "mode": CLIENT_MODE,
            },
            "role": "operator",
            "scopes": scopes,
            "auth": {"token": auth_token},
            "device": {
                "id": creds.device_id,
                "publicKey": creds.public_key_b64url(),
                "signature": signature,
                "signedAt": signed_at_ms,
                # nonce omitted — not required for loopback connections
            },
        },
    }))

    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if msg.get("type") == "res" and msg.get("id") == req_id:
            if not msg.get("ok"):
                await ws.close()
                raise RuntimeError(f"Connect failed: {msg.get('error')}")
            issued_token = msg.get("payload", {}).get("auth", {}).get("deviceToken")
            return ws, issued_token


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def bootstrap(
    gateway_url: str,
    credentials_path: Path = CREDENTIALS_PATH,
    scopes: list[str] = SCOPES,
) -> DeviceCredentials:
    """Load stored credentials, or run the first-time pairing flow.

    First-time flow:
      1. Generate a new Ed25519 keypair + deviceId
      2. Read the gateway shared auth token from openclaw.json
         (or OPENCLAW_GATEWAY_TOKEN env var)
      3. Connect to the gateway using the shared token — for loopback
         connections with an unknown device, the gateway auto-pairs silently
         and inline (creates + immediately approves the pairing request within
         the connect handler) and returns a device token in hello-ok
      4. Persist credentials (deviceId, private key, device token) to disk

    Subsequent calls just load and return stored credentials.
    """
    creds = load_credentials(credentials_path)
    if creds is not None:
        return creds

    print(f"[device_auth] No credentials at {credentials_path}. Pairing with gateway...")
    creds = DeviceCredentials.generate()
    gateway_token = _read_gateway_auth_token()

    ws, issued_token = await _handshake(gateway_url, creds, scopes, auth_token=gateway_token)
    await ws.close()

    if not issued_token:
        raise RuntimeError(
            "[device_auth] Gateway did not issue a device token after connect. "
            "This may mean the gateway requires manual device approval — "
            "check `openclaw devices list` for a pending request and approve it, "
            "then retry."
        )

    creds.token = issued_token
    save_credentials(creds, credentials_path)
    print(f"[device_auth] Paired successfully. Device ID: {creds.device_id}")
    return creds


async def connect(
    gateway_url: str,
    creds: DeviceCredentials,
    scopes: list[str] = SCOPES,
) -> websockets.WebSocketClientProtocol:
    """Open an authenticated WebSocket connection using stored device credentials.

    Uses creds.token (the device token) as the auth credential. If the gateway
    rotates the token on reconnect, creds.token is updated in-memory — callers
    should save updated credentials to persist the new token across restarts.
    """
    if not creds.token:
        raise RuntimeError("[device_auth] No device token stored. Run bootstrap() first.")
    ws, issued_token = await _handshake(gateway_url, creds, scopes, auth_token=creds.token)
    if issued_token:
        creds.token = issued_token
    return ws
