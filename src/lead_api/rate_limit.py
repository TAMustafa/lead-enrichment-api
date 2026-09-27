import hashlib


class PostgresRateLimiter:
    """One bounded row per configured API client; database time avoids replica clock skew."""

    def __init__(self, pool, limit: int):
        self.pool, self.limit = pool, limit

    async def allow(self, identity: str) -> bool:
        key = hashlib.sha256(identity.encode()).hexdigest()
        async with self.pool.acquire(timeout=2) as connection:
            count = await connection.fetchval(
                """
                INSERT INTO rate_limits(client_key, window_start, request_count)
                VALUES ($1, clock_timestamp(), 1)
                ON CONFLICT(client_key) DO UPDATE SET
                    request_count = CASE
                        WHEN rate_limits.window_start <= clock_timestamp() - interval '60 seconds'
                        THEN 1 ELSE LEAST(rate_limits.request_count + 1, $2 + 1) END,
                    window_start = CASE
                        WHEN rate_limits.window_start <= clock_timestamp() - interval '60 seconds'
                        THEN clock_timestamp() ELSE rate_limits.window_start END
                RETURNING request_count
                """,
                key,
                self.limit,
                timeout=2,
            )
        return count <= self.limit
