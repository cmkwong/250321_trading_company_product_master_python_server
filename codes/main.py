import sys
import inspect

sys.path.append(".")
from codes.CommandChecker import CommandChecker

command = False
# define the apHelper
# reporthelper = ProjectHelper()
commandChecker = CommandChecker()

while (not (command == 'quit')):

    params = {}

    # setting placeholder
    placeholder = 'Input: '
    commandChecker.ans = input(placeholder)         # waiting user input

    # getting all functions and see if match input command
    attrs = [getattr(commandChecker, name) for name in dir(commandChecker)]
    commandFuncs = [attr for attr in attrs if inspect.ismethod(attr)]
    for i, func in enumerate(commandFuncs):
        # print(f'{i}: ', func.__name__)
        # first command init is not needed
        if func.__name__ in ['__init__']:
            continue
        command = func()
        if command == 'CHECKED':
            break
    if not command:
        print("Not is command")
