class DependencyUnavailable(Exception):
    pass


class Conflict(Exception):
    pass


class Busy(Exception):
    pass


class ProviderRejected(DependencyUnavailable):
    def __init__(self, provider: str, status_code: int):
        super().__init__(f"{provider} rejected request")
        self.status_code = status_code
