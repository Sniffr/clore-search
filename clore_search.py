#!/usr/bin/env python3
"""
Clore.ai GPU Marketplace Searcher
Searches for cheapest GPU servers with >20GB VRAM and >50Mbps bandwidth,
and optionally rents them.
"""

import os
import requests
import sys
import time
from typing import Optional

# Fetch current BTC price in USD (cached for session)
_BTC_USD_PRICE = None


def get_btc_usd_price() -> float:
    """Get current BTC/USD exchange rate."""
    global _BTC_USD_PRICE
    if _BTC_USD_PRICE is not None:
        return _BTC_USD_PRICE
    try:
        resp = requests.get("https://api.coinbase.com/v2/exchange-rates?currency=BTC", timeout=10)
        _BTC_USD_PRICE = float(resp.json()["data"]["rates"]["USD"])
    except Exception:
        _BTC_USD_PRICE = 80000.0  # fallback
    return _BTC_USD_PRICE


def btc_to_usd(btc: float) -> float:
    """Convert BTC price to USD per day."""
    return btc * get_btc_usd_price()


# ─── Default configuration ───────────────────────────────────────────────────
DEFAULT_IMAGE = "archer304/llama.cpp:server-cuda"
DEFAULT_PORTS = {"22": "tcp", "5000": "tcp", "8080": "http"}
DEFAULT_SSH_PASSWORD = "DRpjRuu88XtvWYHnNXcB5Ksx"
DEFAULT_ENTRYPOINT = "ssh_autoinstall"
DEFAULT_CURRENCY = "USD-Blockchain"
DEFAULT_MIN_BANDWIDTH = 50  # Mbps, both up and down

DEFAULT_STARTUP_SCRIPT = """#!/bin/sh
set -e
mkdir -p /models
MODEL="/models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
URL="https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
export HF_TOKEN="hf_your_token_here"

echo "[$(date)] Starting startup script..." >> /var/log/startup.log

# Download model if not present
if [ ! -f "$MODEL" ]; then
    echo "[$(date)] Starting model download..." >> /var/log/startup.log

    # Try 1: aria2c (multi-connection, fastest that actually works)
    echo "[$(date)] Trying aria2c..." >> /var/log/startup.log
    aria2c -x 16 -s 16 -k 1M -d /models -o Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf \
        --header="Authorization: Bearer $HF_TOKEN" \
        "$URL" >> /var/log/startup.log 2>&1

    # Try 2: wget
    if [ ! -f "$MODEL" ]; then
        echo "[$(date)] aria2c failed, trying wget..." >> /var/log/startup.log
        wget --header="Authorization: Bearer $HF_TOKEN" \
            -O "$MODEL" "$URL" >> /var/log/startup.log 2>&1
    fi

    # Try 3: curl
    if [ ! -f "$MODEL" ]; then
        echo "[$(date)] wget failed, trying curl..." >> /var/log/startup.log
        curl -L -H "Authorization: Bearer $HF_TOKEN" \
            -o "$MODEL" "$URL" >> /var/log/startup.log 2>&1
    fi
fi

# Verify download
FILESIZE=$(stat -c%s "$MODEL" 2>/dev/null || echo 0)
if [ "$FILESIZE" -lt 1000000000 ]; then
    echo "[$(date)] ERROR: All download methods failed ($FILESIZE bytes)" >> /var/log/startup.log
    rm -f "$MODEL"
    exit 1
fi

echo "[$(date)] Download complete ($FILESIZE bytes)" >> /var/log/startup.log

# Also persist onstart.sh for Clore.ai platform compatibility
mkdir -p /root
cat > /root/onstart.sh << 'ONSTART'
#!/bin/sh
export LD_LIBRARY_PATH=/app:$LD_LIBRARY_PATH
/app/llama-server \
  -m /models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf \
  --host 0.0.0.0 \
  --port 8080 \
  -ngl 999 \
  -fa on \
  -c 65536 \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --no-mmap \
  --jinja >> /var/log/llama-server.log 2>&1
ONSTART
chmod +x /root/onstart.sh
echo "[$(date)] onstart.sh written ($(wc -c < /root/onstart.sh) bytes)" >> /var/log/startup.log

# Launch llama-server in the background (so startup script can exit cleanly)
echo "[$(date)] Starting llama-server..." >> /var/log/startup.log
nohup /app/llama-server \
  -m /models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf \
  --host 0.0.0.0 \
  --port 8080 \
  -ngl 999 \
  -fa on \
  -c 65536 \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --no-mmap \
  --jinja >> /var/log/llama-server.log 2>&1 &

echo "[$(date)] llama-server launched (PID: $!) on port 8080" >> /var/log/startup.log
echo "[$(date)] Startup script complete!" >> /var/log/startup.log
"""


class CloreClient:
    """Simple Python SDK for Clore.ai REST API."""

    BASE_URL = "https://api.clore.ai/v1"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {"auth": api_key}
        self.rate_limit_delay = 1.1  # Slightly above 1 req/sec limit

    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        """Make an API request with rate limiting."""
        url = f"{self.BASE_URL}{endpoint}"
        time.sleep(self.rate_limit_delay)

        response = requests.request(method, url, headers=self.headers, **kwargs)
        data = response.json()

        if data.get("code") == 5:
            print("⚠️ Rate limit hit. Waiting...")
            time.sleep(5)
            return self._request(method, endpoint, **kwargs)

        if data.get("code") == 3:
            print("❌ Invalid API key. Check your token.")
            sys.exit(1)

        if data.get("code") != 0:
            print(f"⚠️ API error (code {data.get('code')}): {data.get('error', 'Unknown')}")

        return data

    def get_marketplace(self) -> list:
        """Fetch all marketplace servers."""
        data = self._request("GET", "/marketplace")
        return data.get("servers", [])

    def create_order(self, server_id: int, image: str = DEFAULT_IMAGE,
                     ports: dict = None, ssh_password: str = None,
                     currency: str = DEFAULT_CURRENCY, order_type: str = "on-demand",
                     spot_price: float = None, env: dict = None,
                     autossh_entrypoint: str = DEFAULT_ENTRYPOINT,
                     command: str = None) -> dict:
        """Create a rental order for a server.

        Uses official Clore.ai API parameter names:
        - autossh_entrypoint (not 'entrypoint')
        - command (not 'startup_script')
        """
        payload = {
            "renting_server": server_id,
            "type": order_type,
            "currency": currency,
            "image": image,
            "ports": ports or DEFAULT_PORTS,
            "ssh_password": ssh_password or DEFAULT_SSH_PASSWORD,
            "autossh_entrypoint": autossh_entrypoint or DEFAULT_ENTRYPOINT,
        }
        if command:
            payload["command"] = command
        if env:
            payload["env"] = env
        if order_type == "spot" and spot_price:
            payload["spotprice"] = spot_price

        data = self._request("POST", "/create_order", json=payload)
        return data

    def cancel_order(self, order_id: int, issue: str = None) -> dict:
        """Cancel an existing order."""
        payload = {"id": order_id}
        if issue:
            payload["issue"] = issue
        return self._request("POST", "/cancel_order", json=payload)

    def get_orders(self, return_completed: bool = False) -> list:
        """Get your current orders."""
        params = {"return_completed": "true"} if return_completed else {}
        data = self._request("GET", "/my_orders", params=params)
        return data.get("orders", [])

    def get_wallets(self) -> list:
        """Get wallet balances."""
        return self._request("GET", "/wallets").get("wallets", [])


def extract_gpu_info(specs: dict) -> tuple:
    """Extract GPU name and VRAM from server specs.

    Returns (gpu_name, vram_gb).
    """
    gpu_raw = specs.get("gpu", "Unknown GPU")
    gpuram = specs.get("gpuram", 0)

    clean = gpu_raw.strip()
    return clean, gpuram


def filter_gpu_servers(servers: list, min_vram_gb: int = 20, currency: str = None,
                       min_bandwidth_mbps: int = 50) -> list:
    """Filter servers with GPU VRAM > min_vram_gb, bandwidth > min_bandwidth_mbps,
    and not currently rented.

    Args:
        servers: Raw marketplace server list.
        min_vram_gb: Minimum GPU VRAM in GB (exclusive).
        currency: Optional currency filter — 'bitcoin', 'CLORE-Blockchain', 'USD-Blockchain'.
                  If None, shows all servers that accept at least one currency.
        min_bandwidth_mbps: Minimum network bandwidth (both up and down) in Mbps.
    """
    results = []
    btc_usd = get_btc_usd_price()

    for server in servers:
        specs = server.get("specs", {})
        gpu_name, gpuram = extract_gpu_info(specs)

        if gpuram <= min_vram_gb:
            continue

        if server.get("rented", True):
            continue

        # Check bandwidth (specs.net.up and specs.net.down in Mbps)
        net = specs.get("net", {})
        up_speed = net.get("up", 0)
        down_speed = net.get("down", 0)
        if up_speed < min_bandwidth_mbps or down_speed < min_bandwidth_mbps:
            continue

        price_info = server.get("price", {})

        # Check allowed coins (the actual field name on the API)
        allowed = server.get("allowed_coins", [])
        if not allowed:
            # Fallback: infer from what currencies are present in pricing
            for pkey in ("on_demand", "spot"):
                for k in price_info.get(pkey, {}):
                    if k == "bitcoin":
                        allowed = ["bitcoin"]
                        break
                    elif "clore" in k.lower():
                        allowed = ["CLORE-Blockchain"]
                        break
                    elif "usd" in k.lower():
                        allowed = ["USD-Blockchain"]
                        break
                if allowed:
                    break

        # If currency filter specified, skip servers that don't support it
        if currency and currency not in allowed:
            continue

        # Extract USD prices — prefer USD-Blockchain, fall back to BTC*rate
        od_btc = price_info.get("on_demand", {}).get("bitcoin", 0)
        spot_btc = price_info.get("spot", {}).get("bitcoin", 0)

        od_usd_raw = price_info.get("on_demand", {}).get("USD-Blockchain", 0)
        spot_usd_raw = price_info.get("spot", {}).get("USD-Blockchain", 0)

        # If USD-Blockchain price is 0 but BTC is available, convert BTC->USD
        od_usd = od_usd_raw if od_usd_raw > 0 else btc_to_usd(od_btc) if od_btc > 0 else 0
        spot_usd = spot_usd_raw if spot_usd_raw > 0 else btc_to_usd(spot_btc) if spot_btc > 0 else 0

        # Also get CLORE prices
        od_clore = price_info.get("on_demand", {}).get("CLORE-Blockchain", 0)
        spot_clore = price_info.get("spot", {}).get("CLORE-Blockchain", 0)

        cc = net.get("cc", "N/A")  # country code

        results.append({
            "id": server.get("id"),
            "gpu": gpu_name,
            "vram": gpuram,
            "on_demand_usd": od_usd,
            "spot_usd": spot_usd,
            "on_demand_btc": od_btc,
            "spot_btc": spot_btc,
            "on_demand_clore": od_clore,
            "spot_clore": spot_clore,
            "cpu": specs.get("cpu", "N/A"),
            "ram": specs.get("ram", "N/A"),
            "disk": specs.get("disk", "N/A"),
            "net_up": up_speed,
            "net_down": down_speed,
            "net_cc": cc,
            "mrl": server.get("mrl", "N/A"),
            "allowed_coins": allowed,
            "reliability": server.get("reliability", 0),
        })

    # Sort by cheapest on-demand USD price first
    results.sort(key=lambda x: x["on_demand_usd"])
    return results


def print_server_table(servers: list):
    """Print a formatted table of GPU servers with multi-currency pricing."""
    btc_usd = get_btc_usd_price()
    if not servers:
        print(f"\n🔍 No servers found with >20GB VRAM, >50Mbps bandwidth.")
        print(f"   (BTC/USD rate: ${btc_usd:.2f})")
        return

    print("\n" + "=" * 180)
    print(f"  Found {len(servers)} available GPU server(s) with >20GB VRAM, >50Mbps bandwidth")
    print(f"  BTC/USD rate: ${btc_usd:,.2f}")
    print("=" * 180)
    print(f"  {'#':<4} {'ID':<7} {'GPU':<28} {'VRAM':<5} {'Up':<7} {'Down':<7} {'Rel':<5} {'Loc':<4} "
          f"{'Allowed':<20} {'$ OD':<8} {'$ Spot':<8} {'BTC OD':<10} {'BTC Spot':<10} {'CLORE OD':<10} {'CLORE Spot':<10}")
    print("-" * 180)

    for i, s in enumerate(servers, 1):
        gpu_display = s["gpu"][:26] + ".." if len(s["gpu"]) > 28 else s["gpu"]
        od_usd = s['on_demand_usd']
        sp_usd = s['spot_usd']
        od_btc = s['on_demand_btc']
        sp_btc = s['spot_btc']
        od_clore = s.get('on_demand_clore', 0)
        sp_clore = s.get('spot_clore', 0)
        rel = f"{s['reliability']*100:.0f}%" if s.get('reliability') else "N/A"
        allowed = ", ".join(s['allowed_coins'])[:18] + ".." if len(", ".join(s['allowed_coins'])) > 20 else ", ".join(s['allowed_coins'])
        net_up = f"{s['net_up']:.0f}"
        net_down = f"{s['net_down']:.0f}"
        cc = s.get('net_cc', '??')

        # Format BTC values (they're per-day, often very small numbers like 1.364e-05)
        def fmt_btc(v):
            if v >= 0.001:
                return f"{v:.5f}"
            elif v >= 0.0001:
                return f"{v:.6f}"
            elif v >= 0.00001:
                return f"{v:.7f}"
            else:
                return f"{v:.8f}"

        print(f"  {i:<4} {s['id']:<7} {gpu_display:<28} {s['vram']:<5}GB "
              f"{net_up:<7}Mbps {net_down:<7}Mbps "
              f"{rel:<5} {cc:<4} {allowed:<20} "
              f"${od_usd:<7.2f} ${sp_usd:<7.2f} "
              f"{fmt_btc(od_btc):<10} {fmt_btc(sp_btc):<10} "
              f"{fmt_btc(od_clore):<10} {fmt_btc(sp_clore):<10}")

    print("-" * 180)
    print(f"  * Prices are per day. '$' = USD, 'BTC' = Bitcoin (per day), 'CLORE' = CLORE tokens (per day).")
    print(f"  * '$ OD/Spot' = on-demand/guaranteed / spot (cheaper, can be outbid) in USD.")
    print(f"  * 'BTC OD/Spot' = on-demand/guaranteed / spot in BTC (converted at ${btc_usd:,.0f}/BTC).")
    print(f"  * 'Rel' = host reliability. 'Loc' = server country code.")
    print(f"  * 'Up/Down' = measured bandwidth in Mbps (both must exceed 50Mbps).")
    print(f"  * All servers accept at least one currency: bitcoin, USD-Blockchain, CLORE-Blockchain.")
    print("=" * 180)


def rent_server(client: CloreClient, server_id: int, order_type: str = "on-demand",
                image: str = DEFAULT_IMAGE, currency: str = DEFAULT_CURRENCY,
                ssh_password: str = DEFAULT_SSH_PASSWORD,
                ports: dict = DEFAULT_PORTS,
                autossh_entrypoint: str = DEFAULT_ENTRYPOINT,
                command: str = DEFAULT_STARTUP_SCRIPT,
                spot_price: float = None, env: dict = None):
    """Rent a server with llama.cpp + Qwen3.6-35B-A3B configuration.

    Uses official Clore.ai API parameter names:
    - autossh_entrypoint (triggers SSH auto-install on the host)
    - command (startup script for model download + llama-server)
    """
    print(f"\n🔒 Preparing to rent server #{server_id} ({order_type})...")
    print(f"  🖼️  Docker image: {image}")
    print(f"  🔑 Entrypoint: {autossh_entrypoint}")
    print(f"  🌐 Ports: {ports}")
    print(f"  💰 Currency: {currency}")
    print(f"  🔑 SSH password: {ssh_password[:4]}... (custom)")
    if command:
        lines = command.strip().split('\n')
        print(f"  📜 Startup script: {len(lines)} lines (model downloader + llama-server)")
    if order_type == "spot":
        print(f"  💸 Spot bid: {spot_price} {currency}/day")

    print(f"\n  📦 Creating {order_type} order for server #{server_id}...")
    result = client.create_order(
        server_id=server_id,
        image=image,
        ports=ports,
        ssh_password=ssh_password,
        currency=currency,
        order_type=order_type,
        spot_price=spot_price,
        env=env,
        autossh_entrypoint=autossh_entrypoint,
        command=command,
    )

    if result.get("code") == 0:
        print("\n  ✅ Order created successfully!")
        order_data = result.get("order", {})
        order_id = order_data.get("id", "N/A")
        print(f"  🆔 Order ID: {order_id}")
        print(f"  🖼️  Image: {image}")
        print(f"  🔑 SSH password: {ssh_password[:4]}...")
        print(f"  📌 Use 'clore_search.py orders' to check connection details.")
        print(f"  📌 SSH: ssh root@<host> -p <port> (check orders for details)")
        print(f"  🌐 llama-server API: http://<host>:8080 (check orders for host)")
    else:
        print(f"\n  ❌ Order failed: {result.get('error', 'Unknown error')}")
        if result.get("error") == "server-offline":
            print(f"  💡 Server #{server_id} is offline. Try a different server.")
        elif result.get("error") == "not_enough_balance":
            print(f"  💡 Your wallet balance is too low. Top up via the CLORE dashboard.")


def show_orders(client: CloreClient):
    """Show current orders with connection details."""
    orders = client.get_orders(return_completed=True)

    if not orders:
        print("\n📭 No orders found.")
        return

    print("\n" + "=" * 80)
    print(f"  Your Orders ({len(orders)} total)")
    print("=" * 80)

    for o in orders:
        status = o.get("status", "unknown")
        server_id = o.get("renting_server", "N/A")
        expired = o.get("expired", False)
        pub = o.get("pub_cluster", [])
        ports = o.get("tcp_ports", [])

        print(f"\n  📋 Order #{o.get('id')} | Server #{server_id}")
        print(f"     Status: {status} | Expired: {expired}")
        if pub:
            for host in pub:
                print(f"     SSH Host: {host}")
        if ports:
            for p in ports:
                print(f"     Port mapping: {p}")


def show_wallets(client: CloreClient):
    """Show wallet balances."""
    wallets = client.get_wallets()

    print("\n" + "=" * 60)
    print("  Your Wallet Balances")
    print("=" * 60)

    for w in wallets:
        balance = w.get("balance", 0)
        name = w.get("name", "Unknown")
        deposit = w.get("deposit", "N/A")
        print(f"\n  💰 {name}")
        print(f"     Balance: {balance}")
        print(f"     Deposit: {deposit}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python clore_search.py <action> [args]")
        print("\nActions:")
        print("  search [min_vram] [--currency bitcoin|CLORE-Blockchain|USD-Blockchain]")
        print("                       [--min-bandwidth 50]  - Search marketplace for GPU servers")
        print("  rent <server_id> <on-demand|spot> [--currency USD-Blockchain|bitcoin|CLORE-Blockchain]")
        print("                       [--spot-price X]  - Rent a server (choose currency)")
        print("  orders             - Show your current orders")
        print("  wallets            - Show wallet balances")
        print("\nDefault config:")
        print(f"  Image:      {DEFAULT_IMAGE}")
        print(f"  Ports:      {DEFAULT_PORTS}")
        print(f"  SSH pass:   {DEFAULT_SSH_PASSWORD[:4]}...")
        print(f"  Entrypoint: {DEFAULT_ENTRYPOINT}")
        print(f"  Currency:   {DEFAULT_CURRENCY}")
        print(f"  Startup:    Model downloader + llama-server (Qwen3.6-35B-A3B)")
        print(f"  Bandwidth:  >= {DEFAULT_MIN_BANDWIDTH} Mbps (both up & down)")
        print("\nSet API key via CLORE_API_KEY env var.")
        sys.exit(1)

    action = sys.argv[1]

    # Get API key
    api_key = os.environ.get("CLORE_API_KEY", "")

    if action in ("search", "rent"):
        if not api_key:
            print("❌ No API key provided (set CLORE_API_KEY env var).")
            sys.exit(1)

    client = CloreClient(api_key)

    if action == "search":
        min_vram = 20
        currency_filter = None
        min_bandwidth = DEFAULT_MIN_BANDWIDTH
        i = 2
        while i < len(sys.argv):
            arg = sys.argv[i]
            if arg == "--currency":
                if i + 1 < len(sys.argv):
                    currency_filter = sys.argv[i + 1]
                    if currency_filter not in ("bitcoin", "CLORE-Blockchain", "USD-Blockchain"):
                        print(f"❌ Invalid currency '{currency_filter}'. Must be bitcoin, CLORE-Blockchain, or USD-Blockchain")
                        sys.exit(1)
                    i += 1
                else:
                    print("❌ --currency requires a value")
                    sys.exit(1)
            elif arg == "--min-bandwidth":
                if i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit():
                    min_bandwidth = int(sys.argv[i + 1])
                    i += 1
                else:
                    print("❌ --min-bandwidth requires a numeric value")
                    sys.exit(1)
            else:
                try:
                    min_vram = int(arg)
                except ValueError:
                    print(f"❌ Invalid argument: {arg}")
                    sys.exit(1)
            i += 1

        servers = client.get_marketplace()
        filtered = filter_gpu_servers(servers, min_vram, currency_filter, min_bandwidth)
        print_server_table(filtered)

    elif action == "rent":
        if len(sys.argv) < 3:
            print("❌ Please specify server ID and type")
            print("   Example: python clore_search.py rent 103669 on-demand --currency USD-Blockchain")
            sys.exit(1)
        server_id = int(sys.argv[2])
        order_type = "on-demand"
        currency = DEFAULT_CURRENCY
        spot_price = None
        i = 3
        while i < len(sys.argv):
            arg = sys.argv[i]
            if arg in ("on-demand", "spot"):
                order_type = arg
            elif arg == "--currency":
                if i + 1 < len(sys.argv):
                    currency = sys.argv[i + 1]
                    if currency not in ("bitcoin", "CLORE-Blockchain", "USD-Blockchain"):
                        print(f"❌ Invalid currency '{currency}'. Must be bitcoin, CLORE-Blockchain, or USD-Blockchain")
                        sys.exit(1)
                    i += 1
                else:
                    print("❌ --currency requires a value")
                    sys.exit(1)
            elif arg == "--spot-price":
                if i + 1 < len(sys.argv):
                    spot_price = float(sys.argv[i + 1])
                    i += 1
                else:
                    print("❌ --spot-price requires a numeric value")
                    sys.exit(1)
            else:
                print(f"❌ Unknown rent argument: {arg}")
                sys.exit(1)
            i += 1
        rent_server(client, server_id, order_type, currency=currency, spot_price=spot_price)

    elif action == "orders":
        show_orders(client)

    elif action == "wallets":
        show_wallets(client)

    else:
        print(f"❌ Unknown action: {action}")
        sys.exit(1)


if __name__ == "__main__":
    main()
