import os
import sys
import uvicorn

# Ensure repository root is on sys.path
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)


def main():
    # Firestore is the only datastore: there is no schema sync, migration or seeding step at startup.
    # Railway injects PORT and routes the public URL to that port.
    raw_port = os.environ.get("PORT", "8000")
    try:
        port = int(raw_port)
    except ValueError:
        port = 8000

    print(f"--> Starting Uvicorn server on host 0.0.0.0 port {port}...")
    # Forwarded proxy headers: allow configuring specific proxy IPs (e.g. Cloud Run / load balancer)
    # Default to 127.0.0.1 rather than '*' to prevent spoofing client IP via client-crafted X-Forwarded-For
    forwarded_ips = os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")
    proxy_headers = os.environ.get("PROXY_HEADERS", "true").lower() in ("1", "true")

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        proxy_headers=proxy_headers,
        forwarded_allow_ips=forwarded_ips
    )


if __name__ == "__main__":
    main()
