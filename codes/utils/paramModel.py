import inspect
import re
from datetime import date, datetime
from prompt_toolkit import prompt


def decodeParam(input_data, dataType):
    """
    list:   ["AUDCAD", "EURUSD", "AUDUSD"] -> "AUDCAD EURUSD AUDUSD"
    tuple:  ("AUDCAD", "EURUSD", "AUDUSD") -> '("AUDCAD", "EURUSD", "AUDUSD")'
    other:  1 -> '1'
    """
    if dataType == list:
        required_input_data = input_data
        if len(input_data) == 0:
            required_input_data = []
    elif dataType == tuple:
        required_input_data = input_data
    elif dataType == dict:
        required_input_data = input_data
    elif dataType == bool:
        required_input_data = False
        if input_data.upper() == 'TRUE' or input_data:
            required_input_data = True
    # as space/empty cannot int / float the transform
    elif dataType in (int, float) and (input_data == '' or input_data.isspace()):
        required_input_data = 0
    elif type(input_data) != dataType:
        required_input_data = dataType(input_data)  # __new__, refer: https://www.pythontutorial.net/python-oop/python-__new__/
    else:
        required_input_data = input_data
    return required_input_data

# convert dictionary parameter into raw string
def encodeParam(param):
    """
    list:   ["AUDCAD", "EURUSD", "AUDUSD"] -> "AUDCAD EURUSD AUDUSD"
    tuple:  ("AUDCAD", "EURUSD", "AUDUSD") -> '("AUDCAD", "EURUSD", "AUDUSD")'
    other:  1 -> '1'
    """
    if isinstance(param, list):
        encoded_param = " ".join([str(p) for p in param])
    elif isinstance(param, tuple):
        encoded_param = str(param)
    elif isinstance(param, dict):
        encoded_param = str(param)
    else:
        encoded_param = str(param)
    return encoded_param

# ask tuple and list
def _ask_batch(paramName, paramValue, dataType):
    stop = False
    init_count = 0
    input_data = []
    print("Type \q to finish. ")
    while not stop:
        # setup default value
        defaultValue = ''
        if init_count < len(paramValue):
            defaultValue = paramValue[init_count]
        ind = prompt(f"{paramName}({dataType.__name__}) - {init_count + 1}:", default=f"{defaultValue}")
        # check if input finished
        if ind == r'\q':
            stop = True
        else:
            input_data.append(ind)
        init_count += 1
    if dataType == list:
        return input_data
    elif dataType == tuple:
        return tuple(input_data)

# ask dict
def _ask_dic(paramName, paramValue, dataType):
    stop = False
    init_count = 0
    input_data = {}
    dic_keys = list(paramValue.keys())
    dic_values = list(paramValue.values())
    while not stop:
        # setup default value
        defaultKey = ''
        defaultValue = ''
        if init_count < len(paramValue):
            defaultKey = dic_keys[init_count]
            defaultValue = dic_values[init_count]
        ind_key = prompt(f"{paramName}({dataType.__name__}) - key: ", default=f"{defaultKey}")
        if ind_key != r'\q':
            ind_value = prompt(f"{paramName}({dataType.__name__}) - {ind_key} of value: ", default=f"{defaultValue}")
            input_data[ind_key] = ind_value
        else:
            stop = True
        init_count += 1
    return input_data

# user input the param
def input_param(paramName, paramValue, dataType, remark=''):
    # ask use input parameter and allow user to modify the default parameter
    if remark:
        print(f"Remark: {remark}")
    if dataType in [list, tuple]:
        input_data = _ask_batch(paramName, paramValue, dataType)
    elif dataType == dict:
        input_data = _ask_dic(paramName, paramValue, dataType)
    else:
        input_data = prompt(f"{paramName}({dataType.__name__}): ", default=f"{paramValue}")
    # if no input, then assign default parameter
    if len(input_data) == 0:
        input_data = paramValue
    return input_data

def ask_param_fn(class_object, **overwrote_paramFormat):
    """
    :param class_object: class / function attribute
    :param overwrote_paramFormat: dict
    :return: obj, dict of param
    """
    # if it is none
    if not overwrote_paramFormat: overwrote_paramFormat = {}
    # params details from object
    signatures = inspect.signature(class_object)
    paramFormat = {}
    # looping the signature
    for sig in signatures.parameters.values():
        # argument after(*)
        if sig.kind == sig.KEYWORD_ONLY:
            # encode the param
            if sig.name in overwrote_paramFormat.keys():
                paramFormat[sig.name] = overwrote_paramFormat[sig.name]
            else:
                # has no default parameter
                if sig.default == sig.empty:
                    paramFormat[sig.name] = ['', sig.annotation]
                else:
                    paramFormat[sig.name] = [sig.default, sig.annotation]
    # ask user to input the param
    param = ask_param(paramFormat)
    return class_object, param

def ask_param(paramFormat):
    """
    purely to ask the param base on the dictionary
    :param params: dict, { name: [value, dataType, remark[optional] }
    :return:
    """
    params = {}
    for name, paramData in paramFormat.items():
        # getting the param data
        if len(paramData) == 3:
            param, dataType, remark = paramData
        else:
            param, dataType = paramData
            remark = ''
        # encode the param (for user input)
        # param = encodeParam(param)
        # asking params
        input_data = input_param(name, param, dataType, remark)
        # decode the param
        decode_data = decodeParam(input_data, dataType)
        params[name] = decode_data
    return params

def command_check(commands: list = None):
    def decorator(func):
        def command_check_wrapper(self, **params):
            ans = getattr(self, 'ans')
            ans = ans[1:]  # it must start with '-'
            # function-name + defined command-sets
            if commands:
                commandMatches = set([func.__name__] + commands)
            # function-name
            else:
                commandMatches = set([func.__name__])
            # check if command being matched
            # print("commandMatches: ", commandMatches)
            if ans not in commandMatches:
                setattr(self, 'COMMAND_HIT', False)
                return False
            else:
                # print("commandMatches hit: ", commandMatches)
                setattr(self, 'COMMAND_HIT', True)
                setattr(self, 'ans', None)
                return func(self, **params)

        return command_check_wrapper

    return decorator

def params_check(default_params: dict = None):
    def decorator(func):
        def params_check_wrapper(self):
            params = {}
            if self.COMMAND_HIT:
                params = ask_param(default_params)
                setattr(self, 'COMMAND_HIT', False)
                return func(self, **params)
            else:
                return func(self, **params)
        # assign the target function for upper wrapper
        params_check_wrapper.__name__ = func.__name__
        return params_check_wrapper

    return decorator