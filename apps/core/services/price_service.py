from codes.datas import Exchange


class PriceController:
    """Price-range calculation service (pure, no I/O)."""

    def price_range(self, *, qtyString: str, cost: float, profit_max: float, profit_min: float):
        """Return a dict mapping quantity -> USD price at each profit step.

        Previously printed the result via ``printModel.print_dict``; it now
        returns the raw dict so callers (CLI or HTTP) can format as they wish.
        """
        qtyLs = qtyString.split(',')
        qtys = [int(q.strip()) for q in qtyLs]
        # calculate the step
        step = (profit_max - profit_min) / (len(qtys) - 1)
        percents = [profit_max - i * step for i in range(len(qtys))]
        data = {}
        for i, percent in enumerate(percents):
            data[qtys[i]] = cost * (1 + percent / 100) / Exchange.ToUSD['RMB']
        return data
