"""Constants for the PitPat integration."""

DOMAIN = "ha_pitpat"

AUTH_BASE_URL = "https://auth.pitpat.com"
AUTHORIZE_URL = f"{AUTH_BASE_URL}/connect/authorize"
TOKEN_URL = f"{AUTH_BASE_URL}/connect/token"

API_BASE_URL = "https://api.pitpat.com"
ACTIVITY_BASE_URL = "https://activity.pitpat.com"
LOCATION_BASE_URL = "https://location.pitpat.com"

CLIENT_ID = "PitPatAppSso"
REDIRECT_URI = "pitpatpetsso://callback"
SCOPE = "PitPatApi openid offline_access email"

# The login pages are served to the in-app browser; API calls come from the app.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/26.6.1 Mobile/15E148 Safari/604.1"
)
APP_USER_AGENT = "PitPat/456 CFNetwork/3860.700.1 Darwin/25.6.0"
API_USER_AGENT = "pitpat-mobile-ios-2.37.11"
SIGNALR_USER_AGENT = (
    "Microsoft SignalR/10.0 (10.0.5+a612c2a1056fe3265387ae3ff7c94eba1505caf9; "
    "Unknown OS; .NET; .NET 10.0.5)"
)

CONF_REFRESH_TOKEN = "refresh_token"
CONF_USER_ID = "user_id"
