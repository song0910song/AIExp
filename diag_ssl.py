"""Diagnose SSL certificate for opencode.ai from Python's perspective."""
import socket
import ssl
import urllib.request
import traceback


def main() -> None:
    print("=== System proxies (from urllib) ===")
    try:
        proxies = urllib.request.getproxies()
        print(proxies)
    except Exception as exc:
        print("getproxies error:", exc)

    print("\n=== Default ssl context validation ===")
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection(("opencode.ai", 443), timeout=8) as sock:
            with ctx.wrap_socket(sock, server_hostname="opencode.ai") as ssock:
                cert = ssock.getpeercert()
                print("VALIDATION OK")
                print("subject:", cert.get("subject"))
                print("subjectAltName:", cert.get("subjectAltName"))
                print("issuer:", cert.get("issuer"))
    except Exception as exc:
        print("validation FAILED:", type(exc).__name__, exc)

    print("\n=== Unverified cert inspection ===")
    try:
        raw_ctx = ssl._create_unverified_context()
        with socket.create_connection(("opencode.ai", 443), timeout=8) as sock:
            with raw_ctx.wrap_socket(sock, server_hostname="opencode.ai") as ssock:
                cert = ssock.getpeercert()
                print("subject:", cert.get("subject"))
                print("subjectAltName:", cert.get("subjectAltName"))
                print("issuer:", cert.get("issuer"))
                print("notAfter:", cert.get("notAfter"))
    except Exception as exc:
        print("unverified FAILED:", type(exc).__name__, exc)
        traceback.print_exc()

    print("\n=== Proxy env vars ===")
    import os
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
                "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE"):
        print(f"{key} = {os.environ.get(key)!r}")


if __name__ == "__main__":
    main()
