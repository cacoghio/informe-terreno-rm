"""Errores del pipeline. El rechazo es una accion de primera clase (spec sec. 7)."""
from __future__ import annotations


class RejectRequest(Exception):
    """El pedido se rechaza en vez de adivinar. ``reason`` se registra tal cual."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
