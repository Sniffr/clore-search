#!/usr/bin/env python3
"""
Clore.ai GPU Marketplace Searcher
Searches for cheapest GPU servers with >20GB VRAM and optionally rents them.
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

DEFAULT_STARTUP_SCRIPT = """#!/bin/sh
mkdir -p /models
MODEL="/models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
URL="https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
export HF_TOKEN="hf_your_token_here"

if [ ! -f "$MODEL" ]; then
    echo "[$(date)] Starting model download..." >> /var/log/startup.log

    # Try 1: aria2c (multi-connection, fastest that actually works)
    echo "[$(date)] Trying aria2c..." >> /var/log/startup.log
    aria2c -x 16 -s 16 -k 1M -d /models -o Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf \\
        --header="Authorization: Bearer $HF_TOKEN" \\
        "$URL" >> /var/log/startup.log 2>&1

    # Try 2: wget
    if [ ! -f "$MODEL" ]; then
        echo "[$(date)] aria2c failed, trying wget..." >> /var/log/startup.log
        wget --header="Authorization: Bearer $HF_TOKEN" \\
            -O "$MODEL" "$URL" >> /var/log/startup.log 2>&1
    fi

    # Try 3: curl
    if [ ! -f "$MODEL" ]; then
        echo "[$(date)] wget failed, trying curl..." >> /var/log/startup.log
        curl -L -H "Authorization: Bearer $HF_TOKEN" \\
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

cat > /root/onstart.sh << 'EOF'
#!/bin/sh
export LD_LIBRARY_PATH=/app:$LD_LIBRARY_PATH
/app/llama-server \\
  -m /models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf \\
  --host 0.0.0.0 \\
  --port 8080 \\
  -ngl 999 \\
  -fa on \\
  -c 65536 \\
  --cache-type-k q8_0 \\
  --cache-type-v q8_0 \\
  --no-mmap \\
  --jinja >> /var/log/llama-server.log 2>&1
EOF

chmod +x /root/onstart.sh
s6-svc -r /var/run/s6/services/onstart
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
                     entrypoint: str = DEFAULT_ENTRYPOINT,
                     startup_script: str = None) -> dict:
        """Create a rental order for a server.

        Args:
            server_id: The marketplace server ID to rent.
            image: Docker image — defaults to archer304/llama.cpp:server-cuda.
            ports: Port mapping dict, e.g. {"22": "tcp", "5000": "tcp", "8080": "http"}.
            ssh_password: SSH password (auto-generated if not provided).
            currency: "bitcoin", "CLORE-Blockchain", or "USD-Blockchain".
            order_type: "on-demand" or "spot".
            spot_price: Required for spot orders — your bid in the currency unit.
            env: Optional environment variables dict.
            entrypoint: Container entrypoint — defaults to "ssh_autoinstall".
            startup_script: Shell script to run on container startup (downloads model,
                           configures llama-server, etc.).
        """
        payload = {
            "renting_server": server_id,
            "type": order_type,
            "currency": currency,
            "image": image,
            "ports": ports or DEFAULT_PORTS,
            "ssh_password": ssh_password or DEFAULT_SSH_PASSWORD,
            "entrypoint": entrypoint or DEFAULT_ENTRYPOINT,
        }
        if startup_script:
            payload["startup_script"] = startup_script
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


def filter_gpu_servers(servers: list, min_vram_gb: int = 20, currency: str = None) -> list:
    """Filter servers with GPU VRAM > min_vram_gb and not currently rented.

    Args:
        servers: Raw marketplace server list.
        min_vram_gb: Minimum GPU VRAM in GB (exclusive).
        currency: Optional currency filter — 'bitcoin', 'CLORE-Blockchain', 'USD-Blockchain'.
                  If None, shows all servers that accept at least one currency.
    """
    results = []

    for server in servers:
        specs = server.get("specs", {})
        gpu_name, gpuram = extract_gpu_info(specs)

        if gpuram <= min_vram_gb:
            continue

        if server.get("rented", True):
            continue

        price_info = server.get("price", {})
        on_demand_btc = price_info.get("on_demand", {}).get("bitcoin", 0)
        spot_btc = price_info.get("spot", {}).get("bitcoin", 0)

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

        results.append({
            "id": server.get("id"),
            "gpu": gpu_name,
            "vram": gpuram,
            "on_demand_btc": on_demand_btc,
            "spot_btc": spot_btc,
            "cpu": specs.get("cpu", "N/A"),
            "ram": specs.get("ram", "N/A"),
            "disk": specs.get("disk", "N/A"),
            "net": specs.get("net", {}),
            "mrl": server.get("mrl", "N/A"),
            "allowed_coins": allowed,
            "reliability": server.get("reliability", 0),
        })

    # Sort by cheapest on-demand price first
    results.sort(key=lambda x: x["on_demand_btc"])
    return results


def print_server_table(servers: list):
    """Print a formatted table of GPU servers."""
    if not servers:
        print("\n🔍 No servers found with >{}GB VRAM.".format(20))
        return

    print("\n" + "=" * 140)
    print(f"  Found {len(servers)} available GPU server(s) with >20GB VRAM")
    print("=" * 140)
    print(f"  {'#':<4} {'ID':<7} {'GPU':<30} {'VRAM':<7} {'Rel':<5} {'Allowed':<25} "
          f"{'On-Demand BTC':<14} {'On-Demand $':<14} "
          f"{'Spot BTC':<14} {'Spot $':<14}")
    print("-" * 140)

    for i, s in enumerate(servers, 1):
        gpu_display = s["gpu"][:28] + ".." if len(s["gpu"]) > 30 else s["gpu"]
        od_btc = s['on_demand_btc']
        sp_btc = s['spot_btc']
        od_usd = btc_to_usd(od_btc)
        sp_usd = btc_to_usd(sp_btc)
        rel = f"{s['reliability']*100:.0f}%" if s.get('reliability') else "N/A"
        allowed = ", ".join(s['allowed_coins'])[:23] + ".." if len(", ".join(s['allowed_coins'])) > 25 else ", ".join(s['allowed_coins'])
        print(f"  {i:<4} {s['id']:<7} {gpu_display:<30} {s['vram']:<7}GB "
              f"{rel:<5} {allowed:<25} "
              f"{od_btc:<14.8f} ${od_usd:<13.2f} "
              f"{sp_btc:<14.8f} ${sp_usd:<13.2f}")

    print("-" * 140)
    print(f"  * Prices are per day in BTC. 'on_demand' = guaranteed uptime.")
    print(f"  * 'spot' = cheaper but can be outbid.")
    print(f"  * 'Rel' = host reliability score. 'Allowed' = accepted payment currencies.")
    print("=" * 140)


def rent_server(client: CloreClient, server_id: int, order_type: str = "on-demand",
                image: str = DEFAULT_IMAGE, currency: str = DEFAULT_CURRENCY,
                ssh_password: str = DEFAULT_SSH_PASSWORD,
                ports: dict = DEFAULT_PORTS,
                entrypoint: str = DEFAULT_ENTRYPOINT,
                startup_script: str = DEFAULT_STARTUP_SCRIPT,
                spot_price: float = None, env: dict = None):
    """Rent a server with llama.cpp + Qwen3.6-35B-A3B configuration.

    Args:
        client: CloreClient instance.
        server_id: Server ID from marketplace.
        order_type: "on-demand" or "spot".
        image: Docker image (defaults to archer304/llama.cpp:server-cuda).
        currency: Payment currency (defaults to USD-Blockchain).
        ssh_password: SSH password (defaults to preset).
        ports: Port mappings (defaults to 22/tcp, 5000/tcp, 8080/http).
        entrypoint: Container entrypoint (defaults to ssh_autoinstall).
        startup_script: Startup script (defaults to model downloader + llama-server).
        spot_price: Your bid for spot orders.
        env: Environment variables to inject.
    """
    print(f"\n🔒 Preparing to rent server #{server_id} ({order_type})...")
    print(f"  🖼️  Docker image: {image}")
    print(f"  🔑 Entrypoint: {entrypoint}")
    print(f"  🌐 Ports: {ports}")
    print(f"  💰 Currency: {currency}")
    print(f"  🔑 SSH password: {ssh_password[:4]}... (custom)")
    if startup_script:
        lines = startup_script.strip().split('\n')
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
        entrypoint=entrypoint,
        startup_script=startup_script,
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
        print("                       - Search marketplace for GPU servers")
        print("  rent <server_id> [on-demand|spot]")
        print("                       type: on-demand|spot (default: on-demand)")
        print("  orders             - Show your current orders")
        print("  wallets            - Show wallet balances")
        print("\nDefault config:")
        print(f"  Image:      {DEFAULT_IMAGE}")
        print(f"  Ports:      {DEFAULT_PORTS}")
        print(f"  SSH pass:   {DEFAULT_SSH_PASSWORD[:4]}...")
        print(f"  Entrypoint: {DEFAULT_ENTRYPOINT}")
        print(f"  Currency:   {DEFAULT_CURRENCY}")
        print(f"  Startup:    Model downloader + llama-server (Qwen3.6-35B-A3B)")
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
            elif arg.isdigit():
                min_vram = int(arg)
            i += 1

        print(f"\n🔍 Searching Clore.ai marketplace for GPUs with >{min_vram}GB VRAM"
              f"{f' ({currency_filter})' if currency_filter else ''}...")

        servers = client.get_marketplace()
        filtered = filter_gpu_servers(servers, min_vram, currency=currency_filter)
        print_server_table(filtered)

        if not filtered:
            print("\n💡 Tip: Try lowering the VRAM threshold or check back later.")
            return

    elif action == "rent":
        if len(sys.argv) < 3:
            print("❌ Usage: python clore_search.py rent <server_id> [on-demand|spot]")
            sys.exit(1)

        server_id = int(sys.argv[2])
        order_type = sys.argv[3] if len(sys.argv) > 3 else "on-demand"

        if order_type not in ("on-demand", "spot"):
            print("❌ Order type must be 'on-demand' or 'spot'")
            sys.exit(1)

        # For spot, calculate a default bid at 90% of the listed spot price
        spot_price = None
        if order_type == "spot":
            spot_price = 0.000001  # minimum bid

        rent_server(
            client=client,
            server_id=server_id,
            order_type=order_type,
        )

    elif action == "orders":
        show_orders(client)

    elif action == "wallets":
        show_wallets(client)

    else:
        print(f"❌ Unknown action: {action}")
        sys.exit(1)


if __name__ == "__main__":
    main()
