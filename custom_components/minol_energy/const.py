"""Constants for the Minol Energy integration."""

DOMAIN = "minol_energy"

BASE_URL = "https://webservices.minol.com"

# Entry point that triggers the SAML/Azure B2C redirect flow.
LOGIN_ENTRY_URL = f"{BASE_URL}/?redirect2=true"

# Legacy SAP form-based login – no longer used, kept for reference.
LOGIN_URL = (
    f"{BASE_URL}/irj/servlet/prt/portal/prttarget/uidpwlogon"
    "/prtroot/com.sap.portal.navigation.portallauncher.default"
)
J_SECURITY_CHECK_URL = (
    f"{BASE_URL}/irj/servlet/prt/portal/prtroot/j_security_check"
)
EMDATA_REST = f"{BASE_URL}/minol.com~kundenportal~em~web/rest/EMData"
NUDATA_REST = f"{BASE_URL}/minol.com~kundenportal~em~web/rest/NuData"

DEFAULT_SCAN_INTERVAL = 3600  # 1 hour in seconds

CONF_SCAN_INTERVAL = "scan_interval"
CONF_HEATING_PRICE = "heating_price"
CONF_HOT_WATER_PRICE = "hot_water_price"
CONF_COLD_WATER_PRICE = "cold_water_price"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
