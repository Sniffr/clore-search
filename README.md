# Clore.ai GPU Marketplace Searcher

Search the Clore.ai GPU marketplace for the cheapest servers with **>20GB VRAM** and optionally rent them via the REST API.

## Features

- **Search** — Find all available GPU servers with VRAM > threshold (default: 20GB)
- **Filter by currency** — Only show servers accepting `bitcoin`, `CLORE-Blockchain`, or `USD-Blockchain`
- **Rent** — Spin up a server with pre-configured Docker image, ports, SSH password, and a startup script that downloads a large GGUF model and starts `llama-server` on port 8080
- **SSH Autoinstall** entrypoint — automatic SSH access setup
- **Startup script** — Downloads Qwen3.6-35B-A3B-GGUF and runs it via `llama-server`

## Quick Start

```bash
# Search for GPUs with >20GB VRAM (any currency)
CLORE_API_KEY="your_token" python3 clore_search.py search

# Filter by currency
CLORE_API_KEY="your_token" python3 clore_search.py search 20 --currency USD-Blockchain

# Rent a server (uses all default config: archer304/llama.cpp, ports 22/5000/8080, ssh_autoinstall)
CLORE_API_KEY="your_token" python3 clore_search.py rent 94246 on-demand

# Check your active orders for SSH connection details
CLORE_API_KEY="your_token" python3 clore_search.py orders

# Check wallet balances
CLORE_API_KEY="your_token" python3 clore_search.py wallets
```

## Default Configuration

| Setting | Value |
|---|---|
| Docker image | `archer304/llama.cpp:server-cuda` |
| Ports | `22/tcp`, `5000/tcp`, `8080/http` |
| SSH password | `DRpjRuu88XtvWYHnNXcB5Ksx` |
| Entrypoint | `ssh_autoinstall` |
| Currency | `USD-Blockchain` |
| Startup script | Downloads Qwen3.6-35B-A3B → starts llama-server on `:8080` |

## Startup Script

The default startup script does:
1. Downloads `Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf` from HuggingFace (tries aria2c → wget → curl)
2. Writes a secondary `onstart.sh` that launches `llama-server` with full GPU offload (`-ngl 999`)
3. Registers the service via `s6-svc`

> **Note:** Replace `hf_your_token_here` in the startup script with your actual HuggingFace token before deploying.

## Prerequisites

- Python 3.10+
- `requests` library: `pip install requests`
- A Clore.ai API token (set `CLORE_API_KEY` env var)

## API Reference

The script uses the Clore.ai v1 REST API:
- `GET /v1/marketplace` — List all servers
- `POST /v1/create_order` — Rent a server
- `GET /v1/my_orders` — List orders
- `POST /v1/cancel_order` — Cancel an order
- `GET /v1/wallets` — Check balances

## License

MIT
