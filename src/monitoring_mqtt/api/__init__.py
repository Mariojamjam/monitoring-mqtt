"""HTTP API: the single entry point to the database (FastAPI).

Write side: the ingestion consumer posts batches of events (``/api/ingest/...``).
Read side: JSON queries over the stored history (devices, measurements, alerts, runs).
"""
