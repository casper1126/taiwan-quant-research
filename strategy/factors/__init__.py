"""
strategy/factors/
─────────────────────────────────────────────────────────────
因子庫模組化重構（Task 2）。統一簽名：

    def factor_name(data: dict) -> pd.DataFrame | pd.Series

`data` 是 quant_layer2.py `load_matrices()` 產出的寬格式資料字典
（index=date, columns=stock_id）。
"""
