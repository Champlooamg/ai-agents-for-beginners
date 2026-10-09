"""缠论因子 — 桥接 ChanlunAnalyser。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from easy_tdx.factor.base import Factor, register_factor


@register_factor
class ChanlunBiDir(Factor):
    name = "chanlun_bi_dir"
    category = "chanlun"
    description = "当前笔方向（+1=向上笔，-1=向下笔，0=无笔）"
    inputs = ("open", "high", "low", "close", "vol", "amount")

    def compute(self, df: pd.DataFrame) -> pd.Series:
        result = pd.Series(0.0, index=df.index, dtype=np.float64)

        try:
            from easy_tdx.chanlun.analyser import ChanlunAnalyser

            analyser = ChanlunAnalyser(frequency="DAILY")
            chanlun_result = analyser.process_klines(df)
            bis = chanlun_result.bis

            if not bis:
                return result

            for bi in bis:
                direction = 1.0 if bi.direction == "up" else -1.0
                start = getattr(bi, "start_index", 0)
                end = getattr(bi, "end_index", len(df) - 1)
                lo = max(0, start)
                hi = min(len(df), end + 1)
                result.iloc[lo:hi] = direction

            last_bi = bis[-1]
            direction = 1.0 if last_bi.direction == "up" else -1.0
            result.iloc[-1] = direction

        except Exception:
            pass

        return result


@register_factor
class ChanlunMMD(Factor):
    name = "chanlun_mmd"
    category = "chanlun"
    description = "最近买卖点类型编码（正=买点，负=卖点，0=无信号）"
    inputs = ("open", "high", "low", "close", "vol", "amount")

    _MMD_MAP: dict[str, float] = {
        "1buy": 1.0,
        "2buy": 2.0,
        "3buy": 3.0,
        "l3buy": 3.0,
        "1sell": -1.0,
        "2sell": -2.0,
        "3sell": -3.0,
        "s3sell": -3.0,
    }

    def compute(self, df: pd.DataFrame) -> pd.Series:
        result = pd.Series(0.0, index=df.index, dtype=np.float64)

        try:
            from easy_tdx.chanlun.analyser import ChanlunAnalyser

            analyser = ChanlunAnalyser(frequency="DAILY")
            chanlun_result = analyser.process_klines(df)
            mmds = chanlun_result.mmds

            if not mmds:
                return result

            for mmd in mmds:
                mmd_type = getattr(mmd, "type", "")
                mmd_index = getattr(mmd, "index", -1)
                value = self._MMD_MAP.get(mmd_type, 0.0)
                if 0 <= mmd_index < len(df):
                    result.iloc[mmd_index] = value

        except Exception:
            pass

        return result
