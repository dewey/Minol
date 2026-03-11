"""Constants for the Minol Energy integration."""

DOMAIN = "minol_energy"

# ---------------------------------------------------------------------------
# Mobile app (Mulesoft) API – captured from MinolApp iOS traffic (Charles Proxy)
# ---------------------------------------------------------------------------

# Azure B2C authorization endpoint (starts the OAuth2 browser flow)
B2C_AUTH_URL = (
    "https://minolauth.b2clogin.com/tfp/"
    "minolauth.onmicrosoft.com/"
    "B2C_1A_SEAMLESS_MIGRATION_AND_GROUPS/oauth2/v2.0/authorize"
)

# Azure B2C token endpoint (code exchange and token refresh)
B2C_TOKEN_URL = (
    "https://minolauth.b2clogin.com/tfp/"
    "minolauth.onmicrosoft.com/"
    "B2C_1A_SEAMLESS_MIGRATION_AND_GROUPS/oauth2/v2.0/token"
)

# MSAL app registration client ID (iOS app, production tenant)
B2C_CLIENT_ID = "b751cea9-de3f-498b-9dcf-33a22a28d578"

# Redirect URI registered in the B2C app (Postman OAuth2 callback)
B2C_REDIRECT_URI = "https://oauth.pstmn.io/v1/callback"

# Client secret required for the authorization code exchange with the Postman redirect URI
B2C_CLIENT_SECRET = "uKy8Q~r0FdaYeCotNNT0390HW2yoN-rk7srD1cbR"

# OAuth2 scopes requested by the mobile app
B2C_SCOPES = " ".join([
    "openid",
    "profile",
    "offline_access",
    "https://minolauth.onmicrosoft.com/"
    "e7cf3202-37a5-4b92-aae1-9e4f675ad9ed/access_as_user",
])

# Mulesoft Experience API (production)
API_BASE_URL = "https://minol-prod-minol-app-eapi.de-c1.eu1.cloudhub.io/api/app"

# Mulesoft API client credentials (shared, embedded in every app copy)
API_CLIENT_ID = "e846b9d0008344d58c532147ba3fa6d1"
API_CLIENT_SECRET = "a1DCEd2605424FecBB1046CbBD4ae3d6"

# App version sent in the appVersion header (API version, not app version)
APP_VERSION = "1.1"

# User-Agent mimicking the MSAL library used by the app
USER_AGENT = "MinolApp/2.11.14 (iOS; MSAL.Xamarin.iOS/4.78.0.0)"

# ---------------------------------------------------------------------------
# Service type codes  (from /lookup endpoint)
# ---------------------------------------------------------------------------
SERVICE_HEATING = "100"       # Heizung
SERVICE_HOT_WATER = "200"     # Warmwasser
SERVICE_COLD_WATER = "300"    # Kaltwasser

# ---------------------------------------------------------------------------
# Integration config / options keys
# ---------------------------------------------------------------------------
DEFAULT_SCAN_INTERVAL = 3600  # 1 hour in seconds

CONF_SCAN_INTERVAL = "scan_interval"
CONF_HEATING_PRICE = "heating_price"
CONF_HOT_WATER_PRICE = "hot_water_price"
CONF_COLD_WATER_PRICE = "cold_water_price"

# Config entry data keys for OAuth2 tokens
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
