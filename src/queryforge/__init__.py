"""QueryForge — a natural-language query agent for Oracle on GCP.

Ask questions in plain English; a Claude agent (running on Google Vertex AI)
writes Oracle SQL, runs it read-only against your database, and returns results.
"""

__version__ = "0.1.0"


def main() -> None:
    """Entry point for the ``queryforge`` console command: start the web server.

    Serves the FastAPI app (UI + streaming ``/query`` endpoint) with uvicorn.
    Host/port are overridable via ``QUERYFORGE_HOST`` / ``QUERYFORGE_PORT``;
    the default binds to localhost only, since the app has no auth and reaches a
    live database.
    """
    import os

    import uvicorn

    host = os.getenv("QUERYFORGE_HOST", "127.0.0.1")
    port = int(os.getenv("QUERYFORGE_PORT", "8000"))
    uvicorn.run("queryforge.web.app:app", host=host, port=port)
