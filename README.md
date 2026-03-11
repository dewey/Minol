# Minol Energy — Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-blue.svg)](https://hacs.xyz/)
[![HA Version](https://img.shields.io/badge/HA-2024.1%2B-blue.svg)](https://www.home-assistant.io/)

A custom [Home Assistant](https://www.home-assistant.io/) integration that reads
consumption data from the **Minol MinolApp mobile API** (Mulesoft Experience API).

Authentication uses the same Azure B2C OAuth2 flow as the official Minol iOS/Android app.
Tokens are refreshed silently in the background — you only need to log in once.

---

## Features

- **Energy Dashboard compatible** — consumption sensors use `TOTAL_INCREASING` and can be added to the HA Energy Dashboard
- **Heating, hot water, and cold water** sensors with kWh and volume values
- **CO₂ tracking** — carbon intensity sensor per service
- **Cost estimation** — configure energy prices to see estimated monthly costs
- **Configurable update interval** — 15 min to 24 h polling
- **Silent token refresh** — access tokens are renewed automatically; the refresh token is good for 14 days
- **Reauthentication flow** — HA prompts for a new login if the refresh token expires
- **Diagnostics** — export anonymized data for debugging
- **English and German** translations

---

## Sensors

For each service type available on your account (heating, hot water, cold water):

| Sensor | Unit | Description |
|---|---|---|
| Heating Latest Month | kWh | Energy consumption for the most recent complete month |
| Heating CO₂ Latest Month | kg | CO₂ equivalent for heating |
| Hot Water Latest Month | kWh | Hot water energy consumption |
| Hot Water CO₂ Latest Month | kg | CO₂ equivalent for hot water |
| Cold Water Latest Month | m³ | Cold water volume consumption |
| Cold Water CO₂ Latest Month | kg | CO₂ equivalent for cold water |

Which sensors appear depends on which services your property has metered.
Cost sensors are created automatically when a non-zero price is set in Options.

---

## How it works

```
Home Assistant                          Minol backend
 ┌─────────────────┐   Bearer token    ┌──────────────────────────────────────┐
 │  Coordinator    │──────────────────►│ Mulesoft Experience API              │
 │  (polls 1/hr)   │                   │  GET /api/app/profiles               │
 │                 │◄──────────────────│  GET /api/app/billingUnit/{}/...      │
 └────────┬────────┘   JSON data       │    /masterdata                       │
          │                            │    /consumptions/availableData        │
     sensor entities                   │    /consumptions?startdate=…&enddate=…│
                                       └──────────────────────────────────────┘

 ┌─────────────────┐
 │  Azure B2C      │  OAuth2 Authorization Code + PKCE
 │  (minolauth)    │  access_token valid 1 h, refresh_token 14 days
 └─────────────────┘
```

---

## Installation

### Via HACS (recommended)

1. Open **HACS** in Home Assistant.
2. **Integrations** → three-dot menu → **Custom repositories**.
3. Enter the repository URL, category **Integration**, click **Add**.
4. Search for **Minol Energy** and click **Download**.
5. **Restart Home Assistant.**

### Manual

Copy `custom_components/minol_energy/` into your HA `config/custom_components/` directory and restart.

---

## Setup

The integration uses the same login flow as the Minol mobile app (Azure B2C OAuth2 with PKCE).
There is no username/password field — you authenticate via your browser once.

1. **Settings → Devices & Services → Add Integration → Minol Energy**
2. Click the login link shown in the setup form.
3. Sign in with your Minol account in the browser.
4. After signing in, the browser navigates to a Postman callback page. Copy the full URL from the address bar (it starts with `https://oauth.pstmn.io/v1/callback?code=…`).
5. Paste it into the field in HA and click **Submit**.

Tokens are stored in the config entry and refreshed automatically. You will only need to repeat this if you haven't used HA for more than 14 days.

---

## Options

After setup, click **Configure** on the integration to adjust:

| Option | Default | Description |
|---|---|---|
| Update interval | 60 min | How often to poll the Minol API (15–1440 min) |
| Heating price | 0.00 €/kWh | Price per kWh for heating cost estimation |
| Hot water price | 0.00 €/kWh | Price per kWh for hot water cost estimation |
| Cold water price | 0.00 €/m³ | Price per m³ for cold water cost estimation |

Setting a price to `0` disables the corresponding cost sensor.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| **"Cannot connect"** | Check your internet connection and that the Minol app works on your phone. |
| **"Authentication failed"** | Your session may have expired. Click **Re-authenticate** on the integration card. |
| **No sensors** | Your Minol account may not have eMonitoring enabled. Contact Minol customer support. |
| **Sensors show unknown** | No consumption data was found for the last 3 months. Check the Minol app. |

### Debug logging

Add to `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.minol_energy: debug
```

Logs include every API request/response, token expiry times, and refresh attempts.

### Diagnostics

**Settings → Devices & Services → Minol Energy → three-dot menu → Download diagnostics**

Sensitive fields (tokens, email) are redacted automatically.

---

## Development

### Running live tests

The test suite makes real API calls. You need a valid token first:

```bash
uv run python scripts/get_token.py
```

The script opens the Minol B2C login URL. Sign in, copy the redirect URL from the browser
address bar, paste it when prompted, and press Enter. The script prints two `export` commands:

```bash
export MINOL_ACCESS_TOKEN="eyJ..."
export MINOL_REFRESH_TOKEN="eyJ..."
```

Run those, then execute the tests:

```bash
uv run pytest tests/test_api_live.py -v -s
```

Tests are automatically skipped when `MINOL_ACCESS_TOKEN` is not set or when running in CI.

---

## License

MIT
