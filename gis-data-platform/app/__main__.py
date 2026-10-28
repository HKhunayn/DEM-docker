import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local open GIS data platform")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    # Bounded graceful shutdown: `docker stop` SIGKILLs after 10 s, so open
    # connections get at most 5 s and the shutdown hooks (app.jobs.shutdown,
    # also registered via atexit) still run and record running jobs as
    # interrupted instead of losing them.
    uvicorn.run("app.main:app", host=args.host, port=args.port, timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
