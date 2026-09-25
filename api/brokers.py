# api/brokers.py — real prop-firm bridges: MetaAPI (MT4/MT5) + cTrader OpenAPI
# Orders are DRY-RUN until env LIVE_TRADING=true is set on the host.
import requests, os, json

LIVE = os.environ.get("LIVE_TRADING", "false") == "true"

META_BASE = os.environ.get("METAAPI_BASE", "https://mt-client-api-v1.agiliumtrade.agiliumtrade.io")
CT_AUTH_BASE = "https://openapi.ctrader.com"
CT_API_BASE = "https://api.spotware.com"


def _live_guard(action: str):
    if not LIVE:
        return {"status": "dry_run", "message": f"{action} validated — dry-run (set LIVE_TRADING=true to arm real orders)."}
    return None


# ---------------- MetaAPI (MT4 / MT5) ----------------

def _meta_headers(token):
    return {"auth-token": token, "Content-Type": "application/json"}


def meta_list_accounts(token):
    r = requests.get(f"{META_BASE}/users/current/accounts", headers=_meta_headers(token), timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"MetaAPI auth failed ({r.status_code}): {r.text[:200]}")
    return r.json()


def meta_connect(token, login, password, server, platform="mt5"):
    """Register/connect an MT4/MT5 account through the MetaAPI cloud bridge."""
    guard = _live_guard("MetaAPI connection")
    if guard and not token:
        raise RuntimeError("MetaAPI token required.")
    if guard:
        # dry-run: only validate the token works
        meta_list_accounts(token)
        return guard
    payload = {"login": login, "password": password, "server": server, "platform": platform}
    r = requests.post(f"{META_BASE}/users/current/accounts", headers=_meta_headers(token), json=payload, timeout=60)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"MetaAPI connect failed ({r.status_code}): {r.text[:200]}")
    return {"status": "connected", "account": r.json()}


def meta_place_market(token, account_id, symbol, side, volume, sl, tp):
    """Place a bracket market order (SL+TP attached) — same signal, real execution."""
    guard = _live_guard("MetaAPI order")
    if guard:
        return guard
    payload = {
        "actionType": "ORDER_TYPE_MARKET",
        "symbol": symbol,
        "position": side.upper(),  # BUY / SELL
        "volume": round(float(volume), 2),
        "stopLoss": round(float(sl), 8),
        "takeProfit": round(float(tp), 8),
    }
    r = requests.post(
        f"{META_BASE}/users/current/accounts/{account_id}/trade",
        headers=_meta_headers(token), json=payload, timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"MetaAPI order failed ({r.status_code}): {r.text[:200]}")
    return {"status": "placed", "response": r.json()}


# ---------------- cTrader (Spotware OpenAPI) ----------------

def ct_auth_url(client_id, redirect_uri, scope="trading"):
    return (f"{CT_AUTH_BASE}/auth?client_id={client_id}&redirect_uri={redirect_uri}"
            f"&scope={scope}&response_type=code")


def ct_exchange_code(client_id, secret, code, redirect_uri):
    """OAuth2 authorization-code swap — the flow a real cTrader connection uses."""
    r = requests.post(
        f"{CT_AUTH_BASE}/apps/token",
        data={"grant_type": "authorization_code", "code": code,
              "client_id": client_id, "client_secret": secret, "redirect_uri": redirect_uri},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"cTrader token swap failed ({r.status_code}): {r.text[:200]}")
    return r.json()  # {access_token, refresh_token, ...}


def ct_refresh(client_id, secret, refresh_token):
    r = requests.post(
        f"{CT_AUTH_BASE}/apps/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token,
              "client_id": client_id, "client_secret": secret},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"cTrader refresh failed ({r.status_code}): {r.text[:200]}")
    return r.json()


def ct_trading_accounts(access_token):
    r = requests.get(f"{CT_API_BASE}/connect/tradingaccounts",
                     headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"cTrader accounts failed ({r.status_code}): {r.text[:200]}")
    return r.json()


def ct_validate(client_id, secret, account_id=None):
    """Validate the app credentials (client credentials grant gives app-level access)."""
    guard = _live_guard("cTrader connection")
    r = requests.post(
        f"{CT_AUTH_BASE}/apps/token",
        data={"grant_type": "client_credentials", "client_id": client_id,
              "client_secret": secret, "scope": "trading"},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"cTrader auth failed ({r.status_code}): {r.text[:200]}")
    body = {"status": "connected", "scope": "app"}
    if account_id:
        body["account_id"] = account_id
        body["note"] = ("Order placement uses the OpenAPI protobuf socket (ctrader-open-api). "
                        "Tokens stored; connect the account via OAuth to authorize trading.")
    if guard:
        guard.update(body)
        return guard
    return body


def dispatch_signal(connection, signal, risk_dollars):
    """Route one Gate A signal to whichever bridge is connected. DRY-RUN by default."""
    platform = connection["platform"]
    cfg = connection.get("config", {})
    side = "BUY" if signal["dir"] == "LONG" else "SELL"
    if platform == "MetaTrader 5" or platform == "MetaTrader 4":
        volume = cfg.get("fixed_lots", 0.10)
        return meta_place_market(cfg.get("meta_token", ""), cfg.get("meta_account_id", ""),
                                 signal["symbol"], side, volume, signal["sl"], signal["tp"])
    if platform == "cTrader":
        guard = _live_guard("cTrader order")
        if guard:
            return guard
        return {"status": "queued",
                "note": "cTrader orders go through the OpenAPI socket (ProtoOANewOrderReq) — enabled once OAuth completes."}
    return {"status": "unsupported_platform", "platform": platform}
