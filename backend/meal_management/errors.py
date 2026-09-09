class DomainError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ConfigurationError(DomainError):
    pass


class DependencyError(DomainError):
    pass
