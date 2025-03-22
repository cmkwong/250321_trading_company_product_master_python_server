import pynput
from pynput import mouse, keyboard



class SystemController:

    def __init__(self):
        self.curr_pos = None

    def on_leftClick(self, x, y, button, pressed):
        print(x, y)
        # Check if the left button was pressed
        if pressed and button == mouse.Button.left:
            # Print the click coordinates
            print(f'x={x} and y={y}')


    def start_left_click(self):
        xy = []
        # Initialize the Listener to monitor mouse clicks
        # listener = mouse.Listener(on_click=self.on_leftClick)
        listener = mouse.Listener(on_click=self.on_leftClick)
        listener.start()
        return listener
