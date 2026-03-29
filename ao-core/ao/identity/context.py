"""Identity context — UserDelegated and ServiceIdentity modes."""

from dataclasses import dataclass
from enum import Enum
from typing import Any


class IdentityMode(Enum):
    USER_DELEGATED = "user_delegated"
    SERVICE = "service"


@dataclass
class IdentityContext:
    """Encapsulates the identity used for a given operation."""

    mode: IdentityMode
    tenant_id: str
    claims: dict[str, Any] | None = None

    # For USER_DELEGATED: the user's OBO token
    user_token: str | None = None

    # For SERVICE: the managed identity client ID
    managed_identity_client_id: str | None = None
