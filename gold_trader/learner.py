"""과거 거래에서 '어떤 신호가 이겼는지'를 배우는 로지스틱 회귀 필터 (numpy만 사용).

진화가 고른 전략의 신호 중 이길 확률이 낮아 보이는 것을 건너뛴다.
검증 구간에서 실제로 성과가 좋아질 때만 채택된다.
"""
from __future__ import annotations

import numpy as np

from strategy import FEATURES, SIGNED_FEATURES, UNIT_FEATURES


class TradeFilter:
    def __init__(self, w=None, b=0.0, mu=None, sd=None, threshold=0.5):
        self.w = None if w is None else np.asarray(w, float)
        self.b, self.threshold = float(b), float(threshold)
        self.mu = None if mu is None else np.asarray(mu, float)
        self.sd = None if sd is None else np.asarray(sd, float)

    @staticmethod
    def _x(feats: np.ndarray, direction: np.ndarray) -> np.ndarray:
        # 숏 신호는 방향성 특징의 부호를 뒤집어 롱과 같은 의미가 되게 한다
        x = feats.astype(float).copy()
        d = np.where(direction == 0, 1, direction)
        for name in SIGNED_FEATURES:
            x[:, FEATURES.index(name)] *= d
        for name in UNIT_FEATURES:
            k = FEATURES.index(name)
            x[:, k] = np.where(d > 0, x[:, k], 1 - x[:, k])
        return x

    def fit(self, trades, l2: float = 1.0, epochs: int = 500, lr: float = 0.1) -> "TradeFilter":
        x = self._x(trades[FEATURES].to_numpy(), trades["dir"].to_numpy())
        y = (trades["r"].to_numpy() > 0).astype(float)
        self.mu, self.sd = x.mean(0), x.std(0) + 1e-9
        z = (x - self.mu) / self.sd
        w, b = np.zeros(z.shape[1]), 0.0
        for _ in range(epochs):
            pr = 1 / (1 + np.exp(-(z @ w + b)))
            g = pr - y
            w -= lr * (z.T @ g / len(y) + l2 * w / len(y))
            b -= lr * g.mean()
        self.w, self.b = w, b
        return self

    def proba(self, feats: np.ndarray, direction: np.ndarray) -> np.ndarray:
        z = (self._x(feats, direction) - self.mu) / self.sd
        return 1 / (1 + np.exp(-(z @ self.w + self.b)))

    def allow(self, feats: np.ndarray, direction: np.ndarray) -> np.ndarray:
        return self.proba(feats, direction) >= self.threshold

    def to_dict(self) -> dict:
        return {"w": self.w.tolist(), "b": self.b, "mu": self.mu.tolist(), "sd": self.sd.tolist(),
                "threshold": self.threshold}

    @classmethod
    def from_dict(cls, d: dict | None):
        if not d or len(d.get("w") or []) != len(FEATURES):   # 특징 수가 다른 예전 필터는 쓰지 않음
            return None
        return cls(**d)
