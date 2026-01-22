import pynput
from pynput import mouse, keyboard
import pyautogui
from typing import Optional, Union, Tuple
import time


class PyputController:

    def __init__(self):
        self.curr_pos = None
        self.listener = None

    def on_leftClick(self, x, y, button, pressed):
        # Check if the left button was pressed
        if pressed and button == mouse.Button.left:
            # Print the click coordinates
            print(f"x={x} and y={y}")
            self.curr_pos = (x, y)
            self.listener.stop()

    def get_pos_by_leftClick(self):
        # Initialize the Listener to monitor mouse clicks
        with mouse.Listener(on_click=self.on_leftClick) as self.listener:
            self.listener.join()
        return self.curr_pos

    def get_bbox_from_clicks(
        self, timeout: int = 30, min_size: int = 10
    ) -> Optional[Tuple[int, int, int, int]]:
        """
        Get bounding box by clicking twice (top-left and bottom-right corners).

        Args:
            timeout: Maximum seconds to wait for all clicks
            min_size: Minimum width/height of valid bounding box

        Returns:
            Tuple of (left, top, width, height) or None if cancelled
        """
        print("=== Bounding Box Selection ===")
        print("1. First click: Top-left corner of area")
        print("2. Second click: Bottom-right corner of area")
        print("Press Ctrl+C to cancel at any time\n")

        try:
            # Get first click (top-left corner)
            print("Waiting for first click (top-left corner)...")
            first_click = None
            start_time = time.time()
            while not first_click and time.time() - start_time < timeout:
                if self.get_pos_by_leftClick():
                    first_click = pyautogui.position()
                    print(f"First point recorded: {first_click}")
                time.sleep(0.1)

            if not first_click:
                print("Timeout waiting for first click")
                return None

            # Get second click (bottom-right corner)
            print("\nWaiting for second click (bottom-right corner)...")
            second_click = None
            start_time = time.time()
            while not second_click and time.time() - start_time < timeout:
                if self.get_pos_by_leftClick():
                    second_click = pyautogui.position()

                    # Validate the bounding box
                    width = second_click.x - first_click.x
                    height = second_click.y - first_click.y

                    if width < min_size or height < min_size:
                        print(f"Bounding box too small ({width}x{height}). Try again.")
                        second_click = None
                        continue

                    bbox = (first_click.x, first_click.y, width, height)
                    print(f"\nFinal bounding box (x, y, width, height): {bbox}")
                    return bbox

                time.sleep(0.1)

            print("Timeout waiting for second click")
            return None

        except KeyboardInterrupt:
            print("\nBounding box selection cancelled")
            return None
        except Exception as e:
            print(f"\nError occurred: {str(e)}")
            return None
