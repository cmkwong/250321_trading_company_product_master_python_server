import pynput
from pynput import mouse, keyboard
import pyautogui

class PyputController:

    def __init__(self):
        self.curr_pos = None
        self.listener = None

    def on_leftClick(self, x, y, button, pressed):
        # Check if the left button was pressed
        if pressed and button == mouse.Button.left:
            # Print the click coordinates
            print(f'x={x} and y={y}')
            self.curr_pos = (x, y)
            self.listener.stop()

    def get_pos_by_leftClick(self):
        # Initialize the Listener to monitor mouse clicks
        with mouse.Listener(on_click=self.on_leftClick) as self.listener:
            self.listener.join()
        return self.curr_pos
