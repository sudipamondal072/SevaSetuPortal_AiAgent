class PortalIntegrationError(Exception):
    """The existing portal could not complete the requested operation."""


class AuthorizationError(Exception):
    """The authenticated user is not allowed to perform this action."""


class ConfigurationError(Exception):
    """Required integration configuration is missing."""