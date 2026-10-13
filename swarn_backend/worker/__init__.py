"""The job worker: one process per job, imports agent2, speaks JSON lines.

Only this package imports `agent.*`. The environment (SWARN_DATA_ROOT and
friends) must be set before that import, which `__main__` guarantees.
"""
