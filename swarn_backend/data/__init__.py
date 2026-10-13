"""Dataset serving (parquet in MinIO → paged JSON) and column typing.

Shared by the API process (serving) and the worker (dumping); neither path
imports agent2 — pandas only.
"""
