"""Select an explicitly configured transport for the backend-owned skill."""

from .frontdesk_http import FrontdeskHttpClient
from .mcp_client import FrontdeskMcpClient


def build_frontdesk_client(settings):
    if settings.frontdesk_transport == "http":
        return FrontdeskHttpClient(settings)
    if settings.frontdesk_transport == "mcp":
        return FrontdeskMcpClient(settings)
    raise ValueError("frontdesk_transport must be http or mcp")
