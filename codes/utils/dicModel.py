import copy
from codes.utils import listModel
from typing import List, Tuple, Union

def changeCase(dic, case='l'):
    """
    Change the key case
    """
    old_keys = list(dic.keys())
    new_keys = listModel.changeCase(old_keys, case)
    for i, o_key in enumerate(old_keys):
        dic[new_keys[i]] = dic.pop(o_key)
    return dic

def mergeDict(originDict, newDict):
    """
    Put the new dictionary merged into new dictionary
    """
    for key, value in newDict.items():
        originDict[key] = value
    return originDict

def keepDic(originDict, keepList):
    """
    Keep the dictionary list on the keepList
    """
    newDict = {}
    for key, value in originDict.items():
        if key in keepList:
            newDict[key] = value
    return newDict

# duplicated function with append_dictValues_into_text()
def dic2Txt(dicts):
    """
    concat the dict value (text) into one text format
    """
    txt = ''
    for key, value in dicts.items():
        txt += f"{key}: {value}\n"
    return txt

def append_dictValues_into_text(dic, txt=''):
    values = list(dic.values())
    txt += ','.join([str(value) for value in values]) + '\n'
    return txt


"""
sort_dict(
    keys=[('delivery_time', True), ('fuel_cost', True)],
    weights=[0.6, 0.4]
)
"""
def sort_dict(
        data: List[dict],
        keys: List[Tuple[str, bool]] = None,
        weights: Union[List[float], None] = None
) -> List[dict]:
    """
    Advanced multi-criteria sorting for packing solutions with dynamic key configuration

    Args:
        data: List of packing solution dictionaries
        keys: Sorting criteria configuration list containing
             (field_name, descending) tuples
             - field_name: 要排序的字段名
             - descending: 是否降序排列 (False=升序)
        weights: Optional weight list for hybrid scoring [sum should be 1]

    Returns:
        New sorted list preserving original data

    Features:
        - 支持无限多个排序维度
        - 每个维度可单独设置升/降序
        - 可选混合权重评分模式
        - 自动处理缺失字段和类型错误
    """

    def sort_key(item: dict) -> tuple:
        try:
            # 生成归一化评分元组
            scores = []
            for idx, (field, reverse) in enumerate(keys):
                raw_value = item.get(field, 0)

                # 类型安全转换
                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    value = 0.0

                # 处理升序/降序
                sign = 1 if reverse else -1

                # 混合权重模式处理
                if weights:
                    max_val = max(abs(d.get(field, 0)) for d in data) or 1
                    normalized = (value / max_val) * sign
                    scores.append(weights[idx] * normalized)
                else:
                    scores.append(value * sign)

            if weights:
                return (-sum(scores),)  # 权重模式返回综合评分
            return tuple(scores)

        except Exception as e:
            return (0,)

    # 权重验证
    if weights:
        if len(weights) != len(keys):
            raise ValueError("Weights length must match keys length")
        if not (0.99 <= sum(weights) <= 1.01):
            weights = [w / sum(weights) for w in weights]

    return sorted(data, key=sort_key)
