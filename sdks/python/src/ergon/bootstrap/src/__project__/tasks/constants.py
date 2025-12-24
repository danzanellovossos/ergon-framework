from ergon.task.policies import RetryPolicy

# ----------------------------------------
# --- RETRY POLICY CONFIGURATION ---
# ----------------------------------------
def default_retry_policy():
    return RetryPolicy(
        max_attempts=3,
        backoff=1,
        backoff_multiplier=2,
        backoff_cap=10,
    )