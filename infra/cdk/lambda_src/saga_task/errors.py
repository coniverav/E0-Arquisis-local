class WalletError(RuntimeError):
    """Base para errores de Wallet sin incluir secretos ni cuerpos completos."""


class WalletRetryable(WalletError):
    """Timeout, conectividad o respuesta 5xx que admite reintento."""


class WalletRateLimited(WalletRetryable):
    """La cuota local o Wallet exige esperar antes del siguiente intento."""


class WalletPermanent(WalletError):
    """Error que no debe reintentarse automáticamente."""


class WalletIdempotencyMismatch(WalletPermanent):
    pass


class WalletUnauthorized(WalletPermanent):
    pass


class WalletNotFound(WalletPermanent):
    pass


class WalletRejected(WalletPermanent):
    pass
