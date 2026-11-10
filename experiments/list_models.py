"""Ask the live API which models YOUR account can use (docs differ by plan)."""
import httpx

from app.config import settings


def main() -> None:
    key = settings.require_key()
    r = httpx.get(f"{settings.base_url}/models",
                  headers={"Authorization": f"Bearer {key}"}, timeout=30)
    print("status:", r.status_code)
    for k, v in r.headers.items():
        if k.lower().startswith(("x-ratelimit", "retry-after")):
            print(f"  {k}: {v}")
    if r.status_code == 200:
        for m in sorted(x["id"] for x in r.json()["data"]):
            print(" ", m)
    else:
        print(r.text[:500])


if __name__ == "__main__":
    main()
