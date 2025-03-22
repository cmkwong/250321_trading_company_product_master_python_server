from codes.utils import listModel

def enter():
    # setting placeholder
    placeholder = 'Input: '
    user_input = input(placeholder) # waiting user input
    return user_input

def askNum(placeholder="Please enter a number: ", outType=int):
    """
    change the type for user input, float, int etc
    """
    print("~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*")
    usr_input = input(placeholder)
    if not usr_input.isnumeric():
        print("Wrong input. \nPlease input again.\n")
        return None
    usr_input = outType(usr_input)
    return usr_input

def num_input(outType=int):
    """
    change the type for user input, float, int etc
    """
    try:
        usr_input = input()
        if not usr_input.isnumeric():
            print("That is not a number. \nPlease enter a Number.")
            return None
        usr_input = outType(usr_input)
    except KeyboardInterrupt:
        print("Wrong input")
        return None
    return usr_input

def user_confirm():
    placeholder = 'Input [y]es to confirm OR others to cancel: '
    confirm_input = input(placeholder)
    if confirm_input == 'y' or confirm_input == "yes":
        return True
    else:
        return False

def askSelection(options: list):
    placeholder = f"{listModel.optionsTxt(options)}\nPlease Select: "
    userInput = askNum(placeholder)
    return options[userInput]
