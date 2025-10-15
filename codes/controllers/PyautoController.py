import pyautogui
import pyperclip
import time
import os
from datetime import datetime, timedelta
from typing import Optional, Union, Tuple

from codes.controllers.PyputController import PyputController
from codes.utils import fileModel, imgModel
from codes import config

class PyautoController:

    def __init__(self):
        self.pyputController = PyputController()
        self.CONVA_ICON_PATH = os.path.join(config.AUTO_IMAGE, 'canva')

    # ctrl + v to paste the text
    def paste_text(self, text):
        pyperclip.copy(text)
        pyautogui.hotkey('ctrl', 'v')

    def findPattern_and_click(self,
                              patternImgs: list,
                              offset_x=0,
                              offset_y=0,
                              timeout=10,
                              confidence=0.8,
                              region: Optional[Tuple[int, int, int, int]] = None,
                              grayscale=True,
                              click='left' # 'left' / 'right' / 'double' / ''
                              ):
        """
        Wait to find an image on screen and click it with optional offset

        Args:
            patternImgs (str): Image filename to search for
            offset_x (int): X offset from center of found image
            offset_y (int): Y offset from center of found image
            timeout (int): Maximum seconds to wait for image to appear
            confidence (float): Matching confidence (0-1)

        Returns:
            bool: True if clicked successfully, False otherwise
        """
        start_time = datetime.now()
        end_time = start_time + timedelta(seconds=timeout)

        while datetime.now() < end_time:
            try:
                button_location = None
                for patternImg in patternImgs:
                    button_location = pyautogui.locateOnScreen(
                        patternImg,
                        confidence=confidence,
                        grayscale=grayscale,
                        region=region
                    )
                    time.sleep(0.2)
                    if button_location:
                        break

                if button_location:
                    button_center = pyautogui.center(button_location)
                    target_x = button_center.x + offset_x
                    target_y = button_center.y + offset_y

                    print(f"Found at: {target_x},{target_y} - Clicking...")

                    pyautogui.moveTo(target_x, target_y, duration=0.2)
                    if click == 'left':
                        pyautogui.click()
                    elif click == 'right':
                        pyautogui.rightClick()
                    elif click == 'double':
                        pyautogui.doubleClick()
                    time.sleep(0.2)
                    return target_x, target_y

            except pyautogui.ImageNotFoundException:
                pass  # Expected during search
            except Exception as e:
                print(f"Search error: {str(e)}")

            # Wait a bit before retrying
            time.sleep(0.5)

        # Timeout reached
        print(f"Timeout: Could not find {patternImgs} after {timeout} seconds")
        return False, False

    def paste_image_at_position(self, image_path, x, y, paste_delay=0.5):
        """
        Copies an image from file and pastes it at current mouse position

        Parameters:
        - image_path: Path to the image file to paste
        - paste_delay: Delay between paste operations (in seconds)
        """
        try:
            # Copy image to clipboard
            imgModel.copy_image_to_clipboard(image_path)
            time.sleep(0.5)  # Small delay for clipboard operation

            # Move to target position and paste
            pyautogui.moveTo(x, y)
            pyautogui.click()
            pyautogui.hotkey('ctrl', 'v')  # Paste command

            # Wait for paste to complete
            time.sleep(paste_delay)

            print(f"Image: {image_path} pasted at position ({x}, {y})")
            return True

        except Exception as e:
            print(f"Error pasting image: {e}")
            return False

    def scroll_updown(self, magnitude, x=None, y=None):
        if not x or not y:
            x, y = pyautogui.position()
            pyautogui.scroll(magnitude, x=x, y=y)
        else:
            pyautogui.scroll(magnitude)
        return True

    def scroll_startend(self, home=True):
        if home:
            pyautogui.hotkey('ctrl', 'home')
        else:
            pyautogui.hotkey('ctrl', 'end')

    def press_key(self, key):
        pyautogui.press(key)

    def press_keys(self, keys):
        pyautogui.hotkey(*keys)

    def input_text(
            self,
            text,
            interval: float = 0.1,
            enter: bool = False,
            tab: bool = False,
            clear_first: bool = False,
            select_all_before: bool = False,
            click_position: Optional[tuple[int, int]] = None,
            wait_before: float = 0.5,
            wait_after: float = 0.2
    ) -> bool:
        """
        Input text using pyautogui with various options.

        Args:
            text: Text to input
            interval: Delay between keystrokes (seconds)
            enter: Press Enter after input
            tab: Press Tab after input
            clear_first: Clear field before input (Ctrl+A then Delete)
            select_all_before: Select all text before input (Ctrl+A)
            click_position: (x,y) position to click before typing
            wait_before: Seconds to wait before starting
            wait_after: Seconds to wait after finishing

        Returns:
            bool: True if successful, False if error occurred
        """
        if not isinstance(text, str):
            text = f"{text}"
        try:
            time.sleep(wait_before)

            if click_position:
                pyautogui.click(click_position)
                time.sleep(0.2)

            if clear_first:
                pyautogui.hotkey('ctrl', 'a')
                pyautogui.press('delete')
                time.sleep(0.1)

            if select_all_before:
                pyautogui.hotkey('ctrl', 'a')
                time.sleep(0.1)

            pyautogui.write(text, interval=interval)

            if enter:
                pyautogui.press('enter')
            if tab:
                pyautogui.press('tab')

            time.sleep(wait_after)
            return True

        except Exception as e:
            print(f"Error in input_text: {str(e)}")
            return False

    def get_bbox_from_clicks(
            self,
            timeout: int = 30,
            min_size: int = 10
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
                if self.pyputController.get_pos_by_leftClick():
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
                if self.pyputController.get_pos_by_leftClick():
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

    def find_canva_home_icon_and_click(self, offset_x=0, offset_y=0, click='left'):
        return self.findPattern_and_click([os.path.join(self.CONVA_ICON_PATH, 'canva_home_1.png'),
                                    os.path.join(self.CONVA_ICON_PATH, 'canva_home_2.png')],
                                   offset_x=offset_x,
                                   offset_y=offset_y,
                                   click=click
                                   )

    def _open_canva(self):
        # open the canva
        self.findPattern_and_click([os.path.join(self.CONVA_ICON_PATH, 'canva_icon.png'),
                                    os.path.join(self.CONVA_ICON_PATH, 'canva_icon_2.png')], grayscale=False, region=(908, 955, 479, 124))
        return True

    def _init_canva_design(self, width, height):
        """
        init a design based on init image dimension
        """
        self._open_canva()
        self.find_canva_home_icon_and_click()
        self.findPattern_and_click([os.path.join(self.CONVA_ICON_PATH, 'canva_create.png')])
        self.findPattern_and_click([os.path.join(self.CONVA_ICON_PATH, 'custom_size_1.png')])
        self.findPattern_and_click([os.path.join(self.CONVA_ICON_PATH, 'width_1.png')], offset_y=30)
        self.input_text(text=width, clear_first=True)
        self.findPattern_and_click([os.path.join(self.CONVA_ICON_PATH, 'height_1.png')], offset_y=30)
        self.input_text(text=height, clear_first=True)
        self.findPattern_and_click([os.path.join(self.CONVA_ICON_PATH, 'canva_create_design.png')], offset_y=15)
        return True

    def _create_custom_size_page(self, width, height):
        """
        create custom size of page in a design
        """
        self.findPattern_and_click([os.path.join(self.CONVA_ICON_PATH, 'create_page.png')], offset_x=10)
        self.findPattern_and_click([os.path.join(self.CONVA_ICON_PATH, 'more.png')])
        self.findPattern_and_click([os.path.join(self.CONVA_ICON_PATH, 'custom_size_2.png')])
        self.findPattern_and_click([os.path.join(self.CONVA_ICON_PATH, 'width_1.png')], offset_y=40)
        self.input_text(text=width, clear_first=True)
        pyautogui.press('tab')
        self.input_text(text=height, clear_first=True)
        pyautogui.press(['tab', 'tab', 'tab'])
        pyautogui.press('enter')
        time.sleep(0.2)
        if not self.findPattern_and_click([os.path.join(self.CONVA_ICON_PATH, 'canva_warning_20_types.png')], click='', timeout=1) == (False, False):
            return False
        return True

    def _set_images_as_background(self, image_infos):
        """
        right click to set image into background
        """
        # scroll back to the first image
        x, y = self.find_canva_home_icon_and_click(offset_x=172, offset_y=970)
        for _ in range(len(image_infos) + 5):
            pyautogui.press('left')
        # set as background image
        for key, image_info in image_infos.items():
            # for background image setup
            x, y = self.find_canva_home_icon_and_click(offset_x=943, offset_y=600)
            pyautogui.rightClick(x, y)
            self.findPattern_and_click([os.path.join(self.CONVA_ICON_PATH, 'set_image_as_background.png')])
            x, y = self.find_canva_home_icon_and_click(offset_x=943, offset_y=600)
            # for next page
            time.sleep(0.1)
            x, y = self.find_canva_home_icon_and_click(offset_x=172, offset_y=970)
            pyautogui.leftClick(x, y)
            pyautogui.press('right')

    def _rename_images(self, image_infos, index_only=False):
        """
        product_SEO_keywords the image
        """
        self.findPattern_and_click([os.path.join(self.CONVA_ICON_PATH, 'grid_view.png')])
        time.sleep(1)
        for i, (key, image_info) in enumerate(image_infos.items()):
            if i == 0:
                x, y = self.find_canva_home_icon_and_click(offset_x=83, offset_y=226)
                pyautogui.press(['tab', 'tab'])
                # self.input_text(text=image_info['name'], clear_first=True)
            else:
                if i % 9 != 0:
                    pyautogui.press(['tab', 'tab', 'tab', 'tab'])
                else:
                    pyautogui.press(['tab', 'tab', 'tab', 'tab', 'tab'])
                # self.input_text(text=image_info['name'], clear_first=True)
            if not index_only:
                pyperclip.copy(image_info['name'])
            else:
                pyperclip.copy(f"{i+1}")
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(0.2)
        return True


    def product_into_canva(self, design_name, image_infos) -> dict:
        edited_image_infos = {}
        # # first image of width and height and create the design
        width, height = list(image_infos.values())[0]['width'], list(image_infos.values())[0]['height']
        image_counts = len(image_infos)
        self._init_canva_design(width, height)
        for i, (key, image_info) in enumerate(image_infos.copy().items()):
            if i == 0:
                time.sleep(1)
                x, y = self.find_canva_home_icon_and_click(offset_x=943, offset_y=600)
                self.paste_image_at_position(image_info['path'], x=x, y=y, paste_delay=3)
            else:
                create_succeed = self._create_custom_size_page(image_info['width'], image_info['height'])
                # warning then break
                if not create_succeed:
                    break
                # paste the image
                x, y = self.find_canva_home_icon_and_click(offset_x=943, offset_y=600)
                self.paste_image_at_position(image_info['path'], x=x, y=y)
                # scroll down
                x, y = self.find_canva_home_icon_and_click(offset_x=172, offset_y=970, click=False)
                self.scroll_updown(-1000)
                # delete an item after finished
            edited_image_infos[key] = image_infos.pop(key)
        # setting each of image to background
        self._set_images_as_background(edited_image_infos)
        # product_SEO_keywords images
        self._rename_images(edited_image_infos, index_only=True)

        # naming the design
        self.find_canva_home_icon_and_click(offset_x=1467, offset_y=58)
        self.input_text(text=design_name, clear_first=True)
        x, y = self.find_canva_home_icon_and_click(offset_x=943, offset_y=600)
        return image_infos

    def product_into_canva_attempts(self, product_index, imagesType, attempts=5):
        product_path = os.path.join(config.PRODUCT_FOLDER_PATH, product_index, imagesType)
        product_images = fileModel.getFileList(pathDir=product_path)
        image_infos = {}
        for product_image in product_images:
            _, _, i, size = product_image.split('.')[0].split('_')
            width, height = size.split('x')
            path = os.path.join(product_path, product_image)
            image_infos[int(i)] = {'width': int(width), 'height': int(height), 'path': path, 'name': product_image.split('.')[0]}
        # sorted the image infos in asc order
        image_infos = dict(sorted(image_infos.items()))

        max_attempts = 0
        for i in range(attempts):
            if i > 0:
                design_name = f"{imagesType}_{product_index}_{i}"
            else:
                design_name = f"{imagesType}_{product_index}"
            image_infos = self.product_into_canva(design_name, image_infos)
            # every time finish, zip the window
            self.find_canva_home_icon_and_click(1723, 5)
            if len(image_infos) == 0:
                max_attempts = i
                break
        print(f"{product_index} - {imagesType} completed. \nMax attempts: {max_attempts}")
        return True

