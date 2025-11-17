from codes.utils import printModel
from codes.datas import Exchange
class PriceController:
    def __init__(self):
        pass

    def price_range(self, *, qtyString: str, cost: float, profit_max: float, profit_min: float):
        qtyLs = qtyString.split(',')
        qtys = [int(q.strip()) for q in qtyLs]
        # calculate the step
        step = (profit_max - profit_min) / (len(qtys) - 1)
        percents = [profit_max - i * step for i in range(len(qtys))]
        data = {}
        for i, percent in enumerate(percents):
            data[qtys[i]] = cost * (1 + percent/100) / Exchange.ToUSD['RMB']
        printModel.print_dict(data)

