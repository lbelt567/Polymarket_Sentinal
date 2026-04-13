def build_execution_stage_summary(status: str, symbol: str | None = None) -> str:
    if symbol:
        return f"Execution status for {symbol}: {status}"
    return f"Execution status: {status}"
